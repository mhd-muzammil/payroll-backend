"""The forgotten-session case, which is the one the office actually saw.

An engineer who does not tap Logout leaves a session open. Sixteen hours is the
sweep, so at 08:12 the next morning last night's session is still open -- and
because the lookup that guards Login is date-blind while the board asks for
sessions STARTED today, Login did nothing and the board read "Not on duty" all
day beside a live position and a climbing distance.

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

    def test_login_after_a_forgotten_session_opens_one_dated_today(self):
        yesterday = timezone.now() - datetime.timedelta(hours=14)
        stale = DutySession.objects.create(engineer=self.employee)
        DutySession.objects.filter(pk=stale.pk).update(started_at=yesterday)

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
        started = timezone.now() - datetime.timedelta(hours=14)
        last_fix = timezone.now() - datetime.timedelta(hours=12)
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
        yesterday = timezone.now() - datetime.timedelta(hours=14)
        stale = DutySession.objects.create(engineer=self.employee)
        DutySession.objects.filter(pk=stale.pk).update(started_at=yesterday)

        body = self._start().json()
        self.assertTrue(body["on_duty"])
        self.assertIsNotNone(body["session_id"])
        self.assertNotEqual(body["session_id"], stale.pk, "a new session, not the old one")
