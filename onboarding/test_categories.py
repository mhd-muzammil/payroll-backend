"""An employee, a freelancer and a vendor are onboarded differently.

All three used to go through the employee's form, so a vendor had to be given
a department and a date of joining and had nowhere to put the name of the firm.
Three of those fields are no longer required by the database -- which is the
part worth testing carefully, because "not required by the database" must not
quietly become "not required of an employee either".
"""
import datetime

from rest_framework.test import APITestCase

from authentication.models import User

from .models import Onboarding

_PHONE = [9500000000]


def _phone():
    _PHONE[0] += 1
    return str(_PHONE[0])


class OnboardingCategoryTests(APITestCase):
    def setUp(self):
        self.hr = User.objects.create_user(username="hr_cat", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        self.url = "/api/onboarding/"

    def _payload(self, **extra):
        body = {
            "employee_name": "Somebody",
            "email_id": "somebody%s@example.com" % _PHONE[0],
            "mobile_number": _phone(),
            "work_location": "Chennai",
        }
        body.update(extra)
        return body

    # ------------------------------------------------------- what is already there

    def test_an_existing_record_is_an_employee(self):
        row = Onboarding.objects.create(
            employee_name="Praveen S", email_id="p@example.com", mobile_number=_phone(),
            department="Service", designation="Engineer", work_location="Chennai",
            date_of_joining=datetime.date(2025, 1, 1),
        )
        self.assertEqual(row.category, "Employee", "nobody becomes a freelancer by accident")

    # --------------------------------------------------------------- employees

    def test_an_employee_saves_without_department_or_joining_date(self):
        """Nothing is compulsory: whatever somebody has, they fill in."""
        response = self.client.post(
            self.url, self._payload(category="Employee", designation="Engineer"), format="multipart"
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_completely_blank_form_still_saves(self):
        response = self.client.post(self.url, {"category": "Employee"}, format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["employee_name"], "")

    def test_a_complete_employee_saves(self):
        response = self.client.post(
            self.url,
            self._payload(
                category="Employee", department="Service", designation="Engineer",
                date_of_joining="2026-01-05",
            ),
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["category"], "Employee")

    def test_the_category_defaults_to_employee_when_the_form_omits_it(self):
        response = self.client.post(
            self.url,
            self._payload(department="Service", designation="Engineer", date_of_joining="2026-01-05"),
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["category"], "Employee")

    # ------------------------------------------------------------- freelancers

    def test_a_freelancer_needs_no_department_or_joining_date(self):
        response = self.client.post(
            self.url,
            self._payload(
                category="Freelancer", skills="printer",
                rate_type="Per case", rate_amount="750.00",
                contract_start="2026-02-01",
            ),
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["category"], "Freelancer")
        self.assertEqual(response.data["rate_type"], "Per case")
        self.assertEqual(response.data["rate_amount"], "750.00")

    # ----------------------------------------------------------------- vendors

    def test_a_vendor_saves_without_the_name_of_the_firm(self):
        response = self.client.post(
            self.url, self._payload(category="Vendor"), format="multipart"
        )
        self.assertEqual(response.status_code, 201, response.data)

    def test_a_vendor_saves_with_the_firm_and_its_gst(self):
        response = self.client.post(
            self.url,
            self._payload(
                category="Vendor", company_name="Sri Balaji Services",
                gst_number="33ABCDE1234F1Z5", service_type="Spares and logistics",
                contact_person_role="Proprietor",
            ),
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["company_name"], "Sri Balaji Services")
        self.assertEqual(response.data["gst_number"], "33ABCDE1234F1Z5")

    # ------------------------------------------------------------- the contract

    def test_a_contract_cannot_end_before_it_starts(self):
        response = self.client.post(
            self.url,
            self._payload(
                category="Freelancer", contract_start="2026-03-01", contract_end="2026-02-01"
            ),
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("contract_end", response.data)

    def test_an_open_ended_contract_is_fine(self):
        response = self.client.post(
            self.url,
            self._payload(category="Freelancer", contract_start="2026-03-01"),
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertIsNone(response.data["contract_end"])

    # ------------------------------------------------------------- editing one

    def test_editing_a_freelancer_does_not_demand_an_employee_s_fields(self):
        created = self.client.post(
            self.url, self._payload(category="Freelancer"), format="multipart"
        )
        self.assertEqual(created.status_code, 201, created.data)
        response = self.client.patch(
            f"{self.url}{created.data['id']}/", {"rate_amount": "900.00"}, format="multipart"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["rate_amount"], "900.00")
        self.assertEqual(response.data["category"], "Freelancer", "and it stays a freelancer")

    def test_editing_an_employee_can_clear_a_field(self):
        created = self.client.post(
            self.url,
            self._payload(
                category="Employee", department="Service", designation="Engineer",
                date_of_joining="2026-01-05",
            ),
            format="multipart",
        )
        self.assertEqual(created.status_code, 201, created.data)
        response = self.client.patch(
            f"{self.url}{created.data['id']}/", {"department": ""}, format="multipart"
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["department"], "")
