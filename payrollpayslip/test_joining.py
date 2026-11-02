"""Paid from the day they joined.

A 25th-24th cycle that ended before somebody's date of joining is not theirs:
bulk generation leaves them out, generating or undoing one slip for it is
refused, and the Payroll page's month does not count them or their salary.
The cycle they join in is paid from the joining day. Nothing is marked on a day
before somebody joined, so without this both read as days with no absence: a
full-pay slip for a month before joining, and paid days before it.
"""
import datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from attendance.models import Attendance
from authentication.models import User
from employees.models import Employee

from .models import Payslip


def _at(day, hour=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, 0)))


def _person(name, doj, salary="30000"):
    return Employee.objects.create(
        employee_name=name, email=f"{name.replace(' ', '.').lower()}@example.com",
        role="Engineer", department="Service", branch="Chennai",
        salary=Decimal(salary), date_of_joining=doj, status="active",
    )


def _absent(emp, day):
    return Attendance.objects.create(
        employee=emp, employee_name=emp.employee_name, role="Engineer",
        department="Service", salary=emp.salary, status="Absent", intime=_at(day),
    )


class PaidFromTheDayTheyJoinedTests(APITestCase):
    def setUp(self):
        self.office = User.objects.create_user(username="joining-office", password="x", role="hr")
        self.client.force_authenticate(self.office)
        # October 2026's slip is 25 Sep - 24 Oct: 30 days.
        self.veteran = _person("Long Here", datetime.date(2025, 1, 1))
        self.joiner = _person("New Joiner", datetime.date(2026, 10, 8))
        self.no_doj = _person("No Date", None)

    def _generate(self, month, year=2026, **extra):
        return self.client.post(
            "/api/payslips/generate_all/", {"month": month, "year": year, **extra}, format="json"
        )

    def _slip(self, emp, month):
        return Payslip.objects.get(employee=emp, month=month, year=2026)

    def test_a_cycle_before_they_joined_gets_no_slip(self):
        response = self._generate(9)  # 25 Aug - 24 Sep, before 8 Oct
        self.assertEqual(response.status_code, 200, response.data)
        self.assertFalse(Payslip.objects.filter(employee=self.joiner).exists())
        self.assertEqual(response.data["skipped_not_joined"], 1)
        self.assertIn("1 employee who joined after this cycle was left out", response.data["message"])
        # Everybody else as before, a missing date of joining included.
        self.assertEqual(self._slip(self.veteran, 9).lop_days, Decimal("0"))
        self.assertEqual(self._slip(self.no_doj, 9).lop_days, Decimal("0"))

    def test_the_cycle_they_join_in_is_paid_from_the_joining_day(self):
        # 25 Sep - 7 Oct is 13 days before joining, and one Absent after it.
        _absent(self.joiner, datetime.date(2026, 10, 15))
        self._generate(10)
        slip = self._slip(self.joiner, 10)
        self.assertEqual(slip.total_days, 30)
        self.assertEqual((slip.lop_days, slip.paid_days), (Decimal("14"), Decimal("16")))

    def test_a_row_dated_before_joining_is_not_counted_twice(self):
        # Marked Absent on 1 Oct, before they joined: that day is unpaid
        # already, as one of the 13.
        _absent(self.joiner, datetime.date(2026, 10, 1))
        self._generate(10)
        self.assertEqual(self._slip(self.joiner, 10).lop_days, Decimal("13"))

    def test_joined_on_the_25th_the_whole_cycle_is_theirs(self):
        on_the_25th = _person("On The 25th", datetime.date(2026, 9, 25))
        self._generate(10)
        self.assertEqual(self._slip(on_the_25th, 10).lop_days, Decimal("0"))

    def test_joined_on_the_24th_that_cycle_pays_one_day(self):
        on_the_24th = _person("On The 24th", datetime.date(2026, 10, 24))
        self._generate(10)
        slip = self._slip(on_the_24th, 10)
        self.assertEqual((slip.lop_days, slip.paid_days), (Decimal("29"), Decimal("1")))

    def test_people_already_here_are_generated_exactly_as_before(self):
        _absent(self.veteran, datetime.date(2026, 10, 5))
        _absent(self.no_doj, datetime.date(2026, 9, 30))
        self._generate(10)
        self.assertEqual(self._slip(self.veteran, 10).lop_days, Decimal("1"))
        self.assertEqual(self._slip(self.no_doj, 10).lop_days, Decimal("1"))

    def test_generating_one_slip_before_they_joined_is_refused(self):
        response = self._generate(9, employee_id=self.joiner.id)
        self.assertEqual(response.status_code, 400)
        self.assertIn("joined on 08 Oct 2026", response.data["error"])
        self.assertIn("first payslip is October 2026", response.data["error"])
        self.assertFalse(Payslip.objects.filter(employee=self.joiner).exists())

    def test_their_first_slip_by_itself_is_generated(self):
        response = self._generate(10, employee_id=self.joiner.id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(self._slip(self.joiner, 10).lop_days, Decimal("13"))

    def test_undo_edits_on_a_slip_from_before_joining_is_refused(self):
        slip = Payslip.objects.create(employee=self.joiner, month=9, year=2026, total_days=31)
        response = self.client.post(f"/api/payslips/{slip.id}/revert/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertIn("after this cycle", response.data["error"])

    def test_undo_edits_in_the_joining_cycle_keeps_the_days_before_joining_unpaid(self):
        self._generate(10)
        slip = self._slip(self.joiner, 10)
        # HR's own figure wins on an edit...
        self.client.post(f"/api/payslips/{slip.id}/recalculate/", {"lop_days": 0}, format="json")
        slip.refresh_from_db()
        self.assertEqual(slip.lop_days, Decimal("0"))
        # ...and Undo Edits goes back to the slip as generation makes it.
        response = self.client.post(f"/api/payslips/{slip.id}/revert/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        slip.refresh_from_db()
        self.assertEqual(slip.lop_days, Decimal("13"))

    def test_the_month_before_they_joined_does_not_count_them(self):
        def stats(month):
            response = self.client.get(f"/api/payslips/company_stats/?month={month}&year=2026")
            self.assertEqual(response.status_code, 200, response.data)
            data = response.data
            return data["totalActiveEmployees"], data["totalBaseSalaryRaw"], data["notGeneratedCount"]

        self.assertEqual(stats(9), (2, 60000.0, 2))   # the veteran and No Date
        self.assertEqual(stats(10), (3, 90000.0, 3))  # the joiner counts from October
        self._generate(9)
        self.assertEqual(stats(9), (2, 60000.0, 0))   # nothing left to generate
