"""Not logged in by 10am is Absent.

Absent is pay -- a payslip's loss-of-pay days are the count of Absent rows --
so most of what is pinned here is who must NOT be marked: anybody with a row
already, anybody who could not have logged in, a Sunday, a day nobody logged
in at all, and any run before 10am.

And the half that is easy to miss: a Login after 10am is still accepted. The
app starts an engineer's duty, and their km, only once Login answers yes; a
refusal would cost them the whole day's trail. The day itself stays Absent.
"""
import datetime
from decimal import Decimal
from io import StringIO
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from attendance.models import Attendance, LeaveRequest
from authentication.models import User
from employees.models import Employee

WEDNESDAY = datetime.date(2026, 10, 7)
SUNDAY = datetime.date(2026, 10, 11)
SOMEWHERE = {"latitude": 13.08, "longitude": 80.27}


def _at(day, hour, minute=0, second=0):
    """A wall-clock time on `day`, in the local zone -- the way it is stored."""
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute, second)))


def _clock(moment):
    """Run as if the clock read `moment`."""
    return mock.patch("django.utils.timezone.now", return_value=moment)


class People:
    def _person(self, name, branch="Chennai", status="active", role="employee", account=True, **extra):
        user = None
        if account:
            user = User.objects.create_user(
                username=name.lower().replace(" ", "."), password="x", role=role
            )
        return Employee.objects.create(
            user=user, employee_name=name, role="Service engineer", department="Service",
            branch=branch, salary=Decimal("20000"), status=status,
            # Field engineers: the location check is not what these tests are about.
            flexible_location=True, **extra,
        )

    def _row(self, employee, day, status="Present", hour=9, link=True):
        return Attendance.objects.create(
            employee=employee if link else None,
            employee_name=employee.employee_name,
            role="Service engineer", department="Service", salary=Decimal("20000"),
            intime=_at(day, hour), status=status,
        )


class MarkAbsentTests(People, TestCase):
    def setUp(self):
        self.anand = self._person("Anand K")
        self.bala = self._person("Bala M", branch="Salem")
        self.chitra = self._person("Chitra R")
        self._row(self.chitra, WEDNESDAY, "Present", hour=9)

    def _run(self, moment=None, *args):
        out = StringIO()
        with _clock(moment or _at(WEDNESDAY, 10, 0, 5)):
            call_command("mark_absent_no_login", *args, stdout=out)
        return out.getvalue()

    # ------------------------------------------------------------ what it does

    def test_nobody_logged_in_by_ten_is_absent(self):
        self._run()
        for person in (self.anand, self.bala):
            row = Attendance.objects.get(employee=person, intime__date=WEDNESDAY)
            self.assertEqual(row.status, "Absent")
            self.assertIsNone(row.outtime)
            at = timezone.localtime(row.intime)
            self.assertEqual((at.date(), at.hour, at.minute), (WEDNESDAY, 0, 0), "the date, not an arrival")
            self.assertEqual(row.employee_name, person.employee_name)
            self.assertEqual(row.department, "Service")
            self.assertEqual(row.salary, person.salary)

    def test_hr_logs_in_like_anybody_else(self):
        hema = self._person("Hema S", role="hr")
        self._run()
        self.assertTrue(Attendance.objects.filter(employee=hema, status="Absent").exists())

    def test_leave_applied_for_does_not_stop_it(self):
        # The office's decision: a leave day is corrected by hand.
        LeaveRequest.objects.create(
            employee=self.anand, reason="family", start_date=WEDNESDAY,
            end_date=WEDNESDAY, status="Approved",
        )
        self._run()
        self.assertTrue(Attendance.objects.filter(employee=self.anand, status="Absent").exists())

    # -------------------------------------------------- what it must not touch

    def test_somebody_who_logged_in_is_left_alone(self):
        self._run()
        statuses = list(
            Attendance.objects.filter(employee=self.chitra, intime__date=WEDNESDAY)
            .values_list("status", flat=True)
        )
        self.assertEqual(statuses, ["Present"])

    def test_rows_the_office_marked_are_left_alone(self):
        self._row(self.anand, WEDNESDAY, "Leave", hour=0)
        # Saved belonging to nobody: recognised by the name.
        self._row(self.bala, WEDNESDAY, "Present", hour=0, link=False)
        self._run()
        self.assertFalse(Attendance.objects.filter(status="Absent").exists())

    def test_people_who_could_not_have_logged_in_are_not_marked(self):
        cannot = [
            self._person("Inactive One", status="inactive"),
            self._person("Relieved One", status="relieved"),
            self._person("No Account", account=False),
            self._person("Admin Account", role="admin"),
            self._person("Not Yet Joined", date_of_joining=WEDNESDAY + datetime.timedelta(days=1)),
        ]
        switched_off = self._person("Switched Off")
        switched_off.user.is_active = False
        switched_off.user.save()
        cannot.append(switched_off)

        self._run()
        for person in cannot:
            self.assertFalse(
                Attendance.objects.filter(employee=person).exists(), person.employee_name
            )

    def test_running_it_twice_marks_nobody_twice(self):
        self._run()
        first = Attendance.objects.count()
        self._run(_at(WEDNESDAY, 10, 30))
        self.assertEqual(Attendance.objects.count(), first)

    def test_other_days_are_untouched(self):
        self._run()
        self.assertFalse(Attendance.objects.exclude(intime__date=WEDNESDAY).exists())

    # ------------------------------------------------------------- the guards

    def test_never_before_ten(self):
        with self.assertRaises(CommandError) as caught:
            self._run(_at(WEDNESDAY, 9, 59, 59))
        self.assertIn("before 10:00", str(caught.exception))
        self.assertFalse(Attendance.objects.filter(status="Absent").exists())

    def test_never_on_a_sunday(self):
        out = self._run(_at(SUNDAY, 10, 30))
        self.assertIn("Sunday", out)
        self.assertFalse(Attendance.objects.filter(status="Absent").exists())

    def test_never_on_a_day_nobody_logged_in(self):
        # A holiday, or the app was down: a whole company of Absent rows is the
        # one mistake this must never make.
        Attendance.objects.all().delete()
        out = self._run()
        self.assertIn("nobody has logged in", out)
        self.assertFalse(Attendance.objects.exists())

    def test_marked_leave_alone_does_not_make_it_a_working_day(self):
        Attendance.objects.all().delete()
        self._row(self.anand, WEDNESDAY, "Leave", hour=0)
        self._run()
        self.assertEqual(Attendance.objects.count(), 1)

    def test_dry_run_writes_nothing(self):
        out = self._run(None, "--dry-run")
        self.assertIn("2 would be marked", out)
        self.assertFalse(Attendance.objects.filter(status="Absent").exists())


class LoginAfterTenTests(People, APITestCase):
    def setUp(self):
        self.anand = self._person("Anand K")
        self.client.force_authenticate(self.anand.user)

    def _post(self, what, moment):
        with _clock(moment):
            return self.client.post(f"/api/attendance/{what}/", SOMEWHERE, format="json")

    def test_a_login_after_ten_is_accepted_and_the_day_stays_absent(self):
        marked = self._row(self.anand, WEDNESDAY, "Absent", hour=0)
        response = self._post("check_in", _at(WEDNESDAY, 10, 42))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["id"], marked.id, "the same row, not a second one")
        self.assertEqual(response.data["status"], "Absent")
        marked.refresh_from_db()
        self.assertEqual(timezone.localtime(marked.intime).strftime("%H:%M"), "10:42")
        self.assertEqual(Attendance.objects.count(), 1)

    def test_a_second_login_is_still_refused(self):
        self._row(self.anand, WEDNESDAY, "Absent", hour=0)
        self._post("check_in", _at(WEDNESDAY, 10, 42))
        response = self._post("check_in", _at(WEDNESDAY, 11, 0))
        self.assertEqual(response.status_code, 400)
        self.assertIn("Already checked in", response.data["detail"])

    def test_logout_after_the_late_login_works(self):
        self._row(self.anand, WEDNESDAY, "Absent", hour=0)
        self._post("check_in", _at(WEDNESDAY, 10, 42))
        response = self._post("check_out", _at(WEDNESDAY, 18, 5))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "Absent")

    def test_logout_without_a_login_is_refused(self):
        marked = self._row(self.anand, WEDNESDAY, "Absent", hour=0)
        response = self._post("check_out", _at(WEDNESDAY, 18, 5))
        self.assertEqual(response.status_code, 400)
        marked.refresh_from_db()
        self.assertIsNone(marked.outtime, "no shift from midnight")

    def test_a_leave_day_keeps_its_leave(self):
        self._row(self.anand, WEDNESDAY, "Leave", hour=0)
        response = self._post("check_in", _at(WEDNESDAY, 9, 5))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["status"], "Leave")

    def test_an_ordinary_second_login_is_refused_as_before(self):
        first = self._post("check_in", _at(WEDNESDAY, 9, 5))
        self.assertEqual(first.status_code, 201, first.data)
        self.assertEqual(first.data["status"], "Present")
        second = self._post("check_in", _at(WEDNESDAY, 9, 6))
        self.assertEqual(second.status_code, 400)

    def test_a_present_row_the_office_entered_still_refuses(self):
        # Midnight Present is not a no-punch day; nothing about it changes.
        self._row(self.anand, WEDNESDAY, "Present", hour=0)
        response = self._post("check_in", _at(WEDNESDAY, 9, 5))
        self.assertEqual(response.status_code, 400)


class NotLoggedInListTests(People, APITestCase):
    def setUp(self):
        self.anand = self._person("Anand K")
        self.bala = self._person("Bala M", branch="Salem")
        self.chitra = self._person("Chitra R")
        self._row(self.chitra, WEDNESDAY, "Present", hour=9)
        self.office = User.objects.create_user(username="office", password="x", role="admin")

    def _get(self, user, moment):
        self.client.force_authenticate(user)
        with _clock(moment):
            return self.client.get("/api/attendance/not_logged_in/")

    def test_it_names_who_has_not_logged_in_by_branch(self):
        response = self._get(self.office, _at(WEDNESDAY, 9, 11))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["branches"], {"Chennai": ["Anand K"], "Salem": ["Bala M"]})
        self.assertIsNone(response.data["skip"])
        self.assertEqual(response.data["cutoff"], "10:00")
        self.assertEqual(response.data["date"], WEDNESDAY.isoformat())

    def test_it_is_the_list_the_ten_oclock_job_marks(self):
        listed = self._get(self.office, _at(WEDNESDAY, 9, 37)).data["branches"]
        with _clock(_at(WEDNESDAY, 10, 0, 5)):
            call_command("mark_absent_no_login", stdout=StringIO())
        marked = {}
        for row in Attendance.objects.filter(status="Absent").select_related("employee"):
            marked.setdefault(row.employee.branch, []).append(row.employee_name)
        self.assertEqual(listed, marked)

    def test_a_branch_office_sees_only_its_branch(self):
        salem_office = User.objects.create_user(
            username="salem.office", password="x", role="hr",
            allowed_sections={"attendance": ["Salem"]},
        )
        response = self._get(salem_office, _at(WEDNESDAY, 9, 11))
        self.assertEqual(response.data["branches"], {"Salem": ["Bala M"]})

    def test_nobody_to_chase_on_a_sunday(self):
        response = self._get(self.office, _at(SUNDAY, 9, 11))
        self.assertEqual(response.data["skip"], "Sunday")
        self.assertEqual(response.data["branches"], {})

    def test_nobody_to_chase_when_nobody_has_logged_in(self):
        Attendance.objects.all().delete()
        response = self._get(self.office, _at(WEDNESDAY, 9, 11))
        self.assertIn("nobody has logged in", response.data["skip"])
        self.assertEqual(response.data["branches"], {})

    def test_an_employee_cannot_read_it(self):
        response = self._get(self.anand.user, _at(WEDNESDAY, 9, 11))
        self.assertEqual(response.status_code, 403)
