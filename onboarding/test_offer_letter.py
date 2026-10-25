"""Offer letters made in the Hiring Portal.

The letter is HR's own six pages with five things filled in, so most of what is
below is about those five: the Annexure adding up, the numbers written the way
the sample writes them, the right person's details on the page and nobody
else's (the sample's included), and the letter staying with HR.
"""
import io
from datetime import date
from unittest import mock

from django.core import mail
from django.core.cache import cache
from django.test import TestCase, override_settings
from pypdf import PdfReader
from rest_framework.test import APIClient

from authentication.models import User
from hrmail.models import OutboundMail, SystemMail

from .models import OfferLetter, OfferLetterMail
from .offer_letter import TEMPLATE, annexure, field_texts, indian, offer_letter_filename, render_offer_letter_pdf

# The sample letter HR gave: Kumar V, Service engineer, joining 31.08.2026.
KUMAR = dict(
    ref_no="RTPL/NJ/2026/140", name="Kumar V", designation="Service engineer",
    joining_date=date(2026, 8, 31),
    basic=12104, hra=6052, conveyance=1600, cea=200, personal_allowance=4252,
    professional_tax=208, insurance=207, petrol_allowance=3000,
)


def letter(**changes):
    return OfferLetter(**{**KUMAR, **changes})


def pdf_text(pdf):
    return "\n".join(page.extract_text() for page in PdfReader(io.BytesIO(pdf)).pages)


class AnnexureTests(TestCase):
    def test_the_sample_letter_adds_up_as_it_did(self):
        sums = annexure(letter())
        self.assertEqual(sums, {"gross": 24208, "net": 24000, "benefits": 3207, "ctc": 27415})

    def test_numbers_are_written_as_the_sample_writes_them(self):
        texts = field_texts(letter())
        self.assertEqual(texts["basic_m"], "12104")
        self.assertEqual(texts["basic_y"], "145248")
        self.assertEqual(texts["gross_m"], "24,208")
        self.assertEqual(texts["gross_y"], "2,90,496")
        self.assertEqual(texts["pt_m"], "208.0")
        self.assertEqual(texts["pt_y"], "2496")
        self.assertEqual(texts["net_y"], "2,88,000")
        self.assertEqual(texts["package"], "27,415")
        self.assertEqual(texts["joining_date"], "31.08.2026")
        # Every line's year is twelve of its months, petrol included.
        self.assertEqual(texts["petrol_y"], "36000")
        self.assertEqual(texts["total_c_y"], "38484")
        self.assertEqual(texts["ctc_y"], "3,28,980")

    def test_lakhs(self):
        self.assertEqual(indian(999), "999")
        self.assertEqual(indian(1000), "1,000")
        self.assertEqual(indian(290496), "2,90,496")
        self.assertEqual(indian(10000000), "1,00,00,000")

    def test_the_file_is_named_after_the_person(self):
        self.assertEqual(offer_letter_filename(letter(name="Arun Kumar S.")), "Offer-Letter-Arun-Kumar-S.pdf")


class OfferLetterPdfTests(TestCase):
    def test_the_template_holds_no_text_at_all(self):
        """Not the sample's name, not its salary: deleted, not covered."""
        with open(TEMPLATE, "rb") as fh:
            raw = fh.read()
        self.assertEqual(pdf_text(raw).strip(), "")
        for leak in (b"Kumar", b"27,415", b"24,208", b"RTPL/NJ/2026/140"):
            self.assertNotIn(leak, raw)

    def test_a_letter_carries_its_own_details_and_nobody_else_s(self):
        pdf = render_offer_letter_pdf(letter(
            ref_no="RTPL/NJ/2026/151", name="Priya Dharshini", designation="Senior Service Engineer",
            joining_date=date(2026, 11, 3), basic=15000, hra=7500, personal_allowance=5000,
        ))
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertEqual(len(PdfReader(io.BytesIO(pdf)).pages), 6)
        text = pdf_text(pdf)
        # 15000 + 7500 + 1600 + 200 + 5000 = 29,300 gross; + 207 + 3000 = 32,507 a month.
        for expected in ("RTPL/NJ/2026/151", "Priya Dharshini", "Senior Service Engineer", "03.11.2026",
                         "32,507", "3,51,600", "Responsibilities", "ANNEXURE-I", "Medical"):
            self.assertIn(expected, text)
        for leak in ("Kumar", "27,415", "31.08.2026", "RTPL/NJ/2026/140", "12104"):
            self.assertNotIn(leak, text)

    def test_a_long_role_keeps_its_line_inside_the_margin(self):
        pdf = render_offer_letter_pdf(letter(designation="Regional Service Operations Coordinator"))
        page = PdfReader(io.BytesIO(pdf)).pages[0]
        ends = []

        def visit(text, cm, tm, font, size):
            if "conditions:" in text:
                ends.append(tm[4])

        page.extract_text(visitor_text=visit)
        self.assertTrue(ends)
        self.assertLess(max(ends), 531.0)


class OfferLetterApiTests(TestCase):
    def setUp(self):
        self.hr = User.objects.create_user(username="hr-offers", password="x", role="hr")
        self.engineer = User.objects.create_user(username="eng-offers", password="x", role="employee")
        self.client = APIClient()
        self.client.force_authenticate(self.hr)

    def _body(self, **changes):
        body = {k: (v.isoformat() if isinstance(v, date) else v) for k, v in KUMAR.items()}
        body["ref_no"] = "RTPL/NJ/2026/141"
        body.update(changes)
        return body

    def test_hr_makes_a_letter_and_it_adds_up(self):
        response = self.client.post("/api/offer-letters/", self._body(), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["gross"], 24208)
        self.assertEqual(response.data["net"], 24000)
        self.assertEqual(response.data["ctc"], 27415)
        self.assertEqual(OfferLetter.objects.get().created_by, self.hr)
        self.assertEqual(len(self.client.get("/api/offer-letters/").data), 1)

    def test_the_sums_cannot_be_typed(self):
        response = self.client.post("/api/offer-letters/", self._body(ctc=1, gross=1), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["ctc"], 27415)

    def test_an_engineer_cannot_see_or_make_letters(self):
        engineer = APIClient()
        engineer.force_authenticate(self.engineer)
        self.assertEqual(engineer.get("/api/offer-letters/").status_code, 403)
        self.assertEqual(engineer.post("/api/offer-letters/", self._body(), format="json").status_code, 403)
        self.assertEqual(APIClient().get("/api/offer-letters/").status_code, 401)

    def test_what_would_go_wrong_on_paper_is_refused(self):
        cases = {
            "empty name": self._body(name="   "),
            "name too long": self._body(name="A" * 61),
            "role too long": self._body(designation="R" * 41),
            "a zero too many": self._body(basic=12_10_400_0),
            "no basic": self._body(basic=0),
            "odd reference": self._body(ref_no="RTPL NJ 141"),
        }
        for label, body in cases.items():
            with self.subTest(label):
                self.assertEqual(self.client.post("/api/offer-letters/", body, format="json").status_code, 400)
        self.client.post("/api/offer-letters/", self._body(), format="json")
        self.assertEqual(self.client.post("/api/offer-letters/", self._body(), format="json").status_code, 400)

    def test_names_are_tidied_not_printed_raw(self):
        response = self.client.post("/api/offer-letters/", self._body(name="  Ravi\t   Kumar \n"), format="json")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["name"], "Ravi Kumar")
        # A NUL is refused outright (DRF's own rule), never printed.
        response = self.client.post(
            "/api/offer-letters/", self._body(ref_no="RTPL/NJ/2026/150", name="Ravi\x00"), format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_the_next_reference_follows_the_highest_this_year(self):
        with mock.patch("onboarding.views.timezone.localdate", return_value=date(2026, 10, 19)):
            self.assertEqual(self.client.get("/api/offer-letters/next_ref/").data["ref_no"], "RTPL/NJ/2026/141")
            self.client.post("/api/offer-letters/", self._body(ref_no="RTPL/NJ/2026/157"), format="json")
            self.assertEqual(self.client.get("/api/offer-letters/next_ref/").data["ref_no"], "RTPL/NJ/2026/158")
        with mock.patch("onboarding.views.timezone.localdate", return_value=date(2027, 1, 2)):
            self.assertEqual(self.client.get("/api/offer-letters/next_ref/").data["ref_no"], "RTPL/NJ/2027/1")

    def test_the_pdf_opens_with_a_ticket_and_only_for_its_letter(self):
        first = self.client.post("/api/offer-letters/", self._body(), format="json").data["id"]
        second = self.client.post("/api/offer-letters/", self._body(ref_no="RTPL/NJ/2026/142"), format="json").data["id"]
        ticket = self.client.get(f"/api/offer-letters/{first}/pdf_ticket/").data

        browser = APIClient()  # no JWT, as a plain navigation has none
        response = browser.get(ticket["path"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn('attachment; filename="Offer-Letter-Kumar-V.pdf"', response["Content-Disposition"])
        self.assertTrue(response.content.startswith(b"%PDF"))

        self.assertEqual(browser.get(f"/api/offer-letters/{second}/pdf/?t={ticket['ticket']}").status_code, 403)
        self.assertEqual(browser.get(f"/api/offer-letters/{first}/pdf/?t=forged").status_code, 403)
        with mock.patch("onboarding.views.OfferLetterViewSet.PDF_TICKET_MAX_AGE", -1):
            self.assertEqual(browser.get(ticket["path"]).status_code, 403)

    def test_an_engineer_gets_no_ticket(self):
        letter_id = self.client.post("/api/offer-letters/", self._body(), format="json").data["id"]
        engineer = APIClient()
        engineer.force_authenticate(self.engineer)
        self.assertEqual(engineer.get(f"/api/offer-letters/{letter_id}/pdf_ticket/").status_code, 403)


@override_settings(
    EMAIL_HOST_USER="hr@renderways.in", EMAIL_HOST_PASSWORD="test-only-not-a-real-password",
    CORS_ALLOWED_ORIGINS=["https://payroll.systimus.in"],
)
class OfferLetterMailTests(TestCase):
    """The letter goes out in HR's own offer mail (the one HR sent on 17 June
    2026), with the PDF attached, and is kept in the Sent folder and the Mail
    section's Sent list."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr-offer-mail", password="x", role="hr", first_name="Hema")
        self.client = APIClient()
        self.client.force_authenticate(self.hr)
        self.letter = OfferLetter.objects.create(**{**KUMAR, "name": "Mohamed vaseem M A", "designation": "Software engineer"})
        filed = mock.patch("hrmail.outbound.file_in_sent", return_value=True)
        self.filed = filed.start()
        self.addCleanup(filed.stop)

    def _send(self, **body):
        return self.client.post(
            f"/api/offer-letters/{self.letter.id}/send_mail/",
            {"to": "vaseem@example.com", "cc": "Dinesh <dinesh@renderways.in>", "dear": "Vaseem",
             "origin": "https://payroll.systimus.in", **body},
            format="json",
        )

    def test_the_preview_is_the_mail_that_will_go(self):
        data = self.client.get(f"/api/offer-letters/{self.letter.id}/mail_preview/", {"dear": "Vaseem"}).data
        self.assertEqual(data["subject"], "Offer Letter")
        self.assertEqual(data["attachment"], "Offer-Letter-Mohamed-vaseem-M-A.pdf")
        for words in ("Dear <b", ">Vaseem</b>", ">Software engineer</b>", "Renderways Technology Private Limited",
                      "Please confirm your acceptance of this offer letter by replying to this mail.",
                      "Karthikeyan D", "Human Resources(HR)", "+91 9087019170 | +91 95430 95480",
                      "Chennai 600 095.", "Chip Level Services", "DISCLAIMER:", "may constitute unlawful act."):
            self.assertIn(words, data["html"])

    def test_it_goes_with_the_letter_attached(self):
        response = self._send()
        self.assertEqual(response.status_code, 200, response.data)
        sent = mail.outbox[-1]
        self.assertEqual((sent.subject, sent.from_email), ("Offer Letter", "hr@renderways.in"))
        self.assertEqual((sent.to, sent.cc), (["vaseem@example.com"], ["dinesh@renderways.in"]))
        self.assertIn("Dear Vaseem,", sent.body)
        html = sent.alternatives[0][0]
        self.assertIn(">Vaseem</b>", html)
        name, content, kind = sent.attachments[0]
        self.assertEqual((name, kind), ("Offer-Letter-Mohamed-vaseem-M-A.pdf", "application/pdf"))
        self.assertTrue(content.startswith(b"%PDF"))
        self.filed.assert_called_once()
        record = OutboundMail.objects.get()
        self.assertEqual((record.kind, record.sent_by, record.status), ("OFFER", self.hr, "SENT"))
        # And in the Mail section's Payroll mails, to be opened again with the letter.
        logged = SystemMail.objects.get()
        self.assertEqual((logged.kind, logged.about, logged.cc_text), ("offer_letter", "Mohamed vaseem M A", "dinesh@renderways.in"))
        self.assertIn(">Vaseem</b>", logged.body_html)
        self.assertEqual(logged.attachments.get().filename, "Offer-Letter-Mohamed-vaseem-M-A.pdf")
        # Accept / Decline lead to the candidate's page for this letter.
        self.letter.refresh_from_db()
        token = self.letter.response_token
        self.assertTrue(token)
        self.assertIn(f'href="https://payroll.systimus.in/offer/{token}?answer=accept"', html)
        # HR's mail stays whole; the buttons come after it, below the disclaimer.
        self.assertGreater(html.index("Accept Offer"), html.index("may constitute unlawful act."))
        self.assertGreater(sent.body.index("Accept the offer:"), sent.body.index("may constitute unlawful act."))
        self.assertIn(f"Accept the offer: https://payroll.systimus.in/offer/{token}?answer=accept", sent.body)
        self.assertEqual(self.letter.mailed_to, "vaseem@example.com")
        self.assertIsNotNone(self.letter.mailed_at)
        self.assertEqual(self.letter.mails.get().message_id, record.message_id)

    def test_the_preview_buttons_lead_nowhere(self):
        html = self.client.get(f"/api/offer-letters/{self.letter.id}/mail_preview/").data["html"]
        self.assertIn('href="#accept"', html)
        self.assertGreater(html.index("Accept Offer"), html.index("DISCLAIMER:"))
        self.letter.refresh_from_db()
        self.assertIsNone(self.letter.response_token, "previewing makes no link")

    def test_a_name_is_never_read_as_markup(self):
        data = self.client.get(f"/api/offer-letters/{self.letter.id}/mail_preview/", {"dear": "<script>x</script>"}).data
        self.assertNotIn("<script>", data["html"])
        self.assertIn("&lt;script&gt;", data["html"])

    def test_nothing_goes_without_a_proper_address_and_name(self):
        self.assertEqual(self._send(to="vaseem").status_code, 400)
        self.assertEqual(self._send(dear="  ").status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_only_hr_can_mail_a_letter(self):
        engineer = APIClient()
        engineer.force_authenticate(User.objects.create_user(username="eng-offer-mail", password="x", role="employee"))
        response = engineer.post(f"/api/offer-letters/{self.letter.id}/send_mail/", {"to": "a@b.com", "dear": "A"}, format="json")
        self.assertEqual(response.status_code, 403)
        self.assertEqual(len(mail.outbox), 0)

    @override_settings(EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD="")
    def test_no_mailbox_set_up_is_said(self):
        response = self._send()
        self.assertEqual(response.status_code, 502)
        self.assertIn("not set up", response.data["detail"])


class OfferResponseTests(TestCase):
    """The candidate's answer: from the buttons in the mail, from HR after a
    reply, and the reply itself found against its letter."""

    def setUp(self):
        cache.clear()  # the candidate's page is rate limited in the cache
        self.hr = User.objects.create_user(username="hr-offer-answer", password="x", role="hr", first_name="Hema")
        self.client = APIClient()
        self.client.force_authenticate(self.hr)
        self.letter = OfferLetter.objects.create(**{**KUMAR, "response_token": "tok-kumar"})
        self.public = APIClient()

    def _row(self):
        return next(r for r in self.client.get("/api/offer-letters/").data if r["id"] == self.letter.id)

    def test_opening_the_link_records_nothing(self):
        data = self.public.get("/api/offer/tok-kumar/").data
        self.assertEqual((data["name"], data["designation"], data["response"]), ("Kumar V", "Service engineer", ""))
        self.letter.refresh_from_db()
        self.assertEqual(self.letter.response, "", "mail scanners open links; only the confirm counts")
        self.assertEqual(self.public.get("/api/offer/not-a-token/").status_code, 404)

    def test_the_candidate_accepts_once(self):
        response = self.public.post("/api/offer/tok-kumar/respond/", {"answer": "accept"}, format="json",
                                    REMOTE_ADDR="49.37.1.2")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["response"], "ACCEPTED")
        again = self.public.post("/api/offer/tok-kumar/respond/", {"answer": "decline"}, format="json")
        self.assertEqual(again.status_code, 409)
        self.assertEqual(again.data["response"], "ACCEPTED")
        self.letter.refresh_from_db()
        self.assertEqual((self.letter.response_source, self.letter.response_ip), ("LINK", "49.37.1.2"))
        row = self._row()
        self.assertEqual((row["response"], row["response_by"]), ("ACCEPTED", "Candidate"))
        self.assertEqual(self.public.post("/api/offer/tok-kumar/respond/", {"answer": "maybe"}, format="json").status_code, 400)

    def test_hr_marks_and_clears_the_answer(self):
        data = self.client.post(f"/api/offer-letters/{self.letter.id}/response/", {"response": "DECLINED"}, format="json").data
        self.assertEqual((data["response"], data["response_by"]), ("DECLINED", "Hema"))
        data = self.client.post(f"/api/offer-letters/{self.letter.id}/response/", {"response": ""}, format="json").data
        self.assertEqual((data["response"], data["response_at"]), ("", None))
        engineer = APIClient()
        engineer.force_authenticate(User.objects.create_user(username="eng-answer", password="x", role="employee"))
        self.assertEqual(
            engineer.post(f"/api/offer-letters/{self.letter.id}/response/", {"response": "ACCEPTED"}, format="json").status_code,
            403,
        )
        # Editing the letter cannot set an answer.
        body = {**KUMAR, "joining_date": "2026-08-31", "response": "ACCEPTED"}
        self.assertEqual(self.client.put(f"/api/offer-letters/{self.letter.id}/", body, format="json").status_code, 200)
        self.letter.refresh_from_db()
        self.assertEqual(self.letter.response, "")

    def _reply(self, **fields):
        from django.utils import timezone

        from hrmail.models import InboundMail

        defaults = dict(
            mailbox="hr@renderways.in", uid_validity=1, uid=InboundMail.objects.count() + 1,
            from_email="kumar@example.com", subject="Re: Offer Letter", received_at=timezone.now(),
            preview="I accept the offer. Thank you.",
        )
        return InboundMail.objects.create(**{**defaults, **fields})

    def test_a_reply_is_found_by_its_thread(self):
        OfferLetterMail.objects.create(offer_letter=self.letter, message_id="offer-1@renderways.in", sent_to="kumar@example.com")
        reply = self._reply(from_email="someone-else@example.com", subject="Thanks",
                            in_reply_to="<offer-1@renderways.in>")
        row = self._row()
        self.assertEqual(row["reply"]["id"], reply.id)
        self.assertEqual(row["reply"]["preview"], "I accept the offer. Thank you.")

    def test_a_reply_without_its_thread_is_found_by_sender_and_subject(self):
        OfferLetterMail.objects.create(offer_letter=self.letter, message_id="offer-2@renderways.in", sent_to="kumar@example.com")
        self._reply(subject="Unrelated", from_email="kumar@example.com")
        self.assertIsNone(self._row()["reply"], "a mail that is not a reply to the offer is not one")
        reply = self._reply()
        self.assertEqual(self._row()["reply"]["id"], reply.id)
