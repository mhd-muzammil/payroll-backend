"""Downloading a payslip as a PDF.

The button in the app called window.print() on a hidden iframe, which does
nothing whatsoever inside an Android WebView — so "download panna mudila" was
exactly right, and no amount of client-side PDF generation would have fixed it
either, because Capacitor registers no DownloadListener and a blob download is
dropped in silence. The one thing that does work is a navigation to a URL on a
different host, which Capacitor hands to the system browser.

A navigation cannot carry an Authorization header, so these tests are mostly
about the thing that replaces it: a signed ticket, minted over the authenticated
channel, that names one payslip and expires.
"""
import datetime
from decimal import Decimal

from django.core import signing
from django.test import override_settings
from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee
from payrollpayslip.models import Payslip
from payrollpayslip.views import PayslipViewSet


def _payslip(employee, **extra):
    defaults = dict(
        month=6,
        year=2026,
        total_days=30,
        lop_days=Decimal("2.00"),
        paid_days=Decimal("28.00"),
        gross_basic=Decimal("8354.00"),
        gross_hra=Decimal("4177.00"),
        gross_salary=Decimal("16708.00"),
        earned_basic=Decimal("7797.07"),
        earned_hra=Decimal("3898.53"),
        gross_earnings=Decimal("15594.13"),
        deduction_prof_tax=Decimal("208.00"),
        gross_deductions=Decimal("208.00"),
        net_salary=Decimal("15386.13"),
    )
    defaults.update(extra)
    return Payslip.objects.create(employee=employee, **defaults)


class PayslipPdfTests(APITestCase):
    def setUp(self):
        self.emp_user = User.objects.create_user(
            username="slipper", password="x", role="employee"
        )
        self.employee = Employee.objects.create(
            user=self.emp_user,
            employee_name="Kausarbadshah M",
            email="kausar@example.com",
            emp_code="113",
            role="Staff",
            department="General",
            branch="Chennai",
            salary=Decimal("16708.00"),
            date_of_joining=datetime.date(2024, 4, 1),
        )
        self.slip = _payslip(self.employee)

        self.other_user = User.objects.create_user(
            username="somebody", password="x", role="employee"
        )
        self.other = Employee.objects.create(
            user=self.other_user,
            employee_name="Somebody Else",
            email="else@example.com",
            role="Staff",
            department="General",
            branch="Chennai",
            salary=Decimal("20000.00"),
        )
        self.other_slip = _payslip(self.other, month=5)

    # ------------------------------------------------------------- the ticket

    def test_employee_gets_a_ticket_for_their_own_payslip(self):
        self.client.force_authenticate(self.emp_user)
        response = self.client.get(f"/api/payslips/{self.slip.id}/pdf_ticket/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIn("ticket", response.data)
        self.assertEqual(response.data["path"], f"/api/payslips/{self.slip.id}/pdf/?t={response.data['ticket']}")

    def test_employee_cannot_get_a_ticket_for_somebody_elses_payslip(self):
        """The scoping that hides other people's payslips has to hide this too."""
        self.client.force_authenticate(self.emp_user)
        response = self.client.get(f"/api/payslips/{self.other_slip.id}/pdf_ticket/")
        self.assertEqual(response.status_code, 404)

    def test_a_ticket_needs_a_login(self):
        response = self.client.get(f"/api/payslips/{self.slip.id}/pdf_ticket/")
        self.assertIn(response.status_code, (401, 403))

    # ---------------------------------------------------------------- the PDF

    def _ticket_for(self, slip, user):
        self.client.force_authenticate(user)
        response = self.client.get(f"/api/payslips/{slip.id}/pdf_ticket/")
        self.assertEqual(response.status_code, 200, response.data)
        self.client.force_authenticate(None)
        return response.data["ticket"]

    def test_the_pdf_downloads_with_a_valid_ticket_and_no_login(self):
        """No login: the browser Capacitor hands this to has no JWT."""
        ticket = self._ticket_for(self.slip, self.emp_user)

        response = self.client.get(f"/api/payslips/{self.slip.id}/pdf/", {"t": ticket})

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response["Content-Type"], "application/pdf")
        self.assertIn("attachment;", response["Content-Disposition"])
        self.assertIn("Kausarbadshah-M", response["Content-Disposition"])
        body = b"".join(response.streaming_content) if response.streaming else response.content
        self.assertTrue(body.startswith(b"%PDF-"), body[:20])
        self.assertGreater(len(body), 2000)

    def test_the_pdf_carries_the_payslip_figures(self):
        from pypdf import PdfReader
        import io as _io

        ticket = self._ticket_for(self.slip, self.emp_user)
        response = self.client.get(f"/api/payslips/{self.slip.id}/pdf/", {"t": ticket})
        text = PdfReader(_io.BytesIO(response.content)).pages[0].extract_text()

        self.assertIn("KAUSARBADSHAH M", text)   # uppercased, as on screen
        self.assertIn("113", text)               # employee code
        self.assertIn("June 2026", text)
        self.assertIn("25 May 2026 to 24 Jun 2026", text)
        self.assertIn("15,386.13", text)         # net
        self.assertIn("15,594.13", text)         # gross earnings
        self.assertIn("208.00", text)            # deductions
        self.assertIn("2.00", text)              # LOP days
        self.assertIn("01-04-2024", text)        # DOJ, dd-mm-yyyy

    def test_the_pdf_never_prints_a_missing_glyph_box(self):
        """The first draft used the rupee sign, which is in no bundled font."""
        from pypdf import PdfReader
        import io as _io

        ticket = self._ticket_for(self.slip, self.emp_user)
        response = self.client.get(f"/api/payslips/{self.slip.id}/pdf/", {"t": ticket})
        text = PdfReader(_io.BytesIO(response.content)).pages[0].extract_text()

        self.assertNotIn("■", text)
        self.assertIn("Rs.", text)

    def test_it_is_one_page(self):
        from pypdf import PdfReader
        import io as _io

        ticket = self._ticket_for(self.slip, self.emp_user)
        response = self.client.get(f"/api/payslips/{self.slip.id}/pdf/", {"t": ticket})
        self.assertEqual(len(PdfReader(_io.BytesIO(response.content)).pages), 1)

    def test_petrol_is_shown_but_left_out_of_both_totals(self):
        """A rule the office asked for twice; the PDF must not quietly undo it."""
        from pypdf import PdfReader
        import io as _io

        self.slip.petrol_allowance = Decimal("3000.00")
        self.slip.employer_epf = Decimal("1000.00")
        self.slip.save()

        ticket = self._ticket_for(self.slip, self.emp_user)
        response = self.client.get(f"/api/payslips/{self.slip.id}/pdf/", {"t": ticket})
        text = PdfReader(_io.BytesIO(response.content)).pages[0].extract_text()

        self.assertIn("3,000.00", text)                    # the line is there
        self.assertIn("Rs.16,594.13", text)                # 15,594.13 + 1,000 employer EPF
        self.assertNotIn("19,594.13", text)                # and NOT with petrol added

    # ------------------------------------------------------------- the guards

    def test_a_tampered_ticket_is_refused(self):
        ticket = self._ticket_for(self.slip, self.emp_user)
        response = self.client.get(
            f"/api/payslips/{self.slip.id}/pdf/", {"t": ticket[:-2] + "xy"}
        )
        self.assertEqual(response.status_code, 403)

    def test_no_ticket_is_refused(self):
        response = self.client.get(f"/api/payslips/{self.slip.id}/pdf/")
        self.assertEqual(response.status_code, 403)

    def test_a_ticket_only_opens_the_payslip_it_names(self):
        """Otherwise one valid ticket would fetch every payslip in the table."""
        ticket = self._ticket_for(self.slip, self.emp_user)
        response = self.client.get(f"/api/payslips/{self.other_slip.id}/pdf/", {"t": ticket})
        self.assertEqual(response.status_code, 403)

    def test_an_expired_ticket_is_refused(self):
        ticket = signing.dumps(
            {"payslip": self.slip.id, "user": self.emp_user.id},
            salt=PayslipViewSet.PDF_TICKET_SALT,
        )
        with override_settings():
            # Older than the window, without waiting five minutes for it.
            original = PayslipViewSet.PDF_TICKET_MAX_AGE
            PayslipViewSet.PDF_TICKET_MAX_AGE = -1
            try:
                response = self.client.get(
                    f"/api/payslips/{self.slip.id}/pdf/", {"t": ticket}
                )
            finally:
                PayslipViewSet.PDF_TICKET_MAX_AGE = original

        self.assertEqual(response.status_code, 403)
        self.assertIn("expired", response.data["detail"].lower())
