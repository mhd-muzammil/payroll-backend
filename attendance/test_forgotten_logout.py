"""Somebody logged in and never logged out.

The day is closed for them at 11.59pm. What these tests are really for is
everything that must NOT be closed: an absent day is stored as midnight with no
logout, so the rule applied carelessly would turn every absence in the month
into a fifteen-hour shift.
"""
import datetime
from io import StringIO

from django.core.management import call_command
from django.test import TestCase
from django.utils import timezone

from attendance.models import Attendance
from employees.models import Employee


def _at(day, hour, minute=0):
    """A wall-clock time on `day`, in the local zone -- the way it is stored."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


class ForgottenLogoutTests(TestCase):
    def setUp(self):
        self.employee = Employee.objects.create(
            employee_name="Praveen S", role="Service engineer", department="Service",
            branch="Chennai", salary=27208,
        )
        self.today = timezone.localdate()
        self.yesterday = self.today - datetime.timedelta(days=1)

    def _row(self, day, status="Present", hour=9, minute=15, outtime=None, name=None):
        return Attendance.objects.create(
            employee=self.employee,
            employee_name=name or self.employee.employee_name,
            role="Service engineer",
            department="Service",
            salary=27208,
            intime=_at(day, hour, minute) if hour is not None else None,
            outtime=outtime,
            status=status,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("close_forgotten_logouts", *args, stdout=out)
        return out.getvalue()

    # ------------------------------------------------------------ what it does

    def test_a_forgotten_logout_is_closed_at_1159_pm(self):
        row = self._row(self.today)
        self._run()
        row.refresh_from_db()
        self.assertIsNotNone(row.outtime, "the day has to end somewhere")
        closed = timezone.localtime(row.outtime)
        self.assertEqual((closed.hour, closed.minute), (23, 59))
        self.assertEqual(closed.date(), self.today, "on the day they logged in")

    def test_the_row_says_nobody_pressed_it(self):
        """Otherwise a fifteen-hour day cannot be told from a missed Logout."""
        row = self._row(self.today)
        self._run()
        row.refresh_from_db()
        self.assertTrue(row.auto_closed_out)

    def test_late_and_overtime_days_are_closed_too(self):
        for status in ("Late", "Overtime"):
            with self.subTest(status=status):
                Attendance.objects.all().delete()
                row = self._row(self.today, status=status)
                self._run()
                row.refresh_from_db()
                self.assertIsNotNone(row.outtime, f"{status} is a day somebody worked")

    # -------------------------------------------------- what it must NOT touch

    def test_an_absent_day_is_left_alone(self):
        """THE one that matters.

        An absent day is stored as midnight with no logout -- that is how a day
        with no punch is written. Closing it would read as a fifteen-hour shift
        and every absence in the month would become one.
        """
        row = self._row(self.today, status="Absent", hour=0, minute=0)
        self._run()
        row.refresh_from_db()
        self.assertIsNone(row.outtime)
        self.assertFalse(row.auto_closed_out)

    def test_a_leave_day_is_left_alone(self):
        row = self._row(self.today, status="Leave", hour=0, minute=0)
        self._run()
        row.refresh_from_db()
        self.assertIsNone(row.outtime)

    def test_a_day_that_was_logged_out_of_is_untouched(self):
        real = _at(self.today, 18, 32)
        row = self._row(self.today, outtime=real)
        self._run()
        row.refresh_from_db()
        self.assertEqual(row.outtime, real)
        self.assertFalse(row.auto_closed_out)

    def test_running_it_twice_changes_nothing(self):
        row = self._row(self.today)
        self._run()
        row.refresh_from_db()
        first = row.outtime
        output = self._run()
        row.refresh_from_db()
        self.assertEqual(row.outtime, first)
        self.assertIn("logged out", output)

    def test_an_earlier_day_is_not_swept_unless_it_is_asked_for(self):
        """An imported sheet leaves old rows with no logout.

        Quietly stamping those would rewrite months of hours nobody asked
        about, so the command only ever closes the one day it is run for.
        """
        old = self._row(self.yesterday)
        self._run()
        old.refresh_from_db()
        self.assertIsNone(old.outtime)

    def test_a_row_with_no_login_at_all_is_left_alone(self):
        row = Attendance.objects.create(
            employee=self.employee, employee_name="Praveen S", role="Service engineer",
            department="Service", salary=27208, intime=None, outtime=None, status="Present",
        )
        self._run()
        row.refresh_from_db()
        self.assertIsNone(row.outtime)

    def test_somebody_elses_open_day_on_another_date_is_not_closed(self):
        today_row = self._row(self.today)
        old_row = self._row(self.yesterday, name="Karthik R")
        self._run()
        today_row.refresh_from_db()
        old_row.refresh_from_db()
        self.assertIsNotNone(today_row.outtime)
        self.assertIsNone(old_row.outtime)

    # ------------------------------------------------------------- the options

    def test_a_named_day_is_closed_at_its_own_1159_pm(self):
        row = self._row(self.yesterday)
        self._run("--date", self.yesterday.isoformat())
        row.refresh_from_db()
        closed = timezone.localtime(row.outtime)
        self.assertEqual(closed.date(), self.yesterday)
        self.assertEqual((closed.hour, closed.minute), (23, 59))

    def test_a_dry_run_writes_nothing(self):
        row = self._row(self.today)
        output = self._run("--dry-run")
        row.refresh_from_db()
        self.assertIsNone(row.outtime)
        self.assertIn("Dry run", output)

    def test_a_bad_date_is_refused_rather_than_guessed(self):
        with self.assertRaises(Exception):
            self._run("--date", "2026-13-45")
