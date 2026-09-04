"""An engineer who loses signal keeps working, and the day has to survive it.

The phone's GPS does not need a network. So a basement, a lift, a village
stretch means the fixes are still taken — they just cannot be sent yet. What
these tests protect:

  The backlog arrives with the times it HAPPENED, so the route is drawn in the
  order the engineer travelled rather than the order the network delivered.

  The phone's clock is taken but not trusted, and a batch replayed after a
  timeout does not double the trail.

  The board can tell the three states apart: live, dark, and caught up. And it
  can say WHY someone went dark, because the last fix carries the charge that
  was left.
"""
import datetime
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee

from .models import DutySession, LocationPing
from .pings import MAX_BACKLOG_HOURS, MAX_BATCH, PingRejected, battery_percent, resolve_timestamp


def make_engineer(name="Offline Tester", user=None):
    return Employee.objects.create(
        user=user,
        employee_name=name,
        email=f"{name.replace(' ', '.').lower()}@example.com",
        role="Field Engineer",
        department="Service",
        branch="Chennai",
        salary=Decimal("20000"),
    )


class BatteryPercentTests(TestCase):
    """Capacitor reports batteryLevel as 0.0-1.0. Storing that as written turns
    a full battery into 1% — indistinguishable from a phone about to die."""

    def test_the_capacitor_fraction_becomes_a_percentage(self):
        self.assertEqual(battery_percent(1.0), 100)
        self.assertEqual(battery_percent(0.72), 72)
        self.assertEqual(battery_percent(0.04), 4)

    def test_a_plain_percentage_is_left_alone(self):
        self.assertEqual(battery_percent(72), 72)
        self.assertEqual(battery_percent(100), 100)

    def test_zero_is_zero_either_way(self):
        self.assertEqual(battery_percent(0), 0)
        self.assertEqual(battery_percent(0.0), 0)

    def test_nonsense_is_not_stored(self):
        for value in (None, "", "full", -5, 101, 250, True, False):
            self.assertIsNone(battery_percent(value), f"{value!r} was accepted")


class TimestampTrustTests(TestCase):
    def setUp(self):
        self.now = timezone.now()

    def test_no_timestamp_means_now(self):
        self.assertEqual(resolve_timestamp(None, self.now), self.now)
        self.assertEqual(resolve_timestamp("", self.now), self.now)

    def test_a_time_from_the_backlog_is_kept(self):
        taken = self.now - datetime.timedelta(minutes=25)
        self.assertEqual(resolve_timestamp(taken.isoformat(), self.now), taken)

    def test_the_future_is_refused(self):
        ahead = (self.now + datetime.timedelta(hours=3)).isoformat()
        with self.assertRaises(PingRejected):
            resolve_timestamp(ahead, self.now)

    def test_a_little_clock_skew_is_tolerated(self):
        """Phones are often a minute or two out. That is skew, not a lie."""
        skewed = self.now + datetime.timedelta(minutes=2)
        self.assertEqual(resolve_timestamp(skewed.isoformat(), self.now), skewed)

    def test_a_stale_queue_cannot_rewrite_last_week(self):
        old = (self.now - datetime.timedelta(hours=MAX_BACKLOG_HOURS + 1)).isoformat()
        with self.assertRaises(PingRejected):
            resolve_timestamp(old, self.now)

    def test_gibberish_is_refused_not_silently_dropped(self):
        with self.assertRaises(PingRejected):
            resolve_timestamp("yesterday", self.now)


class PingEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="eng-off", password="x", role="employee")
        self.engineer = make_engineer(user=self.user)
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_X_PAYROLL_CLIENT="app")

    def _post(self, **body):
        payload = {"latitude": 13.08, "longitude": 80.27}
        payload.update(body)
        return self.client.post("/api/tracking/ping/", payload, format="json")

    def test_a_live_ping_records_the_charge(self):
        response = self._post(battery_level=0.72, is_charging=False)
        self.assertEqual(response.status_code, 201, response.data)

        ping = LocationPing.objects.get()
        self.assertEqual(ping.battery_level, 72)
        self.assertIs(ping.is_charging, False)

    def test_every_fix_is_stamped_with_when_we_got_it(self):
        self._post()
        ping = LocationPing.objects.get()
        self.assertIsNotNone(ping.received_at)

    def test_a_backlogged_fix_keeps_the_time_it_was_taken(self):
        taken = timezone.now() - datetime.timedelta(minutes=30)
        self._post(timestamp=taken.isoformat())

        ping = LocationPing.objects.get()
        self.assertEqual(ping.timestamp.replace(microsecond=0), taken.replace(microsecond=0))
        self.assertGreater(ping.received_at, ping.timestamp)

    def test_a_replayed_fix_is_not_stored_twice(self):
        """The phone posted, timed out, and posted again. It has not moved."""
        first = self._post(client_key="fix-abc")
        second = self._post(client_key="fix-abc")

        self.assertEqual(first.status_code, 201)
        self.assertEqual(second.status_code, 200, "a repeat was treated as new")
        self.assertEqual(LocationPing.objects.count(), 1)

    def test_a_fix_in_the_future_is_refused(self):
        ahead = (timezone.now() + datetime.timedelta(hours=2)).isoformat()
        response = self._post(timestamp=ahead)
        self.assertEqual(response.status_code, 400)
        self.assertFalse(LocationPing.objects.exists())

    def test_coordinates_are_still_required(self):
        response = self.client.post("/api/tracking/ping/", {"latitude": 13.0}, format="json")
        self.assertEqual(response.status_code, 400)


class BatchEndpointTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="eng-batch", password="x", role="employee")
        self.engineer = make_engineer("Batch Tester", user=self.user)
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_X_PAYROLL_CLIENT="app")
        self.now = timezone.now()

    def _fixes(self, count, start_minutes_ago=60, key_prefix="k"):
        return [
            {
                "latitude": 13.08 + i * 0.001,
                "longitude": 80.27 + i * 0.001,
                "accuracy": 10,
                "battery_level": 0.5,
                "timestamp": (
                    self.now - datetime.timedelta(minutes=start_minutes_ago - i)
                ).isoformat(),
                "client_key": f"{key_prefix}-{i}",
            }
            for i in range(count)
        ]

    def _post(self, fixes):
        return self.client.post(
            "/api/tracking/ping/batch/", {"pings": fixes}, format="json"
        )

    def test_an_outage_is_delivered_whole(self):
        """25 minutes dark is 50 fixes at the 30-second cadence."""
        response = self._post(self._fixes(50))
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["stored"], 50)
        self.assertEqual(LocationPing.objects.count(), 50)

    def test_the_route_is_in_travel_order_not_arrival_order(self):
        self._post(self._fixes(10))
        stamps = list(
            LocationPing.objects.order_by("timestamp").values_list("timestamp", flat=True)
        )
        self.assertEqual(stamps, sorted(stamps))
        # Every one of them arrived after it happened.
        for ping in LocationPing.objects.all():
            self.assertGreater(ping.received_at, ping.timestamp)

    def test_a_replayed_batch_stores_nothing_new(self):
        fixes = self._fixes(20)
        self._post(fixes)
        again = self._post(fixes)

        self.assertEqual(again.data["stored"], 0)
        self.assertEqual(again.data["duplicates"], 20)
        self.assertEqual(LocationPing.objects.count(), 20)

    def test_a_batch_that_repeats_itself_is_deduped(self):
        fixes = self._fixes(5)
        response = self._post(fixes + fixes)
        self.assertEqual(response.data["stored"], 5)
        self.assertEqual(response.data["duplicates"], 5)

    def test_one_bad_fix_does_not_lose_the_rest(self):
        fixes = self._fixes(5)
        fixes[2] = {"latitude": "not a number", "longitude": 80.0}
        response = self._post(fixes)

        self.assertEqual(response.data["stored"], 4)
        self.assertEqual(len(response.data["rejected"]), 1)
        self.assertEqual(response.data["rejected"][0]["index"], 2)

    def test_an_oversized_batch_is_refused_with_the_limit(self):
        response = self._post(self._fixes(MAX_BATCH + 1))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["max_batch"], MAX_BATCH)
        self.assertFalse(LocationPing.objects.exists())

    def test_an_empty_batch_is_fine(self):
        response = self._post([])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["stored"], 0)

    def test_a_batch_that_is_not_a_list_is_refused(self):
        response = self.client.post(
            "/api/tracking/ping/batch/", {"pings": "everything"}, format="json"
        )
        self.assertEqual(response.status_code, 400)

    def test_fixes_land_on_the_engineer_who_posted_them(self):
        other = make_engineer("Somebody Else")
        self._post(self._fixes(3))
        self.assertEqual(LocationPing.objects.filter(engineer=self.engineer).count(), 3)
        self.assertEqual(LocationPing.objects.filter(engineer=other).count(), 0)


class BoardStateTests(APITestCase):
    """Live, dark, or caught up — and why."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr-off", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        self.engineer = make_engineer("Board Tester")
        DutySession.objects.create(engineer=self.engineer, started_at=timezone.now())
        self.now = timezone.now()

    def _ping(self, minutes_ago, delay_minutes=0, battery=None):
        taken = self.now - datetime.timedelta(minutes=minutes_ago)
        return LocationPing.objects.create(
            engineer=self.engineer,
            latitude=13.08,
            longitude=80.27,
            accuracy=10,
            battery_level=battery,
            timestamp=taken,
            received_at=taken + datetime.timedelta(minutes=delay_minutes),
        )

    def _row(self):
        response = self.client.get("/api/tracking/live/")
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data), 1, response.data)
        return response.data[0]

    def test_a_live_engineer_reads_as_live(self):
        self._ping(minutes_ago=1, battery=80)
        row = self._row()
        self.assertFalse(row["stale"])
        self.assertEqual(row["queued_minutes"], 0)
        self.assertEqual(row["battery_level"], 80)

    def test_an_engineer_who_has_caught_up_says_how_long_they_were_dark(self):
        self._ping(minutes_ago=2, delay_minutes=25)
        row = self._row()
        self.assertEqual(row["queued_minutes"], 25)

    def test_a_dying_phone_and_a_lost_signal_are_told_apart(self):
        """Both go dark. The charge on the last fix is the difference."""
        self._ping(minutes_ago=30, battery=4)
        row = self._row()
        self.assertTrue(row["stale"])
        self.assertEqual(row["battery_level"], 4)
        self.assertGreaterEqual(row["last_seen_minutes"], 29)

    def test_an_old_row_does_not_pretend_to_know(self):
        """Rows from before received_at existed cannot say whether they queued."""
        ping = self._ping(minutes_ago=1)
        LocationPing.objects.filter(pk=ping.pk).update(received_at=None)
        self.assertIsNone(self._row()["queued_minutes"])

    def test_an_engineer_with_no_fix_at_all_is_still_listed(self):
        row = self._row()
        self.assertTrue(row["stale"])
        self.assertIsNone(row["battery_level"])
        self.assertIsNone(row["queued_minutes"])
