"""Half Day (first named Off Day): a day the office marks, half unpaid.

The office's decisions (24 Oct 2026): a Half Day costs half a day's pay --
an Absent costs the whole day -- and the register names it apart, so it is not
read as a day somebody failed to turn up. So it is pinned from both sides:

  * pay: a payslip, its Undo Edits, and the expense tracker's day counts all
    take a Half Day row as half a day of loss of pay;
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


class OfficeMarksAnHalfDayTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="office-halfday", password="x", role="superadmin", is_superuser=True
        )
        self.divya = Employee.objects.create(
            employee_name="Divya P", role="Service engineer", department="Service",
            branch="Salem", salary=27208,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def test_it_is_saved_as_half_day(self):
        response = self.client.post("/api/attendance/", {
            "employee": self.divya.id,
            "employee_name": "Divya P",
            "role": "Service engineer",
            "department": "Service",
            "salary": "27208.00",
            "status": "Half Day",
            "intime": f"{WEDNESDAY.isoformat()}T00:00:00",
        }, format="json")
        self.assertEqual(response.status_code, 201, response.content)
        row = Attendance.objects.get()
        self.assertEqual((row.status, row.employee_id), ("Half Day", self.divya.id))
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

    def test_somebody_on_a_half_day_is_not_marked_absent(self):
        self._row(self.chitra, WEDNESDAY, "Present", hour=9)
        self._row(self.anand, WEDNESDAY, "Half Day", hour=0)
        self._run()
        anand = list(Attendance.objects.filter(employee=self.anand).values_list("status", flat=True))
        self.assertEqual(anand, ["Half Day"], "one row, still the Half Day")
        self.assertTrue(Attendance.objects.filter(employee=self.bala, status="Absent").exists())

    def test_a_half_day_alone_does_not_make_it_a_working_day(self):
        # Like a marked Leave: nobody has logged in, so nobody is marked.
        self._row(self.anand, WEDNESDAY, "Half Day", hour=0)
        out = self._run()
        self.assertIn("nobody has logged in", out)
        self.assertEqual(Attendance.objects.count(), 1)


class LoginOnAnHalfDayTests(People, APITestCase):
    def setUp(self):
        self.anand = self._person("Anand K")
        self.client.force_authenticate(self.anand.user)

    def _post(self, what, moment):
        with _clock(moment):
            return self.client.post(f"/api/attendance/{what}/", SOMEWHERE, format="json")

    def test_a_login_is_accepted_on_the_same_row_and_the_day_stays_off(self):
        marked = self._row(self.anand, WEDNESDAY, "Half Day", hour=0)
        response = self._post("check_in", _at(WEDNESDAY, 9, 40))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], marked.id)
        self.assertEqual(response.data["status"], "Half Day")
        self.assertEqual(Attendance.objects.count(), 1)

    def test_logout_without_a_login_is_refused(self):
        marked = self._row(self.anand, WEDNESDAY, "Half Day", hour=0)
        response = self._post("check_out", _at(WEDNESDAY, 18, 5))
        self.assertEqual(response.status_code, 400)
        marked.refresh_from_db()
        self.assertIsNone(marked.outtime, "no shift from midnight")


class ForgottenLogoutLeavesHalfDayAloneTests(TestCase):
    def test_a_half_day_is_never_closed_at_1159_pm(self):
        employee = Employee.objects.create(
            employee_name="Praveen S", role="Service engineer", department="Service",
            branch="Chennai", salary=27208,
        )
        today = timezone.localdate()
        row = Attendance.objects.create(
            employee=employee, employee_name="Praveen S", role="Service engineer",
            department="Service", salary=27208, status="Half Day",
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


class HalfDayIsHalfADayOfPayTests(APITestCase):
    """The 25 Jul - 24 Aug 2026 cycle (payslip month 8): 31 days. One Absent and
    one Half Day are a day and a half of loss of pay, never two."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr-halfday", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        # No date of joining: no casual leave, so every unpaid day shows.
        self.divya = Employee.objects.create(
            employee_name="Divya P", email="divya@example.com", role="Engineer",
            department="Service", branch="Chennai", salary=Decimal("31000"), status="active",
        )
        _row(self.divya, datetime.date(2026, 8, 4), "Present", hour=9)
        _row(self.divya, datetime.date(2026, 8, 5), "Absent")
        _row(self.divya, datetime.date(2026, 8, 6), "Half Day")
        _row(self.divya, datetime.date(2026, 8, 7), "Leave")  # a holiday: paid

    def _generate(self, employee):
        response = self.client.post(
            "/api/payslips/generate_all/",
            {"month": 8, "year": 2026, "employee_id": employee.id},
            format="json",
        )
        self.assertIn(response.status_code, (200, 201), response.data)
        return Payslip.objects.get(employee=employee, month=8, year=2026)

    def test_generation_cuts_half_a_day(self):
        slip = self._generate(self.divya)
        self.assertEqual(slip.lop_days, Decimal("1.5"), "the Absent 1 + the Half Day 0.5; the Leave is paid")
        self.assertEqual(slip.paid_days, Decimal("29.5"))
        # 31,000 over 31 days is 1,000 a day: a day and a half is 1,500 off.
        self.assertEqual(slip.gross_salary - slip.gross_earnings, Decimal("1500.00"))

    def test_casual_leave_covers_a_half_day_by_half(self):
        veteran = Employee.objects.create(
            employee_name="Old Hand", email="old@example.com", role="Engineer",
            department="Service", branch="Chennai", salary=Decimal("31000"), status="active",
            date_of_joining=datetime.date(2025, 1, 10),
        )
        _row(veteran, datetime.date(2026, 8, 6), "Half Day")
        slip = self._generate(veteran)
        self.assertEqual(slip.lop_days, Decimal("0.5"))
        self.assertEqual(slip.casual_leave_used, Decimal("0.5"), "half of the one day, not all of it")

    def test_undo_edits_counts_it_the_same(self):
        slip = Payslip.objects.create(
            employee=self.divya, month=8, year=2026,
            **compute_payslip_fields(self.divya, 31, 5),  # a manual edit to undo
        )
        response = self.client.post(f"/api/payslips/{slip.id}/revert/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        slip.refresh_from_db()
        self.assertEqual(slip.lop_days, Decimal("1.5"))

    def test_the_expense_trackers_day_counts_agree(self):
        admin = User.objects.create_superuser(username="boss-halfday", password="x", email="b@x.in")
        self.client.force_authenticate(admin)
        response = self.client.get("/api/attendance/working_days/", {
            "start_date": "2026-07-25", "end_date": "2026-08-24",
        })
        self.assertEqual(response.status_code, 200, response.data)
        row = next(r for r in response.data["results"] if r["employee_id"] == self.divya.id)
        self.assertEqual(
            (row["present_days"], row["absent_days"], row["half_days"], row["leave_days"]), (1, 1, 1, 1)
        )
        self.assertEqual(row["lop_days"], 1.5)
        self.assertEqual(row["casual_leave_days"], 0)
        # 26 working days (five Sundays) less the four marked ones.
        self.assertEqual(row["unmarked_days"], 22)


class OffDayRowsBecomeHalfDayTests(TestCase):
    """Off Day shipped first and was renamed the same day: a row the office had
    already marked Off Day is moved to Half Day by migration 0012."""

    def test_rows_saved_as_off_day_are_renamed(self):
        from importlib import import_module

        from django.apps import apps

        migration = import_module("attendance.migrations.0012_half_day")
        employee = Employee.objects.create(
            employee_name="Praveen S", role="Service engineer", department="Service",
            branch="Chennai", salary=27208,
        )
        marked = _row(employee, WEDNESDAY, "Off Day")
        absent = _row(employee, WEDNESDAY + datetime.timedelta(days=1), "Absent")
        migration.off_day_rows_to_half_day(apps, None)
        marked.refresh_from_db()
        absent.refresh_from_db()
        self.assertEqual((marked.status, absent.status), ("Half Day", "Absent"))
