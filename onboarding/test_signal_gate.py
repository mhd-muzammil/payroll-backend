"""Who an onboarding record is allowed to turn into, and when.

Saving an onboarding record runs a signal that matches it against existing
employees by email, code, phone and name, and writes the form over whoever it
finds -- and creates an Employee row and a login when it finds nobody. Right
for a record the office typed. These tests pin the two cases where it must do
nothing at all:

  * a freelancer or a vendor, who are not staff;
  * a form that came in through the public link and has not been accepted --
    because the alternative is that anybody holding the link can rename and
    deactivate a working engineer by typing their email.

And the one where it must still run: the office accepting the form.
"""
import datetime

from django.test import override_settings
from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee

from .models import Onboarding, OnboardingInvite

NO_THROTTLE = {"DEFAULT_THROTTLE_RATES": {"onboarding_link": None}}

_PHONE = [9300000000]


def _phone():
    _PHONE[0] += 1
    return str(_PHONE[0])


def _onboarding(**fields):
    base = {
        "employee_name": "Somebody",
        "email_id": "somebody%s@example.com" % _PHONE[0],
        "mobile_number": _phone(),
        "work_location": "Chennai",
        "department": "Service",
        "designation": "Engineer",
        "date_of_joining": datetime.date(2026, 1, 5),
    }
    base.update(fields)
    return Onboarding.objects.create(**base)


class NotEveryOnboardingIsAnEmployee(APITestCase):
    def test_a_freelancer_gets_no_employee_row_and_no_login(self):
        _onboarding(category="Freelancer", employee_name="Free Lance", email_id="free@example.com")
        self.assertFalse(Employee.objects.filter(email="free@example.com").exists())
        self.assertFalse(User.objects.filter(email="free@example.com").exists())

    def test_a_vendor_gets_no_employee_row_and_no_login(self):
        _onboarding(category="Vendor", company_name="Balaji", email_id="vendor@example.com")
        self.assertFalse(Employee.objects.filter(email="vendor@example.com").exists())
        self.assertFalse(User.objects.filter(email="vendor@example.com").exists())

    def test_an_employee_still_does(self):
        """The office's own path, which must be exactly as it was."""
        _onboarding(category="Employee", employee_name="New Hire", email_id="hire@example.com")
        employee = Employee.objects.get(email="hire@example.com")
        self.assertEqual(employee.employee_name, "New Hire")
        self.assertIsNotNone(employee.user, "and they get their login, as before")

    def test_a_freelancer_does_not_touch_an_employee_who_shares_the_email(self):
        existing = Employee.objects.create(
            employee_name="Praveen S", email="shared@example.com", phone="9111111111",
            department="Service", role="Engineer", branch="Chennai", salary=27208,
        )
        _onboarding(category="Freelancer", employee_name="Somebody Else", email_id="shared@example.com")
        existing.refresh_from_db()
        self.assertEqual(existing.employee_name, "Praveen S")
        self.assertEqual(existing.phone, "9111111111")


@override_settings(REST_FRAMEWORK={**NO_THROTTLE})
class TheLinkCannotTouchAnybody(APITestCase):
    """The public link is unauthenticated. What it sends must not reach staff."""

    def setUp(self):
        self.invite = OnboardingInvite.objects.create(category="Employee")
        self.url = f"/api/onboard/{self.invite.token}/"
        self.engineer = Employee.objects.create(
            employee_name="Sarwana N", email="sarwana@example.com", phone="9222222222",
            department="Service", role="Service engineer", branch="Vellore",
            salary=30000, status="active",
        )

    def _post(self, **fields):
        body = {
            "employee_name": "Stranger",
            "email_id": "stranger@example.com",
            "mobile_number": "9333333333",
            "work_location": "Chennai",
        }
        body.update(fields)
        return self.client.post(self.url, body, format="multipart")

    def test_typing_an_engineer_s_email_changes_nothing_about_them(self):
        response = self._post(employee_name="Not Sarwana", email_id="sarwana@example.com",
                              mobile_number="9444444444")
        self.assertEqual(response.status_code, 201, response.data)
        self.engineer.refresh_from_db()
        self.assertEqual(self.engineer.employee_name, "Sarwana N")
        self.assertEqual(self.engineer.phone, "9222222222")
        self.assertEqual(self.engineer.status, "active", "and they are not switched off")
        self.assertEqual(self.engineer.branch, "Vellore")

    def test_typing_an_engineer_s_phone_or_name_changes_nothing_either(self):
        self._post(employee_name="Sarwana N", email_id="other@example.com", mobile_number="9222222222")
        self.engineer.refresh_from_db()
        self.assertEqual(self.engineer.email, "sarwana@example.com")
        self.assertEqual(self.engineer.status, "active")

    def test_a_submission_creates_no_employee_and_no_login(self):
        before_employees = Employee.objects.count()
        before_users = User.objects.count()
        self._post()
        self.assertEqual(Employee.objects.count(), before_employees)
        self.assertEqual(User.objects.count(), before_users)

    def test_accepting_it_is_what_makes_them_an_employee(self):
        self._post(employee_name="Joiner", email_id="joiner@example.com", mobile_number="9555555555")
        record = Onboarding.objects.get(email_id="joiner@example.com")
        self.assertFalse(Employee.objects.filter(email="joiner@example.com").exists())

        hr = User.objects.create_user(username="hr_gate", password="x", role="hr")
        self.client.force_authenticate(hr)
        response = self.client.patch(
            f"/api/onboarding/{record.id}/",
            {"status": "Completed", "employment_status": "Active"},
            format="multipart",
        )
        self.assertEqual(response.status_code, 200, response.data)

        employee = Employee.objects.get(email="joiner@example.com")
        self.assertEqual(employee.employee_name, "Joiner")
        self.assertEqual(employee.status, "active")
        self.assertIsNotNone(employee.user)

    def test_editing_a_pending_form_still_provisions_nothing(self):
        self._post(employee_name="Joiner", email_id="joiner2@example.com", mobile_number="9666666666")
        record = Onboarding.objects.get(email_id="joiner2@example.com")
        hr = User.objects.create_user(username="hr_gate2", password="x", role="hr")
        self.client.force_authenticate(hr)
        self.client.patch(f"/api/onboarding/{record.id}/", {"bank_name": "SBI"}, format="multipart")
        self.assertFalse(Employee.objects.filter(email="joiner2@example.com").exists())
