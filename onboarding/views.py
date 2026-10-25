import logging
import mimetypes
from pathlib import Path

from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework import status as http_status, viewsets
from rest_framework.decorators import action
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.throttling import ScopedRateThrottle
from rest_framework.views import APIView

from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from authentication.permissions import IsHRStaff
from .candidate_import import build_candidates, UnreadableUpload
from .models import AppointmentOrder, Onboarding, OnboardingInvite, Candidate, OfferLetter, new_invite_token
from .serializers import (
    DOCUMENT_FIELDS,
    AppointmentOrderSerializer,
    CandidateSerializer,
    OfferLetterSerializer,
    OnboardingSerializer,
)

logger = logging.getLogger(__name__)

# Big enough for the lead exports HR actually has (the largest is ~400 KB and
# 680 rows) with plenty of room, small enough that a wrong file cannot tie up a
# worker parsing it.
MAX_IMPORT_BYTES = 10 * 1024 * 1024

class OnboardingViewSet(viewsets.ModelViewSet):
    queryset = Onboarding.objects.all().order_by('-created_at')
    serializer_class = OnboardingSerializer
    # Onboarding holds Aadhaar/PAN/bank PII — restrict to HR/admin/superadmin.
    permission_classes = [IsAuthenticated, IsHRStaff]

    @action(
        detail=True,
        methods=["get"],
        url_path=r"documents/(?P<field_name>[a-z_]+)",
        url_name="document",
    )
    def document(self, request, pk=None, field_name=None):
        if field_name not in DOCUMENT_FIELDS:
            raise Http404

        onboarding = self.get_object()
        document = getattr(onboarding, field_name, None)
        if not document or not document.name:
            raise Http404

        try:
            file_handle = document.open("rb")
        except (FileNotFoundError, OSError):
            raise Http404

        filename = Path(document.name).name
        content_type, _ = mimetypes.guess_type(filename)
        response = FileResponse(
            file_handle,
            as_attachment=True,
            filename=filename,
            content_type=content_type or "application/octet-stream",
        )
        response["Cache-Control"] = "private, no-store"
        response["Pragma"] = "no-cache"
        response["X-Content-Type-Options"] = "nosniff"
        response["Content-Security-Policy"] = "default-src 'none'; sandbox"
        return response


class OnboardingLinkViewSet(viewsets.ViewSet):
    """The office's end: read the three links, and replace one.

    The token itself is handed over, because the page has to build a URL out
    of it to put on the clipboard. It is already behind the same HR-only wall
    as the records the link creates.
    """
    permission_classes = [IsAuthenticated, IsHRStaff]

    def list(self, request):
        links = []
        for category, _ in Onboarding.CATEGORY_CHOICES:
            # Created on first sight rather than by a migration, so a category
            # added later needs no backfill.
            invite, _created = OnboardingInvite.objects.get_or_create(category=category)
            links.append(
                {
                    "category": invite.category,
                    "token": invite.token,
                    "rotated_at": invite.rotated_at,
                }
            )
        return Response(links)

    @action(detail=False, methods=["post"], url_path=r"(?P<category>[A-Za-z]+)/rotate")
    def rotate(self, request, category=None):
        """Replace the link for one category. The old one stops working at once."""
        valid = {name for name, _ in Onboarding.CATEGORY_CHOICES}
        if category not in valid:
            raise Http404
        invite, _created = OnboardingInvite.objects.get_or_create(category=category)
        invite.token = new_invite_token()
        invite.rotated_at = timezone.now()
        invite.save(update_fields=["token", "rotated_at"])
        return Response(
            {"category": invite.category, "token": invite.token, "rotated_at": invite.rotated_at}
        )


class PublicOnboardingView(APIView):
    """The open door, and the only one. See the module docstring."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "onboarding_link"
    parser_classes = [MultiPartParser, FormParser]

    def _invite(self, token):
        # Exactly the same answer for a token that never existed and one that
        # has been replaced: nothing to learn from either.
        try:
            return OnboardingInvite.objects.get(token=token)
        except OnboardingInvite.DoesNotExist:
            raise Http404

    def get(self, request, token):
        """Which form to draw. Nothing else is readable here."""
        invite = self._invite(token)
        return Response({"category": invite.category})

    def post(self, request, token):
        invite = self._invite(token)

        # A plain dict, not request.data.copy(). That copy is a DEEP copy, and
        # an upload over 2 MB is a temporary file on disk that cannot be deep
        # copied -- every phone photo of an Aadhaar came back as a 500.
        data = {key: request.data.get(key) for key in request.data}
        # THE LINK DECIDES THESE, NOT THE BROWSER. Whatever was sent for them
        # is dropped: otherwise a freelancer's link would file an employee, and
        # a self-filled form would arrive already approved and working here.
        data["category"] = invite.category
        data["source"] = "Self"
        data["status"] = "Pending Review"
        data["employment_status"] = "Inactive"

        serializer = OnboardingSerializer(data=data)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        # Nothing of the record goes back out of the public door -- the person
        # filling it in already knows what they typed, and anybody else with
        # the link should learn nothing by posting to it.
        return Response({"submitted": True}, status=http_status.HTTP_201_CREATED)


class CandidateViewSet(viewsets.ModelViewSet):
    queryset = Candidate.objects.all().order_by('-created_at')
    serializer_class = CandidateSerializer
    # Candidate records hold salary slips / bank statements — HR-only.
    permission_classes = [IsAuthenticated, IsHRStaff]

    @action(
        detail=False,
        methods=["post"],
        url_path="import-file",
        parser_classes=[MultiPartParser, FormParser],
    )
    def import_file(self, request):
        """Load a hiring spreadsheet into the portal.

        Defaults to a PREVIEW. Hundreds of rows arriving in the wrong columns is
        a mess to undo by hand, so the first call only reports what would happen
        and nothing is written until the caller asks again with commit=true.

        De-duplication is on the phone number, against the file AND against the
        candidates already here — the same person turns up in a college list and
        a lead export, and the sheets themselves repeat people across tabs.
        """
        upload = request.FILES.get("file")
        if not upload:
            return Response({"detail": "No file was uploaded."}, status=400)
        if upload.size > MAX_IMPORT_BYTES:
            return Response(
                {"detail": f"File is too large (limit {MAX_IMPORT_BYTES // (1024 * 1024)} MB)."},
                status=400,
            )

        # No extension check: the WorkIndia export downloads as
        # "Workindia Profile vv" with no extension at all, and a perfectly good
        # CSV was turned away at the door. build_candidates decides from the
        # bytes, and says so in words if it cannot read them.
        name = upload.name or "upload"

        # Where these people came from. Defaults to the file's own name, which is
        # how HR already labels them ("600 FB_Leads_Updated_02Aug2026").
        source = (request.data.get("source") or "").strip() or name.rsplit(".", 1)[0]

        try:
            report = build_candidates(upload.read(), name, source)
        except UnreadableUpload as exc:
            # Written for HR: it says what to do, not what broke.
            return Response({"detail": str(exc)}, status=400)
        except Exception as exc:  # a corrupt or password-protected workbook
            return Response(
                {"detail": f"Could not read that file: {exc.__class__.__name__}."}, status=400
            )

        payload = report.as_dict()
        payload["source"] = source
        payload["file"] = name

        # Which of them we already hold, so the preview can say "new" honestly
        # rather than promising a count that de-duplication will cut down.
        phones = [c["phone_number"] for c in report.candidates]
        existing = {
            candidate.phone_number: candidate
            for candidate in Candidate.objects.filter(phone_number__in=phones)
        }
        fresh = [c for c in report.candidates if c["phone_number"] not in existing]
        payload["already_in_portal"] = len(existing)
        payload["new"] = len(fresh)
        payload["sample"] = fresh[:5]

        # Re-importing a corrected sheet is the normal way a gap gets filled: half
        # the rows in a college list arrive with no location, HR types them in,
        # and sends the file again. Skipping everyone already here meant that
        # second file did nothing at all.
        update_existing = str(request.data.get("update_existing", "")).lower() in {
            "1", "true", "yes", "on"
        }
        fills = self._blank_fills(report.candidates, existing) if update_existing else []
        payload["will_fill"] = len(fills)
        payload["fill_fields"] = sorted({field for _, field, _ in fills})

        commit = str(request.data.get("commit", "")).lower() in {"1", "true", "yes", "on"}
        if not commit:
            payload["committed"] = False
            return Response(payload)

        created = Candidate.objects.bulk_create(
            [Candidate(**c) for c in fresh], batch_size=200
        )

        touched = set()
        for candidate, field, value in fills:
            setattr(candidate, field, value)
            touched.add(candidate.pk)
        if fills:
            Candidate.objects.bulk_update(
                [c for c in existing.values() if c.pk in touched],
                sorted({field for _, field, _ in fills}),
                batch_size=200,
            )

        payload["committed"] = True
        payload["created"] = len(created)
        payload["updated"] = len(touched)
        payload.pop("sample", None)
        return Response(payload, status=201)

    # Only ever FILLS a blank. A sheet is a snapshot and the portal is worked in
    # daily — a re-import that overwrote a location HR had corrected by hand, or
    # a stage they had moved someone to, would lose real work. So a value in the
    # portal always wins, and the sheet only supplies what is missing.
    FILLABLE_TEXT_FIELDS = (
        "email",
        "qualification",
        "present_address",
        "permanent_address",
        "previous_company",
        "remarks",
        "source",
    )
    FILLABLE_NUMBER_FIELDS = ("years_of_experience", "last_salary", "expecting_salary")

    @classmethod
    def _blank_fills(cls, parsed, existing):
        """(candidate, field, value) for every blank a re-imported sheet can fill."""
        fills = []
        for row in parsed:
            candidate = existing.get(row["phone_number"])
            if candidate is None:
                continue
            for field in cls.FILLABLE_TEXT_FIELDS:
                incoming = row.get(field)
                if incoming and not (getattr(candidate, field) or "").strip():
                    fills.append((candidate, field, incoming))
            for field in cls.FILLABLE_NUMBER_FIELDS:
                incoming = row.get(field) or 0
                if incoming and not float(getattr(candidate, field) or 0):
                    fills.append((candidate, field, incoming))
        return fills


# Letters before Payroll made them were numbered by hand; 2026's had reached 140.
# Every other year starts at 1.
FIRST_OFFER_SERIAL = {2026: 141}


class OfferLetterViewSet(viewsets.ModelViewSet):
    """Offer letters made in the Hiring Portal. HR only, like the candidates they
    are written for: a letter states somebody's pay."""

    queryset = OfferLetter.objects.select_related("created_by").all()
    serializer_class = OfferLetterSerializer
    permission_classes = [IsAuthenticated, IsHRStaff]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    @action(detail=False, methods=["get"], url_path="next_ref")
    def next_ref(self, request):
        """The reference the next letter should carry, RTPL/NJ/<year>/<serial>:
        one past this year's highest. HR can still type another."""
        year = timezone.localdate().year
        prefix = f"RTPL/NJ/{year}/"
        highest = 0
        for ref in OfferLetter.objects.filter(ref_no__startswith=prefix).values_list("ref_no", flat=True):
            tail = ref[len(prefix):]
            if tail.isdigit():
                highest = max(highest, int(tail))
        serial = max(highest + 1, FIRST_OFFER_SERIAL.get(year, 1))
        return Response({"ref_no": f"{prefix}{serial}"})

    # The PDF opens by plain navigation -- in the app that is the only way a file
    # reaches the phone (see payrollpayslip.payslip_pdf) -- and a navigation
    # carries no JWT. So, as for payslips: a signed ticket for one letter, minted
    # over the authenticated channel and spent within five minutes.
    PDF_TICKET_SALT = "offer-letter.pdf.download"
    PDF_TICKET_MAX_AGE = 300  # seconds

    @action(detail=True, methods=["get"], url_path="pdf_ticket")
    def pdf_ticket(self, request, pk=None):
        from django.core import signing

        letter = self.get_object()
        ticket = signing.dumps(
            {"offer_letter": letter.id, "user": request.user.id}, salt=self.PDF_TICKET_SALT
        )
        return Response({
            "ticket": ticket,
            "path": f"/api/offer-letters/{letter.id}/pdf/?t={ticket}",
            "expires_in": self.PDF_TICKET_MAX_AGE,
        })

    @action(
        detail=True,
        methods=["get"],
        url_path="pdf",
        permission_classes=[AllowAny],
        authentication_classes=[],
    )
    def pdf(self, request, pk=None):
        """The letter as a PDF, authorised by the ticket above."""
        from django.core import signing
        from django.http import HttpResponse

        from .offer_letter import offer_letter_filename, render_offer_letter_pdf

        ticket = request.query_params.get("t") or ""
        try:
            payload = signing.loads(ticket, salt=self.PDF_TICKET_SALT, max_age=self.PDF_TICKET_MAX_AGE)
        except signing.SignatureExpired:
            return Response({"detail": "This download link has expired. Please try again."}, status=403)
        except signing.BadSignature:
            return Response({"detail": "Invalid download link."}, status=403)
        # The ticket names its letter; trusting the URL instead would let one
        # ticket fetch every letter in the table.
        if str(payload.get("offer_letter")) != str(pk):
            return Response({"detail": "Invalid download link."}, status=403)

        letter = OfferLetter.objects.filter(pk=pk).first()
        if letter is None:
            return Response({"detail": "Offer letter not found."}, status=404)
        try:
            pdf_bytes = render_offer_letter_pdf(letter)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
            logger.exception("offer letter PDF render failed for %s", pk)
            return Response({"detail": f"Could not build the PDF: {exc}"}, status=500)

        response = HttpResponse(pdf_bytes, content_type="application/pdf")
        response["Content-Disposition"] = f'attachment; filename="{offer_letter_filename(letter)}"'
        response["Cache-Control"] = "no-store"
        return response


def _pdf_response(pdf_bytes, filename):
    from django.http import HttpResponse

    response = HttpResponse(pdf_bytes, content_type="application/pdf")
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    return response


def _signing_error(exc):
    return Response({"detail": exc.message}, status=exc.status)


class AppointmentOrderViewSet(viewsets.ModelViewSet):
    """Appointment orders, made in the Hiring Portal after the offer letter and
    signed by the employee online (onboarding/appointment_order). HR only: an
    order states somebody's pay and address.

    A signed order is a record of what somebody agreed to, so it can be
    neither changed nor deleted. Changing an order that has been sent kills
    its link; it has to be sent again."""

    queryset = AppointmentOrder.objects.select_related("created_by", "offer_letter").all()
    serializer_class = AppointmentOrderSerializer
    permission_classes = [IsAuthenticated, IsHRStaff]

    def perform_create(self, serializer):
        serializer.save(created_by=self.request.user)

    def update(self, request, *args, **kwargs):
        if self.get_object().signed_at:
            return Response({"detail": "A signed appointment order cannot be changed."}, status=409)
        return super().update(request, *args, **kwargs)

    def perform_update(self, serializer):
        from .appointment_order.signing import reset_signing

        order = serializer.save()
        if order.sign_token:
            reset_signing(order)

    def destroy(self, request, *args, **kwargs):
        order = self.get_object()
        if order.signed_at:
            return Response({"detail": "A signed appointment order cannot be deleted."}, status=409)
        if order.sent_pdf:
            order.sent_pdf.delete(save=False)
        return super().destroy(request, *args, **kwargs)

    @action(detail=True, methods=["post"], url_path="send")
    def send(self, request, pk=None):
        """Email the employee a link to sign. The site to link to is the one HR
        is sending from (checked against the sites this backend serves)."""
        from .appointment_order.signing import SigningError, send_for_signature

        order = self.get_object()
        try:
            link = send_for_signature(order, request.data.get("origin"))
        except SigningError as exc:
            return _signing_error(exc)
        order.refresh_from_db()
        data = self.get_serializer(order).data
        data["link"] = link
        return Response(data)

    @action(detail=True, methods=["get"], url_path="link")
    def link(self, request, pk=None):
        """The link that was sent, for HR to pass on another way (WhatsApp)."""
        from .appointment_order.signing import SigningError, link_base, sign_link

        order = self.get_object()
        if not order.sign_token or order.signed_at:
            return Response({"detail": "This order has no open link."}, status=404)
        try:
            base = link_base(request.query_params.get("origin"))
        except SigningError as exc:
            return _signing_error(exc)
        return Response({"link": sign_link(base, order.sign_token)})

    PDF_TICKET_SALT = "appointment-order.pdf.download"
    PDF_TICKET_MAX_AGE = 300  # seconds

    @action(detail=True, methods=["get"], url_path="pdf_ticket")
    def pdf_ticket(self, request, pk=None):
        """As for offer letters: the PDF opens by navigation, which carries no
        JWT, so a five-minute ticket for this one order stands in for it."""
        from django.core import signing

        order = self.get_object()
        ticket = signing.dumps({"appointment_order": order.id, "user": request.user.id}, salt=self.PDF_TICKET_SALT)
        return Response({
            "ticket": ticket,
            "path": f"/api/appointment-orders/{order.id}/pdf/?t={ticket}",
            "expires_in": self.PDF_TICKET_MAX_AGE,
        })

    @action(detail=True, methods=["get"], url_path="pdf", permission_classes=[AllowAny], authentication_classes=[])
    def pdf(self, request, pk=None):
        """The signed copy once there is one; until then the order as it stands."""
        from django.core import signing

        from .appointment_order import appointment_order_filename, render_appointment_order_pdf

        try:
            payload = signing.loads(
                request.query_params.get("t") or "", salt=self.PDF_TICKET_SALT, max_age=self.PDF_TICKET_MAX_AGE
            )
        except signing.SignatureExpired:
            return Response({"detail": "This download link has expired. Please try again."}, status=403)
        except signing.BadSignature:
            return Response({"detail": "Invalid download link."}, status=403)
        if str(payload.get("appointment_order")) != str(pk):
            return Response({"detail": "Invalid download link."}, status=403)

        order = AppointmentOrder.objects.filter(pk=pk).first()
        if order is None:
            return Response({"detail": "Appointment order not found."}, status=404)
        if order.signed_at:
            with order.signed_pdf.open("rb") as fh:
                return _pdf_response(fh.read(), appointment_order_filename(order, signed=True))
        try:
            pdf_bytes = render_appointment_order_pdf(order)
        except Exception as exc:  # noqa: BLE001 - surfaced to the caller below
            logger.exception("appointment order PDF render failed for %s", pk)
            return Response({"detail": f"Could not build the PDF: {exc}"}, status=500)
        return _pdf_response(pdf_bytes, appointment_order_filename(order))


class _PublicAppointmentOrder(APIView):
    """The employee's end of an appointment order: no login, the token in the
    link stands in for one. An unknown token and a replaced one get the same
    404, so there is nothing to learn from trying."""

    authentication_classes = []
    permission_classes = [AllowAny]
    throttle_classes = [ScopedRateThrottle]
    throttle_scope = "appointment_sign"

    def _order(self, token):
        order = AppointmentOrder.objects.filter(sign_token=token).first() if token else None
        if order is None:
            raise Http404
        return order


class PublicAppointmentOrderView(_PublicAppointmentOrder):
    def get(self, request, token):
        from .appointment_order.signing import public_view

        order = self._order(token)
        if not order.viewed_at and not order.signed_at:
            order.viewed_at = timezone.now()
            order.save(update_fields=["viewed_at", "updated_at"])
        return Response(public_view(order))


class PublicAppointmentOrderPdfView(_PublicAppointmentOrder):
    def get(self, request, token):
        from .appointment_order import appointment_order_filename

        order = self._order(token)
        document = order.signed_pdf if order.signed_at else order.sent_pdf
        with document.open("rb") as fh:
            return _pdf_response(fh.read(), appointment_order_filename(order, signed=bool(order.signed_at)))


class PublicAppointmentOrderCodeView(_PublicAppointmentOrder):
    def post(self, request, token):
        from .appointment_order.signing import SigningError, send_code

        try:
            return Response(send_code(self._order(token)))
        except SigningError as exc:
            return _signing_error(exc)


class PublicAppointmentOrderSignView(_PublicAppointmentOrder):
    def post(self, request, token):
        from .appointment_order.signing import SigningError, client_ip, email_signed_copy, sign

        order = self._order(token)
        try:
            order = sign(
                order.pk,
                request.data.get("code"),
                request.data.get("signature"),
                request.data.get("agree"),
                client_ip(request),
                request.META.get("HTTP_USER_AGENT", ""),
            )
        except SigningError as exc:
            return _signing_error(exc)
        return Response({"signed": True, "signed_at": order.signed_at, "emailed": email_signed_copy(order)})
