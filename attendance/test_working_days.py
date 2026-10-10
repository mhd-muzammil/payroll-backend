"""The expense tracker's Engineer P&L reads each engineer's working days here.

The numbers that matter: a Sunday is not a working day unless somebody worked
it, two rows on one day are still one day, and a day outside the range does
not leak in.
"""
import datetime

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from attendance.models import Attendance
from authentication.models import User
from employees.models import Employee


def _at(day, hour=9):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour)))


class WorkingDaysTests(TestCase):
    # 2026-08-25 (Tue) .. 2026-09-24 (Thu): 31 days, 4 Sundays.
    START = datetime.date(2026, 8, 25)
    END = datetime.date(2026, 9, 24)

    def setUp(self):
        self.admin = User.objects.create_superuser(username="boss", password="x", email="boss@x.in")
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.praveen = Employee.objects.create(
            employee_name="Praveen S", role="Service engineer", department="Service",
            branch="Chennai", salary=27208, email="praveen@x.in",
        )

    def _row(self, day, status="Present", hour=9):
        Attendance.objects.create(
            employee=self.praveen, employee_name="Praveen S", role="Service engineer",
            department="Service", salary=27208, intime=_at(day, hour), status=status,
        )

    def _get(self, start=START, end=END):
        return self.client.get("/api/attendance/working_days/", {
            "start_date": start.isoformat(), "end_date": end.isoformat(),
        })

    def test_counts(self):
        self._row(datetime.date(2026, 8, 25))
        self._row(datetime.date(2026, 8, 26), status="Late")
        self._row(datetime.date(2026, 8, 26), hour=14)              # same day twice
        self._row(datetime.date(2026, 8, 27), status="Absent")
        self._row(datetime.date(2026, 8, 30), status="Leave")       # Sunday off
        self._row(datetime.date(2026, 9, 6), status="Overtime")     # Sunday worked
        self._row(datetime.date(2026, 9, 25))                       # outside range

        res = self._get()
        self.assertEqual(res.status_code, 200)
        self.assertEqual(res.data["calendar_days"], 31)
        self.assertEqual(res.data["working_days"], 27)
        [row] = res.data["results"]
        self.assertEqual(row["email"], "praveen@x.in")
        self.assertEqual(row["present_days"], 3)
        self.assertEqual(row["leave_days"], 2)

    def test_dates_are_required(self):
        res = self.client.get("/api/attendance/working_days/")
        self.assertEqual(res.status_code, 400)
        res = self._get(start=self.END, end=self.START)
        self.assertEqual(res.status_code, 400)
