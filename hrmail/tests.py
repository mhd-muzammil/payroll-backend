"""The Mail section: the HR mailbox read into Payroll, answered from it, and
the record of what Payroll sends by itself.

The mail server is a fake (FakeIMAP below) holding real RFC 822 messages, so
every test runs the real parsing, storing and filing code; only the socket is
missing. SMTP is Django's test outbox.
"""
import imaplib
import shutil
import tempfile
from datetime import timedelta
from email.message import EmailMessage as RawMessage
from email.utils import format_datetime
from unittest import mock

from django.core import mail
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User

from . import imapbox
from .mailparse import check_compose, is_auto_generated, is_no_reply_sender, parse_message, parse_recipients, sanitize_html
from .models import InboundMail, MailboxState, OutboundMail, SystemMail
from .outbound import record_system_mail

BOX = "hr@renderways.in"
TEST_PASSWORD = "test-only-not-a-real-password"


def raw_mail(sender="Divya P <divya@example.com>", subject="Resume for Service Engineer", text="Please find my resume.",
             html=None, when=None, attachment=None, inline_logo=False, headers=None, message_id=None):
    message = RawMessage()
    message["From"] = sender
    message["To"] = BOX
    message["Subject"] = subject
    message["Date"] = format_datetime(when or timezone.now())
    message["Message-ID"] = message_id or f"<{abs(hash((sender, subject, text)))}@example.com>"
    for name, value in (headers or {}).items():
        message[name] = value
    message.set_content(text)
    if html:
        message.add_alternative(html, subtype="html")
        if inline_logo:
            message.get_payload()[1].add_related(b"\x89PNG\r\n\x1a\nlogo", maintype="image", subtype="png", cid="<logo1>")
    if attachment:
        name, data = attachment
        message.add_attachment(data, maintype="application", subtype="pdf", filename=name)
    return message.as_bytes()


class FakeIMAP:
    """Just enough of a Dovecot server for the HR mailbox."""

    messages = {}  # uid -> raw bytes
    validity = 7
    appended = []
    fail_login = False

    def __init__(self, host, port, ssl_context=None, timeout=None):
        self.host, self.port = host, port
        self._selected = False

    def login(self, user, password):
        if FakeIMAP.fail_login or user != BOX or password != TEST_PASSWORD:
            raise imaplib.IMAP4.error(b"[AUTHENTICATIONFAILED] Authentication failed.")
        return "OK", [b"Logged in"]

    def select(self, mailbox="INBOX", readonly=False):
        assert readonly, "the inbox is only ever opened read-only"
        self._selected = True
        return "OK", [str(len(FakeIMAP.messages)).encode()]

    def response(self, code):
        return code, [str(FakeIMAP.validity).encode()] if code == "UIDVALIDITY" else [None]

    def uid(self, command, *args):
        if command == "SEARCH":
            return "OK", [" ".join(str(uid) for uid in sorted(FakeIMAP.messages)).encode()]
        if command == "FETCH":
            uid = int(args[0])
            assert "PEEK" in args[1], "bodies are peeked, never marked read"
            raw = FakeIMAP.messages[uid]
            head = f'{uid} (UID {uid} INTERNALDATE "25-Oct-2026 10:00:00 +0530" BODY[] {{{len(raw)}}}'.encode()
            return "OK", [(head, raw), b")"]
        raise AssertionError(command)

    def list(self):
        return "OK", [b'(\\HasNoChildren) "." INBOX', b'(\\HasNoChildren \\Sent) "." "INBOX.Sent"']

    def append(self, mailbox, flags, date_time, message):
        FakeIMAP.appended.append((mailbox, message))
        return "OK", [b"APPEND completed"]

    def logout(self):
        return "BYE", [b""]


class _Mailbox(TestCase):
    def setUp(self):
        self.media = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.media, ignore_errors=True)
        overrides = override_settings(
            MEDIA_ROOT=self.media, EMAIL_HOST_USER=BOX, EMAIL_HOST_PASSWORD=TEST_PASSWORD,
        )
        overrides.enable()
        self.addCleanup(overrides.disable)
        patcher = mock.patch("hrmail.imapbox.imaplib.IMAP4_SSL", FakeIMAP)
        patcher.start()
        self.addCleanup(patcher.stop)
        FakeIMAP.messages, FakeIMAP.appended, FakeIMAP.fail_login, FakeIMAP.validity = {}, [], False, 7
        self.hr = User.objects.create_user(username="hr-mail", password="x", role="hr", first_name="Hema")
        self.client = APIClient()
        self.client.force_authenticate(self.hr)

    def deliver(self, *raws):
        start = max(FakeIMAP.messages, default=0)
        for i, raw in enumerate(raws, 1):
            FakeIMAP.messages[start + i] = raw

    def sync(self):
        response = self.client.post("/api/hr-mail/sync/", {"force": True}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return response.data


class SanitizerTests(TestCase):
    def test_what_runs_is_removed_and_the_look_is_kept(self):
        dirty = (
            '<div style="color:red" onclick="steal()">Hi<script>alert(1)</script>'
            '<a href="javascript:alert(1)">x</a><img src="data:image/png;base64,AA" onerror="x()">'
            '<a href="data:text/html;base64,PHNjcmlwdD4=">y</a><img src="data:image/svg+xml;base64,PHN2Zz4=">'
            '<iframe src="https://evil"></iframe><form action="/x"><input name="p"></form>'
            '<table><tr><td style="width:expression(alert(1))">cell</td></tr></table><!-- </div> --></div>'
        )
        clean = sanitize_html(dirty)
        for gone in ("<script", "onclick", "onerror", "javascript:", "data:text/html", "svg+xml", "<iframe",
                     "<form", "<input", "expression(", "<!--"):
            self.assertNotIn(gone, clean)
        # A picture written into the mail stays, so the mail looks as it was sent.
        for kept in ('style="color:red"', "<table>", "cell", ">x</a>", 'src="data:image/png;base64,AA"'):
            self.assertIn(kept, clean)

    def test_a_preview_is_what_the_reader_sees(self):
        raw = raw_mail(
            text='<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0"><html><body>Dear Karthikeyan</body></html>',
            html="<html><head><style>body{margin:0}</style></head><body><p>Dear Karthikeyan D,</p>"
                 "<p>Here is a list of today's birthdays.</p></body></html>",
        )
        parsed = parse_message(raw)
        self.assertTrue(parsed["preview"].startswith("Dear Karthikeyan D, Here is a list"), parsed["preview"])
        self.assertNotIn("<", parsed["body_text"])
        css_text = parse_message(raw_mail(
            text="Candidate Chat @media only screen and (max-width:600px) { .a { width:100%; } } body { margin:0; }",
            html="<p>Candidates are calling you</p>",
        ))
        self.assertEqual(css_text["preview"], "Candidates are calling you")

    def test_machine_mail_cannot_be_answered(self):
        self.assertTrue(is_auto_generated({"auto-submitted": "auto-replied"}))
        self.assertTrue(is_auto_generated({"list-unsubscribe": "<mailto:x>"}))
        self.assertFalse(is_auto_generated({"auto-submitted": "no"}))
        self.assertTrue(is_no_reply_sender("no-reply@naukri.com"))
        self.assertTrue(is_no_reply_sender("MAILER-DAEMON@renderways.in"))
        self.assertFalse(is_no_reply_sender("divya@example.com"))

    def test_addresses_are_read_as_people_type_them(self):
        self.assertEqual(
            parse_recipients('a@x.com; "Sharma, Bhupesh" <B.Sharma@hp.com>, Sharma, Bhupesh <b.sharma@hp.com>\nc@y.in'),
            ["a@x.com", "b.sharma@hp.com", "c@y.in"],
        )
        self.assertIn("valid", check_compose("not-an-address", "", "Hi", "Body")[0])
        self.assertIn("To", check_compose("", "", "Hi", "Body")[0])
        self.assertIn("subject", check_compose("a@x.com", "", " ", "Body")[0])
        self.assertIn("20 MB", check_compose("a@x.com", "", "Hi", "Body", 21 * 1024 * 1024)[0])
        self.assertIsNone(check_compose("a@x.com", "b@y.com", "Hi", "Body")[0])

    def test_a_mail_is_read_with_its_parts(self):
        parsed = parse_message(raw_mail(
            html='<p>Hello <img src="cid:logo1"></p><script>x()</script>', inline_logo=True,
            attachment=("resume.pdf", b"%PDF-1.4 resume"),
        ))
        self.assertEqual(parsed["from_email"], "divya@example.com")
        self.assertEqual(parsed["from_name"], "Divya P")
        self.assertIn("Please find my resume.", parsed["body_text"])
        self.assertNotIn("<script", parsed["body_html"])
        files = {a["filename"]: a for a in parsed["attachments"]}
        self.assertEqual(files["resume.pdf"]["data"], b"%PDF-1.4 resume")
        self.assertFalse(files["resume.pdf"]["is_inline"])
        self.assertTrue([a for a in parsed["attachments"] if a["is_inline"] and a["content_id"] == "logo1"])


class SyncTests(_Mailbox):
    def test_new_mail_is_copied_in_once(self):
        self.deliver(raw_mail(attachment=("resume.pdf", b"%PDF resume")), raw_mail(sender="arun@example.com", subject="Leave"))
        result = self.sync()
        self.assertEqual((result["stored"], result["error"]), (2, ""))
        self.assertEqual(result["counts"]["NEW"], 2)
        self.assertEqual(self.sync()["stored"], 0, "a second sync stores nothing again")
        self.assertEqual(InboundMail.objects.count(), 2)

    def test_mail_from_before_the_section_existed_is_not_copied(self):
        self.deliver(raw_mail(subject="old", when=timezone.now() - timedelta(days=40)), raw_mail(subject="new"))
        FakeIMAP.messages = {1: FakeIMAP.messages[1], 2: FakeIMAP.messages[2]}
        # The fake reports its own arrival date; make the old one old there too.
        with mock.patch("hrmail.imapbox._fetched_message", side_effect=lambda data: (
            data[0][1], timezone.now() - timedelta(days=40) if b"old" in data[0][1] else timezone.now()
        )):
            self.sync()
        self.assertEqual(list(InboundMail.objects.values_list("subject", flat=True)), ["new"])

    def test_a_backlog_is_taken_in_bites_newest_first(self):
        self.deliver(*[raw_mail(subject=f"mail {i}", text=f"text {i}") for i in range(imapbox.MAX_PER_SYNC + 5)])
        first = self.sync()
        self.assertEqual((first["stored"], first["pending"]), (imapbox.MAX_PER_SYNC, 5))
        # Today's mail is what HR is waiting for: the newest came in the first bite.
        stored = set(InboundMail.objects.values_list("subject", flat=True))
        self.assertIn(f"mail {imapbox.MAX_PER_SYNC + 4}", stored)
        self.assertNotIn("mail 0", stored)
        self.assertEqual(self.sync()["stored"], 5)

    def test_a_wrong_password_is_said_in_words(self):
        FakeIMAP.fail_login = True
        self.deliver(raw_mail())
        result = self.sync()
        self.assertEqual(result["stored"], 0)
        self.assertIn("refused the HR mailbox's password", result["error"])
        self.assertIn("refused", MailboxState.objects.get().last_error)

    @override_settings(EMAIL_HOST_USER="", EMAIL_HOST_PASSWORD="")
    def test_no_mailbox_set_up_is_said_in_words(self):
        result = self.sync()
        self.assertIn("EMAIL_HOST_USER and EMAIL_HOST_PASSWORD", result["error"])
        self.assertFalse(result["set_up"])


class ReadingTests(_Mailbox):
    def setUp(self):
        super().setUp()
        self.deliver(raw_mail(
            html='<p>Hello <img src="cid:logo1"></p>', inline_logo=True, attachment=("resume.pdf", b"%PDF resume"),
        ))
        self.sync()
        self.mail = InboundMail.objects.get()

    def test_only_hr_staff_see_the_mailbox(self):
        employee = User.objects.create_user(username="emp-mail", password="x", role="employee")
        other = APIClient()
        other.force_authenticate(employee)
        for url in ("/api/hr-mail/", "/api/hr-mail/messages/", f"/api/hr-mail/messages/{self.mail.id}/",
                    "/api/hr-mail/sent/", "/api/hr-mail/system/"):
            self.assertEqual(other.get(url).status_code, 403, url)
        self.assertEqual(APIClient().get("/api/hr-mail/messages/").status_code, 401)

    def test_the_list_and_the_mail(self):
        rows = self.client.get("/api/hr-mail/messages/", {"status": "NEW"}).data["rows"]
        self.assertEqual(rows[0]["from_name"], "Divya P")
        self.assertEqual(rows[0]["attachments"], 1, "the inline logo is not counted as a file")
        detail = self.client.get(f"/api/hr-mail/messages/{self.mail.id}/").data
        logo = next(a for a in detail["attachments"] if a["is_inline"])
        self.assertIn(f"/api/hr-mail/messages/{self.mail.id}/attachments/{logo['id']}/", detail["body_html"])
        self.assertTrue(detail["can_reply"])

    def test_an_attachment_downloads(self):
        resume = self.mail.attachments.get(filename="resume.pdf")
        response = self.client.get(f"/api/hr-mail/messages/{self.mail.id}/attachments/{resume.id}/")
        self.assertEqual(b"".join(response.streaming_content), b"%PDF resume")
        self.assertIn("attachment", response["Content-Disposition"])
        ticket = self.client.get(f"/api/hr-mail/messages/{self.mail.id}/attachments/{resume.id}/ticket/").data
        response = APIClient().get(ticket["path"])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(APIClient().get("/api/hr-mail/attachment/?t=forged").status_code, 403)

    def test_the_newest_mail_is_on_top(self):
        self.deliver(
            raw_mail(subject="older", text="a", when=timezone.now() - timedelta(hours=5)),
            raw_mail(subject="newest", text="b", when=timezone.now() - timedelta(minutes=5)),
            raw_mail(subject="middle", text="c", when=timezone.now() - timedelta(hours=2)),
        )
        self.sync()
        rows = self.client.get("/api/hr-mail/messages/", {"status": "ALL"}).data["rows"]
        received = [r["received_at"] for r in rows]
        self.assertEqual(received, sorted(received, reverse=True))
        ours = [r["subject"] for r in rows if r["subject"] in ("older", "newest", "middle")]
        self.assertEqual(ours, ["newest", "middle", "older"])

    def test_old_previews_are_made_readable(self):
        from importlib import import_module

        from django.apps import apps

        InboundMail.objects.filter(pk=self.mail.pk).update(
            preview='<!DOCTYPE html PUBLIC "-//W3C//DTD XHTML 1.0', body_text="<!DOCTYPE html><html>x</html>",
            body_html="<html><head><title>t</title></head><body><p>Dear HR,</p><p>Resume attached.</p></body></html>",
        )
        import_module("hrmail.migrations.0002_readable_previews").readable_previews(apps, None)
        self.mail.refresh_from_db()
        self.assertEqual(self.mail.preview, "Dear HR, Resume attached.")
        self.assertNotIn("<", self.mail.body_text)

    def test_marking(self):
        response = self.client.post(f"/api/hr-mail/messages/{self.mail.id}/status/", {"status": "IGNORED"}, format="json")
        self.assertEqual(response.data["status"], "IGNORED")
        self.assertEqual(self.client.get("/api/hr-mail/").data["counts"], {"NEW": 0, "REVIEWED": 0, "IGNORED": 1, "ALL": 1})
        self.assertEqual(
            self.client.post(f"/api/hr-mail/messages/{self.mail.id}/status/", {"status": "GONE"}, format="json").status_code,
            400,
        )


class SendingTests(_Mailbox):
    def test_a_reply_goes_to_the_sender_in_the_same_thread(self):
        self.deliver(raw_mail(message_id="<thread-1@example.com>"))
        self.sync()
        inbound = InboundMail.objects.get()
        response = self.client.post(f"/api/hr-mail/messages/{inbound.id}/reply/", {"body": "Please come on Monday."},
                                    format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["filed_in_sent"])
        sent = mail.outbox[-1]
        self.assertEqual(sent.to, ["divya@example.com"])
        self.assertEqual(sent.from_email, BOX)
        self.assertEqual(sent.subject, "Re: Resume for Service Engineer")
        self.assertEqual(sent.extra_headers["In-Reply-To"], "<thread-1@example.com>")
        self.assertIn("Please come on Monday.", sent.body)
        self.assertIn("> Please find my resume.", sent.body)
        self.assertEqual(FakeIMAP.appended[0][0], '"INBOX.Sent"')
        inbound.refresh_from_db()
        self.assertEqual(inbound.status, "REVIEWED")
        record = OutboundMail.objects.get()
        self.assertEqual((record.kind, record.status, record.sent_by), ("REPLY", "SENT", self.hr))

    def test_machine_mail_is_never_answered(self):
        self.deliver(raw_mail(sender="noreply@naukri.com"), raw_mail(sender="x@y.com", headers={"Auto-Submitted": "auto-replied"}))
        self.sync()
        for inbound in InboundMail.objects.all():
            self.assertTrue(inbound.is_auto)
            response = self.client.post(f"/api/hr-mail/messages/{inbound.id}/reply/", {"body": "hi"}, format="json")
            self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_new_mail_with_a_file(self):
        from django.core.files.uploadedfile import SimpleUploadedFile

        response = self.client.post("/api/hr-mail/compose/", {
            "to": "Divya P <divya@example.com>", "cc": "", "subject": "Offer letter",
            "body": "Attached.", "files": [SimpleUploadedFile("offer.pdf", b"%PDF offer", "application/pdf")],
        }, format="multipart")
        self.assertEqual(response.status_code, 200, response.data)
        sent = mail.outbox[-1]
        self.assertEqual((sent.to, sent.subject), (["divya@example.com"], "Offer letter"))
        self.assertEqual(sent.attachments[0][0], "offer.pdf")
        rows = self.client.get("/api/hr-mail/sent/").data
        self.assertEqual((rows[0]["kind"], rows[0]["attachment_names"], rows[0]["sent_by"]), ("COMPOSE", ["offer.pdf"], "Hema"))

    def test_a_bad_address_sends_nothing(self):
        response = self.client.post("/api/hr-mail/compose/", {"to": "divya", "subject": "x", "body": "y"}, format="multipart")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(len(mail.outbox), 0)

    def test_a_refused_send_is_recorded_as_failed(self):
        with mock.patch("hrmail.outbound.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            response = self.client.post("/api/hr-mail/compose/", {"to": "a@b.com", "subject": "x", "body": "y"},
                                        format="multipart")
        self.assertEqual(response.status_code, 502)
        self.assertEqual(OutboundMail.objects.get().status, "FAILED")
        self.assertEqual(FakeIMAP.appended, [], "nothing is filed in Sent for a mail that did not go")


class PayrollMailTests(_Mailbox):
    def test_what_payroll_sends_is_listed_without_secrets(self):
        record_system_mail("appointment_code", ["tharun@example.com"], "Your code to sign the Appointment Order", "Tharun R")
        record_system_mail("payslip", ["arun@example.com"], "Salary Payslip", "Arun", error="smtp down")
        rows = self.client.get("/api/hr-mail/system/").data
        self.assertEqual([(r["kind_label"], r["status"]) for r in rows], [
            ("Payslip", "FAILED"), ("Appointment order signing code", "SENT"),
        ])

    def test_an_appointment_order_logs_its_mails(self):
        from onboarding.models import AppointmentOrder
        from onboarding.appointment_order.signing import send_code

        order = AppointmentOrder.objects.create(
            name="Tharun R", address="Chennai", designation="Service Engineer",
            order_date=timezone.localdate(), joining_date=timezone.localdate(),
            basic=8104, hra=4052, personal_allowance=2252, email="tharun@example.com", sign_token="t",
        )
        send_code(order)
        logged = SystemMail.objects.get()
        code = mail.outbox[-1].subject.split(" ")[0]
        self.assertEqual(logged.kind, "appointment_code")
        self.assertRegex(code, r"^\d{6}$")
        # The mail can be opened again, but the code itself is never written down.
        self.assertNotIn(code, logged.subject)
        self.assertNotIn(code, logged.body_text)
        self.assertIn("•" * 6, logged.body_text)
        self.assertIn("Dear Tharun R", logged.body_text)

    def test_a_payroll_mail_opens_again_with_its_file(self):
        record_system_mail(
            "offer_letter", ["vaseem@example.com"], "Offer Letter", "Mohamed vaseem M A",
            body="Dear Vaseem,", html="<p>Dear <b>Vaseem</b>,</p>", cc=["dinesh@renderways.in"],
            attachments=[("Offer-Letter-Vaseem.pdf", b"%PDF offer", "application/pdf")],
        )
        row = self.client.get("/api/hr-mail/system/").data[0]
        self.assertEqual((row["kind_label"], row["cc_text"]), ("Offer letter", "dinesh@renderways.in"))
        detail = self.client.get(f"/api/hr-mail/system/{row['id']}/").data
        self.assertEqual((detail["from"], detail["body_html"]), (BOX, "<p>Dear <b>Vaseem</b>,</p>"))
        attachment = detail["attachments"][0]
        self.assertEqual(attachment["filename"], "Offer-Letter-Vaseem.pdf")
        ticket = self.client.get(f"/api/hr-mail/system/{row['id']}/attachments/{attachment['id']}/ticket/").data
        response = APIClient().get(ticket["path"])
        self.assertEqual(b"".join(response.streaming_content), b"%PDF offer")
        self.assertEqual(APIClient().get("/api/hr-mail/system-attachment/?t=forged").status_code, 403)
        employee = APIClient()
        employee.force_authenticate(User.objects.create_user(username="emp-sys", password="x", role="employee"))
        self.assertEqual(employee.get(f"/api/hr-mail/system/{row['id']}/").status_code, 403)

    def test_a_payslip_email_is_logged(self):
        from decimal import Decimal

        from employees.models import Employee
        from payrollpayslip.models import Payslip
        from payrollpayslip.views import compute_payslip_fields

        employee = Employee.objects.create(
            employee_name="Arun K", email="arun@example.com", role="Engineer", department="Service",
            branch="Chennai", salary=Decimal("31000"), status="active",
        )
        slip = Payslip.objects.create(employee=employee, month=8, year=2026, **compute_payslip_fields(employee, 31, 0))
        admin = User.objects.create_superuser(username="boss-mail", password="x", email="b@x.in")
        self.client.force_authenticate(admin)
        with mock.patch("django.core.mail.EmailMultiAlternatives.send", side_effect=OSError("smtp down")):
            response = self.client.post(f"/api/payslips/{slip.id}/email_payslip/")
        self.assertEqual(response.status_code, 500)
        logged = SystemMail.objects.get()
        self.assertEqual((logged.kind, logged.status, logged.about), ("payslip", "FAILED", "Arun K"))
