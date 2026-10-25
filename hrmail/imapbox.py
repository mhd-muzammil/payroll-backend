"""The HR mailbox over IMAP: copy new mail in, and file what HR sends into Sent.

Payroll has no background worker, so the inbox is read when somebody looks at
it: the Mail page asks for a sync when it opens and every minute while it
stays open, and keeps asking while a backlog remains. A sync is bounded -- a
few mails and a few megabytes -- so it always finishes well inside a request,
and it takes the newest mail first.

READ ONLY on the server. The mailbox is selected read-only and bodies are
fetched with BODY.PEEK, so nothing is marked read, moved or deleted; webmail
looks exactly as it did. The one write is APPEND to the Sent folder.
"""
import imaplib
import logging
import re
import secrets
import ssl
import time
from datetime import datetime, timedelta
from datetime import timezone as dt_timezone

from django.conf import settings
from django.core.files.base import ContentFile
from django.db import IntegrityError, transaction
from django.utils import timezone

from .mailparse import is_auto_generated, is_no_reply_sender, parse_message
from .models import InboundAttachment, InboundMail, MailboxState

logger = logging.getLogger(__name__)

MAX_PER_SYNC = 20
MAX_SYNC_BYTES = 25 * 1024 * 1024
SOCKET_TIMEOUT = 20  # seconds, per IMAP operation
FIRST_SYNC_DAYS = 7
# Two people with the page open, or a quick double press, should not both read.
SYNC_GAP = timedelta(seconds=15)
IMAP_MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")
SENT_NAMES = ("sent", "sent items", "sent mail", "sent messages", "inbox.sent", "inbox/sent")


class MailNotSetUp(Exception):
    pass


def mailbox_address():
    return (settings.EMAIL_HOST_USER or "").strip().lower()


def is_set_up():
    return bool(mailbox_address() and settings.EMAIL_HOST_PASSWORD)


NOT_SET_UP = (
    "The HR mailbox is not set up on the server: EMAIL_HOST_USER and EMAIL_HOST_PASSWORD "
    "are missing from the backend's settings."
)


def connect():
    if not is_set_up():
        raise MailNotSetUp(NOT_SET_UP)
    host = getattr(settings, "HR_MAIL_IMAP_HOST", "") or settings.EMAIL_HOST
    port = int(getattr(settings, "HR_MAIL_IMAP_PORT", 993))
    client = imaplib.IMAP4_SSL(host, port, ssl_context=ssl.create_default_context(), timeout=SOCKET_TIMEOUT)
    client.login(mailbox_address(), settings.EMAIL_HOST_PASSWORD)
    return client


def _logout(client):
    try:
        client.logout()
    except Exception:  # noqa: BLE001 - already closed
        pass


def imap_date(moment):
    """An IMAP SEARCH date, 18-Oct-2026, in English whatever the server locale."""
    local = timezone.localtime(moment)
    return f"{local.day:02d}-{IMAP_MONTHS[local.month - 1]}-{local.year}"


def mailbox_state():
    state, _ = MailboxState.objects.get_or_create(
        email=mailbox_address() or "not-set-up@localhost",
        defaults={
            "ingest_from": timezone.localtime().replace(hour=0, minute=0, second=0, microsecond=0)
            - timedelta(days=FIRST_SYNC_DAYS),
        },
    )
    return state


def _describe(exc):
    text = str(exc) or exc.__class__.__name__
    if isinstance(exc, bytes):
        text = exc.decode("utf-8", "replace")
    text = re.sub(r"^b'(.*)'$", r"\1", text)
    if "AUTHENTICATIONFAILED" in text.upper() or "authentication failed" in text.lower():
        return "The mail server refused the HR mailbox's password. Check EMAIL_HOST_PASSWORD."
    return f"Could not read the mailbox: {text}"[:500]


def _fetched_message(data):
    """(raw bytes, server's arrival time) out of a UID FETCH answer."""
    for item in data or []:
        if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
            arrived = None
            stamp = imaplib.Internaldate2tuple(item[0]) if b"INTERNALDATE" in item[0] else None
            if stamp:
                arrived = datetime.fromtimestamp(time.mktime(stamp), tz=dt_timezone.utc)
            return bytes(item[1]), arrived
    return None, None


def sync_inbox(force=False):
    """Copy mail that has arrived since the last sync. Never raises: a mailbox
    that cannot be read is reported, and what was read before a failure is kept."""
    result = {"stored": 0, "pending": 0, "error": "", "skipped": False}
    state = mailbox_state()
    if not is_set_up():
        result["error"] = NOT_SET_UP
        MailboxState.objects.filter(pk=state.pk).update(last_error=NOT_SET_UP)
        return result
    now = timezone.now()
    if not force and state.last_polled_at and now - state.last_polled_at < SYNC_GAP:
        result["skipped"] = True
        return result
    MailboxState.objects.filter(pk=state.pk).update(last_polled_at=now)

    # ---- READ, with the connection open for nothing else.
    collected, validity, pending, error = [], state.uid_validity, [], ""
    try:
        client = connect()
        try:
            status, _ = client.select("INBOX", readonly=True)
            if status != "OK":
                raise imaplib.IMAP4.error("could not open INBOX")
            reported = client.response("UIDVALIDITY")[1]
            validity = int(reported[0]) if reported and reported[0] else 0
            status, data = client.uid("SEARCH", None, "SINCE", imap_date(state.ingest_from))
            offered = data[0].split() if status == "OK" and data and data[0] else []
            known = set(
                InboundMail.objects.filter(mailbox=state.email, uid_validity=validity).values_list("uid", flat=True)
            )
            # Newest first: today's mail is what HR is waiting for, so it comes
            # in the first sync and the older backlog fills in behind it.
            pending = sorted({int(uid) for uid in offered if uid.isdigit() and int(uid) > 0} - known, reverse=True)
            budget = MAX_SYNC_BYTES
            for uid in pending[:MAX_PER_SYNC]:
                status, data = client.uid("FETCH", str(uid), "(INTERNALDATE BODY.PEEK[])")
                raw, arrived = _fetched_message(data) if status == "OK" else (None, None)
                if raw is None:
                    continue
                collected.append((uid, raw, arrived))
                budget -= len(raw)
                if budget <= 0:
                    break  # the rest are still pending; the next sync takes them
        finally:
            _logout(client)
    except MailNotSetUp as exc:
        error = str(exc)
    except Exception as exc:  # noqa: BLE001 - imaplib, ssl and sockets all raise their own
        logger.warning("HR mailbox read failed: %s", exc)
        error = _describe(exc)

    # ---- STORE, with the connection closed.
    for uid, raw, arrived in collected:
        try:
            result["stored"] += _store(state, validity, uid, raw, arrived)
        except Exception:  # noqa: BLE001 - one bad mail must not stop the rest
            logger.exception("HR mailbox: could not store UID %s", uid)

    result["pending"] = max(len(pending) - len(collected), 0)
    result["error"] = error
    MailboxState.objects.filter(pk=state.pk).update(last_error=error, uid_validity=validity)
    return result


def _store(state, validity, uid, raw, arrived):
    parsed = parse_message(raw)
    received_at = parsed["sent_at"] or arrived or timezone.now()
    if timezone.is_naive(received_at):
        received_at = timezone.make_aware(received_at, dt_timezone.utc)
    # IMAP SINCE is by day; this is the exact line.
    if (arrived or received_at) < state.ingest_from:
        return 0
    # A mailbox renumbered on the server (UIDVALIDITY changed) offers old mail
    # again under new numbers; the Message-ID says it is already here.
    if parsed["message_id"] and InboundMail.objects.filter(
        mailbox=state.email, message_id=parsed["message_id"]
    ).exists():
        return 0
    with transaction.atomic():
        try:
            mail = InboundMail.objects.create(
                mailbox=state.email,
                uid_validity=validity or 0,
                uid=uid,
                message_id=parsed["message_id"],
                references=parsed["references"],
                from_email=parsed["from_email"],
                from_name=parsed["from_name"],
                to_text=parsed["to_text"],
                cc_text=parsed["cc_text"],
                subject=parsed["subject"],
                received_at=received_at,
                body_text=parsed["body_text"],
                body_html=parsed["body_html"],
                preview=parsed["preview"],
                is_auto=is_auto_generated(parsed["headers"]) or is_no_reply_sender(parsed["from_email"]),
            )
        except IntegrityError:
            return 0  # another sync stored it first
        for item in parsed["attachments"]:
            attachment = InboundAttachment(
                mail=mail,
                filename=item["filename"],
                content_type=item["content_type"],
                size=len(item["data"]),
                content_id=item["content_id"],
                is_inline=item["is_inline"],
            )
            attachment.file.save(f"{mail.pk}-{secrets.token_hex(6)}", ContentFile(item["data"]), save=False)
            attachment.save()
    return 1


def _sent_folder(client):
    """The Sent folder: the one the server flags \\Sent, else one named like it."""
    status, rows = client.list()
    if status != "OK":
        return ""
    folders = []
    for row in rows or []:
        line = row.decode("utf-8", "replace") if isinstance(row, bytes) else str(row)
        match = re.match(r'\((?P<flags>[^)]*)\)\s+(?:"[^"]*"|NIL)\s+(?P<name>.+)$', line)
        if match:
            name = match.group("name").strip()
            if name.startswith('"') and name.endswith('"'):
                name = name[1:-1].replace('\\"', '"').replace("\\\\", "\\")
            folders.append((match.group("flags"), name))
    for flags, name in folders:
        if "\\sent" in flags.lower().split():
            return name
    for _, name in folders:
        if name.strip().lower() in SENT_NAMES:
            return name
    return ""


def file_in_sent(raw):
    """Put a copy of a sent mail in the mailbox's Sent folder, as Outlook does,
    so webmail shows it too. Best effort: the mail has already gone, so a
    failure here is logged and never reported as a failed send."""
    try:
        client = connect()
        try:
            state = mailbox_state()
            folder = state.sent_folder or _sent_folder(client)
            if not folder:
                return False
            if folder != state.sent_folder:
                MailboxState.objects.filter(pk=state.pk).update(sent_folder=folder)
            quoted = '"' + folder.replace("\\", "\\\\").replace('"', '\\"') + '"'
            status, _ = client.append(quoted, r"(\Seen)", imaplib.Time2Internaldate(time.time()), raw)
            return status == "OK"
        finally:
            _logout(client)
    except Exception as exc:  # noqa: BLE001
        logger.warning("HR mailbox: could not file a sent mail in Sent: %s", exc)
        return False
