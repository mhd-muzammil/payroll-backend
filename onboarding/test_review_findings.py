"""The confirmed findings of the adversarial review, each one reproduced.

Every test here is a scenario a reviewer demonstrated against the code: a
stranger's link form putting their bank account on a working engineer's
payslip; one click of Accept writing a stranger's form over an existing
employee; a nameless record breeding a new employee and login on every save; a
name-only record reopening a relieved namesake's login; a phone photo crashing
the public form; a blank location moving somebody to Chennai; a name injecting
HTML into the payslip email. They are written as attacks, and they must fail.
"""
import datetime
from decimal import Decimal

from django.core import mail
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee
from payrollpayslip.models import Payslip
from payrollpayslip.payslip_pdf import _onboarding_for

from .models import Onboarding, OnboardingInvite

NO_THROTTLE = {"DEFAULT_THROTTLE_RATES": {"onboarding_link": None}}


def _engineer(**extra):
    fields = {
        "employee_name": "Sarwana N", "email": "sarwana@example.com", "phone": "9222222222",
        "department": "Service", "role": "Service engineer", "branch": "Vellore",
        "salary": 30000, "status": "active",
    }
    fields.update(extra)
    return Employee.objects.create(**fields)


def _genuine_onboarding(employee, bank="LEGIT BANK", account="111122223333"):
    """The engineer's own, office-entered, accepted row."""
    return Onboarding.objects.create(
        employee_name=employee.employee_name, email_id=employee.email or "",
        mobile_number=employee.phone or "", employee_id=employee.emp_code or "",
        work_location=employee.branch, department="Service", designation="Engineer",
        date_of_joining=datetime.date(2025, 1, 1),
        bank_name=bank, account_number=account, ifsc_code="LEGT0000001",
    )


@override_settings(REST_FRAMEWORK={**NO_THROTTLE})
class AStrangersBankAccountNeverReachesAnEngineer(APITestCase):
    """CRITICAL: the read side of the link."""

    def setUp(self):
        self.link = f"/api/onboard/{OnboardingInvite.objects.create(category='Employee').token}/"
        self.hr = User.objects.create_user(username="hr_review", password="x", role="hr")

    def _attack(self, **fields):
        body = {
            "employee_name": "Stranger", "bank_name": "ATTACKER BANK",
            "account_number": "999999999999", "ifsc_code": "ATTK0000001",
            "account_holder_name": "Stranger",
        }
        body.update(fields)
        response = self.client.post(self.link, body, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)

    def test_by_email_the_engineer_with_no_onboarding_row_shows_no_stranger(self):
        engineer = _engineer()
        self._attack(email_id="sarwana@example.com")
        self.client.force_authenticate(self.hr)
        data = self.client.get(f"/api/employees/{engineer.id}/").data
        self.assertNotEqual(data.get("account_number"), "999999999999")
        self.assertNotEqual(data.get("bank_name"), "ATTACKER BANK")
        self.assertIsNone(_onboarding_for(engineer), "nor on the payslip PDF")

    def test_by_code_and_branch_an_engineer_with_no_email_shows_no_stranger(self):
        """The attendance import makes exactly these: no email, a small per-branch code."""
        engineer = _engineer(email=None, phone=None, emp_code="3", branch="Salem",
                             employee_name="Imported Engineer")
        self._attack(employee_id="3", work_location="salem")
        self.client.force_authenticate(self.hr)
        data = self.client.get(f"/api/employees/{engineer.id}/").data
        self.assertNotEqual(data.get("account_number"), "999999999999")
        self.assertIsNone(_onboarding_for(engineer))

    def test_the_genuine_row_still_answers(self):
        engineer = _engineer()
        _genuine_onboarding(engineer)
        self._attack(email_id="sarwana@example.com")
        self.client.force_authenticate(self.hr)
        data = self.client.get(f"/api/employees/{engineer.id}/").data
        self.assertEqual(data.get("bank_name"), "LEGIT BANK")
        self.assertEqual(_onboarding_for(engineer).bank_name, "LEGIT BANK")

    def test_a_vendor_row_is_never_an_engineer_s_bank(self):
        engineer = _engineer()
        Onboarding.objects.create(
            employee_name="Vendor", email_id="sarwana@example.com", category="Vendor",
            mobile_number="", work_location="Vellore", bank_name="VENDOR BANK",
        )
        self.assertIsNone(_onboarding_for(engineer))


@override_settings(REST_FRAMEWORK={**NO_THROTTLE})
class AcceptCannotWriteAFormOverSomebody(APITestCase):
    def setUp(self):
        self.link = f"/api/onboard/{OnboardingInvite.objects.create(category='Employee').token}/"
        self.hr = User.objects.create_user(username="hr_accept", password="x", role="hr")
        self.engineer = _engineer()

    def _submit_then_accept(self, **fields):
        body = {"employee_name": "Stranger"}
        body.update(fields)
        self.client.post(self.link, body, format="multipart")
        record = Onboarding.objects.get(source="Self")
        self.client.force_authenticate(self.hr)
        return record, self.client.patch(
            f"/api/onboarding/{record.id}/",
            {"status": "Completed", "employment_status": "Active"},
            format="multipart",
        )

    def test_a_form_carrying_an_engineer_s_email_cannot_be_accepted(self):
        record, response = self._submit_then_accept(email_id="sarwana@example.com")
        self.assertEqual(response.status_code, 400)
        self.assertIn("Sarwana N", str(response.data), "and it says who it would have hit")
        self.engineer.refresh_from_db()
        self.assertEqual(self.engineer.employee_name, "Sarwana N")
        record.refresh_from_db()
        self.assertEqual(record.status, "Pending Review", "and the form stays waiting")

    def test_nor_one_carrying_their_phone(self):
        _record, response = self._submit_then_accept(mobile_number="9222222222")
        self.assertEqual(response.status_code, 400)
        self.engineer.refresh_from_db()
        self.assertEqual(self.engineer.email, "sarwana@example.com")

    def test_nor_their_code_in_their_branch(self):
        _engineer(email=None, phone=None, emp_code="7", branch="Hosur", employee_name="Coded")
        _record, response = self._submit_then_accept(employee_id="7", work_location="Hosur")
        self.assertEqual(response.status_code, 400)

    def test_a_new_person_is_accepted_as_before(self):
        _record, response = self._submit_then_accept(
            email_id="newperson@example.com", mobile_number="9888888888")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(Employee.objects.filter(email="newperson@example.com").exists())


class ANameIsNotAnIdentity(APITestCase):
    def test_a_record_with_no_identity_creates_nobody_however_often_it_is_saved(self):
        before = (Employee.objects.count(), User.objects.count())
        record = Onboarding.objects.create(employee_name="", work_location="Chennai")
        record.bank_name = "SBI"
        record.save()
        record.save()
        self.assertEqual((Employee.objects.count(), User.objects.count()), before)

    def test_a_name_alone_does_not_merge_into_a_namesake_or_reopen_their_login(self):
        gone = _engineer(employee_name="Ravi K", email=None, phone=None, status="relieved")
        login = User.objects.create_user(username="ravi_old", password="x", role="employee")
        login.is_active = False
        login.save()
        gone.user = login
        gone.save()
        Onboarding.objects.create(employee_name="Ravi K", work_location="Vellore")
        gone.refresh_from_db()
        login.refresh_from_db()
        self.assertEqual(gone.status, "relieved", "the relieved namesake stays relieved")
        self.assertFalse(login.is_active, "and their old login stays shut")

    def test_adding_an_identity_later_provisions_them(self):
        record = Onboarding.objects.create(employee_name="Late Details", work_location="Chennai")
        self.assertFalse(Employee.objects.filter(employee_name="Late Details").exists())
        record.email_id = "late@example.com"
        record.save()
        self.assertTrue(Employee.objects.filter(email="late@example.com").exists())


class ABlankLocationMovesNobody(APITestCase):
    def test_an_existing_employee_keeps_their_branch(self):
        engineer = _engineer(branch="Vellore")
        Onboarding.objects.create(
            employee_name="Sarwana N", email_id="sarwana@example.com",
            mobile_number="9222222222", work_location="",
        )
        engineer.refresh_from_db()
        self.assertEqual(engineer.branch, "Vellore")

    def test_a_named_branch_still_moves_them(self):
        engineer = _engineer(branch="Vellore")
        Onboarding.objects.create(
            employee_name="Sarwana N", email_id="sarwana@example.com",
            mobile_number="9222222222", work_location="Hosur",
        )
        engineer.refresh_from_db()
        self.assertEqual(engineer.branch, "Hosur")


@override_settings(REST_FRAMEWORK={**NO_THROTTLE})
class APhonePhotoDoesNotCrashTheLink(APITestCase):
    def test_a_three_megabyte_document_is_accepted(self):
        link = f"/api/onboard/{OnboardingInvite.objects.create(category='Employee').token}/"
        # Over FILE_UPLOAD_MAX_MEMORY_SIZE (2 MB), so Django spools it to a
        # temporary file on disk -- the case that used to be a 500.
        big = SimpleUploadedFile(
            "aadhaar.pdf", b"%PDF-1.4\n" + b"0" * (3 * 1024 * 1024), content_type="application/pdf"
        )
        response = self.client.post(
            link, {"employee_name": "Big File", "doc_aadhaar": big}, format="multipart"
        )
        self.assertEqual(response.status_code, 201, getattr(response, "data", response))
        self.assertTrue(Onboarding.objects.get(employee_name="Big File").doc_aadhaar)


class ThePayslipEmailEscapesTheName(APITestCase):
    def test_a_name_cannot_put_html_into_the_email(self):
        employee = _engineer(employee_name='<a href="https://evil.example">Verify your bank</a>')
        payslip = Payslip.objects.create(
            employee=employee, month=9, year=2026, net_salary=Decimal("1000"),
            earned_basic=Decimal("1000"), gross_earnings=Decimal("1000"),
        )
        hr = User.objects.create_user(username="hr_mail", password="x", role="hr")
        self.client.force_authenticate(hr)
        # The view picks its own backend -- real SMTP when the .env has
        # credentials -- so it is handed the in-memory one here. No mail leaves
        # the machine.
        from unittest.mock import patch
        from django.core.mail.backends.locmem import EmailBackend

        with patch("django.core.mail.get_connection", lambda *a, **k: EmailBackend()):
            response = self.client.post(f"/api/payslips/{payslip.id}/email_payslip/")
        self.assertIn(response.status_code, (200, 201, 202), getattr(response, "data", response))
        html = mail.outbox[-1].alternatives[0][0]
        self.assertNotIn('<a href="https://evil.example">', html)
        self.assertIn("&lt;a href=", html)
