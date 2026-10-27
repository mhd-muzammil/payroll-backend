"""Which straight lines count (cases/km_rule.py), one rule at a time.

The replay in test_km_replay.py shows the whole day never going down. These
pin each part of the rule on its own: the Login read off the session row with
phones whose clocks run fast or slow, Location switched off against Location
found off, the app stopped first, word that came too late, fixes sharing a
second, and the cutover that keeps closed days where they closed.
"""
import datetime
import itertools
import os
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.db.migrations.recorder import MigrationRecorder
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from cases import km_rule
from cases.models import DutySession, LocationPing, PhoneEvent
from cases.views import _moving_trail, _trail_km
from employees.models import Employee

DAY = datetime.date(2026, 10, 16)
NEW_RULE = {"KM_RULE_FROM": "2000-01-01T00:00:00+00:00"}
OLD_RULE = {"KM_RULE_FROM": "2100-01-01T00:00:00+00:00"}
DUTY_ON = {"KM_DUTY_FROM": "2000-01-01T00:00:00+00:00"}
KM = 0.009  # about a kilometre of latitude


def at(hour, minute=0, second=0, day=DAY):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute, second)))


class RuleTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.engineer = Employee.objects.create(
            employee_name="Rule Ravi",
            email="rule.ravi@example.com",
            role="Field Engineer",
            department="Service",
            branch="Chennai",
            salary=Decimal("20000"),
        )

    def setUp(self):
        patcher = mock.patch.dict(os.environ, NEW_RULE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def fix(self, taken, latitude, received=None, after_gap=False, accuracy=8):
        return LocationPing.objects.create(
            engineer=self.engineer,
            latitude=latitude,
            longitude=78.0,
            accuracy=accuracy,
            after_gap=after_gap,
            timestamp=taken,
            received_at=received if received is not None else taken,
        )

    def event(self, kind, moment, received=None, detail=""):
        return PhoneEvent.objects.create(
            engineer=self.engineer,
            kind=kind,
            at=moment,
            received_at=received if received is not None else moment,
            client_key=f"{kind}-{moment.isoformat()}",
            detail=detail,
        )

    def login(self, moment, until=None):
        return DutySession.objects.create(engineer=self.engineer, started_at=moment, ended_at=until)

    def km(self):
        return _trail_km(list(LocationPing.objects.filter(engineer=self.engineer)))


class LoginTests(RuleTestCase):
    """The trip to work is left out, read off the session row."""

    def _logout_and_back(self):
        self.login(at(9), until=at(13))
        self.fix(at(12, 59, 50), 11.0)

    def test_the_drive_between_logout_and_login_is_left_out(self):
        self._logout_and_back()
        self.login(at(15, 0, 2))
        self.fix(at(15, 0, 20), 11.0 + 20 * KM)
        self.fix(at(15, 5), 11.0 + 21 * KM)
        self.assertAlmostEqual(self.km(), 1.0, delta=0.05)

    def test_no_flag_is_needed(self):
        """The flag was often lost on the way; the session row never is."""
        self._logout_and_back()
        self.login(at(15, 0, 2))
        self.fix(at(15, 0, 20), 11.0 + 20 * KM)  # not flagged
        self.assertEqual(self.km(), 0.0)

    def test_a_phone_running_fast(self):
        """Three minutes fast: the last fix before the Login is stamped after
        it, but it arrived before it."""
        self.login(at(9), until=at(14, 59))
        self.fix(at(15, 2), 11.0, received=at(14, 59, 1))
        self.login(at(15, 0, 2))
        self.fix(at(15, 3, 20), 11.0 + 20 * KM, received=at(15, 0, 21))
        self.fix(at(15, 8, 20), 11.0 + 21 * KM, received=at(15, 5, 21))
        self.assertAlmostEqual(self.km(), 1.0, delta=0.05)

    def test_a_phone_running_slow(self):
        """Three minutes slow: the first fix after the Login is stamped before
        it, and found by when it arrived."""
        self.login(at(9), until=at(13))
        self.fix(at(12, 56, 50), 11.0, received=at(12, 59, 51))
        self.login(at(15, 0, 2))
        self.fix(at(14, 57, 20), 11.0 + 20 * KM, received=at(15, 0, 21))
        self.assertEqual(self.km(), 0.0)

    def test_a_clock_more_than_five_minutes_slow_is_not_recognised(self):
        """The bound is CLOCK_SKEW: past it a fix is read as taken before the
        Login, which is what pings.py would make of it too."""
        self.login(at(9), until=at(13))
        self.fix(at(12, 50), 11.0, received=at(12, 59, 51))
        self.login(at(15, 0, 2))
        self.fix(at(14, 54), 11.0 + 20 * KM, received=at(15, 0, 21))
        self.assertGreater(self.km(), 19)

    def test_a_row_from_before_arrivals_were_recorded(self):
        """No received_at: taken within two minutes of the Login stands in."""
        self.login(at(9), until=at(13))
        self.fix(at(12, 59, 50), 11.0)
        LocationPing.objects.filter(engineer=self.engineer).update(received_at=None)
        self.login(at(15, 0, 2))
        late = self.fix(at(15, 0, 20), 11.0 + 20 * KM)
        LocationPing.objects.filter(pk=late.pk).update(received_at=None)
        self.assertEqual(self.km(), 0.0)

    def test_the_mornings_held_fixes_delivered_after_the_login_still_count(self):
        """Taken on duty in the morning, sent after the afternoon's Login: the
        morning's road, not the trip to work. Only the line into the first fix
        after the Login is left out."""
        self.login(at(9), until=at(13))
        self.fix(at(10), 11.0)
        for step in (1, 2, 3):
            self.fix(at(10, step * 10), 11.0 + step * KM, received=at(15, 5))
        self.login(at(15, 0, 2))
        self.fix(at(15, 0, 20), 11.0 + 20 * KM, received=at(15, 0, 21))
        self.assertAlmostEqual(self.km(), 3.0, delta=0.05)

    def test_every_login_of_the_day_counts(self):
        """Two Logouts and Logins: both drives are left out."""
        self.login(at(9), until=at(11))
        self.fix(at(10), 11.0)
        self.fix(at(10, 30), 11.0 + KM)
        self.login(at(12), until=at(14))
        self.fix(at(12, 5), 11.0 + 10 * KM)
        self.fix(at(12, 30), 11.0 + 11 * KM)
        self.login(at(15))
        self.fix(at(15, 5), 11.0 + 30 * KM)
        self.fix(at(15, 30), 11.0 + 31 * KM)
        self.assertAlmostEqual(self.km(), 3.0, delta=0.05)

    def test_a_fix_taken_before_the_logout_is_never_the_first_after_the_next_login(self):
        """Held from 2:56 and delivered after a Logout at 2:59 and a Login at
        3:00, within the minutes a slow clock is allowed: still the morning's."""
        self.login(at(9), until=at(14, 59))
        self.fix(at(14, 50), 11.0)
        self.fix(at(14, 56), 11.0 + 3 * KM, received=at(15, 1))
        self.fix(at(14, 58), 11.0 + 4 * KM)
        self.login(at(15, 0, 2))
        self.assertAlmostEqual(self.km(), 4.0, delta=0.05)

    def test_with_no_fix_where_they_logged_in_the_whole_line_is_left_out(self):
        """Nothing says where the Login was, so the line from the Logout to
        the next fix may be the drive to it, and none of it is counted."""
        self._logout_and_back()
        self.login(at(14))
        self.fix(at(15), 11.0 + 30 * KM)
        self.fix(at(15, 5), 11.0 + 31 * KM)
        self.assertAlmostEqual(self.km(), 1.0, delta=0.05)

    def test_a_login_where_the_logout_was_counts_from_the_first_fix_after_it(self):
        """The first fix after the Login is a few metres from the last before
        the Logout, so the wander filter keeps neither of the lines either
        side of it: the line from the Logout to the far fix is all there is.
        Only the few metres to the Login are left out of it."""
        self._logout_and_back()
        self.login(at(14))
        self.fix(at(14, 0, 30), 11.00005)
        self.fix(at(15), 11.0 + 30 * KM)
        self.assertAlmostEqual(self.km(), 30.0, delta=0.05)

    def test_an_inaccurate_fix_after_the_login_does_not_say_where_it_was(self):
        self._logout_and_back()
        self.login(at(14))
        self.fix(at(14, 0, 30), 11.0, accuracy=400)
        self.fix(at(15), 11.0 + 30 * KM)
        self.assertEqual(self.km(), 0.0)

    def test_location_switched_off_after_such_a_login_is_still_left_out(self):
        self._logout_and_back()
        self.login(at(14))
        self.fix(at(14, 0, 30), 11.00005)
        self.event("gps_off", at(14, 1), detail="switched")
        self.fix(at(15), 11.0 + 30 * KM, after_gap=True)
        self.fix(at(15, 5), 11.0 + 31 * KM)
        self.assertAlmostEqual(self.km(), 1.0, delta=0.05)


class LocationOffTests(RuleTestCase):
    """Only Location the phone SAW being switched off, said in time."""

    def _the_stretch(self):
        self.login(at(9))
        self.fix(at(9, 30), 11.0)
        self.fix(at(10), 11.0 + KM)
        self.fix(at(10, 40), 11.0 + 8 * KM, after_gap=True)
        self.fix(at(10, 45), 11.0 + 9 * KM)

    def test_switched_off_is_left_out(self):
        self._the_stretch()
        self.event("gps_off", at(10, 1), detail="switched")
        self.assertAlmostEqual(self.km(), 2.0, delta=0.05)

    def test_found_off_is_counted(self):
        """Found off when the app looked: the app may have been closed for the
        hour before, with Location on."""
        self._the_stretch()
        self.event("gps_off", at(10, 1), detail="found_off")
        self.assertAlmostEqual(self.km(), 9.0, delta=0.05)

    def test_switched_off_after_the_phone_stopped_the_app_is_counted(self):
        self._the_stretch()
        self.event("tracker_stopped", at(10, 0, 30))
        self.event("gps_off", at(10, 1), detail="switched")
        self.assertAlmostEqual(self.km(), 9.0, delta=0.05)

    def test_the_app_stopped_after_the_switch_off_changes_nothing(self):
        self._the_stretch()
        self.event("gps_off", at(10, 1), detail="switched")
        self.event("tracker_stopped", at(10, 20))
        self.assertAlmostEqual(self.km(), 2.0, delta=0.05)

    def test_word_that_arrived_after_the_fix_is_too_late(self):
        """By then the line had been counted on somebody's screen."""
        self._the_stretch()
        self.event("gps_off", at(10, 1), received=at(10, 41), detail="switched")
        self.assertAlmostEqual(self.km(), 9.0, delta=0.05)

    def test_a_later_fix_that_arrived_first_keeps_it_counted(self):
        """The fix after the stretch was not the first to arrive: a later
        one had already joined the line across it."""
        self.login(at(9))
        self.fix(at(9, 30), 11.0)
        self.fix(at(10), 11.0 + KM)
        self.event("gps_off", at(10, 1), detail="switched")
        self.fix(at(10, 40), 11.0 + 8 * KM, received=at(10, 50), after_gap=True)
        self.fix(at(10, 45), 11.0 + 9 * KM, received=at(10, 45))
        self.assertAlmostEqual(self.km(), 9.0, delta=0.05)

    def test_a_fix_arriving_in_the_same_batch_as_the_first_is_fine(self):
        self.login(at(9))
        self.fix(at(9, 30), 11.0)
        self.fix(at(10), 11.0 + KM)
        self.event("gps_off", at(10, 1), detail="switched")
        self.fix(at(10, 40), 11.0 + 8 * KM, received=at(10, 46), after_gap=True)
        self.fix(at(10, 45), 11.0 + 9 * KM, received=at(10, 46))
        self.assertAlmostEqual(self.km(), 2.0, delta=0.05)

    def test_a_switch_off_at_the_very_moment_of_a_fix_leaves_out_one_line(self):
        """The stretch starts after the fix it coincides with: the line into
        that fix was driven with Location on."""
        self.login(at(9))
        self.fix(at(9, 30), 11.0)
        self.fix(at(10), 11.0 + KM)
        self.event("gps_off", at(10), detail="switched")
        self.fix(at(10, 40), 11.0 + 8 * KM, after_gap=True)
        self.assertAlmostEqual(self.km(), 1.0, delta=0.05)

    def test_another_engineers_switch_off_is_not_this_ones(self):
        self._the_stretch()
        other = Employee.objects.create(
            employee_name="Other Oli", email="oli@example.com", role="Field Engineer",
            department="Service", branch="Chennai", salary=Decimal("20000"),
        )
        PhoneEvent.objects.create(
            engineer=other, kind="gps_off", at=at(10, 1), received_at=at(10, 1),
            client_key="other", detail="switched",
        )
        self.assertAlmostEqual(self.km(), 9.0, delta=0.05)


class OrderTests(RuleTestCase):
    """Fixes sharing a second are read in one order, whatever order they come in."""

    def test_ties_give_one_answer_whatever_the_input_order(self):
        self.login(at(9))
        burst = at(14, 17, 43)
        fixes = [
            self.fix(at(14), 11.0),
            self.fix(burst, 11.0 + 3 * KM),
            self.fix(burst, 11.0 + 1 * KM),
            self.fix(burst, 11.0 + 4 * KM),
            self.fix(at(14, 20), 11.0 + 6 * KM),
        ]
        answers = {_trail_km(list(order)) for order in itertools.permutations(fixes)}
        self.assertEqual(len(answers), 1, answers)
        trails = {
            tuple(p.id for p in _moving_trail(list(order))) for order in itertools.permutations(fixes)
        }
        self.assertEqual(len(trails), 1, trails)

    def test_the_same_rows_read_twice_agree(self):
        self.login(at(9))
        burst = at(14, 17, 43)
        for latitude in (11.0 + 2 * KM, 11.0, 11.0 + 5 * KM):
            self.fix(burst, latitude)
        newest_first = list(LocationPing.objects.order_by("-timestamp", "-id"))
        oldest_first = list(LocationPing.objects.order_by("timestamp", "id"))
        self.assertEqual(_trail_km(newest_first), _trail_km(oldest_first))


class CutoverTests(RuleTestCase):
    """Closed days keep the number they closed on."""

    def _restart_day(self, day=DAY):
        """A restart flag, the kind the old app set: the line into it went."""
        self.fix(at(9, 30, day=day), 11.0)
        self.fix(at(10, day=day), 11.0 + KM)
        self.fix(at(10, 40, day=day), 11.0 + 8 * KM, after_gap=True)
        self.fix(at(10, 45, day=day), 11.0 + 9 * KM)

    def test_a_day_that_arrived_before_the_cutover_reads_the_old_way(self):
        self._restart_day()
        with mock.patch.dict(os.environ, OLD_RULE):
            self.assertAlmostEqual(self.km(), 2.0, delta=0.05)

    def test_the_same_day_after_it_reads_the_new_way(self):
        self._restart_day()
        self.assertAlmostEqual(self.km(), 9.0, delta=0.05)

    def test_a_day_across_the_cutover_reads_each_line_by_when_it_arrived(self):
        """The morning's flag still takes its line; the afternoon's does not."""
        self._restart_day()
        self.fix(at(14), 11.0 + 10 * KM)
        self.fix(at(14, 30), 11.0 + 20 * KM, after_gap=True)
        with mock.patch.dict(os.environ, {"KM_RULE_FROM": at(12).isoformat()}):
            self.assertAlmostEqual(self.km(), 2.0 + 1.0 + 10.0, delta=0.05)

    def test_a_line_whose_later_fix_arrived_after_the_cutover_reads_the_new_way(self):
        """The line did not exist until its second fix arrived, after the new
        rule began, so the old rule's flag between them does not take it."""
        self.fix(at(11), 11.0)
        self.fix(at(11, 15), 11.0 + 2 * KM, after_gap=True, accuracy=500)
        self.fix(at(11, 30), 11.0 + 5 * KM, received=at(12, 30))
        with mock.patch.dict(os.environ, {"KM_RULE_FROM": at(12).isoformat()}):
            self.assertAlmostEqual(self.km(), 5.0, delta=0.05)

    def test_a_flag_that_arrived_after_the_cutover_is_no_evidence_for_the_old_rule(self):
        """Both fixes of the line were here before the new rule began, and it
        was counted; a flag that turns up after cannot take it back."""
        self.fix(at(11), 11.0)
        self.fix(at(11, 15), 11.0 + 2 * KM, after_gap=True, accuracy=500, received=at(12, 30))
        self.fix(at(11, 30), 11.0 + 5 * KM)
        with mock.patch.dict(os.environ, {"KM_RULE_FROM": at(12).isoformat()}):
            self.assertAlmostEqual(self.km(), 5.0, delta=0.05)

    def test_a_closed_day_still_reads_its_flags_the_old_way(self):
        """The same three fixes, all here before the cutover: as it closed."""
        self.fix(at(11), 11.0)
        self.fix(at(11, 15), 11.0 + 2 * KM, after_gap=True, accuracy=500)
        self.fix(at(11, 30), 11.0 + 5 * KM)
        with mock.patch.dict(os.environ, {"KM_RULE_FROM": at(12).isoformat()}):
            self.assertEqual(self.km(), 0.0)

    def test_without_the_setting_the_cutover_is_when_migration_0019_ran(self):
        applied = MigrationRecorder.Migration.objects.get(
            app="cases", name=km_rule.RULE_MIGRATION[1]
        ).applied
        with mock.patch.dict(os.environ):
            os.environ.pop("KM_RULE_FROM", None)
            self.assertEqual(km_rule.rule_cutover(), applied)
            # And a day whose fixes all arrived before it reads the old way,
            # through the board's own endpoint.
            self._restart_day()
            office = User.objects.create_user(username="cutover-office", password="x", role="admin")
            client = APIClient()
            client.force_authenticate(office)
            row = next(
                r
                for r in client.get("/api/tracking/roster/", {"date": DAY.isoformat()}).data
                if r["engineer_id"] == self.engineer.id
            )
        self.assertAlmostEqual(row["distance_km"], 2.0, delta=0.05)

    def test_the_setting_reads_a_date_or_a_time(self):
        with mock.patch.dict(os.environ, {"KM_RULE_FROM": "2026-10-20"}):
            self.assertEqual(km_rule.rule_cutover(), at(0, day=datetime.date(2026, 10, 20)))
        with mock.patch.dict(os.environ, {"KM_RULE_FROM": "2026-10-20T18:30:00+05:30"}):
            self.assertEqual(km_rule.rule_cutover(), at(18, 30, day=datetime.date(2026, 10, 20)))

    def test_a_setting_that_is_not_a_date_falls_back_to_the_migration(self):
        """A typo must not quietly move every closed day onto the new rule."""
        with mock.patch.dict(os.environ, {"KM_RULE_FROM": "next tuesday"}):
            with self.assertLogs("cases.km_rule", level="WARNING"):
                cutover = km_rule.rule_cutover()
        self.assertEqual(
            cutover,
            MigrationRecorder.Migration.objects.get(
                app="cases", name=km_rule.RULE_MIGRATION[1]
            ).applied,
        )


class DutyTests(RuleTestCase):
    """Km only on duty: Login to Logout, and no further than the 12:00 AM after
    the day of the Login ("2 a", "3 b")."""

    def setUp(self):
        super().setUp()
        patcher = mock.patch.dict(os.environ, DUTY_ON)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_the_road_after_a_logout_is_not_counted(self):
        """The road home after the Logout, which a phone still tracking used
        to add to the day (Prashanth K, 10 Oct: 90.6 km after a Logout)."""
        self.login(at(9), until=at(13))
        self.fix(at(10), 11.0)
        self.fix(at(12), 11.0 + 5 * KM)
        self.fix(at(12, 59), 11.0 + 6 * KM)
        self.fix(at(14), 11.0 + 16 * KM)
        self.fix(at(15), 11.0 + 26 * KM)
        self.assertAlmostEqual(self.km(), 6.0, delta=0.05)

    def test_a_day_with_no_login_counts_nothing(self):
        self.fix(at(10), 11.0)
        self.fix(at(11), 11.0 + 5 * KM)
        self.assertEqual(self.km(), 0.0)

    def test_the_drive_to_a_login_is_still_left_out(self):
        self.fix(at(8), 11.0)
        self.fix(at(8, 30), 11.0 + 10 * KM)
        self.login(at(9))
        self.fix(at(9, 5), 11.0 + 11 * KM)
        self.fix(at(9, 30), 11.0 + 12 * KM)
        self.assertAlmostEqual(self.km(), 1.0, delta=0.05)

    def test_a_logout_and_a_login_again_count_both_duties_and_not_between(self):
        self.login(at(9), until=at(12))
        self.fix(at(10), 11.0)
        self.fix(at(11), 11.0 + KM)
        self.fix(at(12, 30), 11.0 + 10 * KM)
        self.login(at(13))
        self.fix(at(13, 5), 11.0 + 11 * KM)
        self.fix(at(14), 11.0 + 12 * KM)
        self.assertAlmostEqual(self.km(), 2.0, delta=0.05)

    def test_a_duty_left_open_stops_counting_at_midnight(self):
        """Prashanth K, Saturday 7:49 PM: the duty ran on into Sunday, and
        Sunday read 53 km. It ends at 12:00 AM now, swept or not."""
        self.login(at(20))
        sunday = DAY + datetime.timedelta(days=1)
        self.fix(at(21), 11.0)
        self.fix(at(22), 11.0 + 5 * KM)
        self.fix(at(1, day=sunday), 11.0 + 10 * KM)
        self.fix(at(2, day=sunday), 11.0 + 15 * KM)
        self.assertAlmostEqual(self.km(), 5.0, delta=0.05)

    def test_a_phone_running_fast_keeps_its_last_line_at_the_logout(self):
        """Stamped two minutes after the Logout, sent before it: it is the
        duty's. A fix taken after the Logout is not."""
        self.login(at(9), until=at(13))
        self.fix(at(12), 11.0)
        self.fix(at(13, 2), 11.0 + 2 * KM, received=at(12, 59, 30))
        self.fix(at(13, 3), 11.0 + 3 * KM, received=at(13, 3))
        self.assertAlmostEqual(self.km(), 2.0, delta=0.05)

    def test_a_fix_held_on_the_phone_counts_on_a_duty_the_sweep_ended(self):
        """The sweep ended it at its last fix it had then, 12:00; the
        afternoon, held on the phone and sent the next morning, is still that
        duty's. After midnight is not."""
        session = self.login(at(9))
        self.fix(at(9, 30), 11.0)
        self.fix(at(12), 11.0 + 5 * KM)
        DutySession.objects.filter(pk=session.pk).update(ended_at=at(12), auto_closed=True)
        morning = at(9, day=DAY + datetime.timedelta(days=1))
        self.fix(at(15), 11.0 + 8 * KM, received=morning)
        self.fix(at(0, 30, day=DAY + datetime.timedelta(days=1)), 11.0 + 9 * KM, received=morning)
        self.assertAlmostEqual(self.km(), 8.0, delta=0.05)

    def test_a_duty_the_sweep_ended_reaches_a_trail_read_on_its_own(self):
        """A trail that begins after that Logout -- /path for an afternoon --
        still finds the duty, since its window runs to midnight."""
        session = self.login(at(9))
        DutySession.objects.filter(pk=session.pk).update(ended_at=at(9, 30), auto_closed=True)
        morning = at(9, day=DAY + datetime.timedelta(days=1))
        afternoon = [
            self.fix(at(15), 11.0, received=morning),
            self.fix(at(16), 11.0 + 3 * KM, received=morning),
        ]
        self.assertAlmostEqual(_trail_km(afternoon), 3.0, delta=0.05)

    def test_the_midnight_sweep_takes_back_nothing_counted_while_the_duty_ran(self):
        from cases.views import TrackingViewSet

        self.login(at(18))
        self.fix(at(18, 30), 11.0)
        self.fix(at(20), 11.0 + 4 * KM)
        self.fix(at(21, 30), 11.0 + 7 * KM)
        while_running = self.km()
        with mock.patch(
            "django.utils.timezone.now",
            return_value=at(8, day=DAY + datetime.timedelta(days=1)),
        ):
            TrackingViewSet._close_forgotten_sessions()
        session = DutySession.objects.get(engineer=self.engineer)
        self.assertEqual(session.ended_at, at(21, 30), "at its last fix before midnight")
        self.assertTrue(session.auto_closed)
        self.assertAlmostEqual(while_running, 7.0, delta=0.05)
        self.assertEqual(self.km(), while_running)

    def test_a_line_that_arrived_before_the_cutover_is_read_as_before(self):
        """The deploy at noon: the morning's lines, here before it, count as
        they did; the afternoon's, with no duty, do not."""
        self.fix(at(9), 11.0)
        self.fix(at(10), 11.0 + 3 * KM)
        self.fix(at(13), 11.0 + 5 * KM)
        with mock.patch.dict(os.environ, {"KM_DUTY_FROM": at(12).isoformat()}):
            self.assertAlmostEqual(self.km(), 3.0, delta=0.05)

    def test_a_fix_taken_before_the_cutover_but_sent_after_is_read_as_before(self):
        self.fix(at(10), 11.0)
        self.fix(at(11), 11.0 + 2 * KM, received=at(13))
        with mock.patch.dict(os.environ, {"KM_DUTY_FROM": at(12).isoformat()}):
            self.assertAlmostEqual(self.km(), 2.0, delta=0.05)

    def test_without_the_setting_the_duty_cutover_is_when_migration_0024_ran(self):
        applied = MigrationRecorder.Migration.objects.get(
            app="cases", name=km_rule.DUTY_MIGRATION[1]
        ).applied
        with mock.patch.dict(os.environ):
            os.environ.pop("KM_DUTY_FROM", None)
            self.assertEqual(km_rule.duty_cutover(), applied)
            # A day whose fixes all arrived before the deploy reads as it did,
            # duty or none.
            self.fix(at(10), 11.0)
            self.fix(at(11), 11.0 + 2 * KM)
            self.assertAlmostEqual(self.km(), 2.0, delta=0.05)

    def test_a_duty_setting_that_is_not_a_date_falls_back_to_the_migration(self):
        with mock.patch.dict(os.environ, {"KM_DUTY_FROM": "next tuesday"}):
            with self.assertLogs("cases.km_rule", level="WARNING"):
                cutover = km_rule.duty_cutover()
        self.assertEqual(
            cutover,
            MigrationRecorder.Migration.objects.get(
                app="cases", name=km_rule.DUTY_MIGRATION[1]
            ).applied,
        )

    def test_the_board_reads_only_the_duty(self):
        """Through the roster, the number the office reads."""
        self.login(at(9), until=at(13))
        self.fix(at(10), 11.0)
        self.fix(at(12), 11.0 + 5 * KM)
        self.fix(at(15), 11.0 + 25 * KM)
        office = User.objects.create_user(username="duty-office", password="x", role="admin")
        client = APIClient()
        client.force_authenticate(office)
        row = next(
            r
            for r in client.get("/api/tracking/roster/", {"date": DAY.isoformat()}).data
            if r["engineer_id"] == self.engineer.id
        )
        self.assertAlmostEqual(row["distance_km"], 5.0, delta=0.05)


class QueryCountTests(TestCase):
    """The board's km costs the same queries for three engineers as for
    thirty: the Logins and events are read once for all of them."""

    @classmethod
    def setUpTestData(cls):
        cls.office = User.objects.create_user(username="count-office", password="x", role="admin")

    def setUp(self):
        # Both cutovers from the environment: neither costs a lookup of its
        # migration on the first read in the process.
        patcher = mock.patch.dict(os.environ, {**NEW_RULE, **DUTY_ON})
        patcher.start()
        self.addCleanup(patcher.stop)
        self.client = APIClient()
        self.client.force_authenticate(self.office)

    def _engineers(self, count, start):
        for n in range(start, start + count):
            engineer = Employee.objects.create(
                employee_name=f"Counted {n:02d}", email=f"counted{n}@example.com",
                role="Field Engineer", department="Service", branch="Chennai",
                salary=Decimal("20000"),
            )
            DutySession.objects.create(engineer=engineer, started_at=at(9), ended_at=at(18))
            for step in range(3):
                LocationPing.objects.create(
                    engineer=engineer, latitude=11.0 + step * KM, longitude=78.0, accuracy=8,
                    timestamp=at(10, step * 10), received_at=at(10, step * 10),
                )
            PhoneEvent.objects.create(
                engineer=engineer, kind="gps_off", at=at(10, 5), received_at=at(10, 5),
                client_key=f"off-{n}", detail="switched",
            )

    def _roster_queries(self):
        with CaptureQueriesContext(connection) as queries:
            response = self.client.get("/api/tracking/roster/", {"date": DAY.isoformat()})
        self.assertEqual(response.status_code, 200)
        return len(queries)

    def test_the_roster_does_not_grow_a_query_per_engineer(self):
        self._engineers(3, 0)
        few = self._roster_queries()
        self._engineers(9, 3)
        many = self._roster_queries()
        self.assertEqual(few, many)

    def test_live_grows_only_by_the_trail_it_always_read(self):
        """One trail query per engineer on duty, as before; nothing more."""
        def live_queries():
            with mock.patch("django.utils.timezone.now", return_value=at(12)):
                with CaptureQueriesContext(connection) as queries:
                    response = self.client.get("/api/tracking/live/")
            self.assertEqual(response.status_code, 200)
            return len(queries), len(response.data)

        self._engineers(3, 0)
        DutySession.objects.update(ended_at=None)
        few, few_rows = live_queries()
        self._engineers(9, 3)
        DutySession.objects.update(ended_at=None)
        many, many_rows = live_queries()
        self.assertEqual((few_rows, many_rows), (3, 12))
        self.assertEqual(many - few, many_rows - few_rows)
