"""Appointment orders: made in the Hiring Portal, signed by the employee online.

The print HR gave (Tharun R, 8 June 2026) is the format, so the first tests are
about the letter saying what the print says with this person's details -- and
not repeating the print's two mistakes. The rest are about the signing: only
the person the link was emailed to can sign, only the words that were sent,
and once signed the order cannot change.
"""
import base64
import hashlib
import io
import math
import os
import re
import shutil
import tempfile
from datetime import date, timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image, ImageDraw, ImageFilter
from pypdf import PdfReader
from rest_framework.test import APIClient

from authentication.models import User

from .appointment_order import (
    PAGE_H,
    RECORD_BOTTOM,
    annexure,
    letter_for_page,
    render_appointment_order_pdf,
    sign_pdf,
)
from .appointment_order.ink import NoSignature, extract_ink
from .appointment_order.signing import mask_email
from .models import AppointmentOrder

THARUN = dict(
    salutation="MR.", name="Tharun R",
    address="No: 1/1A 4th cross street Venkateshwara Nagar ramapuram 600089",
    designation="Service Engineer", order_date=date(2026, 6, 8), joining_date=date(2026, 6, 8),
    basic=8104, hra=4052, conveyance=1600, cea=200, personal_allowance=2252, professional_tax=2500,
    email="tharun@example.com",
)
SITE = "https://payroll.systimus.in"


def pdf_text(pdf):
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


def paper(width=900, height=300, strokes="signature", ground="plain"):
    """A camera photo of white paper, lit unevenly as a desk lamp lights it, with
    a pen signature on it -- or nothing, a scribble too small to use, or the
    whole sheet hatched over; the paper plain, ruled, half in a hard shadow, or
    with the dark table showing along the bottom."""
    image = Image.new("RGB", (width, height))
    draw = ImageDraw.Draw(image)
    for x in range(width):  # brighter on the left, a shadow creeping in on the right
        shade = int(238 - 70 * (x / width) ** 2)
        draw.line([(x, 0), (x, height)], fill=(shade, shade, shade - 4))
    if ground == "ruled":
        for y in range(30, height, 50):
            draw.line([(0, y), (width, y)], fill=(70, 90, 130), width=3)
    elif ground == "shadow":
        draw.rectangle([(int(width * 0.7), 0), (width, height)], fill=(110, 110, 108))
    elif ground == "table":
        draw.rectangle([(0, int(height * 0.85)), (width, height)], fill=(60, 45, 35))
    pen = (25, 35, 110)
    if strokes == "signature":
        draw.line([(60, 220), (180, 70), (260, 200), (380, 80), (470, 210), (600, 90), (780, 160)], fill=pen, width=6)
        draw.line([(90, 250), (820, 230)], fill=pen, width=5)
    elif strokes == "tiny":
        draw.line([(440, 150), (452, 146), (460, 152)], fill=pen, width=3)
    elif strokes == "hatched":
        for x in range(-height, width, 9):
            draw.line([(x, 0), (x + height, height)], fill=pen, width=4)
    return image


def paper_signature(**options):
    """The photo of the paper signature as the signing page sends it."""
    out = io.BytesIO()
    paper(**options).save(out, format="JPEG", quality=88)
    return "data:image/jpeg;base64," + base64.b64encode(out.getvalue()).decode()


def drawn_signature():
    """What the page sent before the paper signature: a PNG drawn on screen."""
    image = Image.new("RGBA", (600, 200), (0, 0, 0, 0))
    ImageDraw.Draw(image).line([(40, 150), (200, 40), (360, 150), (560, 60)], fill=(20, 30, 120, 255), width=6)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


LOCATION = {"latitude": 13.0827, "longitude": 80.2707, "accuracy": 20}


def photo(width=480, height=640, blank=False, kind="JPEG"):
    """A camera frame as the signing page sends it: a face-ish oval on a
    background, or one flat colour, as a black frame from a stuck camera is."""
    image = Image.new("RGB", (width, height), (12, 12, 12) if blank else (190, 200, 210))
    if not blank:
        draw = ImageDraw.Draw(image)
        draw.ellipse((width // 4, height // 5, width * 3 // 4, height * 3 // 5), fill=(205, 160, 130))
        draw.rectangle((width // 6, height * 2 // 3, width * 5 // 6, height), fill=(40, 60, 110))
    out = io.BytesIO()
    image.save(out, format=kind)
    mime = "jpeg" if kind == "JPEG" else kind.lower()
    return f"data:image/{mime};base64," + base64.b64encode(out.getvalue()).decode()


class TheLetterTests(TestCase):
    def test_the_annexure_adds_up_as_the_print_does(self):
        sums = annexure(AppointmentOrder(**THARUN))
        self.assertEqual(sums["gross"], 16208)
        self.assertEqual(str(sums["pt_month"]), "208.3")
        self.assertEqual(sums["net"], 16000)
        self.assertEqual(sums["net_year"], 191996)
        # The print said 1,76,496 a year; twelve months of 16,208 is 1,94,496.
        self.assertEqual(sums["ctc_year"], 194496)

    def test_the_pdf_has_this_persons_details_and_the_prints_words(self):
        text = pdf_text(render_appointment_order_pdf(AppointmentOrder(**THARUN)))
        flat = re.sub(r"\s+", " ", text)
        for words in (
            "MR. Tharun R", "Venkateshwara Nagar ramapuram 600089", "APPOINTMENT ORDER",
            "Service Engineer", "Rs. 16,000", "16,208/-", "June 2026",
            "terminable on three months", "58 (fifty-eight) years of age",
            "I agree to the conditions stated", "ANNEXURE-I", "1,94,496", "1,91,996", "208.3", "2500",
        ):
            self.assertIn(words, flat)
        self.assertEqual(len(PdfReader(io.BytesIO(render_appointment_order_pdf(AppointmentOrder(**THARUN)))).pages), 3)

    def test_the_prints_mistakes_are_not_copied(self):
        text = pdf_text(render_appointment_order_pdf(AppointmentOrder(**THARUN)))
        self.assertNotIn("Radhika", text)
        self.assertNotIn("1,76,496", text)

    def test_the_signing_page_reads_the_same_words(self):
        page = letter_for_page(AppointmentOrder(**THARUN))
        words = " ".join("".join(run["t"] for run in block["runs"]) for block in page["pages"][0])
        self.assertIn("You will be paid a sum of Rs. 16,000 per month", words)
        self.assertEqual(page["annexure"]["name"], "Tharun R")
        self.assertEqual(page["annexure"]["rows"][-1], {
            "label": "CTC(A+C)", "month": "16,208", "year": "1,94,496", "style": "total",
        })

    def test_an_email_is_shown_only_in_part(self):
        self.assertEqual(mask_email("tharun@example.com"), "th****@example.com")
        self.assertEqual(mask_email(""), "")


class TheInkTests(TestCase):
    """The signature on the letter is the ink from the photo of the paper: the
    paper itself, its grain and the shadow across it are left behind."""

    def test_the_ink_comes_out_on_a_clear_ground_whatever_the_light(self):
        ink = Image.open(io.BytesIO(extract_ink(paper())))
        self.assertEqual(ink.mode, "RGBA")
        alpha = ink.getchannel("A")
        # Cropped to the signature: the strokes reach both ends.
        self.assertGreater(ink.width, 2 * ink.height)
        # Mostly see-through, and what is not is the strokes (not the shadowed edge).
        clear = alpha.histogram()[0] / (ink.width * ink.height)
        self.assertGreater(clear, 0.75)
        self.assertGreater(alpha.histogram()[255], 500)
        right_edge = alpha.crop((ink.width - 8, 0, ink.width, ink.height // 2))
        self.assertEqual(right_edge.getbbox(), None)

    def test_a_blank_sheet_a_dot_and_a_scrawled_page_are_refused(self):
        for strokes, says in (
            ("none", "No signature could be seen"),
            ("tiny", "No signature could be seen"),
            ("hatched", "too much dark"),
        ):
            with self.assertRaises(NoSignature) as caught:
                extract_ink(paper(strokes=strokes))
            self.assertIn(says, str(caught.exception))

    def test_a_dark_frame_is_refused(self):
        with self.assertRaises(NoSignature) as caught:
            extract_ink(Image.new("RGB", (900, 300), (10, 10, 10)))
        self.assertIn("came out dark", str(caught.exception))

    def test_a_faint_pen_on_evenly_lit_paper_is_still_read(self):
        """A light gel pen, thin, hardly changes the photo as a whole: that is
        not a blank photo, and the ink is found."""
        image = Image.new("RGB", (950, 421), (228, 228, 226))
        draw = ImageDraw.Draw(image)
        line = [(300 + i * 1.3, 210 + 55 * math.sin(i / 17)) for i in range(290)]
        draw.line(line, fill=(90, 125, 205), width=2)
        draw.line([(310, 290), (660, 280)], fill=(90, 125, 205), width=2)
        ink = Image.open(io.BytesIO(extract_ink(image.filter(ImageFilter.GaussianBlur(0.6)))))
        self.assertEqual(ink.mode, "RGBA")
        self.assertGreater(ink.width, 300)

    def test_lines_or_the_papers_edge_across_the_box_are_refused(self):
        """Dark ruled lines and the table along the bottom would be stamped on
        the letter as ink: a signature fitted inside the box never reaches both
        of its sides, so these are refused -- and a blank sheet in a hard
        shadow, whose edge is all there is to see. (Faint lines that fade out
        into a shadow before the far side get through on a blank sheet; the
        page shows the ink before signing and asks for a retake if it has any.)"""
        for ground, strokes in (
            ("ruled", "signature"), ("ruled", "none"), ("table", "signature"), ("table", "none"), ("shadow", "none"),
        ):
            with self.assertRaises(NoSignature, msg=f"{ground}, {strokes}") as caught:
                extract_ink(paper(strokes=strokes, ground=ground))
            self.assertIn("runs right across the box", str(caught.exception))

    def test_a_shadow_across_the_signature_changes_nothing(self):
        """Ink is read as how many times darker it is than its own paper: the
        part of the signature in a hard shadow is kept, and the shadow's edge
        is left out -- the same ink as on paper with no shadow."""
        plain = Image.open(io.BytesIO(extract_ink(paper())))
        shaded = Image.open(io.BytesIO(extract_ink(paper(ground="shadow"))))
        self.assertEqual(shaded.size, plain.size)


def drawn(page):
    """What a PDF page has drawn on it, in points from the top: each picture's
    (top, bottom, height) -- not the letterhead's, which fills the page -- and
    the baseline of each piece of text."""
    pictures, baselines = [], []

    def operator(op, args, cm, tm):
        if op == b"Do" and cm[3] < 800:  # a picture fills the unit square under cm
            pictures.append((PAGE_H - cm[5] - cm[3], PAGE_H - cm[5], cm[3]))
        elif op in (b"Tj", b"TJ") and str(args[0]).strip():
            # Where the text is shown, not where pypdf's text visitor says: that
            # one hands some pieces over after the position is reset.
            baselines.append(PAGE_H - (tm[4] * cm[1] + tm[5] * cm[3] + cm[5]))

    page.extract_text(visitor_operand_before=operator)
    return pictures, baselines


class TheRecordPageTests(TestCase):
    def _record_page(self, email, device):
        record = [
            ("Document", "Appointment Order for MR. Tharun R, Service Engineer"),
            ("Reference", "AO-1"),
            ("Sent to", f"{email}, on 31-10-2026, 12:23 AM"),
            ("Opened", "31-10-2026, 12:23 AM"),
            ("Code", f"A one-time code was emailed to {email} at 31-10-2026, 12:23 AM and typed in correctly."),
            ("Agreed", "Ticked: “I agree to the conditions stated in the Appointment Order.”"),
            ("Signed", "31-10-2026, 12:23 AM (India time)"),
            ("Signature", "Signed on paper and photographed with the camera on the signing page; "
                          "the signature on pages 1 to 3 is the ink from that photo. Both are below."),
            ("Photo", "Taken with the camera on the signing page when signing. It is below."),
            ("Location", "13.082700, 80.270700 (within 1,235 m), as the phone reported it when signing."),
            ("Map", "https://www.google.com/maps?q=13.082700,80.270700"),
            ("IP address", "2401:4900:1c80:5a2b:9d3e:47ff:fe21:8c10"),
            ("Device", device),
        ] + [(f"{name} SHA-256", ["ab" * 16, "cd" * 16]) for name in ("Document", "Signature photo", "Photo")]
        selfie = base64.b64decode(photo().split(",", 1)[1])  # upright, 480 x 640
        sheet = base64.b64decode(paper_signature().split(",", 1)[1])
        pdf = sign_pdf(render_appointment_order_pdf(AppointmentOrder(**THARUN)), extract_ink(paper()),
                       timezone.now(), record, photo=selfie, signature_photo=sheet)
        return PdfReader(io.BytesIO(pdf)).pages[3]

    def test_the_photos_are_full_size_on_a_usual_record(self):
        page = self._record_page("tharun@example.com", "Mozilla/5.0 (Linux; Android 14; SM-A146B) AppleWebKit/537.36 "
                                 "(KHTML, like Gecko) Chrome/141.0.0.0 Mobile Safari/537.36")
        pictures, baselines = drawn(page)
        self.assertIn(130.0, [round(height, 1) for _, _, height in pictures])  # the photo, passport size
        self.assertLessEqual(max(bottom for _, bottom, _ in pictures), RECORD_BOTTOM)
        self.assertLessEqual(max(baselines), RECORD_BOTTOM)

    def test_a_long_record_never_runs_into_the_footer(self):
        """The longest a record gets -- a long email, the longest browser name
        kept, an IPv6 address -- and the pictures shrink to stay above the
        letterhead's footer, all of them still there."""
        email = "mohamed.vaseem.kumar.service.engineer.chennai@renderways-technology.co.in"
        device = ("Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) "
                  "Version/17.5 Mobile/15E148 Safari/604.1 Instagram 312.0.0.34.111 (iPhone15,2; iOS 17_5; en_IN; "
                  "en-IN; scale=3.00; 1179x2556; 543210987) NW/3 " * 2)[:255]
        page = self._record_page(email, device)
        pictures, baselines = drawn(page)
        self.assertLessEqual(max(bottom for _, bottom, _ in pictures), RECORD_BOTTOM)
        self.assertLessEqual(max(baselines), RECORD_BOTTOM)
        self.assertEqual(len([p for p in pictures if p[0] > 500]), 3)  # the ink, the paper and the photo
        text = re.sub(r"\s+", " ", page.extract_text())
        for words in ("Photo SHA-256", "Signature and photos", "The signature on paper", "The employee"):
            self.assertIn(words, text)


class _Office(TestCase):
    def setUp(self):
        cache.clear()  # the public door's rate limit counts in the cache
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        overrides = override_settings(
            MEDIA_ROOT=self.media,
            CORS_ALLOWED_ORIGINS=[SITE],
            EMAIL_HOST_USER="hr@renderways.in",
        )
        overrides.enable()
        self.addCleanup(overrides.disable)
        self.hr = User.objects.create_user(username="hr-ao", password="x", role="hr", email="hr-desk@renderways.in")
        self.office = APIClient()
        self.office.force_authenticate(self.hr)
        self.public = APIClient()

    def _create(self, **changes):
        body = {**THARUN, **changes}
        body["order_date"] = str(body["order_date"])
        body["joining_date"] = str(body["joining_date"])
        response = self.office.post("/api/appointment-orders/", body, format="json")
        self.assertEqual(response.status_code, 201, response.data)
        return response.data

    def _send(self, order_id, origin=SITE):
        return self.office.post(f"/api/appointment-orders/{order_id}/send/", {"origin": origin}, format="json")

    def _sent(self, **changes):
        order = self._create(**changes)
        response = self._send(order["id"])
        self.assertEqual(response.status_code, 200, response.data)
        token = response.data["link"].rsplit("/", 1)[1]
        return order["id"], token

    def _code(self, token):
        response = self.public.post(f"/api/appointment/{token}/code/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return re.search(r"code to sign your Appointment Order is: (\d{6})", mail.outbox[-1].body).group(1)

    def _sign(self, token, code, headers=None, **changes):
        body = {
            "code": code, "signature_photo": paper_signature(), "photo": photo(),
            "location": LOCATION, "agree": True, **changes,
        }
        return self.public.post(f"/api/appointment/{token}/sign/", body, format="json", **(headers or {}))


class OfficeTests(_Office):
    def test_only_hr_can_see_appointment_orders(self):
        employee = User.objects.create_user(username="emp-ao", password="x", role="employee")
        client = APIClient()
        client.force_authenticate(employee)
        self.assertEqual(client.get("/api/appointment-orders/").status_code, 403)
        self.assertEqual(APIClient().get("/api/appointment-orders/").status_code, 401)

    def test_words_the_letter_cannot_print_are_refused(self):
        body = {**THARUN, "name": "தருண்", "order_date": "2026-06-08", "joining_date": "2026-06-08"}
        response = self.office.post("/api/appointment-orders/", body, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("name", response.data)
        body = {**body, "name": "Tharun R", "professional_tax": 16208 * 12}
        self.assertEqual(self.office.post("/api/appointment-orders/", body, format="json").status_code, 400)

    def test_sending_needs_an_email_and_a_real_site(self):
        order = self._create(email="")
        response = self._send(order["id"])
        self.assertEqual(response.status_code, 400)
        self.assertIn("email", response.data["detail"])
        order = self._create()
        self.assertEqual(self._send(order["id"], "http://localhost:5173").status_code, 400)
        self.assertEqual(self._send(order["id"], "https://evil.example.com").status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_sending_emails_a_link_and_keeps_the_pdf_it_points_at(self):
        order_id, token = self._sent()
        self.assertEqual(len(mail.outbox), 1)
        self.assertEqual(mail.outbox[0].to, ["tharun@example.com"])
        self.assertIn(f"{SITE}/appointment/{token}", mail.outbox[0].body)
        order = AppointmentOrder.objects.get(pk=order_id)
        self.assertEqual(order.status, "sent")
        self.assertEqual(len(order.sent_pdf_sha256), 64)

    def test_sending_again_kills_the_first_link(self):
        order_id, first = self._sent()
        second = self._send(order_id).data["link"].rsplit("/", 1)[1]
        self.assertNotEqual(first, second)
        self.assertEqual(self.public.get(f"/api/appointment/{first}/").status_code, 404)
        self.assertEqual(self.public.get(f"/api/appointment/{second}/").status_code, 200)

    def test_changing_a_sent_order_kills_its_link(self):
        order_id, token = self._sent()
        body = {**THARUN, "basic": 9000, "order_date": "2026-06-08", "joining_date": "2026-06-08"}
        response = self.office.put(f"/api/appointment-orders/{order_id}/", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "draft")
        self.assertEqual(self.public.get(f"/api/appointment/{token}/").status_code, 404)

    def test_the_link_can_be_copied_for_whatsapp(self):
        order_id, token = self._sent()
        response = self.office.get(f"/api/appointment-orders/{order_id}/link/", {"origin": SITE})
        self.assertEqual(response.data["link"], f"{SITE}/appointment/{token}")


class SigningTests(_Office):
    def test_an_unknown_link_says_nothing(self):
        self.assertEqual(self.public.get("/api/appointment/not-a-real-token/").status_code, 404)
        self.assertEqual(self.public.post("/api/appointment/not-a-real-token/code/").status_code, 404)

    def test_opening_the_link_shows_the_letter_and_marks_it_opened(self):
        order_id, token = self._sent()
        response = self.public.get(f"/api/appointment/{token}/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["status"], "open")
        self.assertEqual(response.data["email_hint"], "th****@example.com")
        self.assertNotIn("email", response.data)
        self.assertEqual(AppointmentOrder.objects.get(pk=order_id).status, "opened")
        pdf = self.public.get(f"/api/appointment/{token}/pdf/")
        self.assertEqual(pdf.status_code, 200)
        self.assertEqual(pdf["Content-Type"], "application/pdf")

    def test_a_code_is_emailed_and_not_again_within_a_minute(self):
        _, token = self._sent()
        self._code(token)
        self.assertEqual(mail.outbox[-1].to, ["tharun@example.com"])
        again = self.public.post(f"/api/appointment/{token}/code/", {}, format="json")
        self.assertEqual(again.status_code, 429)

    def test_a_wrong_code_is_refused_and_counted(self):
        order_id, token = self._sent()
        code = self._code(token)
        wrong = "000000" if code != "000000" else "111111"
        for left in (4, 3, 2, 1):
            response = self._sign(token, wrong)
            self.assertEqual(response.status_code, 400)
            self.assertIn(f"{left} tr", response.data["detail"])
        self.assertEqual(self._sign(token, wrong).status_code, 400)
        # Five wrong: even the right code no longer works.
        response = self._sign(token, code)
        self.assertEqual(response.status_code, 400)
        self.assertIn("Too many", response.data["detail"])
        self.assertIsNone(AppointmentOrder.objects.get(pk=order_id).signed_at)

    def test_an_old_code_has_expired(self):
        order_id, token = self._sent()
        code = self._code(token)
        AppointmentOrder.objects.filter(pk=order_id).update(otp_sent_at=timezone.now() - timedelta(minutes=11))
        response = self._sign(token, code)
        self.assertEqual(response.status_code, 400)
        self.assertIn("expired", response.data["detail"])

    def test_it_must_be_agreed_and_actually_signed_on_paper(self):
        order_id, token = self._sent()
        code = self._code(token)
        self.assertEqual(self._sign(token, code, agree=False).status_code, 400)
        blank = self._sign(token, code, signature_photo=paper_signature(strokes="none"))
        self.assertEqual(blank.status_code, 400)
        self.assertIn("No signature could be seen", blank.data["detail"])
        # A page from before the paper signature sends a drawn one: told to reload.
        old_page = self._sign(token, code, signature_photo=None, signature=drawn_signature())
        self.assertEqual(old_page.status_code, 400)
        self.assertIn("reload", old_page.data["detail"])
        for wrong in ("", drawn_signature(), "data:image/jpeg;base64,AAAA", "https://example.com/x.jpg"):
            self.assertEqual(self._sign(token, code, signature_photo=wrong).status_code, 400)
        self.assertEqual(AppointmentOrder.objects.get(pk=order_id).otp_attempts, 0)

    def test_a_location_must_be_shared(self):
        order_id, token = self._sent()
        code = self._code(token)
        missing = self._sign(token, code, location=None)
        self.assertEqual(missing.status_code, 400)
        self.assertIn("Share your location", missing.data["detail"])
        for wrong in (
            {}, "13.08,80.27", {"latitude": "x", "longitude": 80.27, "accuracy": 20},
            {"latitude": "NaN", "longitude": 80.27, "accuracy": 20},
            {"latitude": 95, "longitude": 80.27, "accuracy": 20},
            {"latitude": 13.08, "longitude": 200, "accuracy": 20},
            {"latitude": 0, "longitude": 0, "accuracy": 20},
            {"latitude": 13.08, "longitude": 80.27, "accuracy": -1},
        ):
            self.assertEqual(self._sign(token, code, location=wrong).status_code, 400, wrong)
        # None of that used up a try of the code: it still signs.
        self.assertEqual(AppointmentOrder.objects.get(pk=order_id).otp_attempts, 0)
        self.assertEqual(self._sign(token, code).status_code, 200)

    def test_the_page_shows_the_ink_before_signing(self):
        order_id, token = self._sent()
        url = f"/api/appointment/{token}/signature-preview/"
        response = self.public.post(url, {"signature_photo": paper_signature()}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["signature"].startswith("data:image/png;base64,"))
        preview = base64.b64decode(response.data["signature"].split(",", 1)[1])
        # What the preview shows is exactly what goes on the letter.
        self._sign(token, self._code(token))
        with AppointmentOrder.objects.get(pk=order_id).signature.open("rb") as fh:
            self.assertEqual(fh.read(), preview)
        self.assertEqual(self.public.post(url, {"signature_photo": paper_signature()}, format="json").status_code, 409)

    def test_the_preview_says_what_to_change(self):
        _, token = self._sent()
        url = f"/api/appointment/{token}/signature-preview/"
        response = self.public.post(url, {"signature_photo": paper_signature(strokes="tiny")}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("No signature could be seen", response.data["detail"])
        self.assertEqual(self.public.post("/api/appointment/nope/signature-preview/", {}, format="json").status_code, 404)

    def test_a_preview_keeps_nothing(self):
        order_id, token = self._sent()
        stored = lambda: sorted(os.path.join(root, name) for root, _, names in os.walk(self.media) for name in names)
        before = stored()
        for _ in range(2):
            response = self.public.post(
                f"/api/appointment/{token}/signature-preview/", {"signature_photo": paper_signature()}, format="json"
            )
            self.assertEqual(response.status_code, 200, response.data)
        order = AppointmentOrder.objects.get(pk=order_id)
        self.assertFalse(order.signature or order.signature_photo or order.signed_at)
        self.assertEqual((order.otp_attempts, order.otp_sends), (0, 0))
        self.assertEqual(stored(), before)

    def test_odd_requests_get_a_reason_not_a_crash(self):
        """What only a hand-made request would send: each is a 400 saying what
        is wrong, and no try at the code is used up."""
        order_id, token = self._sent()
        code = self._code(token)
        preview = f"/api/appointment/{token}/signature-preview/"
        sign = f"/api/appointment/{token}/sign/"
        for odd in (123, 1.5, True, ["a"], {"a": 1}):
            self.assertEqual(self.public.post(preview, {"signature_photo": odd}, format="json").status_code, 400)
            self.assertEqual(self._sign(token, code, signature_photo=odd).status_code, 400)
            self.assertEqual(self._sign(token, code, photo=odd).status_code, 400)
        # A whole number too big for a float.
        huge = self._sign(token, code, location={"latitude": 10 ** 400, "longitude": 80, "accuracy": 5})
        self.assertEqual(huge.status_code, 400)
        self.assertIn("Share your location", huge.data["detail"])
        for body in ([], "x", 5):
            self.assertEqual(self.public.post(preview, body, format="json").status_code, 400)
            self.assertEqual(self.public.post(sign, body, format="json").status_code, 400)
        # JSON only: nothing is spooled to disk as an upload.
        self.assertEqual(self.public.post(preview, {"signature_photo": "x"}, format="multipart").status_code, 415)
        # Nothing bigger than two photos need is read at all.
        big = self.public.post(preview, {"signature_photo": "x" * 6_100_000}, format="json")
        self.assertEqual(big.status_code, 400)
        self.assertIn("too big", big.data["detail"])
        self.assertEqual(AppointmentOrder.objects.get(pk=order_id).otp_attempts, 0)

    def test_only_the_map_is_a_link_on_the_record(self):
        """The Device row is whatever the browser sent: written down, never a link."""
        order_id, token = self._sent()
        response = self._sign(token, self._code(token), headers={"HTTP_USER_AGENT": "https://evil.example/login Mozilla/5.0"})
        self.assertEqual(response.status_code, 200, response.data)
        with AppointmentOrder.objects.get(pk=order_id).signed_pdf.open("rb") as fh:
            record = PdfReader(io.BytesIO(fh.read())).pages[3]
        links = [
            annotation.get_object()["/A"]["/URI"]
            for annotation in record.get("/Annots", [])
            if annotation.get_object().get("/A", {}).get("/URI")
        ]
        self.assertEqual(links, ["https://www.google.com/maps?q=13.082700,80.270700"])
        self.assertIn("https://evil.example/login", re.sub(r"\s+", " ", record.extract_text()))

    def test_a_photo_must_be_taken(self):
        order_id, token = self._sent()
        code = self._code(token)
        missing = self._sign(token, code, photo=None)
        self.assertEqual(missing.status_code, 400)
        self.assertIn("Take your photo", missing.data["detail"])
        for wrong in (
            "", "data:image/jpeg;base64,AAAA", "https://example.com/me.jpg",
            photo(kind="PNG"),  # the page only ever sends a JPEG
            photo(width=80, height=80),  # no camera takes a picture this small
        ):
            self.assertEqual(self._sign(token, code, photo=wrong).status_code, 400)
        dark = self._sign(token, code, photo=photo(blank=True))
        self.assertEqual(dark.status_code, 400)
        self.assertIn("dark or blank", dark.data["detail"])
        # None of that used up a try of the code: it still signs.
        order = AppointmentOrder.objects.get(pk=order_id)
        self.assertEqual(order.otp_attempts, 0)
        self.assertIsNone(order.signed_at)
        self.assertEqual(self._sign(token, code).status_code, 200)

    def test_signing(self):
        order_id, token = self._sent()
        self.public.get(f"/api/appointment/{token}/")
        code = self._code(token)
        response = self._sign(token, code, headers={"HTTP_USER_AGENT": "Mozilla/5.0 (Linux; Android 14)"})
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["emailed"])

        order = AppointmentOrder.objects.get(pk=order_id)
        self.assertEqual(order.status, "signed")
        with order.signed_pdf.open("rb") as fh:
            signed = fh.read()
        reader = PdfReader(io.BytesIO(signed))
        self.assertEqual(len(reader.pages), 4)
        record = re.sub(r"\s+", " ", reader.pages[3].extract_text())
        self.assertIn("Electronic Signature Record", record)
        self.assertIn(order.sent_pdf_sha256[:32], record)
        self.assertIn("tharun@example.com", record)
        self.assertIn("Android 14", record)
        # The photo is kept as a JPEG of its own, named by its hash on the
        # record page, and drawn there beside the signature.
        with order.signer_photo.open("rb") as fh:
            kept = fh.read()
        self.assertEqual(Image.open(io.BytesIO(kept)).format, "JPEG")
        self.assertEqual(order.signer_photo_sha256, hashlib.sha256(kept).hexdigest())
        self.assertIn("Signature and photos", record)
        self.assertIn("The employee", record)
        self.assertIn(order.signer_photo_sha256[:32], record)
        sizes = [image.image.size for image in reader.pages[3].images]
        self.assertIn((480, 640), sizes)
        # So is the photo of the paper; the ink on the letter came from it.
        with order.signature_photo.open("rb") as fh:
            paper_kept = fh.read()
        self.assertEqual(Image.open(io.BytesIO(paper_kept)).format, "JPEG")
        self.assertEqual(order.signature_photo_sha256, hashlib.sha256(paper_kept).hexdigest())
        self.assertIn((900, 300), sizes)
        self.assertIn(order.signature_photo_sha256[:32], record)
        self.assertIn("The signature on paper", record)
        with order.signature.open("rb") as fh:
            self.assertEqual(Image.open(io.BytesIO(fh.read())).mode, "RGBA")
        # Where they were, and a link to it on the map.
        self.assertEqual((order.signer_lat, order.signer_lon, order.signer_accuracy), (13.0827, 80.2707, 20.0))
        self.assertIn("13.082700, 80.270700 (within 20 m)", record)
        self.assertIn("https://www.google.com/maps?q=13.082700,80.270700", record)
        links = [
            annotation.get_object()["/A"]["/URI"]
            for annotation in reader.pages[3].get("/Annots", [])
            if annotation.get_object().get("/A", {}).get("/URI")
        ]
        self.assertEqual(links, ["https://www.google.com/maps?q=13.082700,80.270700"])
        # The letter underneath is the one that was sent; page 2 gains the date.
        with order.sent_pdf.open("rb") as fh:
            sent = PdfReader(io.BytesIO(fh.read()))
        self.assertEqual(reader.pages[0].extract_text(), sent.pages[0].extract_text())
        self.assertEqual(reader.pages[2].extract_text(), sent.pages[2].extract_text())
        self.assertIn(timezone.localtime(order.signed_at).strftime("%d-%m-%Y"), reader.pages[1].extract_text())

        copy = mail.outbox[-1]
        self.assertEqual(copy.to, ["tharun@example.com", "hr-desk@renderways.in"])
        self.assertEqual(copy.attachments[0][0], "Appointment-Order-Tharun-R-signed.pdf")

        # Signed is final.
        self.assertEqual(self._sign(token, code).status_code, 409)
        self.assertEqual(self.public.post(f"/api/appointment/{token}/code/").status_code, 409)
        body = {**THARUN, "order_date": "2026-06-08", "joining_date": "2026-06-08"}
        self.assertEqual(self.office.put(f"/api/appointment-orders/{order_id}/", body, format="json").status_code, 409)
        self.assertEqual(self.office.delete(f"/api/appointment-orders/{order_id}/").status_code, 409)
        self.assertEqual(self._send(order_id).status_code, 409)
        # The link still shows the employee their signed copy.
        self.assertEqual(self.public.get(f"/api/appointment/{token}/").data["status"], "signed")
        self.assertEqual(self.public.get(f"/api/appointment/{token}/pdf/").content, signed)

    def test_the_office_downloads_the_signed_copy(self):
        order_id, token = self._sent()
        self._sign(token, self._code(token))
        ticket = self.office.get(f"/api/appointment-orders/{order_id}/pdf_ticket/").data
        response = APIClient().get(ticket["path"])
        self.assertEqual(response.status_code, 200)
        self.assertIn("-signed.pdf", response["Content-Disposition"])
        self.assertEqual(len(PdfReader(io.BytesIO(response.content)).pages), 4)

    def test_the_office_sees_the_photo(self):
        order_id, token = self._sent()
        url = f"/api/appointment-orders/{order_id}/photo/"
        self.assertFalse(self.office.get(f"/api/appointment-orders/{order_id}/").data["has_photo"])
        self.assertEqual(self.office.get(url).status_code, 404)
        self._sign(token, self._code(token))
        self.assertTrue(self.office.get(f"/api/appointment-orders/{order_id}/").data["has_photo"])
        response = self.office.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response["Cache-Control"], "no-store")
        order = AppointmentOrder.objects.get(pk=order_id)
        with order.signer_photo.open("rb") as fh:
            self.assertEqual(response.content, fh.read())
        # Nobody else: not without a login, not an employee, not the link.
        self.assertEqual(APIClient().get(url).status_code, 401)
        employee = User.objects.create_user(username="emp-photo", password="x", role="employee")
        client = APIClient()
        client.force_authenticate(employee)
        self.assertEqual(client.get(url).status_code, 403)
        self.assertNotIn("photo", self.public.get(f"/api/appointment/{token}/").data)

    def test_the_office_sees_the_paper_and_the_place(self):
        order_id, token = self._sent()
        url = f"/api/appointment-orders/{order_id}/signature-photo/"
        before = self.office.get(f"/api/appointment-orders/{order_id}/").data
        self.assertFalse(before["has_signature_photo"])
        self.assertIsNone(before["location"])
        self.assertEqual(self.office.get(url).status_code, 404)
        self._sign(token, self._code(token))
        after = self.office.get(f"/api/appointment-orders/{order_id}/").data
        self.assertTrue(after["has_signature_photo"])
        self.assertEqual(after["location"], {
            "latitude": 13.0827, "longitude": 80.2707, "accuracy": 20.0,
            "text": "13.082700, 80.270700 (within 20 m)",
            "map": "https://www.google.com/maps?q=13.082700,80.270700",
        })
        response = self.office.get(url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "image/jpeg")
        self.assertEqual(response["Cache-Control"], "no-store")
        with AppointmentOrder.objects.get(pk=order_id).signature_photo.open("rb") as fh:
            self.assertEqual(response.content, fh.read())
        self.assertEqual(APIClient().get(url).status_code, 401)
        employee = User.objects.create_user(username="emp-paper", password="x", role="employee")
        client = APIClient()
        client.force_authenticate(employee)
        self.assertEqual(client.get(url).status_code, 403)
        # Nothing about where they were reaches the link.
        public = self.public.get(f"/api/appointment/{token}/").data
        self.assertNotIn("location", public)
        # The office cannot write a location.
        draft = self._create()
        self.office.patch(f"/api/appointment-orders/{draft['id']}/", {"location": LOCATION, "signer_lat": 1}, format="json")
        unsigned = AppointmentOrder.objects.get(pk=draft["id"])
        self.assertIsNone(unsigned.signer_lat)

    def test_a_failed_email_does_not_undo_the_signing(self):
        order_id, token = self._sent()
        code = self._code(token)
        with mock.patch("onboarding.appointment_order.signing.EmailMessage.send", side_effect=OSError("smtp down")):
            response = self._sign(token, code)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["emailed"])
        self.assertIsNotNone(AppointmentOrder.objects.get(pk=order_id).signed_at)
