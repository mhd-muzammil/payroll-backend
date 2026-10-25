"""Sending an appointment order to be signed, and the signing itself.

The employee gets a link by email. It opens the order on their phone with no
login; to sign they draw a signature, tick that they agree, take a photo of
themselves with the page's camera, and type a code emailed to the same
address -- the code shows it was their email, the photo shows HR their face.
The link is a long random token, kept on the order and replaced every time it
is sent, and any change to the order kills it.
"""
import base64
import binascii
import hashlib
import io
import ipaddress
import logging
import re
import secrets
from datetime import timedelta
from urllib.parse import urlsplit

from django.conf import settings
from django.contrib.auth.hashers import check_password, make_password
from django.core.files.base import ContentFile
from django.core.mail import EmailMessage
from django.db import transaction
from django.utils import timezone

from . import (
    AGREEMENT,
    _when,
    appointment_order_filename,
    letter_for_page,
    render_appointment_order_pdf,
    sign_pdf,
)

logger = logging.getLogger(__name__)

CODE_LIFETIME = timedelta(minutes=10)
CODE_GAP = timedelta(seconds=60)
MAX_CODE_SENDS = 10
MAX_CODE_TRIES = 5
MAX_SIGNATURE_BYTES = 300_000
MAX_PHOTO_BYTES = 1_500_000


class SigningError(Exception):
    def __init__(self, message, status=400):
        super().__init__(message)
        self.message = message
        self.status = status


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def link_base(origin):
    """Where the link should point: the site HR is sending it from, if that is
    one of the sites this backend serves -- never anywhere else, and never a
    localhost address that only works on the sender's own machine."""
    origin = (origin or "").strip().rstrip("/")
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not parts.hostname or parts.path or parts.query:
        raise SigningError("Could not tell which site to link to. Reload the page and try again.")
    allowed = set(settings.CORS_ALLOWED_ORIGINS) | set(settings.CSRF_TRUSTED_ORIGINS)
    known = origin in allowed or any(
        re.fullmatch(pattern, origin) for pattern in getattr(settings, "CORS_ALLOWED_ORIGIN_REGEXES", [])
    )
    local = parts.hostname in ("localhost", "127.0.0.1")
    if not known or (local and not settings.DEBUG):
        raise SigningError("Send it from the Payroll website, so the link works on the employee's phone.")
    return origin


def sign_link(base, token):
    return f"{base}/appointment/{token}"


def mask_email(email):
    """tharun.r@gmail.com -> th******@gmail.com: enough to recognise, not to copy."""
    local, _, domain = (email or "").partition("@")
    if not domain:
        return ""
    shown = local[:2]
    return f"{shown}{'*' * max(len(local) - len(shown), 3)}@{domain}"


def client_ip(request):
    """The signer's address as the proxy in front of us saw it."""
    forwarded = request.META.get("HTTP_X_FORWARDED_FOR", "")
    candidate = forwarded.split(",")[0].strip() if forwarded else request.META.get("REMOTE_ADDR", "")
    try:
        ipaddress.ip_address(candidate)
    except ValueError:
        return None
    return candidate


def _mail(subject, body, to, attachment=None, log=None):
    """One plain email from the account payslips go out from. log is (kind,
    who it is about, the subject to write down, the text to write down) for
    the Mail section's Payroll mails tab, where the mail can be opened again --
    the subject and text given there when the real ones hold a secret."""
    from hrmail.outbound import record_system_mail

    message = EmailMessage(
        subject=subject,
        body=body,
        from_email=settings.EMAIL_HOST_USER or settings.DEFAULT_FROM_EMAIL,
        to=to,
    )
    if attachment:
        message.attach(*attachment)

    def record(error=""):
        if log:
            kind, about, shown_subject, shown_body = (tuple(log) + (None, None))[:4]
            record_system_mail(
                kind, to, shown_subject or subject, about, error=error,
                body=body if shown_body is None else shown_body,
                attachments=[attachment] if attachment else (),
            )

    try:
        message.send()
    except Exception as exc:
        record(str(exc))
        raise
    record()


def _forget_code(order):
    order.otp_hash = ""
    order.otp_sent_at = None
    order.otp_sends = 0
    order.otp_attempts = 0


def reset_signing(order):
    """The order changed after it was sent: the link and the PDF it showed
    stop working, and it is a draft again."""
    if order.sent_pdf:
        order.sent_pdf.delete(save=False)
    order.sign_token = None
    order.sent_at = None
    order.viewed_at = None
    order.sent_pdf_sha256 = ""
    _forget_code(order)
    order.save()


def send_for_signature(order, origin):
    """Fix the PDF as it is now, make a fresh link to it and email the link.
    Any earlier link stops working. Returns the link."""
    if order.signed_at:
        raise SigningError("This appointment order is already signed.", 409)
    if not order.email:
        raise SigningError("Add the employee's email to the order first: the link and the signing code go there.")
    base = link_base(origin)
    try:
        pdf = render_appointment_order_pdf(order)
    except ValueError as exc:
        raise SigningError(str(exc))
    token = secrets.token_urlsafe(32)
    link = sign_link(base, token)
    try:
        _mail(
            "Appointment Order - Renderways Technology Private Limited",
            f"Dear {order.name},\n\n"
            "Your Appointment Order from Renderways Technology Private Limited is ready.\n"
            "Please open the link below, read it, and sign it:\n\n"
            f"{link}\n\n"
            "To sign, you will be asked for a one-time code. It will be sent to this email address.\n\n"
            "Renderways Technology Private Limited\n",
            [order.email],
            log=("appointment_link", order.name, None),
        )
    except Exception as exc:  # noqa: BLE001 - said to HR as it is
        logger.exception("appointment order %s: link email failed", order.pk)
        raise SigningError(f"Could not email the link: {exc}", 502)

    old = order.sent_pdf.name if order.sent_pdf else ""
    order.sent_pdf.save(f"order-{order.pk}-sent-{secrets.token_hex(6)}.pdf", ContentFile(pdf), save=False)
    order.sent_pdf_sha256 = sha256(pdf)
    order.sign_token = token
    order.sent_at = timezone.now()
    order.viewed_at = None
    _forget_code(order)
    order.save()
    if old and old != order.sent_pdf.name:
        order.sent_pdf.storage.delete(old)
    return link


def public_view(order):
    """What the signing page may know: this person's own letter, nothing else."""
    return {
        "status": "signed" if order.signed_at else "open",
        "name": order.name,
        "designation": order.designation,
        "email_hint": mask_email(order.email),
        "signed_at": order.signed_at,
        "agreement": AGREEMENT,
        "letter": letter_for_page(order),
        "filename": appointment_order_filename(order, signed=bool(order.signed_at)),
    }


def send_code(order):
    """Email a fresh six-digit code. One a minute, ten per link."""
    if order.signed_at:
        raise SigningError("This appointment order is already signed.", 409)
    now = timezone.now()
    if order.otp_sent_at and now - order.otp_sent_at < CODE_GAP:
        wait = int((CODE_GAP - (now - order.otp_sent_at)).total_seconds()) + 1
        raise SigningError(f"A code was just sent. You can ask for another in {wait} seconds.", 429)
    if order.otp_sends >= MAX_CODE_SENDS:
        raise SigningError("Too many codes for this link. Ask the office to send the order again.", 429)
    code = f"{secrets.randbelow(10 ** 6):06d}"
    body = (
        f"Dear {order.name},\n\n"
        f"Your one-time code to sign your Appointment Order is: {code}\n\n"
        "It works for 10 minutes. If you did not ask for it, ignore this email.\n\n"
        "Renderways Technology Private Limited\n"
    )
    try:
        _mail(
            f"{code} is your code to sign the Appointment Order",
            body,
            [order.email],
            # The real subject and text carry the code; it is never written down.
            log=(
                "appointment_code", order.name, "Your code to sign the Appointment Order",
                body.replace(code, "•" * 6),
            ),
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("appointment order %s: code email failed", order.pk)
        raise SigningError(f"Could not send the code: {exc}", 502)
    order.otp_hash = make_password(code)
    order.otp_sent_at = now
    order.otp_sends += 1
    order.otp_attempts = 0
    order.save(update_fields=["otp_hash", "otp_sent_at", "otp_sends", "otp_attempts", "updated_at"])
    return {"sent_to": mask_email(order.email), "expires_in": int(CODE_LIFETIME.total_seconds())}


def clean_signature(data_url):
    """The drawn signature as a PNG cropped to its ink. Refuses anything that
    is not a PNG, is too big, or has next to nothing drawn on it."""
    from PIL import Image, UnidentifiedImageError

    match = re.fullmatch(r"data:image/png;base64,([A-Za-z0-9+/=\s]+)", data_url or "")
    if not match or len(match.group(1)) > MAX_SIGNATURE_BYTES * 4 // 3 + 4:
        raise SigningError("Draw your signature in the box.")
    try:
        raw = base64.b64decode(match.group(1), validate=False)
        image = Image.open(io.BytesIO(raw))
        if image.format != "PNG" or image.width > 3000 or image.height > 1500:
            raise SigningError("Draw your signature in the box.")
        image = image.convert("RGBA")
    except (binascii.Error, UnidentifiedImageError, OSError, ValueError):
        raise SigningError("Draw your signature in the box.")
    alpha = image.getchannel("A").point(lambda a: 255 if a > 40 else 0)
    box = alpha.getbbox()
    inked = sum(1 for a in alpha.getdata() if a)
    if not box or inked < 150 or (box[2] - box[0]) < 40:
        raise SigningError("That signature is too small. Draw your full signature in the box.")
    pad = 8
    box = (max(box[0] - pad, 0), max(box[1] - pad, 0), min(box[2] + pad, image.width), min(box[3] + pad, image.height))
    out = io.BytesIO()
    image.crop(box).save(out, format="PNG")
    return out.getvalue()


def clean_photo(data_url):
    """The photo from the signing page's camera, saved afresh as a JPEG, so
    nothing but the picture is kept. Refuses anything that is not a JPEG the
    size a camera takes, and a frame that came out black or blank."""
    from PIL import Image, ImageStat, UnidentifiedImageError

    # A page from before the camera step sends no photo at all.
    missing = "Take your photo with the camera before signing. If this page has no camera step, reload it."
    match = re.fullmatch(r"data:image/jpeg;base64,([A-Za-z0-9+/=\s]+)", data_url or "")
    if not match or len(match.group(1)) > MAX_PHOTO_BYTES * 4 // 3 + 4:
        raise SigningError(missing)
    try:
        raw = base64.b64decode(match.group(1), validate=False)
        image = Image.open(io.BytesIO(raw))
        if image.format != "JPEG" or min(image.size) < 120 or max(image.size) > 2000:
            raise SigningError(missing)
        image = image.convert("RGB")
    except (binascii.Error, UnidentifiedImageError, Image.DecompressionBombError, OSError, ValueError):
        raise SigningError(missing)
    if ImageStat.Stat(image.convert("L")).stddev[0] < 6:
        raise SigningError("The photo came out dark or blank. Take it again, facing the camera in good light.")
    out = io.BytesIO()
    image.save(out, format="JPEG", quality=85)
    return out.getvalue()


def sign(order_id, code, signature, photo, agreed, ip, user_agent):
    """Sign the order with the drawn signature and the photo, if the code is
    right. Returns the order. A wrong code is counted even though the call
    fails; a missing photo or signature is refused before the code is tried."""
    from ..models import AppointmentOrder

    if agreed is not True:
        raise SigningError("Tick that you agree to the conditions stated in the Appointment Order.")
    png = clean_signature(signature)
    jpeg = clean_photo(photo)
    code = re.sub(r"\D", "", str(code or ""))

    problem = None
    with transaction.atomic():
        order = AppointmentOrder.objects.select_for_update().get(pk=order_id)
        if order.signed_at:
            raise SigningError("This appointment order is already signed.", 409)
        if not order.sign_token or not order.sent_pdf:
            raise SigningError("This link is no longer open.", 404)
        now = timezone.now()
        if not order.otp_hash or not order.otp_sent_at or now - order.otp_sent_at > CODE_LIFETIME:
            problem = "The code has expired or was never sent. Ask for a new code."
        elif order.otp_attempts >= MAX_CODE_TRIES:
            problem = "Too many wrong codes. Ask for a new code."
        elif len(code) != 6 or not check_password(code, order.otp_hash):
            order.otp_attempts += 1
            order.save(update_fields=["otp_attempts", "updated_at"])
            left = MAX_CODE_TRIES - order.otp_attempts
            problem = f"That code is not right. {left} {'try' if left == 1 else 'tries'} left." if left else (
                "Too many wrong codes. Ask for a new code."
            )
        else:
            with order.sent_pdf.open("rb") as fh:
                sent = fh.read()
            if sha256(sent) != order.sent_pdf_sha256:
                raise SigningError("The order could not be read back as it was sent. Ask the office to send it again.", 409)
            order.signed_at = now
            order.signer_ip = ip
            order.signer_user_agent = (user_agent or "")[:255]
            order.signer_photo_sha256 = sha256(jpeg)
            record = [
                ("Document", f"Appointment Order for {order.salutation} {order.name}, {order.designation}"),
                ("Reference", f"AO-{order.pk}"),
                ("Sent to", f"{order.email}, on {_when(order.sent_at)}"),
                ("Opened", _when(order.viewed_at)),
                ("Code", f"A one-time code was emailed to {order.email} at {_when(order.otp_sent_at)} "
                         "and typed in correctly."),
                ("Agreed", f"Ticked: “{AGREEMENT}”"),
                ("Signed", f"{_when(now)} (India time)"),
                ("Photo", "Taken with the camera on the signing page when signing. It is below."),
                ("IP address", ip or "-"),
                ("Device", order.signer_user_agent or "-"),
                ("Document SHA-256", [order.sent_pdf_sha256[:32], order.sent_pdf_sha256[32:]]),
                ("Photo SHA-256", [order.signer_photo_sha256[:32], order.signer_photo_sha256[32:]]),
            ]
            signed = sign_pdf(sent, png, now, record, photo=jpeg)
            stamp = secrets.token_hex(6)
            order.signature.save(f"order-{order.pk}-signature-{stamp}.png", ContentFile(png), save=False)
            order.signer_photo.save(f"order-{order.pk}-photo-{stamp}.jpg", ContentFile(jpeg), save=False)
            order.signed_pdf.save(f"order-{order.pk}-signed-{stamp}.pdf", ContentFile(signed), save=False)
            order.signed_pdf_sha256 = sha256(signed)
            order.otp_hash = ""
            order.save()
    if problem:
        raise SigningError(problem)
    return order


def email_signed_copy(order):
    """The signed copy to the employee and to whoever made the order. Never
    undoes the signing: a failure is logged and reported, nothing more."""
    to = [order.email]
    office = (order.created_by.email if order.created_by_id and order.created_by.email else "") or settings.EMAIL_HOST_USER
    if office and office.lower() != order.email.lower():
        to.append(office)
    try:
        with order.signed_pdf.open("rb") as fh:
            pdf = fh.read()
        _mail(
            "Signed Appointment Order - Renderways Technology Private Limited",
            f"Dear {order.name},\n\n"
            f"Thank you. Your Appointment Order was signed on {_when(order.signed_at)}.\n"
            "A copy is attached for your records.\n\n"
            "Renderways Technology Private Limited\n",
            to,
            (appointment_order_filename(order, signed=True), pdf, "application/pdf"),
            log=("appointment_signed", order.name, None),
        )
        return True
    except Exception:  # noqa: BLE001
        logger.exception("appointment order %s: signed copy email failed", order.pk)
        return False
