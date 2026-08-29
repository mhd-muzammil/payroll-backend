"""Someone marked Relieved in onboarding leaves the working screens.

The promise these tests protect has two halves, and the second matters more.

They go: off the Employees list, off the Users list, off Attendance, off
Payslips and Payroll, and their login stops working. A person who has left the
company should not be sitting in the middle of today's screens.

Nothing of theirs goes with them. Every payslip they were ever paid and every
attendance row they ever punched is still in the database afterwards — past
payroll was calculated from those rows and must stay auditable — and setting
them back to Active in onboarding brings the whole lot back into view.
"""
import datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APITestCase

from attendance.models import Attendance, LeaveRequest
from authentication.models import User
from employees.models import Employee
from payrollpayslip.models import Payslip

from .models import Onboarding


_PHONE = [9000000000]


def _next_phone():
    _PHONE[0] += 1
    return str(_PHONE[0])


def make_onboarding(name, email, status="Active", **extra):
    fields = {
        "employee_name": name,
        "email_id": email,
        "employee_id": f"EMP{abs(hash(email)) % 100000}",
        "mobile_number": _next_phone(),
        "department": "Service",
        "designation": "Engineer",
        "work_location": "Chennai",
        "date_of_joining": datetime.date(2025, 1, 1),
        "employment_status": status,
    }
    fields.update(extra)
    return Onboarding.objects.create(**fields)


class StatusMappingTests(TestCase):
    """Relieved used to be flattened into 'inactive', which left no way to tell
    someone who had left from someone temporarily off the roster."""

    def test_active_stays_active(self):
        make_onboarding("Stays", "stays@example.com", "Active")
        self.assertEqual(Employee.objects.get(email="stays@example.com").status, "active")

    def test_inactive_is_inactive(self):
        make_onboarding("Paused", "paused@example.com", "Inactive")
        self.assertEqual(Employee.objects.get(email="paused@example.com").status, "inactive")

    def test_relieved_is_its_own_state(self):
        make_onboarding("Gone", "gone@example.com", "Relieved")
        self.assertEqual(Employee.objects.get(email="gone@example.com").status, "relieved")

    def test_relieving_someone_already_here_updates_them(self):
        onboarding = make_onboarding("Later", "later@example.com", "Active")
        employee = Employee.objects.get(email="later@example.com")
        self.assertEqual(employee.status, "active")

        onboarding.employment_status = "Relieved"
        onboarding.save()

        employee.refresh_from_db()
        self.assertEqual(employee.status, "relieved")


class LoginAccessTests(TestCase):
    def setUp(self):
        self.onboarding = make_onboarding("Leaver", "leaver@example.com", "Active")
        self.employee = Employee.objects.get(email="leaver@example.com")
        self.user = self.employee.user
        self.assertIsNotNone(self.user, "onboarding should have provisioned a login")

    def test_relieving_closes_the_login(self):
        self.assertTrue(self.user.is_active)
        self.onboarding.employment_status = "Relieved"
        self.onboarding.save()
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

    def test_putting_them_back_reopens_it(self):
        """An accidental relief is undone in onboarding, not by an admin going
        hunting through the user list."""
        self.onboarding.employment_status = "Relieved"
        self.onboarding.save()
        self.user.refresh_from_db()
        self.assertFalse(self.user.is_active)

        self.onboarding.employment_status = "Active"
        self.onboarding.save()
        self.user.refresh_from_db()
        self.assertTrue(self.user.is_active)

    def test_an_employee_with_no_login_is_no_trouble(self):
        make_onboarding("Loginless", "loginless@example.com", "Relieved")
        self.assertEqual(Employee.objects.get(email="loginless@example.com").status, "relieved")


class RelievedLeavesTheScreensTests(APITestCase):
    """The five sections, and the history that must survive them."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr_rel", password="x", role="hr")
        self.client.force_authenticate(self.hr)

        # One person who stays, one who leaves.
        self.staying = self._employee_with_history("Staying", "staying@example.com")
        self.leaving = self._employee_with_history("Leaving", "leaving@example.com")

        self.leaving_onboarding = Onboarding.objects.get(email_id="leaving@example.com")

    def _employee_with_history(self, name, email):
        make_onboarding(name, email, "Active")
        employee = Employee.objects.get(email=email)
        employee.salary = Decimal("30000")
        employee.save()
        # Onboarding provisions the login itself, from the email local part.
        employee.refresh_from_db()
        assert employee.user is not None, "onboarding should have created a login"
        Attendance.objects.create(
            employee=employee,
            employee_name=employee.employee_name,
            role=employee.role,
            department=employee.department,
            salary=employee.salary,
            intime=datetime.datetime(2026, 8, 3, 9, 0, tzinfo=datetime.timezone.utc),
            status="Present",
        )
        Payslip.objects.create(employee=employee, month=8, year=2026, net_salary=Decimal("30000"))
        LeaveRequest.objects.create(
            employee=employee,
            leave_type="Casual",
            start_date=datetime.date(2026, 8, 5),
            end_date=datetime.date(2026, 8, 5),
            reason="personal",
        )
        return employee

    def _relieve(self):
        self.leaving_onboarding.employment_status = "Relieved"
        self.leaving_onboarding.save()
        self.leaving.refresh_from_db()

    def _names(self, url):
        response = self.client.get(url)
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data if isinstance(response.data, list) else response.data.get("results", [])
        return rows

    # -- before ---------------------------------------------------------------

    def test_both_are_there_to_begin_with(self):
        emails = [row["email"] for row in self._names("/api/employees/")]
        self.assertIn("staying@example.com", emails)
        self.assertIn("leaving@example.com", emails)

    # -- the five sections ----------------------------------------------------

    def test_they_come_off_the_employees_list(self):
        self._relieve()
        emails = [row["email"] for row in self._names("/api/employees/")]
        self.assertIn("staying@example.com", emails)
        self.assertNotIn("leaving@example.com", emails)

    def _as_admin(self):
        """The Users list is admin-only."""
        admin = User.objects.create_user(username="admin_rel", password="x", role="superadmin")
        admin.is_superuser = True
        admin.save()
        self.client.force_authenticate(admin)
        return admin

    def test_they_come_off_the_users_list(self):
        self._relieve()
        self._as_admin()
        usernames = [row["username"] for row in self._names("/api/auth/users/")]
        self.assertIn("staying", usernames)
        self.assertNotIn("leaving", usernames)

    def test_a_login_with_no_employee_behind_it_is_untouched(self):
        """Office admins and service accounts have no employee record, and the
        exclusion must not sweep them up."""
        self._relieve()
        admin = self._as_admin()
        usernames = [row["username"] for row in self._names("/api/auth/users/")]
        self.assertIn(admin.username, usernames)

    def test_they_come_off_attendance(self):
        self._relieve()
        names = [row["employee_name"] for row in self._names("/api/attendance/")]
        self.assertIn("Staying", names)
        self.assertNotIn("Leaving", names)

    def test_they_come_off_the_payslip_and_payroll_screens(self):
        self._relieve()
        ids = [row["employee"] for row in self._names("/api/payslips/")]
        self.assertIn(self.staying.id, ids)
        self.assertNotIn(self.leaving.id, ids)

    def test_they_come_off_leave_requests(self):
        self._relieve()
        ids = [row["employee"] for row in self._names("/api/leave-requests/")]
        self.assertIn(self.staying.id, ids)
        self.assertNotIn(self.leaving.id, ids)

    # -- nothing is destroyed -------------------------------------------------

    def test_every_record_of_theirs_is_still_in_the_database(self):
        """The half that matters. Past payroll was calculated from these rows."""
        self._relieve()
        self.assertTrue(Employee.objects.filter(pk=self.leaving.pk).exists())
        self.assertEqual(Payslip.objects.filter(employee=self.leaving).count(), 1)
        self.assertEqual(Attendance.objects.filter(employee=self.leaving).count(), 1)
        self.assertEqual(LeaveRequest.objects.filter(employee=self.leaving).count(), 1)

    def test_putting_them_back_returns_all_of_it(self):
        self._relieve()
        self.assertNotIn(
            "leaving@example.com", [row["email"] for row in self._names("/api/employees/")]
        )

        self.leaving_onboarding.employment_status = "Active"
        self.leaving_onboarding.save()

        self.assertIn(
            "leaving@example.com", [row["email"] for row in self._names("/api/employees/")]
        )
        ids = [row["employee"] for row in self._names("/api/payslips/")]
        self.assertIn(self.leaving.id, ids)

    def test_hr_can_still_look_them_up_on_purpose(self):
        self._relieve()
        emails = [row["email"] for row in self._names("/api/employees/?include_relieved=1")]
        self.assertIn("leaving@example.com", emails)

    # -- the rest of the office is untouched ----------------------------------

    def test_relieving_one_person_changes_nobody_else(self):
        before = Employee.objects.get(pk=self.staying.pk).status
        self._relieve()
        self.assertEqual(Employee.objects.get(pk=self.staying.pk).status, before)
        self.staying.refresh_from_db()
        self.staying.user.refresh_from_db()
        self.assertTrue(self.staying.user.is_active)

    def test_someone_merely_inactive_stays_on_the_screens(self):
        """Inactive is not gone: they are on the books and not working today."""
        self.leaving_onboarding.employment_status = "Inactive"
        self.leaving_onboarding.save()
        emails = [row["email"] for row in self._names("/api/employees/")]
        self.assertIn("leaving@example.com", emails)
        self.leaving.refresh_from_db()
        self.leaving.user.refresh_from_db()
        self.assertTrue(self.leaving.user.is_active)


class PayslipGenerationSkipsRelievedTests(APITestCase):
    def setUp(self):
        self.hr = User.objects.create_user(username="hr_gen", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        make_onboarding("Runner", "runner@example.com", "Relieved")
        self.employee = Employee.objects.get(email="runner@example.com")
        self.employee.salary = Decimal("30000")
        self.employee.save()

    def test_no_new_payslip_is_generated_for_someone_who_has_left(self):
        response = self.client.post(
            "/api/payslips/generate_all/", {"month": 9, "year": 2026}, format="json"
        )
        self.assertIn(response.status_code, (200, 201), response.data)
        self.assertFalse(
            Payslip.objects.filter(employee=self.employee, month=9, year=2026).exists()
        )
