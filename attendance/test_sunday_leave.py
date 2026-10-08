"""Sunday is marked Leave for the whole company.

What these tests are really for is everything that must NOT be overwritten.
This command writes a row for every single employee at once, so the ways it
could be wrong are the expensive kind: a Sunday somebody actually worked
turned into a holiday, two rows for one person, or -- the worst -- a cron line
typed one field out of place marking a Monday as leave for everybody.
"""
import datetime
from io import StringIO

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone

from attendance.models import Attendance
from employees.models import Employee


def _at(day, hour, minute=0):
    """A wall-clock time on `day`, in the local zone -- the way it is stored."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


def _last_sunday(day):
    return day - datetime.timedelta(days=(day.weekday() - 6) % 7)


class SundayLeaveTests(TestCase):
    def setUp(self):
        self.sunday = _last_sunday(timezone.localdate())
        self.monday = self.sunday + datetime.timedelta(days=1)
        self.praveen = self._employee("Praveen S")
        self.sarwana = self._employee("Sarwana K")
        self.gone = self._employee("Old Hand", status="relieved")

    def _employee(self, name, status="active", **extra):
        return Employee.objects.create(
            employee_name=name, role="Service engineer", department="Service",
            branch="Chennai", salary=27208, status=status, **extra,
        )

    def _row(self, employee, day, status="Present", hour=9, outtime=None, link=True):
        return Attendance.objects.create(
            employee=employee if link else None,
            employee_name=employee.employee_name,
            role="Service engineer", department="Service", salary=27208,
            intime=_at(day, hour) if hour is not None else None,
            outtime=outtime, status=status,
        )

    def _run(self, *args):
        out = StringIO()
        call_command("mark_sunday_leave", *args, stdout=out)
        return out.getvalue()

    # ------------------------------------------------------------ what it does

    def test_everybody_gets_leave_on_sunday(self):
        self._run()
        rows = Attendance.objects.filter(intime__date=self.sunday)
        self.assertEqual(rows.count(), 2, "both people on the books, and only them")
        for row in rows:
            self.assertEqual(row.status, "Leave")
            self.assertIsNone(row.outtime, "a day off has no logout")
            self.assertEqual(timezone.localtime(row.intime).date(), self.sunday)

    def test_the_row_carries_the_date_and_nothing_else(self):
        # Midnight is the convention for a day with no punch: it is where every
        # list on the page reads the date from, and it must not read as a time
        # somebody arrived.
        self._run()
        row = Attendance.objects.get(employee=self.praveen, intime__date=self.sunday)
        marked = timezone.localtime(row.intime)
        self.assertEqual((marked.hour, marked.minute), (0, 0))

    def test_it_copies_the_employee_so_the_row_belongs_to_somebody(self):
        self._run()
        row = Attendance.objects.get(employee=self.praveen, intime__date=self.sunday)
        self.assertEqual(row.employee_name, "Praveen S")
        self.assertEqual(row.department, "Service")
        self.assertEqual(row.salary, self.praveen.salary)

    # -------------------------------------------------- what it must not touch

    def test_a_sunday_somebody_worked_is_left_alone(self):
        worked = self._row(self.praveen, self.sunday, "Present", hour=9,
                           outtime=_at(self.sunday, 18))
        self._run()
        worked.refresh_from_db()
        self.assertEqual(worked.status, "Present", "a real day is not a holiday")
        self.assertEqual(
            Attendance.objects.filter(employee=self.praveen, intime__date=self.sunday).count(),
            1, "and they do not get a second row",
        )

    def test_an_unlinked_office_row_is_recognised_by_name(self):
        # A row the office marked by hand can belong to nobody. Matching only
        # on the id would hand that person a second row for the same day.
        self._row(self.sarwana, self.sunday, "Present", link=False)
        self._run()
        self.assertEqual(
            Attendance.objects.filter(intime__date=self.sunday).count(),
            2, "Praveen's new row and Sarwana's existing one",
        )
        self.assertFalse(
            Attendance.objects.filter(employee=self.sarwana, status="Leave").exists()
        )

    def test_somebody_relieved_is_not_marked(self):
        self._run()
        self.assertFalse(Attendance.objects.filter(employee=self.gone).exists())

    def test_running_it_twice_changes_nothing(self):
        self._run()
        first = Attendance.objects.count()
        self._run()
        self.assertEqual(Attendance.objects.count(), first)

    def test_other_days_are_untouched(self):
        self._run()
        self.assertFalse(
            Attendance.objects.exclude(intime__date=self.sunday).exists(),
            "one day, and only the one asked for",
        )

    # ------------------------------------------------------------- the guards

    def test_it_refuses_a_day_that_is_not_a_sunday(self):
        with self.assertRaises(CommandError) as caught:
            self._run("--date", self.monday.isoformat())
        self.assertIn("not a Sunday", str(caught.exception))
        self.assertFalse(Attendance.objects.exists(), "and it wrote nothing")

    def test_dry_run_writes_nothing(self):
        output = self._run("--dry-run")
        self.assertIn("would mark 2", output)
        self.assertFalse(Attendance.objects.exists())

    def test_somebody_who_had_not_joined_is_skipped(self):
        joined_later = self._employee(
            "New Starter", date_of_joining=self.sunday + datetime.timedelta(days=1),
        )
        self._run()
        self.assertFalse(
            Attendance.objects.filter(employee=joined_later).exists(),
            "no attendance for a day they were not employed",
        )

    def test_since_marks_every_sunday_in_the_stretch(self):
        three_weeks = self.sunday - datetime.timedelta(days=14)
        self._run("--since", three_weeks.isoformat())
        marked = {
            timezone.localtime(row.intime).date()
            for row in Attendance.objects.filter(employee=self.praveen)
        }
        self.assertEqual(
            marked,
            {three_weeks, three_weeks + datetime.timedelta(days=7), self.sunday},
        )

    def test_date_and_since_together_are_refused(self):
        with self.assertRaises(CommandError):
            self._run("--date", self.sunday.isoformat(), "--since", self.sunday.isoformat())
