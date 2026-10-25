"""The Mail section's API. HR, admin and super admin only (IsHRStaff), like
the Hiring Portal: the HR mailbox carries candidates' and employees' mail.

Nothing here sends by itself. A mail leaves only when somebody presses Send
on a reply or a new mail, and every one is recorded with who sent it.
"""
import mimetypes
import re

from django.core import signing
from django.db.models import Count, Q
from django.http import FileResponse, Http404
from django.utils import timezone
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from authentication.permissions import IsHRStaff

from . import imapbox, outbound
from .mailparse import MAX_BODY_LENGTH, check_compose
from .models import InboundAttachment, InboundMail, OutboundMail, SystemMail, SystemMailAttachment

PAGE_SIZE = 100
TICKET_SALT = "hrmail.attachment.download"
TICKET_MAX_AGE = 300  # seconds


def _name(user):
    return (user.get_full_name() or user.username) if user else ""


def _summary():
    state = imapbox.mailbox_state()
    counts = {
        row["status"]: row["n"]
        for row in InboundMail.objects.filter(mailbox=state.email).values("status").annotate(n=Count("id"))
    }
    return {
        "mailbox": imapbox.mailbox_address(),
        "set_up": imapbox.is_set_up(),
        "last_polled_at": state.last_polled_at,
        "last_error": state.last_error,
        "ingest_from": state.ingest_from,
        "counts": {
            "NEW": counts.get("NEW", 0),
            "REVIEWED": counts.get("REVIEWED", 0),
            "IGNORED": counts.get("IGNORED", 0),
            "ALL": sum(counts.values()),
        },
    }


def _row(mail):
    return {
        "id": mail.id,
        "from_email": mail.from_email,
        "from_name": mail.from_name,
        "subject": mail.subject,
        "preview": mail.preview,
        "received_at": mail.received_at,
        "status": mail.status,
        "is_auto": mail.is_auto,
        "attachments": getattr(mail, "file_count", 0),
    }


def _file_response(attachment):
    try:
        handle = attachment.file.open("rb")
    except (FileNotFoundError, OSError):
        raise Http404
    content_type = attachment.content_type or mimetypes.guess_type(attachment.filename)[0]
    response = FileResponse(
        handle, as_attachment=True, filename=attachment.filename, content_type=content_type or "application/octet-stream"
    )
    response["Cache-Control"] = "private, no-store"
    response["X-Content-Type-Options"] = "nosniff"
    response["Content-Security-Policy"] = "default-src 'none'; sandbox"
    return response


class _HR(APIView):
    permission_classes = [IsAuthenticated, IsHRStaff]

    def _mail(self, pk):
        mail = InboundMail.objects.filter(pk=pk, mailbox=imapbox.mailbox_state().email).first()
        if mail is None:
            raise Http404
        return mail


class MailboxView(_HR):
    def get(self, request):
        return Response(_summary())


class SyncView(_HR):
    def post(self, request):
        result = imapbox.sync_inbox(force=bool(request.data.get("force")))
        return Response({**result, **_summary()})


class MessageListView(_HR):
    def get(self, request):
        status = (request.query_params.get("status") or "NEW").upper()
        mails = InboundMail.objects.filter(mailbox=imapbox.mailbox_state().email)
        if status in ("NEW", "REVIEWED", "IGNORED"):
            mails = mails.filter(status=status)
        query = (request.query_params.get("q") or "").strip()
        if query:
            mails = mails.filter(
                Q(subject__icontains=query) | Q(from_email__icontains=query)
                | Q(from_name__icontains=query) | Q(body_text__icontains=query)
            )
        try:
            offset = max(int(request.query_params.get("offset") or 0), 0)
        except ValueError:
            offset = 0
        # Newest first, said here: Django drops the model's own ordering from a
        # query with a COUNT in it, and the inbox came out oldest first.
        mails = mails.annotate(
            file_count=Count("attachments", filter=Q(attachments__is_inline=False))
        ).order_by("-received_at", "-id")
        page = list(mails[offset:offset + PAGE_SIZE + 1])
        return Response({
            "rows": [_row(mail) for mail in page[:PAGE_SIZE]],
            "has_more": len(page) > PAGE_SIZE,
        })


class MessageView(_HR):
    def get(self, request, pk):
        mail = self._mail(pk)
        attachments = list(mail.attachments.all())
        html = mail.body_html
        # The sender's own pictures (cid:) point at the attachment route; the page
        # fetches them with its login and swaps them in, so no request leaves for
        # a stranger's server just because the mail was opened.
        for attachment in attachments:
            if attachment.content_id:
                html = re.sub(
                    rf"cid:{re.escape(attachment.content_id)}",
                    f"/api/hr-mail/messages/{mail.id}/attachments/{attachment.id}/",
                    html,
                    flags=re.I,
                )
        replies = mail.replies.select_related("sent_by").order_by("created_at")
        return Response({
            **_row(mail),
            "to_text": mail.to_text,
            "cc_text": mail.cc_text,
            "body_text": mail.body_text,
            "body_html": html,
            "attachments": [
                {
                    "id": a.id, "filename": a.filename, "size": a.size,
                    "content_type": a.content_type, "is_inline": a.is_inline,
                }
                for a in attachments
            ],
            "replies": [
                {
                    "id": r.id, "body": r.body, "status": r.status, "error": r.error,
                    "created_at": r.created_at, "sent_by": _name(r.sent_by),
                }
                for r in replies
            ],
            "can_reply": not mail.is_auto and "@" in mail.from_email,
            "status_by": _name(mail.status_by),
            "status_at": mail.status_at,
        })


class MessageStatusView(_HR):
    def post(self, request, pk):
        mail = self._mail(pk)
        status = str(request.data.get("status") or "").upper()
        if status not in ("NEW", "REVIEWED", "IGNORED"):
            return Response({"detail": "Status must be NEW, REVIEWED or IGNORED."}, status=400)
        mail.status, mail.status_by, mail.status_at = status, request.user, timezone.now()
        mail.save(update_fields=["status", "status_by", "status_at"])
        return Response(_row(mail))


class ReplyView(_HR):
    def post(self, request, pk):
        mail = self._mail(pk)
        if mail.is_auto or "@" not in mail.from_email:
            return Response(
                {"detail": "This mail was sent by a machine (an auto-reply, a list or a no-reply address). "
                           "It cannot be answered from here."},
                status=400,
            )
        body = str(request.data.get("body") or "").strip()
        if not body:
            return Response({"detail": "Write the reply first."}, status=400)
        if len(body) > MAX_BODY_LENGTH:
            return Response({"detail": "The reply is too long."}, status=400)
        try:
            sent = outbound.send(
                "REPLY", request.user, [mail.from_email], [], outbound.reply_subject(mail.subject),
                body + outbound.quoted(mail), in_reply_to=mail,
            )
        except outbound.SendError as exc:
            return Response({"detail": str(exc)}, status=502)
        mail.status, mail.status_by, mail.status_at = "REVIEWED", request.user, timezone.now()
        mail.save(update_fields=["status", "status_by", "status_at"])
        return Response({"id": sent.id, "status": sent.status, "filed_in_sent": sent.filed_in_sent})


class AttachmentView(_HR):
    """One attachment, for the page's own requests (inline pictures, downloads
    in a browser)."""

    def get(self, request, pk, attachment_id):
        mail = self._mail(pk)
        attachment = InboundAttachment.objects.filter(pk=attachment_id, mail=mail).first()
        if attachment is None:
            raise Http404
        return _file_response(attachment)


class AttachmentTicketView(_HR):
    """In the phone app a file only reaches the phone by navigation, which
    carries no login -- so, as for payslips, a ticket for one file, five minutes."""

    def get(self, request, pk, attachment_id):
        mail = self._mail(pk)
        if not InboundAttachment.objects.filter(pk=attachment_id, mail=mail).exists():
            raise Http404
        ticket = signing.dumps({"attachment": int(attachment_id), "user": request.user.id}, salt=TICKET_SALT)
        return Response({"path": f"/api/hr-mail/attachment/?t={ticket}", "expires_in": TICKET_MAX_AGE})


class AttachmentByTicketView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            payload = signing.loads(request.query_params.get("t") or "", salt=TICKET_SALT, max_age=TICKET_MAX_AGE)
        except signing.SignatureExpired:
            return Response({"detail": "This download link has expired. Please try again."}, status=403)
        except signing.BadSignature:
            return Response({"detail": "Invalid download link."}, status=403)
        attachment = InboundAttachment.objects.filter(pk=payload.get("attachment")).first()
        if attachment is None:
            raise Http404
        return _file_response(attachment)


class ComposeView(_HR):
    def post(self, request):
        files = request.FILES.getlist("files")
        total = sum(upload.size for upload in files)
        error, to, cc = check_compose(
            request.data.get("to"), request.data.get("cc"), request.data.get("subject"),
            request.data.get("body"), total,
        )
        if error:
            return Response({"detail": error}, status=400)
        attachments = [(upload.name, upload.read(), upload.content_type) for upload in files]
        try:
            sent = outbound.send(
                "COMPOSE", request.user, to, cc, str(request.data.get("subject")).strip(),
                str(request.data.get("body")), attachments,
            )
        except outbound.SendError as exc:
            return Response({"detail": str(exc)}, status=502)
        return Response({"id": sent.id, "status": sent.status, "filed_in_sent": sent.filed_in_sent})


class SentView(_HR):
    def get(self, request):
        rows = OutboundMail.objects.select_related("sent_by")[:200]
        return Response([
            {
                "id": r.id, "kind": r.kind, "to_text": r.to_text, "cc_text": r.cc_text, "subject": r.subject,
                "body": r.body, "attachment_names": r.attachment_names, "status": r.status, "error": r.error,
                "filed_in_sent": r.filed_in_sent, "sent_by": _name(r.sent_by), "created_at": r.created_at,
                "in_reply_to": r.in_reply_to_id,
            }
            for r in rows
        ])


def _system_row(r):
    return {
        "id": r.id, "kind": r.kind, "kind_label": r.get_kind_display(), "to_text": r.to_text,
        "cc_text": r.cc_text, "subject": r.subject, "about": r.about, "status": r.status, "error": r.error,
        "created_at": r.created_at,
    }


class SystemMailView(_HR):
    def get(self, request):
        rows = SystemMail.objects.order_by("-created_at", "-id")[:300]
        return Response([_system_row(r) for r in rows])


class SystemMailDetailView(_HR):
    """One Payroll mail, opened: what it said and what it carried."""

    def get(self, request, pk):
        mail = SystemMail.objects.filter(pk=pk).first()
        if mail is None:
            raise Http404
        return Response({
            **_system_row(mail),
            "from": imapbox.mailbox_address(),
            "body_text": mail.body_text,
            "body_html": mail.body_html,
            "attachments": [
                {"id": a.id, "filename": a.filename, "size": a.size, "content_type": a.content_type}
                for a in mail.attachments.all()
            ],
        })


SYSTEM_TICKET_SALT = "hrmail.system-attachment.download"


class SystemAttachmentTicketView(_HR):
    """As for the inbox's files: a five-minute ticket, since in the phone app a
    file only arrives by navigation, which carries no login."""

    def get(self, request, pk, attachment_id):
        if not SystemMailAttachment.objects.filter(pk=attachment_id, mail_id=pk).exists():
            raise Http404
        ticket = signing.dumps({"attachment": int(attachment_id), "user": request.user.id}, salt=SYSTEM_TICKET_SALT)
        return Response({"path": f"/api/hr-mail/system-attachment/?t={ticket}", "expires_in": TICKET_MAX_AGE})


class SystemAttachmentByTicketView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request):
        try:
            payload = signing.loads(request.query_params.get("t") or "", salt=SYSTEM_TICKET_SALT, max_age=TICKET_MAX_AGE)
        except signing.SignatureExpired:
            return Response({"detail": "This download link has expired. Please try again."}, status=403)
        except signing.BadSignature:
            return Response({"detail": "Invalid download link."}, status=403)
        attachment = SystemMailAttachment.objects.filter(pk=payload.get("attachment")).first()
        if attachment is None:
            raise Http404
        return _file_response(attachment)
