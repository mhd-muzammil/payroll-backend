"""The role the Attendance page filters by is the employee's role now.

The `role` on an attendance row is a copy taken the day it was written. Roles
were renamed afterwards ("Service engineer" -> "FIELD ENGINEER"), so on
12 Oct 2026 321 of the cycle's 323 rows disagreed with their employee, and the
page's "All Roles" filter, built from the copies, offered names nobody uses
and split people's days between two of them. `employee_role` is the answer.
"""
import datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from attendance.models import Attendance
from authentication.models import User
from employees.models import Employee


class EmployeeRoleTests(APITestCase):
    def setUp(self):
        self.office = User.objects.create_user(username="office", password="x", role="admin")
        self.client.force_authenticate(self.office)
        self.ragavan = Employee.objects.create(
            employee_name="Ragavan S", role="FIELD ENGINEER", department="Service",
            branch="Chennai", salary=Decimal("20000"),
        )

    def _row(self, employee, role, day, link=True):
        return Attendance.objects.create(
            employee=employee if link else None, employee_name=employee.employee_name,
            role=role, department="Service", salary=Decimal("20000"),
            intime=timezone.make_aware(datetime.datetime.combine(day, datetime.time(9, 0))),
            status="Present",
        )

    def _rows(self):
        response = self.client.get("/api/attendance/")
        self.assertEqual(response.status_code, 200)
        data = response.data if isinstance(response.data, list) else response.data["results"]
        return {row["id"]: row for row in data}

    def test_every_row_carries_the_employees_role_now(self):
        old = self._row(self.ragavan, "Service engineer", datetime.date(2026, 10, 1))
        new = self._row(self.ragavan, "CCO", datetime.date(2026, 10, 2))
        rows = self._rows()
        self.assertEqual(rows[old.id]["employee_role"], "FIELD ENGINEER")
        self.assertEqual(rows[new.id]["employee_role"], "FIELD ENGINEER", "one person, one role")
        # The copy is still there, unchanged, for anything that reads it.
        self.assertEqual(rows[old.id]["role"], "Service engineer")

    def test_a_row_that_belongs_to_nobody_keeps_its_own(self):
        orphan = self._row(self.ragavan, "SLC", datetime.date(2026, 10, 3), link=False)
        self.assertEqual(self._rows()[orphan.id]["employee_role"], "SLC")

    def test_it_follows_a_renamed_role(self):
        row = self._row(self.ragavan, "Service engineer", datetime.date(2026, 10, 1))
        self.ragavan.role = "RMA Executive"
        self.ragavan.save()
        self.assertEqual(self._rows()[row.id]["employee_role"], "RMA Executive")

    def test_it_cannot_be_written(self):
        row = self._row(self.ragavan, "Service engineer", datetime.date(2026, 10, 1))
        self.client.patch(f"/api/attendance/{row.id}/", {"employee_role": "MD"}, format="json")
        self.ragavan.refresh_from_db()
        self.assertEqual(self.ragavan.role, "FIELD ENGINEER")
        self.assertEqual(self._rows()[row.id]["employee_role"], "FIELD ENGINEER")
