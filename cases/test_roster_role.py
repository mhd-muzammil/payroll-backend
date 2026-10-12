"""Roster rows say the employee's role and department.

OpenCall's live tracking board shows Payroll's other staff beside the
engineers (12 Oct 2026), labels them, and filters by department, so the
roster carries both. They are added alongside; every key the board and
Engineer Productivity already read is unchanged.
"""
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APIClient

from authentication.models import User
from employees.models import Employee


class RosterRoleTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(username="ops", password="x", role="admin", is_staff=True)
        self.client_admin = APIClient()
        self.client_admin.force_authenticate(self.admin)
        self.swetha = Employee.objects.create(
            employee_name="Swetha", role="CALL COORDINATOR", department="Office",
            branch="Chennai", salary=Decimal("15000"), email="swetha@example.com",
        )

    def test_every_employee_row_says_the_role(self):
        rows = self.client_admin.get("/api/tracking/roster/").data
        self.assertEqual({r["engineer_name"]: r["role"] for r in rows}, {"Swetha": "CALL COORDINATOR"})
        self.assertEqual({r["engineer_name"]: r["department"] for r in rows}, {"Swetha": "Office"})

    def test_a_named_row_says_it_and_an_unmatched_one_has_none(self):
        rows = self.client_admin.post(
            "/api/tracking/roster/",
            {"engineers": [{"name": "Swetha", "email": "swetha@example.com"}, {"name": "Nobody Here"}]},
            format="json",
        ).data
        by_name = {r["engineer_name"]: r for r in rows}
        self.assertEqual(by_name["Swetha"]["role"], "CALL COORDINATOR")
        self.assertEqual(by_name["Swetha"]["department"], "Office")
        self.assertIsNone(by_name["Nobody Here"]["role"])
        self.assertIsNone(by_name["Nobody Here"]["department"])
        # What the board already relied on is all still there.
        for key in ("engineer_id", "payroll_name", "matched", "branch", "state", "distance_km"):
            self.assertIn(key, by_name["Swetha"])
