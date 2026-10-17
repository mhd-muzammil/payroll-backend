"""The day rail, now that the phone says what happened to it.

GPS off and on, Net off and on, the phone stopping the app and the app
tracking again go on the rail in the phone's own words, and the entries that
used to guess -- "Not tracked", "No network", "Location off" -- give way to
them or say only what the fixes can know.
"""
import datetime
import os
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from authentication.models import User
from cases.models import DutySession, LocationPing, PhoneEvent
from employees.models import Employee

DAY = datetime.date(2026, 10, 16)
NEW_RULE = {"KM_RULE_FROM": "2000-01-01T00:00:00+00:00"}
OLD_RULE = {"KM_RULE_FROM": "2100-01-01T00:00:00+00:00"}


def at(hour, minute=0, second=0, day=DAY):
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute, second)))


class RailTestCase(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.office = User.objects.create_user(username="rail-office", password="x", role="admin")
        cls.engineer = Employee.objects.create(
            employee_name="Rail Rani",
            email="rail.rani@example.com",
            role="Field Engineer",
            department="Service",
            branch="Chennai",
            salary=Decimal("20000"),
        )

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        for patcher in (
            mock.patch.dict(os.environ, NEW_RULE),
            # manage.py loads the real Ola key; a test's places must not
            # spend it.
            mock.patch("cases.olamaps.is_configured", return_value=False),
        ):
            patcher.start()
            self.addCleanup(patcher.stop)
        self.keys = 0

    def ping(self, moment, latitude=11.0, after_gap=False, received=None, accuracy=8):
        return LocationPing.objects.create(
            engineer=self.engineer, latitude=latitude, longitude=78.0, accuracy=accuracy,
            after_gap=after_gap, timestamp=moment,
            received_at=received if received is not None else moment,
        )

    def event(self, kind, moment, detail="", latitude=None, longitude=None, received=None):
        self.keys += 1
        return PhoneEvent.objects.create(
            engineer=self.engineer, kind=kind, at=moment,
            received_at=received if received is not None else moment,
            client_key=f"rail-{self.keys}", detail=detail, latitude=latitude, longitude=longitude,
        )

    def duty(self, start, end=None):
        return DutySession.objects.create(engineer=self.engineer, started_at=start, ended_at=end)

    def day(self, day=DAY):
        response = self.client.get(
            "/api/tracking/day/", {"engineer": self.engineer.id, "date": day.isoformat()}
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()

    def entries(self, *kinds, day=DAY):
        return [e for e in self.day(day)["events"] if not kinds or e["type"] in kinds]


class PhoneEventEntryTests(RailTestCase):
    def setUp(self):
        super().setUp()
        self.duty(at(9), at(18))

    def test_gps_off_and_on_say_how_long(self):
        self.event("gps_off", at(10), detail="switched")
        self.event("gps_on", at(10, 25), detail="switched")

        gps = self.entries("gps_off", "gps_on")
        self.assertEqual([e["label"] for e in gps], ["GPS off", "GPS on"])
        self.assertEqual([e["minutes"] for e in gps], [25, 25])
        self.assertEqual([e["found"] for e in gps], [False, False])

    def test_gps_found_off_says_it_was_found(self):
        self.event("gps_off", at(10), detail="found_off")
        self.event("gps_on", at(10, 5), detail="found_on")
        self.assertEqual([e["found"] for e in self.entries("gps_off", "gps_on")], [True, True])

    def test_gps_still_off_has_no_length_yet(self):
        self.event("gps_off", at(10), detail="switched")
        (entry,) = self.entries("gps_off")
        self.assertNotIn("minutes", entry)

    def test_the_net_going_and_coming_back(self):
        self.event("net_off", at(10))
        self.event("net_on", at(10, 20))
        net = self.entries("net_off", "net_on")
        self.assertEqual([e["label"] for e in net], ["Net off", "Net on"])
        self.assertEqual([e["minutes"] for e in net], [20, 20])

    def test_a_net_blip_is_left_out_entirely(self):
        self.event("net_off", at(10))
        self.event("net_on", at(10, 1, 30))
        self.assertEqual(self.entries("net_off", "net_on"), [])

    def test_the_phone_stopping_the_app_and_the_app_tracking_again(self):
        self.event("tracker_stopped", at(11))
        self.event("tracker_started", at(11, 25))
        stopped, started = self.entries("tracker_stopped", "tracker_started")
        self.assertEqual(stopped["label"], "Phone stopped the app · 25 min")
        self.assertEqual(started["label"], "App tracking again")
        self.assertEqual(started["minutes"], 25)

    def test_the_phone_stopping_the_app_is_measured_to_the_next_fix_without_word_after(self):
        self.event("tracker_stopped", at(11))
        self.ping(at(11, 40))
        (stopped,) = self.entries("tracker_stopped")
        self.assertEqual(stopped["label"], "Phone stopped the app · 40 min")

    def test_the_app_being_opened_has_no_entry_of_its_own(self):
        self.event("app_opened", at(11), detail="resumed")
        self.assertEqual(self.entries("app_opened"), [])

    def test_off_duty_and_other_days_are_left_out(self):
        self.event("gps_off", at(19), detail="switched")
        self.event("net_off", at(10, day=DAY - datetime.timedelta(days=1)))
        self.assertEqual(self.entries("gps_off", "net_off"), [])

    def test_an_event_with_no_place_takes_the_nearest_fix(self):
        self.ping(at(10, 2), latitude=11.25)
        self.event("gps_off", at(10), detail="switched")
        (entry,) = self.entries("gps_off")
        self.assertEqual(entry["latitude"], 11.25)

    def test_entries_read_in_the_order_the_day_happened(self):
        self.event("net_on", at(10, 20))
        self.event("gps_off", at(9, 30), detail="switched")
        self.event("net_off", at(10))
        moments = [parse_datetime(e["at"]) for e in self.entries()]
        self.assertEqual(moments, sorted(moments))

    def test_location_found_off_just_before_the_login_is_on_the_rail(self):
        """The phone looks at Login and stamps what it found by its own
        clock, a few minutes either way of ours -- the board says "GPS off
        since" from the same event, so the rail has to say it too."""
        self.event("gps_off", at(8, 50), detail="found_off")
        self.event("gps_off", at(8, 57), detail="found_off")
        self.assertEqual(
            [parse_datetime(e["at"]) for e in self.entries("gps_off")], [at(8, 57)]
        )

    def test_the_net_a_few_minutes_before_the_login_is_still_off_duty(self):
        self.event("net_off", at(8, 57))
        self.assertEqual(self.entries("net_off"), [])


class CountedTests(RailTestCase):
    """Whether the kilometres took the straight line across an entry, so the
    board can say "counted as a straight line" only when they did."""

    def setUp(self):
        super().setUp()
        self.duty(at(9), at(18))

    def _location_off(self, detail="switched", said_at=None):
        self.ping(at(9, 30), latitude=11.0)
        self.ping(at(10), latitude=11.009)
        self.event("gps_off", at(10, 1), detail=detail, received=said_at)
        self.ping(at(10, 40), latitude=11.07, after_gap=True)
        self.ping(at(10, 45), latitude=11.08)

    def test_location_switched_off_in_time_is_not_counted(self):
        self._location_off()
        (entry,) = self.entries("gps_off")
        self.assertIs(entry["counted"], False)
        self.assertAlmostEqual(self.day()["total_km"], 2.11, delta=0.05)

    def test_word_of_it_that_came_too_late_was_counted(self):
        self._location_off(said_at=at(10, 41))
        (entry,) = self.entries("gps_off")
        self.assertIs(entry["counted"], True)
        self.assertAlmostEqual(self.day()["total_km"], 8.9, delta=0.05)

    def test_location_found_off_is_counted(self):
        self._location_off(detail="found_off")
        (entry,) = self.entries("gps_off")
        self.assertIs(entry["counted"], True)

    def test_location_still_off_has_nothing_counted_across_it_yet(self):
        self.ping(at(9, 30), latitude=11.0)
        self.event("gps_off", at(10), detail="found_off")
        (entry,) = self.entries("gps_off")
        self.assertIs(entry["counted"], False)

    def test_a_hole_is_counted(self):
        self.ping(at(10), latitude=11.0)
        self.ping(at(11), latitude=11.3)
        (hole,) = self.entries("untracked")
        self.assertIs(hole["counted"], True)

    def test_before_the_cutover_a_hole_behind_a_dropped_flag_was_not_and_says_so(self):
        """The flagged fix after the silence was a cold GPS's, too rough to
        keep, so the hole runs to the next one; the old rule left the line
        out on the flag all the same."""
        self.ping(at(10), latitude=11.0, accuracy=10)
        self.ping(at(11), latitude=11.05, after_gap=True, accuracy=300)
        self.ping(at(11, 1), latitude=11.108, accuracy=15)
        with mock.patch.dict(os.environ, OLD_RULE):
            (hole,) = self.entries("untracked")
            (off,) = self.entries("location_off")
            total = self.day()["total_km"]
        self.assertEqual(hole["label"], "No positions · 1h 1m")
        self.assertIs(hole["counted"], False)
        self.assertIs(off["counted"], False)
        self.assertEqual(total, 0.0)

        (hole,) = self.entries("untracked")
        self.assertIs(hole["counted"], True)

    def test_a_hole_after_a_login_where_the_logout_was_is_counted(self):
        DutySession.objects.all().delete()
        self.duty(at(9), at(13))
        self.duty(at(14), at(18))
        self.ping(at(12, 59), latitude=11.0)
        self.ping(at(14, 0, 30), latitude=11.00005)
        self.ping(at(15), latitude=11.27)
        (hole,) = self.entries("untracked")
        self.assertIs(hole["counted"], True)
        self.assertAlmostEqual(self.day()["total_km"], 30.0, delta=0.05)


class HoleTests(RailTestCase):
    """A stretch nobody's fixes cover, while they moved."""

    def setUp(self):
        super().setUp()
        self.duty(at(9), at(18))
        self.ping(at(9), latitude=11.0)
        self.ping(at(11), latitude=11.3)

    def test_with_nothing_said_only_the_silence_is_known(self):
        (hole,) = self.entries("untracked")
        self.assertEqual(hole["label"], "No positions · 2h")
        self.assertGreater(hole["moved_km"], 30)

    def test_the_app_opened_as_the_fixes_came_back_means_it_had_been_closed(self):
        self.event("app_opened", at(10, 59), detail="resumed")
        (hole,) = self.entries("untracked")
        self.assertEqual(hole["label"], "App was closed · 2h")

    def test_an_app_opened_long_before_says_nothing_about_it(self):
        self.event("app_opened", at(9, 30), detail="reload")
        (hole,) = self.entries("untracked")
        self.assertEqual(hole["label"], "No positions · 2h")

    def test_a_hole_the_phone_explained_is_left_to_its_own_entries(self):
        for kind in ("gps_off", "net_off", "tracker_stopped"):
            with self.subTest(kind=kind):
                PhoneEvent.objects.all().delete()
                self.event(kind, at(9, 40), detail="switched")
                self.assertEqual(self.entries("untracked"), [])

    def test_never_the_words_not_tracked(self):
        self.ping(at(14), latitude=11.6)
        labels = [e["label"] for e in self.entries()]
        self.assertTrue(labels)
        self.assertFalse([label for label in labels if "Not tracked" in label], labels)

    def test_a_net_blip_inside_a_hole_does_not_hide_it(self):
        """Off and back within forty seconds is left off the rail, so it
        cannot be what explains two hours with no positions."""
        self.event("net_off", at(10))
        self.event("net_on", at(10, 0, 40))
        (hole,) = self.entries("untracked")
        self.assertEqual(hole["label"], "No positions · 2h")
        self.assertEqual(self.entries("net_off", "net_on"), [])

    def test_word_from_the_phone_within_a_minute_of_a_hole_is_about_it(self):
        self.event("net_off", at(11, 0, 50))
        self.assertEqual(self.entries("untracked"), [])

    def test_word_from_the_phone_more_than_a_minute_after_it_is_not(self):
        self.event("net_off", at(11, 1, 10))
        self.assertEqual(len(self.entries("untracked")), 1)

    def test_the_app_opened_up_to_two_minutes_from_the_return(self):
        self.event("app_opened", at(11, 1, 50), detail="resumed")
        (hole,) = self.entries("untracked")
        self.assertEqual(hole["label"], "App was closed · 2h")

    def test_the_app_opened_three_minutes_after_the_return_says_nothing_about_it(self):
        self.event("app_opened", at(11, 3), detail="resumed")
        (hole,) = self.entries("untracked")
        self.assertEqual(hole["label"], "No positions · 2h")


class HoleAcrossALoginTests(RailTestCase):
    def test_a_stretch_with_a_login_in_it_is_not_a_hole(self):
        """Logout at ten, Login at two: their own afternoon, not counted, and
        said by the Login entry -- not "No positions · 4h 35m"."""
        self.duty(at(9), at(10))
        self.duty(at(14), at(18))
        self.ping(at(9, 30), latitude=11.0)
        self.ping(at(14, 5), latitude=11.4)
        self.assertEqual(self.entries("untracked"), [])


class FlaggedFixTests(RailTestCase):
    """A flag on the first fix after tracking came back."""

    def test_a_flag_at_a_login_is_the_login(self):
        self.duty(at(9), at(10))
        self.duty(at(14), at(18))
        self.ping(at(9, 30), latitude=11.0)
        self.ping(at(14, 5), latitude=11.4, after_gap=True)
        self.assertEqual(self.entries("location_off", "location_back"), [])

    def test_a_flag_the_gps_entries_explain_is_left_to_them(self):
        self.duty(at(9), at(18))
        self.ping(at(9, 30), latitude=11.0)
        self.event("gps_off", at(9, 50), detail="switched")
        self.event("gps_on", at(11), detail="switched")
        self.ping(at(11, 1), latitude=11.4, after_gap=True)

        self.assertEqual(self.entries("location_off", "location_back"), [])
        self.assertEqual(len(self.entries("gps_off", "gps_on")), 2)
        self.assertAlmostEqual(self.day()["total_km"], 0.0)

    def _restart(self):
        self.duty(at(9), at(18))
        self.ping(at(9, 30), latitude=11.0)
        self.ping(at(11, 1), latitude=11.4, after_gap=True)

    def test_any_other_flag_is_tracking_stopped_and_counted(self):
        self._restart()
        off, back = self.entries("location_off", "location_back")
        self.assertEqual(off["label"], "Tracking stopped · 1h 31m")
        self.assertEqual(back["label"], "Tracking back")
        self.assertEqual((off["counted"], back["counted"]), (True, True))
        self.assertEqual(off["minutes"], 91)
        self.assertGreater(self.day()["total_km"], 40)

    def test_before_the_cutover_it_was_not_counted_and_says_so(self):
        self._restart()
        with mock.patch.dict(os.environ, OLD_RULE):
            off, back = self.entries("location_off", "location_back")
            total = self.day()["total_km"]
        self.assertEqual((off["counted"], back["counted"]), (False, False))
        self.assertEqual(total, 0.0)


class OutageTests(RailTestCase):
    """Fixes held on the phone and sent later."""

    def setUp(self):
        super().setUp()
        self.duty(at(9), at(18))
        self.ping(at(9), latitude=11.0)
        for minute in range(20, 40):
            self.ping(at(9, minute), latitude=11.0 + minute * 0.001, received=at(10, 5))
        self.ping(at(10, 10), latitude=11.05)

    def test_an_outage_the_phone_reported_is_said_once_by_the_net_entries(self):
        self.event("net_off", at(9, 20, 30))
        self.event("net_on", at(10, 4, 50))
        self.assertEqual(self.entries("no_network", "network_back"), [])
        self.assertEqual(
            [e["label"] for e in self.entries("net_off", "net_on")], ["Net off", "Net on"]
        )

    def test_one_nobody_reported_says_only_that_the_fixes_were_held(self):
        held, sent = self.entries("no_network", "network_back")
        self.assertEqual(held["label"], "Kept on the phone · 45 min")
        self.assertEqual(sent["label"], "Sent from the phone")

    def test_a_blip_as_the_held_fixes_went_up_leaves_them_said(self):
        """A phone waking: the first send fails, the next goes through. The
        blip is left off the rail, so it cannot be what says the fixes were
        held for forty-five minutes -- nor what explains the half hour after
        them with no positions."""
        self.event("net_off", at(10, 4, 20))
        self.event("net_on", at(10, 4, 50))
        self.assertEqual(
            [e["label"] for e in self.entries("no_network", "network_back")],
            ["Kept on the phone · 45 min", "Sent from the phone"],
        )
        self.assertIn("No positions · 31 min", [e["label"] for e in self.entries("untracked")])
        self.assertEqual(self.entries("net_off", "net_on"), [])

    def test_net_entries_that_do_not_cover_the_outage_leave_it_said(self):
        """The fixes were held from 9:20 but the network only went at 9:50:
        the first half hour is not the Net entries' to explain."""
        self.event("net_off", at(9, 50))
        self.event("net_on", at(10, 4, 50))
        self.assertEqual(len(self.entries("no_network", "network_back")), 2)
        self.assertEqual(len(self.entries("net_off", "net_on")), 2)

    def test_net_back_long_before_the_fixes_went_up_leaves_them_said(self):
        """By the phone's word the network was back at 9:40, yet the fixes
        only went up at 10:05: the Net entries do not say why they waited."""
        self.event("net_off", at(9, 20, 30))
        self.event("net_on", at(9, 40))
        self.assertEqual(len(self.entries("no_network", "network_back")), 2)

    def test_a_net_off_never_followed_by_net_on_covers_what_comes_after_it(self):
        self.event("net_off", at(9, 20, 30))
        self.assertEqual(self.entries("no_network", "network_back"), [])


class OverMidnightTests(RailTestCase):
    """A duty that ran past midnight belongs to both days it touched."""

    def test_the_small_hours_of_a_duty_from_the_night_before_are_on_duty(self):
        self.duty(at(22, day=DAY - datetime.timedelta(days=1)), at(2))
        self.ping(at(0, 30), latitude=11.0)
        self.ping(at(1, 30), latitude=11.4)
        self.event("net_off", at(1, 40))
        self.event("net_on", at(1, 50))

        (hole,) = self.entries("untracked")
        self.assertEqual(hole["label"], "No positions · 1h")
        self.assertEqual(len(self.entries("net_off", "net_on")), 2)

    def test_a_day_with_no_login_of_its_own_is_left_alone_as_before(self):
        """No Login on the day, yet the phone went on recording after the
        night's duty ended: a fault in the record, shown rather than buried --
        as the rail always showed such a day."""
        self.duty(at(22, day=DAY - datetime.timedelta(days=1)), at(2))
        for minute in (0, 5, 10):
            self.ping(at(10, minute), latitude=11.0)
        self.ping(at(12), latitude=11.4)
        self.event("gps_off", at(13), detail="switched")
        self.assertEqual(len(self.entries("untracked")), 1)
        self.assertEqual(len(self.entries("gps_off")), 1)
        self.assertEqual(self.day()["stop_count"], 1)

    def test_with_a_login_of_its_own_the_night_before_still_covers_its_small_hours(self):
        self.duty(at(22, day=DAY - datetime.timedelta(days=1)), at(2))
        self.duty(at(9), at(18))
        self.ping(at(0, 30), latitude=11.0)
        self.ping(at(1, 30), latitude=11.4)
        self.event("net_off", at(1, 40))
        self.event("net_on", at(1, 50))
        self.event("gps_off", at(5), detail="switched")

        (hole,) = self.entries("untracked")
        self.assertEqual(hole["label"], "No positions · 1h")
        self.assertEqual(len(self.entries("net_off", "net_on")), 2)
        self.assertEqual(self.entries("gps_off"), [])

    def test_the_login_and_logout_stay_on_the_day_the_duty_began(self):
        """As ROUTE KM routes them: the Login on the night before's rail."""
        self.duty(at(22, day=DAY - datetime.timedelta(days=1)), at(2))
        today = self.day()
        self.assertEqual([e for e in today["events"] if e["type"] in ("duty_start", "duty_end")], [])
        self.assertEqual(today["duty_minutes"], 0)
        before = self.day(DAY - datetime.timedelta(days=1))
        self.assertEqual(
            [e["type"] for e in before["events"] if e["type"] in ("duty_start", "duty_end")],
            ["duty_start", "duty_end"],
        )

    def test_a_login_just_after_midnight_still_leaves_out_the_drive_before_it(self):
        """A phone three minutes slow stamps the first fix after a 00:02
        Login on the day before -- whose distance must not count the drive."""
        self.duty(at(21), at(23))
        self.ping(at(22), latitude=11.0)
        self.duty(at(0, 2, day=DAY + datetime.timedelta(days=1)))
        self.ping(at(23, 59, 30), latitude=11.4, received=at(0, 2, 31, day=DAY + datetime.timedelta(days=1)))
        self.assertEqual(self.day()["total_km"], 0.0)
