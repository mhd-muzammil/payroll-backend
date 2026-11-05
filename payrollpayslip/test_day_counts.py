"""Absent and Half Day beside the paid days, on the Employee Pay Report.

The report shows, for each slip, how many Absent and Half Day rows its cycle
has in attendance -- the 25th to the 24th, from the joining day, the rows LOP
is priced from. To look at only: nothing is stored or priced from them. Only a
list that asks (with_day_counts=1) gets them, and only the office's.
"""
import datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from attendance.models import Attendance
from authentication.models import User
from employees.models import Employee

from .models import Payslip
from .views import compute_payslip_fields, cycle_loss_of_pay

URL = "/api/payslips/"


def _at(day, hour=9, minute=0):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


def _person(name, joined=datetime.date(2026, 1, 1), user=None):
    return Employee.objects.create(
        employee_name=name, email=f"{name.replace(' ', '.').lower()}@example.com",
        role="Engineer", department="Service", branch="Chennai",
        salary=Decimal("30000"), date_of_joining=joined, status="active", user=user,
    )


def _row(emp, when, status):
    return Attendance.objects.create(
        employee=emp, employee_name=emp.employee_name, role=emp.role,
        department=emp.department, salary=emp.salary, status=status, intime=when,
    )


def _slip(emp, month, year=2026, **extra):
    return Payslip.objects.create(
        employee=emp, month=month, year=year, status="Generated",
        **compute_payslip_fields(emp, 30, 0), **extra,
    )


class DayCountsTests(APITestCase):
    def setUp(self):
        self.office = User.objects.create_user(username="dc-office", password="x", role="hr")
        self.client.force_authenticate(self.office)
        d = datetime.date
        # Whole cycles. October's runs 25 Sep - 24 Oct.
        self.anu = _person("Anu Counted")
        _row(self.anu, _at(d(2026, 9, 25), 0, 30), "Absent")     # first minute of October's cycle
        _row(self.anu, _at(d(2026, 10, 24), 23, 30), "Absent")   # last hour of it
        _row(self.anu, _at(d(2026, 10, 10)), "Half Day")
        _row(self.anu, _at(d(2026, 10, 11)), "Present")
        _row(self.anu, _at(d(2026, 10, 12)), "Leave")
        _row(self.anu, _at(d(2026, 10, 13)), "Late")
        _row(self.anu, _at(d(2026, 9, 24), 10), "Absent")        # September's cycle
        _row(self.anu, _at(d(2026, 10, 25), 0, 30), "Half Day")  # November's cycle
        # Joined mid-cycle: a row before the joining day is not counted.
        self.ravi = _person("Ravi Joined", joined=d(2026, 10, 5))
        _row(self.ravi, _at(d(2026, 10, 1)), "Absent")
        _row(self.ravi, _at(d(2026, 10, 6)), "Absent")
        _row(self.ravi, _at(d(2026, 10, 7)), "Half Day")
        self.anu_oct = _slip(self.anu, 10)
        self.anu_sep = _slip(self.anu, 9)
        self.ravi_oct = _slip(self.ravi, 10)

    def _rows(self, **params):
        response = self.client.get(URL, {"with_relieved": 1, "with_day_counts": 1, **params})
        self.assertEqual(response.status_code, 200, response.data)
        return {row["id"]: row for row in response.data}

    def test_each_slip_gets_its_own_cycle_s_absent_and_half_days(self):
        rows = self._rows()
        self.assertEqual((rows[self.anu_oct.id]["absent_days"], rows[self.anu_oct.id]["half_days"]), (2, 1),
                         "the 25th 00:30 and the 24th 23:30 are both October's; Present/Leave/Late are not counted")
        self.assertEqual((rows[self.anu_sep.id]["absent_days"], rows[self.anu_sep.id]["half_days"]), (1, 0))

    def test_a_row_before_the_joining_day_is_not_counted(self):
        rows = self._rows()
        self.assertEqual((rows[self.ravi_oct.id]["absent_days"], rows[self.ravi_oct.id]["half_days"]), (1, 1))

    def test_the_counts_are_the_rows_lop_is_priced_from(self):
        start, end = datetime.date(2026, 9, 25), datetime.date(2026, 10, 24)
        lop = cycle_loss_of_pay(self.anu, start, _at(start, 0, 0), timezone.make_aware(
            datetime.datetime.combine(end, datetime.time.max)))
        row = self._rows()[self.anu_oct.id]
        self.assertEqual(Decimal(row["absent_days"]) + Decimal("0.5") * row["half_days"], lop)

    def test_a_month_s_list_counts_too(self):
        rows = self._rows(month=10, year=2026)
        self.assertEqual(set(rows), {self.anu_oct.id, self.ravi_oct.id})
        self.assertEqual(rows[self.anu_oct.id]["absent_days"], 2)

    def test_a_list_that_does_not_ask_is_as_it_was(self):
        response = self.client.get(URL, {"with_relieved": 1})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.data)
        for row in response.data:
            self.assertNotIn("absent_days", row)
            self.assertNotIn("half_days", row)

    def test_a_slip_by_its_id_is_as_it_was(self):
        response = self.client.get(f"{URL}{self.anu_oct.id}/", {"with_day_counts": 1})
        self.assertEqual(response.status_code, 200)
        self.assertNotIn("absent_days", response.data)

    def test_an_employee_s_own_list_never_gets_them(self):
        worker = User.objects.create_user(username="dc-worker", password="x", role="employee")
        self.anu.user = worker
        self.anu.save()
        self.anu_oct.sent_at = timezone.now()
        self.anu_oct.save()
        self.client.force_authenticate(worker)
        response = self.client.get(URL, {"with_relieved": 1, "with_day_counts": 1})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([row["id"] for row in response.data], [self.anu_oct.id])
        self.assertNotIn("absent_days", response.data[0])

    def test_nothing_on_a_slip_changes(self):
        before = Payslip.objects.get(id=self.anu_oct.id)
        self._rows()
        after = Payslip.objects.get(id=self.anu_oct.id)
        self.assertEqual((before.lop_days, before.net_salary), (after.lop_days, after.net_salary))
