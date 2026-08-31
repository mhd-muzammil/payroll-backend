"""Putting the GPS trail onto roads, without paying for it twice.

Snapping is a metered call, and the tracking board asks for a trail every 30
seconds. So the promise these tests protect is that a fix is sent to Ola exactly
once in its life, however many times anybody looks at it — and that when Ola
cannot be reached, the trail still draws.
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee

from . import olamaps
from .models import LocationPing, SnappedTrack
from .tracks import BATCH, snapped_trail


def make_engineer(name="Trail Tester", **extra):
    return Employee.objects.create(
        employee_name=name,
        email=f"{name.replace(' ', '.').lower()}@example.com",
        role="Field Engineer",
        department="Service",
        branch="Chennai",
        salary=Decimal("20000"),
        **extra,
    )


def make_pings(engineer, day, count, start_hour=9):
    """`count` fixes on `day`, one every 30 seconds, drifting north-east."""
    base = timezone.make_aware(
        datetime.datetime(day.year, day.month, day.day, start_hour, 0, 0)
    )
    pings = []
    for i in range(count):
        pings.append(
            LocationPing.objects.create(
                engineer=engineer,
                latitude=13.08 + i * 0.0002,
                longitude=80.27 + i * 0.0003,
                accuracy=10,
                timestamp=base + datetime.timedelta(seconds=30 * i),
            )
        )
    return pings


# ~11 m north: enough to tell a snapped point from a raw one in an assertion,
# small enough to be the kind of correction a real snap makes. A bigger offset
# would (rightly) be refused by the implausible-snap guard.
SNAP_NUDGE = 0.0001


class FakeSnapper:
    """Stands in for Ola. Records every batch it was asked to snap.

    Returns each point nudged a road's width north, so a snapped point is
    distinguishable from a raw one without tripping the plausibility guard.
    """

    def __init__(self, fail=False):
        self.batches = []
        self.fail = fail

    def __call__(self, points, enhance_path=False):
        if self.fail:
            raise olamaps.SnapUnavailable("pretend outage")
        self.batches.append(list(points))
        return [(lat + SNAP_NUDGE, lon) for lat, lon in points]

    @property
    def calls(self):
        return len(self.batches)

    @property
    def points_sent(self):
        return sum(len(b) for b in self.batches)


class SnappedTrailTests(TestCase):
    def setUp(self):
        self.engineer = make_engineer()
        self.today = timezone.localdate()
        self.yesterday = self.today - datetime.timedelta(days=1)

    def _run(self, day, pings, snapper=None):
        snapper = snapper or FakeSnapper()
        with mock.patch.object(olamaps, "is_configured", return_value=True), mock.patch.object(
            olamaps, "snap_to_road", snapper
        ):
            result = snapped_trail(self.engineer.id, day, pings)
        return result, snapper

    # -- the money question ---------------------------------------------------

    def test_a_fix_is_sent_to_ola_once_however_often_the_board_asks(self):
        """The board polls every 30 seconds. Ten reads of the same closed day
        must cost exactly one day's snapping, not ten."""
        pings = make_pings(self.engineer, self.yesterday, 120)

        _, first = self._run(self.yesterday, pings)
        self.assertEqual(first.points_sent, 120)

        for _ in range(9):
            _, again = self._run(self.yesterday, pings)
            self.assertEqual(again.calls, 0, "a re-read sent points to Ola again")

    def test_only_the_new_fixes_are_sent_as_the_day_grows(self):
        pings = make_pings(self.engineer, self.yesterday, 60)
        _, first = self._run(self.yesterday, pings)
        self.assertEqual(first.points_sent, 60)

        pings += make_pings(self.engineer, self.yesterday, 20, start_hour=14)
        _, second = self._run(self.yesterday, pings)
        self.assertEqual(second.points_sent, 20, "re-sent fixes that were already snapped")

    def test_every_pending_fix_goes_in_one_handoff(self):
        """tracks decides WHICH fixes to send; olamaps decides how many requests
        that takes. See BatchingTests below for the split into requests."""
        pings = make_pings(self.engineer, self.yesterday, 120)
        _, snapper = self._run(self.yesterday, pings)
        self.assertEqual(snapper.calls, 1)
        self.assertEqual(snapper.points_sent, 120)

    # -- today's tail ---------------------------------------------------------

    def test_todays_part_batch_waits_instead_of_wasting_a_request(self):
        """A live day grows. Sending 3 fixes now would spend a whole request on
        a trail that will have 50 in it shortly."""
        pings = make_pings(self.engineer, self.today, 3)
        result, snapper = self._run(self.today, pings)

        self.assertEqual(snapper.calls, 0)
        self.assertEqual(result["source"], "raw")
        self.assertEqual(result["points"], [[p.latitude, p.longitude] for p in pings])

    def test_todays_full_batch_is_snapped_and_the_rest_drawn_raw(self):
        pings = make_pings(self.engineer, self.today, BATCH + 7)
        result, snapper = self._run(self.today, pings)

        self.assertEqual(snapper.points_sent, BATCH)
        self.assertEqual(result["snapped"], BATCH)
        self.assertEqual(result["raw"], 7)
        self.assertEqual(result["source"], "partial")
        self.assertEqual(len(result["points"]), BATCH + 7)
        # The head is snapped (offset by the fake), the tail is the phone's own.
        self.assertAlmostEqual(result["points"][0][0], pings[0].latitude + SNAP_NUDGE)
        self.assertAlmostEqual(result["points"][-1][0], pings[-1].latitude)

    def test_a_closed_day_is_finalised_part_batch_and_all(self):
        """Yesterday will never grow, so there is nothing to wait for."""
        pings = make_pings(self.engineer, self.yesterday, 7)
        result, snapper = self._run(self.yesterday, pings)

        self.assertEqual(snapper.points_sent, 7)
        self.assertEqual(result["raw"], 0)
        self.assertEqual(result["source"], "ola")

    # -- nothing here may break tracking -------------------------------------

    def test_an_outage_falls_back_to_the_raw_trail(self):
        pings = make_pings(self.engineer, self.yesterday, 60)
        result, _ = self._run(self.yesterday, pings, snapper=FakeSnapper(fail=True))

        self.assertEqual(result["source"], "raw")
        self.assertEqual(result["points"], [[p.latitude, p.longitude] for p in pings])

    def test_an_outage_leaves_the_cache_untouched(self):
        """A failed attempt must not record those fixes as done, or they would
        never be snapped again."""
        pings = make_pings(self.engineer, self.yesterday, 60)
        self._run(self.yesterday, pings, snapper=FakeSnapper(fail=True))

        track = SnappedTrack.objects.filter(engineer=self.engineer, day=self.yesterday).first()
        if track:
            self.assertEqual(track.last_ping_id, 0)
            self.assertEqual(track.points, [])

        # And a later working call still snaps all of them.
        _, snapper = self._run(self.yesterday, pings)
        self.assertEqual(snapper.points_sent, 60)

    def test_no_key_configured_is_not_an_error(self):
        pings = make_pings(self.engineer, self.yesterday, 10)
        with mock.patch.object(olamaps, "is_configured", return_value=False):
            result = snapped_trail(self.engineer.id, self.yesterday, pings)

        self.assertEqual(result["source"], "raw")
        self.assertEqual(len(result["points"]), 10)
        self.assertFalse(SnappedTrack.objects.exists(), "cached a row without snapping anything")

    def test_an_empty_day_asks_ola_nothing(self):
        result, snapper = self._run(self.yesterday, [])
        self.assertEqual(snapper.calls, 0)
        self.assertEqual(result["points"], [])

    def test_one_track_per_engineer_per_day(self):
        pings = make_pings(self.engineer, self.yesterday, 60)
        self._run(self.yesterday, pings)
        self._run(self.yesterday, pings)
        self.assertEqual(
            SnappedTrack.objects.filter(engineer=self.engineer, day=self.yesterday).count(), 1
        )


class PathEndpointTests(APITestCase):
    """The road version is served ALONGSIDE the raw fixes, so nothing that reads
    this endpoint today changes."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr-trail", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        self.engineer = make_engineer("Path Tester")
        self.day = timezone.localdate() - datetime.timedelta(days=1)
        self.pings = make_pings(self.engineer, self.day, 60)

    def _get(self, **params):
        snapper = FakeSnapper()
        with mock.patch.object(olamaps, "is_configured", return_value=True), mock.patch.object(
            olamaps, "snap_to_road", snapper
        ):
            response = self.client.get("/api/tracking/path/", params)
        return response, snapper

    def test_the_raw_points_are_still_exactly_what_they_were(self):
        response, _ = self._get(engineer=self.engineer.id, date=self.day.isoformat())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["count"], 60)
        self.assertEqual(len(response.data["points"]), 60)
        self.assertIn("total_km", response.data)

    def test_the_road_version_comes_alongside(self):
        response, snapper = self._get(engineer=self.engineer.id, date=self.day.isoformat())
        road = response.data["road_path"]
        self.assertEqual(road["source"], "ola")
        self.assertEqual(road["snapped"], 60)
        self.assertEqual(len(road["points"]), 60)
        self.assertEqual(snapper.points_sent, 60)

    def test_no_date_means_no_road_version(self):
        """The cache is keyed by engineer AND day; without a day there is
        nothing coherent to store it under."""
        response, snapper = self._get(engineer=self.engineer.id)
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("road_path", response.data)
        self.assertEqual(snapper.calls, 0)

    def test_a_case_trail_gets_no_road_version(self):
        response, snapper = self._get(case=1, date=self.day.isoformat())
        self.assertEqual(response.status_code, 200, response.data)
        self.assertNotIn("road_path", response.data)
        self.assertEqual(snapper.calls, 0)


class BatchingTests(TestCase):
    """Ola takes at most 50 pairs per request, so a long trail has to be split.
    Tested at the HTTP boundary — the only place the request count is visible."""

    def _fetch_recording(self):
        calls = []

        def fetch(url):
            # The key is in that URL, so only the point count is recorded.
            points = url.split("points=", 1)[1].split("&", 1)[0]
            pairs = points.split("%7C") if "%7C" in points else points.split("|")
            calls.append(len(pairs))
            return {
                "status": "SUCCESS",
                "snapped_points": [
                    {"location": {"lat": 13.0, "lng": 80.0}, "snapped_type": "Nearest",
                     "original_index": i}
                    for i in range(len(pairs))
                ],
            }

        return fetch, calls

    def test_a_long_trail_is_split_into_full_requests(self):
        fetch, calls = self._fetch_recording()
        points = [(13.0 + i * 0.001, 80.0 + i * 0.001) for i in range(120)]
        with mock.patch.object(olamaps, "_fetch", fetch), mock.patch.object(
            olamaps, "api_key", return_value="test-key"
        ):
            out = olamaps.snap_to_road(points)

        self.assertEqual(calls, [50, 50, 20])
        self.assertEqual(len(out), 120)

    def test_a_short_trail_is_one_request(self):
        fetch, calls = self._fetch_recording()
        with mock.patch.object(olamaps, "_fetch", fetch), mock.patch.object(
            olamaps, "api_key", return_value="test-key"
        ):
            olamaps.snap_to_road([(13.0, 80.0), (13.1, 80.1)])
        self.assertEqual(calls, [2])

    def test_a_point_with_no_road_keeps_the_phones_own_coordinate(self):
        """NoSegment means Ola found no road. Dragging that fix to the nearest
        road would move the engineer somewhere they never were."""
        def fetch(url):
            return {
                "status": "SUCCESS",
                "snapped_points": [
                    {"location": {"lat": 99.0, "lng": 99.0}, "snapped_type": "NoSegment",
                     "original_index": 0},
                    {"location": {"lat": 13.5, "lng": 80.5}, "snapped_type": "Nearest",
                     "original_index": 1},
                ],
            }

        with mock.patch.object(olamaps, "_fetch", fetch), mock.patch.object(
            olamaps, "api_key", return_value="test-key"
        ):
            out = olamaps.snap_to_road([(13.111, 80.222), (13.333, 80.444)])

        self.assertEqual(out[0], (13.111, 80.222), "a NoSegment fix was moved")
        self.assertEqual(out[1], (13.5, 80.5))

    def test_no_key_is_reported_not_guessed_at(self):
        with mock.patch.object(olamaps, "api_key", return_value=""):
            with self.assertRaises(olamaps.SnapUnavailable):
                olamaps.snap_to_road([(13.0, 80.0)])


class DayEndpointTests(APITestCase):
    """/day is the view the tracking board actually draws from."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr-day", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        self.engineer = make_engineer("Day Tester")
        self.day = timezone.localdate() - datetime.timedelta(days=1)
        self.pings = make_pings(self.engineer, self.day, 60)

    def _get(self, snapper=None, **params):
        snapper = snapper or FakeSnapper()
        params.setdefault("engineer", self.engineer.id)
        params.setdefault("date", self.day.isoformat())
        with mock.patch.object(olamaps, "is_configured", return_value=True), mock.patch.object(
            olamaps, "snap_to_road", snapper
        ):
            response = self.client.get("/api/tracking/day/", params)
        return response, snapper

    def test_the_day_carries_the_road_route(self):
        response, snapper = self._get()
        self.assertEqual(response.status_code, 200, response.data)
        road = response.data["road_path"]
        self.assertEqual(road["source"], "ola")
        self.assertEqual(len(road["points"]), 60)
        self.assertEqual(snapper.points_sent, 60)

    def test_everything_else_about_the_day_is_untouched(self):
        response, _ = self._get()
        for field in ("total_km", "duty_minutes", "stops", "events", "points", "engineer_name"):
            self.assertIn(field, response.data, f"{field} went missing")
        self.assertEqual(len(response.data["points"]), 60)

    def test_an_outage_still_serves_the_day(self):
        response, _ = self._get(snapper=FakeSnapper(fail=True))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["road_path"]["source"], "raw")
        self.assertEqual(len(response.data["points"]), 60)

    def test_noisy_fixes_are_left_out_of_the_road_route_too(self):
        """The road line has to match the km, and the km drops bad accuracy."""
        LocationPing.objects.create(
            engineer=self.engineer,
            latitude=13.5,
            longitude=80.5,
            accuracy=900,  # a cold-GPS jump across town
            timestamp=timezone.make_aware(
                datetime.datetime(self.day.year, self.day.month, self.day.day, 15, 0)
            ),
        )
        response, snapper = self._get()
        self.assertEqual(snapper.points_sent, 60, "a noise fix was sent to Ola")
        self.assertEqual(len(response.data["road_path"]["points"]), 60)


class SharedCacheTests(APITestCase):
    """/path and /day both cache under one track per engineer-day, so they must
    be snapping the same trail. Feeding them different point sets would leave
    two versions of one route interleaved in the stored polyline."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr-both", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        self.engineer = make_engineer("Both Tester")
        self.day = timezone.localdate() - datetime.timedelta(days=1)
        make_pings(self.engineer, self.day, 60)
        # One unusable fix, which /day drops and /path used to keep.
        LocationPing.objects.create(
            engineer=self.engineer,
            latitude=13.9,
            longitude=80.9,
            accuracy=800,
            timestamp=timezone.make_aware(
                datetime.datetime(self.day.year, self.day.month, self.day.day, 16, 0)
            ),
        )

    def test_the_two_endpoints_agree_and_snap_once_between_them(self):
        snapper = FakeSnapper()
        params = {"engineer": self.engineer.id, "date": self.day.isoformat()}
        with mock.patch.object(olamaps, "is_configured", return_value=True), mock.patch.object(
            olamaps, "snap_to_road", snapper
        ):
            day = self.client.get("/api/tracking/day/", params).data
            path = self.client.get("/api/tracking/path/", params).data

        self.assertEqual(snapper.points_sent, 60, "the second endpoint re-snapped")
        self.assertEqual(day["road_path"]["points"], path["road_path"]["points"])
        self.assertEqual(
            SnappedTrack.objects.filter(engineer=self.engineer, day=self.day).count(), 1
        )


class ImplausibleSnapTests(TestCase):
    """Ola path-matches rather than moving each fix to its own nearest road. A
    trail it cannot match cleanly comes back projected onto a different route
    altogether — measured at 585 m average, 2.3 km at worst. That is not a
    corrected route, it is a route somebody never drove, so it is refused."""

    def setUp(self):
        self.engineer = make_engineer("Shift Tester")
        self.day = timezone.localdate() - datetime.timedelta(days=1)

    def _run(self, snapper):
        pings = make_pings(self.engineer, self.day, 10)
        with mock.patch.object(olamaps, "is_configured", return_value=True), mock.patch.object(
            olamaps, "snap_to_road", snapper
        ):
            return snapped_trail(self.engineer.id, self.day, pings), pings

    def test_a_snap_that_moves_the_trail_a_kilometre_is_refused(self):
        def wanderer(points, enhance_path=False):
            # ~1.1 km north of every fix.
            return [(lat + 0.01, lon) for lat, lon in points]

        result, pings = self._run(wanderer)

        self.assertEqual(result["source"], "raw")
        self.assertEqual(result["points"], [[p.latitude, p.longitude] for p in pings])
        self.assertFalse(
            SnappedTrack.objects.filter(engineer=self.engineer, day=self.day)
            .exclude(points=[])
            .exists(),
            "an implausible snap was cached",
        )

    def test_an_ordinary_correction_is_accepted(self):
        def nudger(points, enhance_path=False):
            # ~11 m — a road's width, which is what a real snap looks like.
            return [(lat + 0.0001, lon) for lat, lon in points]

        result, _ = self._run(nudger)

        self.assertEqual(result["source"], "ola")
        self.assertEqual(result["snapped"], 10)

    def test_the_threshold_is_the_documented_one(self):
        """It decides whether a route is drawn from Ola or from the phone, so a
        change to it should be deliberate."""
        from .tracks import MAX_MEAN_SHIFT_METERS

        self.assertEqual(MAX_MEAN_SHIFT_METERS, 200.0)
