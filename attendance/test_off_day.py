"""Off Day: a day off the office marks, unpaid.

The office's decision (24 Oct 2026): an Off Day costs a day's pay exactly as
Absent does, but the register names it apart -- a day off asked for is not a
day somebody failed to turn up. So it is pinned from both sides:

  * pay: a payslip, its Undo Edits, and the expense tracker's day counts all
    take an Off Day row as a loss-of-pay day;
  * a no-punch day, like Absent and Leave: the 10:30 rule leaves the person
    alone, the row alone does not make a working day, a Login on it is still
    accepted without changing the day, and the 11:59pm rule never closes it.
"""
import datetime
from decimal import Decimal
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from attendance.models import Attendance
from attendance.test_absent_no_login import SOMEWHERE, WEDNESDAY, People, _at, _clock
from authentication.models import User
from employees.models import Employee
from payrollpayslip.models import Payslip
from payrollpayslip.views import compute_payslip_fields


class OfficeMarksAnOffDayTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="office-offday", password="x", role="superadmin", is_superuser=True
        )
        self.divya = Employee.objects.create(
            employee_name="Divya P", role="Service engineer", department="Service",
            branch="Salem", salary=27208,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_it_is_saved_as_off_day(self):
        response = self.client.post("/api/attendance/", {
            "employee": self.divya.id,
            "employee_name": "Divya P",
            "role": "Service engineer",
            "department": "Service",
            "salary": "27208.00",
            "status": "Off Day",
            "intime": f"{WEDNESDAY.isoformat()}T00:00:00",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        row = Attendance.objects.get()
        self.assertEqual((row.status, row.employee_id), ("Off Day", self.divya.id))
        self.assertIsNone(row.outtime)


class TenThirtyRuleTests(People, TestCase):
    def setUp(self):
        self.anand = self._person("Anand K")
        self.bala = self._person("Bala M", branch="Salem")
        self.chitra = self._person("Chitra R")

    def _run(self):
        out = StringIO()
        with _clock(_at(WEDNESDAY, 10, 30, 5)):
            call_command("mark_absent_no_login", stdout=out)
        return out.getvalue()

    def test_somebody_on_an_off_day_is_not_marked_absent(self):
        self._row(self.chitra, WEDNESDAY, "Present", hour=9)
        self._row(self.anand, WEDNESDAY, "Off Day", hour=0)
        self._run()
        anand = list(Attendance.objects.filter(employee=self.anand).values_list("status", flat=True))
        self.assertEqual(anand, ["Off Day"], "one row, still the Off Day")
        self.assertTrue(Attendance.objects.filter(employee=self.bala, status="Absent").exists())

    def test_an_off_day_alone_does_not_make_it_a_working_day(self):
        # Like a marked Leave: nobody has logged in, so nobody is marked.
        self._row(self.anand, WEDNESDAY, "Off Day", hour=0)
        out = self._run()
        self.assertIn("nobody has logged in", out)
        self.assertEqual(Attendance.objects.count(), 1)


class LoginOnAnOffDayTests(People, APITestCase):
    def setUp(self):
        self.anand = self._person("Anand K")
        self.client.force_authenticate(self.anand.user)

    def _post(self, what, moment):
        with _clock(moment):
            return self.client.post(f"/api/attendance/{what}/", SOMEWHERE, format="json")

    def test_a_login_is_accepted_on_the_same_row_and_the_day_stays_off(self):
        marked = self._row(self.anand, WEDNESDAY, "Off Day", hour=0)
        response = self._post("check_in", _at(WEDNESDAY, 9, 40))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], marked.id)
        self.assertEqual(response.data["status"], "Off Day")
        self.assertEqual(Attendance.objects.count(), 1)

    def test_logout_without_a_login_is_refused(self):
        marked = self._row(self.anand, WEDNESDAY, "Off Day", hour=0)
        response = self._post("check_out", _at(WEDNESDAY, 18, 5))
        self.assertEqual(response.status_code, 400)
        marked.refresh_from_db()
        self.assertIsNone(marked.outtime, "no shift from midnight")


class ForgottenLogoutLeavesOffDayAloneTests(TestCase):
    def test_an_off_day_is_never_closed_at_1159_pm(self):
        employee = Employee.objects.create(
            employee_name="Praveen S", role="Service engineer", department="Service",
            branch="Chennai", salary=27208,
        )
        today = timezone.localdate()
        row = Attendance.objects.create(
            employee=employee, employee_name="Praveen S", role="Service engineer",
            department="Service", salary=27208, status="Off Day",
            intime=timezone.make_aware(datetime.datetime.combine(today, datetime.time(0, 0))),
        )
        call_command("close_forgotten_logouts", stdout=StringIO())
        row.refresh_from_db()
        self.assertIsNone(row.outtime)
        self.assertFalse(row.auto_closed_out)


def _row(employee, day, status, hour=0):
    return Attendance.objects.create(
        employee=employee, employee_name=employee.employee_name, role=employee.role,
        department=employee.department, salary=employee.salary, status=status,
        intime=timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour))),
    )


class OffDayIsLossOfPayTests(APITestCase):
    """The 25 Jul - 24 Aug 2026 cycle (payslip month 8): 31 days."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr-offday", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        # No date of joining: no casual leave, so every unpaid day shows.
        self.divya = Employee.objects.create(
            employee_name="Divya P", email="divya@example.com", role="Engineer",
            department="Service", branch="Chennai", salary=Decimal("31000"), status="active",
        )
        _row(self.divya, datetime.date(2026, 8, 4), "Present", hour=9)
        _row(self.divya, datetime.date(2026, 8, 5), "Absent")
        _row(self.divya, datetime.date(2026, 8, 6), "Off Day")
        _row(self.divya, datetime.date(2026, 8, 7), "Leave")  # a holiday: paid

    def test_generation_counts_it_like_absent(self):
        response = self.client.post(
            "/api/payslips/generate_all/",
            {"month": 8, "year": 2026, "employee_id": self.divya.id},
            format="json",
        )
        self.assertIn(response.status_code, (200, 201), response.data)
        slip = Payslip.objects.get(employee=self.divya, month=8, year=2026)
        self.assertEqual(slip.lop_days, Decimal(2), "the Absent and the Off Day; the Leave is paid")
        self.assertEqual(slip.paid_days, Decimal(29))

    def test_undo_edits_counts_it_too(self):
        slip = Payslip.objects.create(
            employee=self.divya, month=8, year=2026,
            **compute_payslip_fields(self.divya, 31, 5),  # a manual edit to undo
        )
        response = self.client.post(f"/api/payslips/{slip.id}/revert/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        slip.refresh_from_db()
        self.assertEqual(slip.lop_days, Decimal(2))

    def test_the_expense_trackers_day_counts_agree(self):
        admin = User.objects.create_superuser(username="boss-offday", password="x", email="b@x.in")
        self.client.force_authenticate(admin)
        response = self.client.get("/api/attendance/working_days/", {
            "start_date": "2026-07-25", "end_date": "2026-08-24",
        })
        self.assertEqual(response.status_code, 200, response.data)
        row = next(r for r in response.data["results"] if r["employee_id"] == self.divya.id)
        self.assertEqual((row["present_days"], row["absent_days"], row["leave_days"]), (1, 2, 1))
        self.assertEqual(row["lop_days"], 2)
