"""Travel nobody measured is not counted.

An engineer can switch their phone's location off mid-day. Until now the last
fix before it and the first fix after it were neighbours in the trail, so the
whole untracked leg was charged as one straight line -- shorter than the road
they drove, longer than an honest zero, and a number nobody had measured.

The obvious fix is wrong, and it is worth writing down why. A time gap cannot
tell the two cases apart: an engineer standing still produces no new rows
either, because the native watcher only fires after 10m of movement and the
30-second re-send of an unchanged fix is deduped on client_key. So a
forty-minute hole is either forty minutes of untracked driving or forty minutes
of work at one customer, and they have to be counted oppositely. Inferring from
the gap threw away the journey after every long stop.

Only the phone knows which it was. So the phone says: the first fix after
tracking resumes carries `after_gap`, and the one segment ending at it is
skipped. Everything on both sides of it still counts in full.
"""
import datetime
from decimal import Decimal

from rest_framework.test import APITestCase
from django.utils import timezone

from authentication.models import User
from cases.models import LocationPing
from cases.views import _trail_km
from employees.models import Employee

CHENNAI_LAT = 13.0827
CHENNAI_LON = 80.2707


class UntrackedGapTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="rover", password="x", role="employee"
        )
        self.engineer = Employee.objects.create(
            user=self.user,
            employee_name="Rover",
            email="rover@example.com",
            role="Field Engineer",
            department="Service",
            branch="Chennai",
            salary=Decimal("20000"),
        )
        self.start = timezone.now().replace(microsecond=0)

    def _ping(self, minutes, lat_offset, after_gap=False, accuracy=8):
        return LocationPing.objects.create(
            engineer=self.engineer,
            latitude=CHENNAI_LAT + lat_offset,
            longitude=CHENNAI_LON,
            accuracy=accuracy,
            after_gap=after_gap,
            timestamp=self.start + datetime.timedelta(minutes=minutes),
        )

    def _trail(self):
        return list(
            LocationPing.objects.filter(engineer=self.engineer).order_by("timestamp")
        )

    # ---------------------------------------------------- nothing is stamped

    def test_an_ordinary_journey_is_counted(self):
        for i in range(5):
            self._ping(minutes=i * 0.5, lat_offset=i * 0.01)  # ~1.1 km a step

        km = _trail_km(self._trail())
        self.assertGreater(km, 4.0)
        self.assertLess(km, 5.0)

    def test_the_flag_defaults_to_off(self):
        """Every fix already in the table predates this and must count."""
        ping = self._ping(minutes=0, lat_offset=0.0)
        self.assertFalse(LocationPing.objects.get(pk=ping.pk).after_gap)

    def test_a_long_stop_with_tracking_on_is_still_travel(self):
        """The case a time-based rule got wrong.

        No fix for an hour because they were parked, then they drive on. Nothing
        is stamped, so nothing is skipped.
        """
        self._ping(minutes=0, lat_offset=0.0)
        self._ping(minutes=1, lat_offset=0.03)  # ~3.3 km out
        # An hour at the customer, no new rows at all.
        self._ping(minutes=62, lat_offset=0.06)  # ~3.3 km on

        km = _trail_km(self._trail())
        self.assertGreater(km, 6.0)
        self.assertLess(km, 7.5)

    # ------------------------------------------------ the location switch off

    def test_the_untracked_leg_is_not_counted(self):
        self._ping(minutes=0, lat_offset=0.0)
        self._ping(minutes=1, lat_offset=0.03)  # ~3.3 km, tracked
        # Location off. They drive 50 km. It comes back on there.
        self._ping(minutes=45, lat_offset=0.50, after_gap=True)
        self._ping(minutes=46, lat_offset=0.53)  # ~3.3 km, tracked again

        km = _trail_km(self._trail())
        # Both tracked legs, and not one metre of the 50 km nobody measured.
        self.assertGreater(km, 6.0)
        self.assertLess(km, 7.5)

    def test_without_the_flag_that_leg_would_have_been_charged(self):
        """The same trail, unstamped, to show what the flag is actually saving."""
        self._ping(minutes=0, lat_offset=0.0)
        self._ping(minutes=1, lat_offset=0.03)
        self._ping(minutes=45, lat_offset=0.50)  # not stamped
        self._ping(minutes=46, lat_offset=0.53)

        self.assertGreater(_trail_km(self._trail()), 50.0)

    def test_only_the_one_segment_is_skipped(self):
        """Not the rest of the day either side of it."""
        for i in range(4):  # ~3.3 km
            self._ping(minutes=i * 0.5, lat_offset=i * 0.01)
        self._ping(minutes=40, lat_offset=0.40, after_gap=True)  # the hole
        for i in range(1, 4):  # ~3.3 km more
            self._ping(minutes=40 + i * 0.5, lat_offset=0.40 + i * 0.01)

        km = _trail_km(self._trail())
        self.assertGreater(km, 6.0)
        self.assertLess(km, 7.5)

    def test_two_separate_switch_offs_are_both_skipped(self):
        self._ping(minutes=0, lat_offset=0.0)
        self._ping(minutes=1, lat_offset=0.01)
        self._ping(minutes=30, lat_offset=0.30, after_gap=True)
        self._ping(minutes=31, lat_offset=0.31)
        self._ping(minutes=70, lat_offset=0.70, after_gap=True)
        self._ping(minutes=71, lat_offset=0.71)

        km = _trail_km(self._trail())
        # Three tracked steps of ~1.1 km. The two jumps of ~32 km each: neither.
        self.assertGreater(km, 3.0)
        self.assertLess(km, 4.0)

    # ---------------------------------- the flag has to survive the filters

    def test_a_noisy_first_fix_still_marks_the_gap(self):
        """The fix that arrives when the GPS wakes up is the noisiest of the day.

        The accuracy filter drops it, so reading the flag off the surviving
        points would lose it there and charge the straight line anyway.
        """
        self._ping(minutes=0, lat_offset=0.0)
        self._ping(minutes=1, lat_offset=0.01)
        # Location comes back; the first fix is junk and gets filtered out.
        self._ping(minutes=45, lat_offset=0.50, after_gap=True, accuracy=5000)
        self._ping(minutes=46, lat_offset=0.50)  # the first usable one
        self._ping(minutes=47, lat_offset=0.51)

        km = _trail_km(self._trail())
        self.assertLess(km, 3.0, "the 50km jump must not be counted")

    def test_a_first_fix_dropped_as_wander_still_marks_the_gap(self):
        """Switched off, drove a loop, came back within a few metres.

        The stamped fix is too close to the previous point to survive the wander
        filter, and the real move comes afterwards. The gap still has to hold.
        """
        self._ping(minutes=0, lat_offset=0.0)
        self._ping(minutes=1, lat_offset=0.01)
        self._ping(minutes=60, lat_offset=0.01005, after_gap=True)  # ~5 m away
        self._ping(minutes=61, lat_offset=0.02)  # then off again, tracked

        km = _trail_km(self._trail())
        self.assertGreater(km, 1.0)
        self.assertLess(km, 3.0)

    # ------------------------------------------------------------- the edges

    def test_a_stamp_on_the_very_first_fix_costs_nothing(self):
        """Going on duty stamps the first fix; there is no segment before it."""
        self._ping(minutes=0, lat_offset=0.0, after_gap=True)
        self._ping(minutes=0.5, lat_offset=0.01)
        self._ping(minutes=1, lat_offset=0.02)

        km = _trail_km(self._trail())
        self.assertGreater(km, 2.0)
        self.assertLess(km, 2.5)

    def test_empty_and_single(self):
        self.assertEqual(_trail_km([]), 0.0)
        self._ping(minutes=0, lat_offset=0.0, after_gap=True)
        self.assertEqual(_trail_km(self._trail()), 0.0)

    # -------------------------------------------------- it arrives over HTTP

    def test_the_phone_can_set_it_through_the_ping_endpoint(self):
        """The flag is no use unless it survives the wire."""
        self.client.force_authenticate(self.user)
        self.client.credentials(HTTP_X_PAYROLL_CLIENT="app")

        plain = self.client.post(
            "/api/tracking/ping/",
            {"latitude": CHENNAI_LAT, "longitude": CHENNAI_LON, "accuracy": 8,
             "client_key": "k1"},
            format="json",
        )
        self.assertEqual(plain.status_code, 201, plain.data)
        self.assertFalse(LocationPing.objects.get(client_key="k1").after_gap)

        resumed = self.client.post(
            "/api/tracking/ping/",
            {"latitude": CHENNAI_LAT + 0.5, "longitude": CHENNAI_LON, "accuracy": 8,
             "client_key": "k2", "after_gap": True},
            format="json",
        )
        self.assertEqual(resumed.status_code, 201, resumed.data)
        self.assertTrue(LocationPing.objects.get(client_key="k2").after_gap)

        # And the 50km between them is not charged to the engineer.
        self.assertEqual(_trail_km(self._trail()), 0.0)

    def test_standing_still_is_still_zero(self):
        for i in range(10):
            self._ping(minutes=i * 0.5, lat_offset=0.00002 * (i % 2))
        self.assertEqual(_trail_km(self._trail()), 0.0)
