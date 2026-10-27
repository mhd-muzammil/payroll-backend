"""The forgotten-session case, which is the one the office actually saw.

An engineer who does not tap Logout leaves a session open. Sixteen hours was the
sweep then, so at 08:12 the next morning last night's session was still open --
and because the lookup that guards Login is date-blind while the board asks for
sessions STARTED today, Login did nothing and the board read "Not on duty" all
day beside a live position and a climbing distance. The sweep now ends such a
duty at 12:00 AM; these still hold.

Run against the old start_duty and test_login_after_a_forgotten_session fails.
"""
import datetime

from django.test import TestCase
from django.urls import reverse
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from cases.models import DutySession, LocationPing
from employees.models import Employee


class ForgottenDutySessionTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="engineer1", password="x")
        self.employee = Employee.objects.create(
            employee_name="Prasanth", role="Service engineer", department="Service",
            branch="Salem", salary=30000, email="prasanth@test.local", user=self.user,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _start(self):
        return self.client.post("/api/tracking/start_duty/")

    @staticmethod
    def _yesterday_at(hour, minute=0):
        """A real time on yesterday's local calendar day."""
        day = timezone.localdate() - datetime.timedelta(days=1)
        return timezone.make_aware(
            datetime.datetime.combine(day, datetime.time(hour, minute)),
            timezone.get_current_timezone(),
        )

    def test_login_after_a_forgotten_session_opens_one_dated_today(self):
        stale = DutySession.objects.create(engineer=self.employee)
        DutySession.objects.filter(pk=stale.pk).update(started_at=self._yesterday_at(21, 55))

        response = self._start()
        self.assertEqual(response.status_code, 201)

        stale.refresh_from_db()
        self.assertIsNotNone(stale.ended_at, "last night's session must be closed")
        self.assertTrue(stale.auto_closed, "and marked as closed for them, not by them")

        today = timezone.localdate()
        opened_today = DutySession.objects.filter(
            engineer=self.employee, started_at__date=today, ended_at__isnull=True
        )
        self.assertEqual(
            opened_today.count(), 1,
            "the board asks for sessions started today -- there must be exactly one",
        )

    def test_a_forgotten_session_is_closed_at_its_last_fix_not_this_morning(self):
        started = self._yesterday_at(19, 10)
        last_fix = self._yesterday_at(21, 40)
        stale = DutySession.objects.create(engineer=self.employee)
        DutySession.objects.filter(pk=stale.pk).update(started_at=started)
        ping = LocationPing.objects.create(
            engineer=self.employee, latitude=11.67, longitude=78.14, accuracy=7,
        )
        LocationPing.objects.filter(pk=ping.pk).update(timestamp=last_fix)

        self._start()
        stale.refresh_from_db()
        self.assertAlmostEqual(
            (stale.ended_at - last_fix).total_seconds(), 0, delta=2,
            msg="closing it at this morning's clock would invent a night of duty",
        )

    def test_a_forgotten_duty_ends_by_midnight_not_at_a_fix_after_it(self):
        """Raathiri 12:00 AM-ku taanaa Logout: a fix the phone took after
        midnight is not that duty's, so it is not where the duty ends."""
        stale = DutySession.objects.create(engineer=self.employee)
        DutySession.objects.filter(pk=stale.pk).update(started_at=self._yesterday_at(19, 49))
        today_half_past = timezone.make_aware(
            datetime.datetime.combine(timezone.localdate(), datetime.time(0, 30)),
            timezone.get_current_timezone(),
        )
        for taken in (self._yesterday_at(21, 0), self._yesterday_at(23, 30), today_half_past):
            ping = LocationPing.objects.create(
                engineer=self.employee, latitude=11.67, longitude=78.14, accuracy=7,
            )
            LocationPing.objects.filter(pk=ping.pk).update(timestamp=taken)

        body = self.client.get("/api/tracking/duty/").json()
        self.assertFalse(body["on_duty"], "yesterday's duty is over")
        stale.refresh_from_db()
        self.assertEqual(stale.ended_at, self._yesterday_at(23, 30))
        self.assertTrue(stale.auto_closed)

    def test_a_logout_that_landed_first_is_kept(self):
        """The sweep read the duty as open; the engineer's Logout reached the
        row before the sweep's write did. The Logout stands."""
        from cases.views import TrackingViewSet

        stale = DutySession.objects.create(engineer=self.employee)
        DutySession.objects.filter(pk=stale.pk).update(started_at=self._yesterday_at(19, 0))
        stale.refresh_from_db()
        logout = self._yesterday_at(20, 15)
        DutySession.objects.filter(pk=stale.pk).update(ended_at=logout)

        TrackingViewSet._end_forgotten(stale)
        stale.refresh_from_db()
        self.assertEqual(stale.ended_at, logout)
        self.assertFalse(stale.auto_closed)

    def test_a_repaired_duty_ends_at_the_punch_out_as_their_own_logout(self):
        """repair_duty_day writes a missing duty from the attendance punch. The
        punch-out is the engineer's own Logout: an auto-closed duty's km run on
        to midnight, and the road home after it is not duty."""
        from io import StringIO

        from django.core.management import call_command

        from attendance.models import Attendance

        intime, outtime = self._yesterday_at(9, 0), self._yesterday_at(18, 0)
        Attendance.objects.create(
            employee=self.employee, employee_name="Prasanth", role="Service engineer",
            department="Service", salary=30000, intime=intime, outtime=outtime, status="Present",
        )
        day = timezone.localtime(intime).date().isoformat()
        call_command("repair_duty_day", "--date", day, "--apply", stdout=StringIO())

        session = DutySession.objects.get(engineer=self.employee)
        self.assertEqual((session.started_at, session.ended_at), (intime, outtime))
        self.assertFalse(session.auto_closed)

    def _check_in_start(self):
        """Duty asked for by a case Check In, as My Cases does when off duty."""
        return self.client.post("/api/tracking/start_duty/", {"from": "check_in"}, format="json")

    def _register(self, intime, status="Present"):
        from attendance.models import Attendance

        return Attendance.objects.create(
            employee=self.employee, employee_name="Prasanth", role="Service engineer",
            department="Service", salary=30000, intime=intime, status=status,
        )

    def test_a_check_in_with_no_login_today_starts_no_duty(self):
        """1 a: the day's duty begins with the Login, not a Check In -- so a call
        taken after the 12:00 AM auto Logout opens no next-day duty."""
        response = self._check_in_start()
        self.assertEqual(response.status_code, 409)
        self.assertTrue(response.json()["login_first"])
        self.assertFalse(DutySession.objects.filter(engineer=self.employee).exists())

    def test_last_nights_login_is_not_todays(self):
        self._register(self._yesterday_at(9, 0))
        self.assertEqual(self._check_in_start().status_code, 409)

    def test_the_10_30_absent_mark_is_not_a_login(self):
        from attendance import absence

        marked = timezone.make_aware(
            datetime.datetime.combine(timezone.localdate(), absence.DAY_MARK),
            timezone.get_current_timezone(),
        )
        self._register(marked, status="Absent")
        self.assertEqual(self._check_in_start().status_code, 409)

    def test_a_check_in_after_todays_login_starts_the_duty(self):
        self._register(timezone.now())
        self.assertEqual(self._check_in_start().status_code, 201)
        self.assertTrue(
            DutySession.objects.filter(engineer=self.employee, ended_at__isnull=True).exists()
        )

    def test_a_login_after_the_10_30_mark_counts(self):
        """In late: the Login is written onto the Absent row, which stays Absent."""
        self._register(timezone.now(), status="Absent")
        self.assertEqual(self._check_in_start().status_code, 201)

    def test_the_login_itself_starts_duty_as_before(self):
        """No "from": the Login and the repair start a duty, register or not."""
        self.assertEqual(self._start().status_code, 201)

    def test_twice_in_one_day_still_opens_only_one(self):
        self._start()
        self._start()
        self.assertEqual(
            DutySession.objects.filter(engineer=self.employee, ended_at__isnull=True).count(),
            1,
            "Login twice must not double-count the day",
        )

    def test_an_open_session_from_today_is_left_alone(self):
        first = self._start()
        self.assertEqual(first.status_code, 201)
        session = DutySession.objects.get(engineer=self.employee, ended_at__isnull=True)
        opened_at = session.started_at

        self._start()
        session.refresh_from_db()
        self.assertIsNone(session.ended_at)
        self.assertEqual(session.started_at, opened_at, "today's session must not be restarted")

    def test_the_state_it_reports_is_on_duty(self):
        stale = DutySession.objects.create(engineer=self.employee)
        DutySession.objects.filter(pk=stale.pk).update(started_at=self._yesterday_at(21, 55))

        body = self._start().json()
        self.assertTrue(body["on_duty"])
        self.assertIsNotNone(body["session_id"])
        self.assertNotEqual(body["session_id"], stale.pk, "a new session, not the old one")
