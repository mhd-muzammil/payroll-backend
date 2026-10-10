"""The expense tracker's Engineer P&L reads each engineer's days here.

They have to come out the way a payslip counts them, or the P&L charges a
different salary from the one Payroll pays: only Absent days are unpaid,
casual leave covers one of them a cycle after six months' service, Leave and
Sundays are paid, and a finished cycle's own payslip is the final word.
"""
import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.models import Attendance
from authentication.models import User
from employees.models import Employee
from payrollpayslip.models import Payslip


def _at(day, hour=9):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour)))


class WorkingDaysTests(TestCase):
    # 2026-08-25 (Tue) .. 2026-09-24 (Thu): the September cycle, 31 days, 4 Sundays.
    START = datetime.date(2026, 8, 25)
    END = datetime.date(2026, 9, 24)

    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password="x", email="boss@x.in")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.praveen = self._employee("Praveen S", "praveen@x.in")

    def _employee(self, name, email, **extra):
        return Employee.objects.create(
            employee_name=name, role="Service engineer", department="Service",
            branch="Chennai", salary=27208, email=email, **extra,
        )

    def _row(self, day, status="Present", hour=9, emp=None):
        emp = emp or self.praveen
        Attendance.objects.create(
            employee=emp, employee_name=emp.employee_name, role="Service engineer",
            department="Service", salary=27208, intime=_at(day, hour), status=status,
        )

    def _get(self, start=START, end=END):
        return self.client.get("/api/attendance/working_days/", {
            "start_date": start.isoformat(), "end_date": end.isoformat(),
        })

    def _row_for(self, res, emp=None):
        emp = emp or self.praveen
        return next(r for r in res.data["results"] if r["employee_id"] == emp.id)

    def test_counts_the_way_a_payslip_does(self):
        self._row(datetime.date(2026, 8, 25))
        self._row(datetime.date(2026, 8, 26), status="Late")
        self._row(datetime.date(2026, 8, 26), hour=14)              # same day twice
        self._row(datetime.date(2026, 8, 27), status="Absent")      # unpaid
        self._row(datetime.date(2026, 8, 28), status="Absent")      # unpaid
        self._row(datetime.date(2026, 8, 29), status="Leave")       # paid
        self._row(datetime.date(2026, 8, 30), status="Leave")       # Sunday off, paid
        self._row(datetime.date(2026, 9, 6), status="Overtime")     # Sunday worked
        self._row(datetime.date(2026, 9, 25))                       # outside range

        res = self._get()
        self.assertEqual(res.status_code, 200)
        self.assertEqual((res.data["calendar_days"], res.data["working_days"]), (31, 27))
        row = self._row_for(res)
        self.assertEqual(row["email"], "praveen@x.in")
        self.assertEqual(row["present_days"], 3)
        self.assertEqual((row["absent_days"], row["leave_days"]), (2, 2))
        # No date of joining, so no casual leave: both absences are unpaid.
        self.assertEqual((row["casual_leave_days"], row["lop_days"]), (0, 2))
        # 27 working days less the 2 worked weekdays, 2 absent and 1 weekday leave.
        self.assertEqual(row["unmarked_days"], 22)
        self.assertIsNone(row["payslip"])

    def test_casual_leave_covers_one_absence_after_six_months(self):
        veteran = self._employee("Old Hand", "old@x.in", date_of_joining=datetime.date(2025, 1, 10))
        rookie = self._employee("New Hand", "new@x.in", date_of_joining=datetime.date(2026, 6, 1))
        for emp in (veteran, rookie):
            for d in (2, 3, 4):
                self._row(datetime.date(2026, 9, d), status="Absent", emp=emp)
        res = self._get()
        self.assertEqual((self._row_for(res, veteran)["casual_leave_days"], self._row_for(res, veteran)["lop_days"]), (1, 2))
        self.assertEqual((self._row_for(res, rookie)["casual_leave_days"], self._row_for(res, rookie)["lop_days"]), (0, 3))

    def test_a_full_cycle_returns_its_payslip(self):
        Payslip.objects.create(
            employee=self.praveen, month=9, year=2026, status="Paid", total_days=31,
            paid_days=Decimal("29"), lop_days=Decimal("2"), gross_salary=Decimal("27208"),
            gross_earnings=Decimal("25452.65"), casual_leave_pay=Decimal("877.68"),
            special_work_pay=Decimal("0"),
        )
        slip = self._row_for(self._get())["payslip"]
        self.assertEqual((slip["status"], slip["paid_days"], slip["earned"]), ("Paid", "29.00", "26330.33"))
        # A part of the cycle is not the cycle: no slip.
        self.assertIsNone(self._row_for(self._get(end=datetime.date(2026, 9, 10)))["payslip"])

    def test_a_linked_employee_with_no_rows_is_still_listed(self):
        res = self._get(start=datetime.date(2026, 9, 1), end=datetime.date(2026, 9, 2))
        row = self._row_for(res)
        self.assertEqual((row["present_days"], row["lop_days"], row["unmarked_days"]), (0, 0, 2))

    def test_dates_are_required(self):
        res = self.client.get("/api/attendance/working_days/")
        self.assertEqual(res.status_code, 400)
        res = self._get(start=self.END, end=self.START)
        self.assertEqual(res.status_code, 400)
