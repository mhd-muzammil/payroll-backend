"""Standing still is not travelling.

A parked phone reports a slightly different position every 30 seconds. Summing
every gap turned that wander into kilometres: four minutes at one spot read as
0.05 km on the board, and a two-hour customer visit would have read as 1.4 km
travelled by someone who never left the building.
"""
import datetime
import math
from decimal import Decimal

from django.test import TestCase
from django.utils import timezone

from employees.models import Employee

from .models import LocationPing
from .views import MIN_STEP_METERS, _moving_trail, _trail_km, _usable_pings, haversine_km

# Degrees of latitude per metre, near enough at Chennai's latitude.
DEG_PER_M = 1.0 / 111_320.0


def make_engineer(name="Wander Tester"):
    return Employee.objects.create(
        employee_name=name,
        email=f"{name.replace(' ', '.').lower()}@example.com",
        role="Field Engineer",
        department="Service",
        branch="Chennai",
        salary=Decimal("20000"),
    )


class WanderTests(TestCase):
    def setUp(self):
        self.engineer = make_engineer()
        self.day = timezone.localdate()
        self.base = timezone.make_aware(
            datetime.datetime(self.day.year, self.day.month, self.day.day, 9, 0)
        )

    def _ping(self, index, north_metres, accuracy=7):
        return LocationPing.objects.create(
            engineer=self.engineer,
            latitude=13.0 + north_metres * DEG_PER_M,
            longitude=80.0,
            accuracy=accuracy,
            timestamp=self.base + datetime.timedelta(seconds=30 * index),
        )

    # -- the reported bug -----------------------------------------------------

    def test_a_parked_phone_travels_nowhere(self):
        """Eight fixes wandering +/-6 m around one spot, the way the reported
        four minutes of standing still looked."""
        drift = [0, 6, -4, 5, -6, 3, -5, 4]
        pings = [self._ping(i, metres) for i, metres in enumerate(drift)]

        self.assertEqual(_trail_km(pings), 0.0)

    def test_two_hours_of_standing_still_is_still_nowhere(self):
        """240 pings is what a customer visit looks like. Before, each 6 m of
        wander was added up."""
        pings = [self._ping(i, 6 if i % 2 else 0) for i in range(240)]
        self.assertEqual(_trail_km(pings), 0.0)

    # -- and real travel still counts ----------------------------------------

    def test_real_travel_is_measured(self):
        """250 m per ping is ordinary city driving."""
        pings = [self._ping(i, i * 250) for i in range(11)]  # 10 steps of 250 m
        self.assertAlmostEqual(_trail_km(pings), 2.5, places=1)

    def test_walking_pace_still_counts(self):
        """~42 m per 30 s is a walk. It must not be filtered away as wander."""
        pings = [self._ping(i, i * 42) for i in range(11)]
        self.assertGreater(_trail_km(pings), 0.35)

    def test_a_step_just_over_the_floor_counts(self):
        pings = [self._ping(0, 0, accuracy=1), self._ping(1, MIN_STEP_METERS + 2, accuracy=1)]
        self.assertGreater(_trail_km(pings), 0.0)

    def test_a_step_just_under_the_floor_does_not(self):
        pings = [self._ping(0, 0, accuracy=1), self._ping(1, MIN_STEP_METERS - 2, accuracy=1)]
        self.assertEqual(_trail_km(pings), 0.0)

    # -- creep, which is what made this subtle -------------------------------

    def test_creeping_in_small_steps_does_not_add_up_to_a_journey(self):
        """Each step is 6 m, which is nothing. Measured against the PREVIOUS fix
        they would each have counted; measured against the last KEPT fix they
        accumulate until the phone has genuinely gone somewhere."""
        pings = [self._ping(i, i * 6) for i in range(4)]  # 18 m total, under the floor
        self.assertEqual(_trail_km(pings), 0.0)

        # Far enough and it does count — the creep is not lost, only held back
        # until it amounts to a move.
        pings = [self._ping(i, i * 6) for i in range(20)]  # 114 m total
        self.assertGreater(_trail_km(pings), 0.09)

    # -- accuracy raises the floor -------------------------------------------

    def test_a_sloppy_pair_of_fixes_needs_a_bigger_gap(self):
        """Two +/-15 m fixes can differ by 30 m while sitting in one place."""
        pings = [self._ping(0, 0, accuracy=15), self._ping(1, 25, accuracy=15)]
        self.assertEqual(_trail_km(pings), 0.0, "25 m between two +/-15 m fixes was believed")

        pings = [self._ping(0, 0, accuracy=15), self._ping(1, 40, accuracy=15)]
        self.assertGreater(_trail_km(pings), 0.0)

    # -- what must NOT change ------------------------------------------------

    def test_the_noise_filter_is_untouched(self):
        """A fix worse than 100 m accuracy is still dropped outright."""
        good = self._ping(0, 0)
        bad = self._ping(1, 5000, accuracy=900)
        self.assertEqual(_usable_pings([good, bad]), [good])

    def test_the_points_list_keeps_the_stationary_fixes(self):
        """_moving_trail thins the trail for distance and the drawn line. The
        stops view needs every fix, because a cluster standing in one place is
        exactly what a stop is."""
        pings = [self._ping(i, 6 if i % 2 else 0) for i in range(10)]
        self.assertEqual(len(_usable_pings(pings)), 10)
        self.assertEqual(len(_moving_trail(pings)), 1)

    def test_an_empty_trail_is_zero_not_an_error(self):
        self.assertEqual(_trail_km([]), 0.0)
        self.assertEqual(_moving_trail([]), [])

    def test_a_single_fix_is_zero(self):
        self.assertEqual(_trail_km([self._ping(0, 0)]), 0.0)
