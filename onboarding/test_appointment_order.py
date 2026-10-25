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
import re
import shutil
import tempfile
from datetime import date, timedelta
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from django.utils import timezone
from PIL import Image, ImageDraw
from pypdf import PdfReader
from rest_framework.test import APIClient

from authentication.models import User

from .appointment_order import annexure, letter_for_page, render_appointment_order_pdf
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


def signature(width=600, height=200, strokes=True):
    image = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    if strokes:
        ImageDraw.Draw(image).line([(40, 150), (200, 40), (360, 150), (560, 60)], fill=(20, 30, 120, 255), width=6)
    out = io.BytesIO()
    image.save(out, format="PNG")
    return "data:image/png;base64," + base64.b64encode(out.getvalue()).decode()


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
        body = {"code": code, "signature": signature(), "photo": photo(), "agree": True, **changes}
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

    def test_it_must_be_agreed_and_actually_signed(self):
        _, token = self._sent()
        code = self._code(token)
        self.assertEqual(self._sign(token, code, agree=False).status_code, 400)
        self.assertEqual(self._sign(token, code, signature=signature(strokes=False)).status_code, 400)
        self.assertEqual(self._sign(token, code, signature="data:image/png;base64,AAAA").status_code, 400)
        self.assertEqual(self._sign(token, code, signature="https://example.com/x.png").status_code, 400)

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
        self.assertIn("Photo taken", record)
        self.assertIn(order.signer_photo_sha256[:32], record)
        self.assertIn((480, 640), [image.image.size for image in reader.pages[3].images])
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

    def test_a_failed_email_does_not_undo_the_signing(self):
        order_id, token = self._sent()
        code = self._code(token)
        with mock.patch("onboarding.appointment_order.signing.EmailMessage.send", side_effect=OSError("smtp down")):
            response = self._sign(token, code)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["emailed"])
        self.assertIsNotNone(AppointmentOrder.objects.get(pk=order_id).signed_at)
