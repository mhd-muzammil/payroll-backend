"""The days a relieved person worked, on the Attendance page.

Relieving somebody took them off the attendance list (29 Aug) -- and with them
every day they had worked, which the payslip and payroll screens now show
again, marked Relieved. The Attendance page asks for the same (with_relieved=1)
and marks them; a day after they left has no row, so it never shows them.

Everything else reads exactly as before: the WhatsApp poster's GET
/api/attendance/ sends no flag, the 10:30 Absent rule and its reminders never
touch this list, and an employee's own list ignores the flag.
"""

import datetime
from unittest import mock

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from attendance.models import Attendance
from authentication.models import User
from employees.models import Employee


def _at(day, hour, minute=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


class RelievedDaysOnTheAttendancePageTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="office-relieved", password="x", role="superadmin", is_superuser=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.day = timezone.localdate() - datetime.timedelta(days=3)
        self.rows = {}
        for name, status in (("Still Here", "active"), ("Has Left", "relieved")):
            employee = Employee.objects.create(
                employee_name=name, role="Engineer", department="Service", branch="Chennai",
                salary=20000, email=f"{name.replace(' ', '.').lower()}@example.com", status=status,
            )
            self.rows[status] = Attendance.objects.create(
                employee=employee, employee_name=name, role="Engineer", department="Service",
                salary=20000, status="Present", intime=_at(self.day, 9), outtime=_at(self.day, 18),
            )

    def _names(self, query=""):
        response = self.client.get(f"/api/attendance/{query}")
        self.assertEqual(response.status_code, 200, response.content)
        rows = response.json()
        rows = rows if isinstance(rows, list) else rows.get("results", [])
        return {row["employee_name"]: row for row in rows}

    def test_the_page_gets_their_days_marked(self):
        rows = self._names("?with_relieved=1")
        self.assertEqual(sorted(rows), ["Has Left", "Still Here"])
        self.assertEqual(rows["Has Left"]["employee_status"], "relieved")
        self.assertEqual(rows["Still Here"]["employee_status"], "active")

    def test_without_the_flag_nothing_changes(self):
        """The WhatsApp poster's call: GET /api/attendance/ with no params."""
        self.assertEqual(sorted(self._names()), ["Still Here"])

    def test_a_row_of_theirs_opens_and_edits_by_its_id(self):
        relieved = self.rows["relieved"]
        self.assertEqual(self.client.get(f"/api/attendance/{relieved.id}/").status_code, 200)
        response = self.client.patch(
            f"/api/attendance/{relieved.id}/", {"status": "Half Day"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        relieved.refresh_from_db()
        self.assertEqual(relieved.status, "Half Day")

    def test_an_employee_never_gets_them(self):
        user = User.objects.create_user(username="left-att", password="x", role="employee")
        relieved = self.rows["relieved"]
        relieved.employee.user = user
        relieved.employee.save()
        self.client.force_authenticate(user)
        self.assertEqual(self._names("?with_relieved=1"), {})

    def test_the_absent_rule_still_never_sees_them(self):
        """The not-logged-in list (the 10:30 rule and its reminders) on a
        working day somebody has logged in: it names the active person with no
        Login, and never the relieved one, flag or not. Both can log in."""
        wednesday = datetime.date(2026, 10, 7)
        for status in ("active", "relieved"):
            employee = self.rows[status].employee
            employee.user = User.objects.create_user(
                username=f"absent-rule-{status}", password="x", role="employee"
            )
            employee.save()
        # Somebody logged in that day, so the rule runs rather than skipping it.
        logged_in = Employee.objects.create(
            employee_name="Logged In", role="Engineer", department="Service", branch="Chennai",
            salary=20000, email="logged.in@example.com", status="active",
            user=User.objects.create_user(username="absent-rule-in", password="x", role="employee"),
        )
        Attendance.objects.create(
            employee=logged_in, employee_name="Logged In", role="Engineer", department="Service",
            salary=20000, status="Present", intime=_at(wednesday, 9),
        )
        with mock.patch("django.utils.timezone.now", return_value=_at(wednesday, 9, 11)):
            response = self.client.get("/api/attendance/not_logged_in/?with_relieved=1")
        self.assertEqual(response.status_code, 200, response.content)
        self.assertIsNone(response.data["skip"])
        listed = [name for names in response.data["branches"].values() for name in names]
        self.assertIn("Still Here", listed)
        self.assertNotIn("Has Left", listed)
