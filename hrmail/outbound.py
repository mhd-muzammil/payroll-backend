"""Mail that leaves the HR mailbox: HR's replies and new mails from the Mail
section, and the record of what Payroll sends by itself.

Everything goes out over the same SMTP account Payroll already uses
(EMAIL_HOST_USER), so the From line is always the HR mailbox and the
employee's or candidate's answer comes back to it.
"""
import logging
from email.utils import formatdate, make_msgid

from django.core.mail import EmailMultiAlternatives
from django.utils.html import strip_tags
from django.utils.timezone import localtime

from .imapbox import NOT_SET_UP, file_in_sent, is_set_up, mailbox_address
from .models import OutboundMail, SystemMail

logger = logging.getLogger(__name__)


class SendError(Exception):
    pass


def send(kind, user, to, cc, subject, body, attachments=(), in_reply_to=None, html=None):
    """Send one mail from the HR mailbox and record it. attachments: (name,
    bytes, content type); html, when given, is the mail as it is meant to be
    seen, with body as its plain-text copy. Raises SendError when the mail did
    not go; the attempt is recorded either way."""
    if not is_set_up():
        raise SendError(NOT_SET_UP)
    sender = mailbox_address()
    message_id = make_msgid(domain=sender.split("@")[-1] or None)
    # Set here, not left to Django, so the copy filed in Sent carries the same
    # Message-ID and date as the mail that went.
    headers = {"Message-ID": message_id, "Date": formatdate(localtime=True)}
    if in_reply_to is not None and in_reply_to.message_id:
        headers["In-Reply-To"] = in_reply_to.message_id
        headers["References"] = f"{in_reply_to.references} {in_reply_to.message_id}".strip()
    message = EmailMultiAlternatives(subject=subject, body=body, from_email=sender, to=to, cc=cc, headers=headers)
    if html:
        message.attach_alternative(html, "text/html")
    for name, content, content_type in attachments:
        message.attach(name, content, content_type or None)

    record = OutboundMail(
        kind=kind,
        in_reply_to=in_reply_to,
        from_email=sender,
        to_text=", ".join(to),
        cc_text=", ".join(cc),
        subject=subject,
        body=body,
        attachment_names=[name for name, _, _ in attachments],
        message_id=message_id.strip("<>")[:255],
        sent_by=user if getattr(user, "is_authenticated", False) else None,
    )
    try:
        message.send()
    except Exception as exc:  # noqa: BLE001 - SMTP refusals, sockets, TLS
        record.status, record.error = "FAILED", str(exc)[:500]
        record.save()
        logger.warning("HR mail to %s failed: %s", record.to_text, exc)
        raise SendError(f"The mail did not go: {exc}")
    record.status = "SENT"
    record.filed_in_sent = file_in_sent(message.message().as_bytes())
    record.save()
    return record


def reply_subject(subject):
    subject = (subject or "").strip()
    return subject if subject.lower().startswith("re:") else f"Re: {subject}".strip()


def quoted(mail):
    """The mail being answered, under the answer, as mail clients quote it."""
    when = localtime(mail.received_at).strftime("%d %b %Y, %I:%M %p")
    sender = f"{mail.from_name} <{mail.from_email}>" if mail.from_name else mail.from_email
    text = mail.body_text or strip_tags(mail.body_html)
    lines = "\n".join(f"> {line}" for line in text.strip().splitlines()[:200])
    return f"\n\nOn {when}, {sender} wrote:\n{lines}"


def record_system_mail(kind, to, subject, about="", error=""):
    """Write down one mail Payroll sent by itself, for the Payroll mails tab.
    Never raises: a mail that went must not be reported as failed because its
    note could not be written."""
    try:
        SystemMail.objects.create(
            kind=kind,
            to_text=", ".join(to) if isinstance(to, (list, tuple)) else str(to),
            subject=str(subject)[:500],
            about=str(about)[:200],
            status="FAILED" if error else "SENT",
            error=str(error)[:500],
        )
    except Exception:  # noqa: BLE001
        logger.exception("could not record a system mail")
