import mimetypes
from pathlib import Path

from django.http import FileResponse, Http404
from rest_framework import viewsets
from rest_framework.decorators import action
from rest_framework.permissions import IsAuthenticated

from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from authentication.permissions import IsHRStaff
from .candidate_import import build_candidates
from .models import Onboarding, Candidate
from .serializers import DOCUMENT_FIELDS, OnboardingSerializer, CandidateSerializer

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

        name = upload.name or "upload"
        if not name.lower().endswith((".xlsx", ".xlsm", ".csv", ".txt")):
            return Response(
                {"detail": "Upload an .xlsx or .csv file."}, status=400
            )

        # Where these people came from. Defaults to the file's own name, which is
        # how HR already labels them ("600 FB_Leads_Updated_02Aug2026").
        source = (request.data.get("source") or "").strip() or name.rsplit(".", 1)[0]

        try:
            report = build_candidates(upload.read(), name, source)
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
        already = set(
            Candidate.objects.filter(phone_number__in=phones).values_list(
                "phone_number", flat=True
            )
        )
        fresh = [c for c in report.candidates if c["phone_number"] not in already]
        payload["already_in_portal"] = len(already)
        payload["new"] = len(fresh)
        payload["sample"] = fresh[:5]

        commit = str(request.data.get("commit", "")).lower() in {"1", "true", "yes", "on"}
        if not commit:
            payload["committed"] = False
            return Response(payload)

        created = Candidate.objects.bulk_create(
            [Candidate(**c) for c in fresh], batch_size=200
        )
        payload["committed"] = True
        payload["created"] = len(created)
        payload.pop("sample", None)
        return Response(payload, status=201)
