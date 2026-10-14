"""ROUTE KM: the road distance through the places an engineer marked.

Login, every Check in, Logout, in the order they happened, measured along the
road by Ola. What these tests hold it to:

  The points are the timeline's. A Login sits exactly where the day timeline
  puts it, so the map beside a figure and the rail beside the trail cannot
  disagree -- ParityWithTheDayTimelineTests asks /day itself, not a copy of its
  rule.

  A cell is measured once, and again only when its points change. Directions
  is metered and Ola throttles per minute, so a re-read that spends a request
  is a bug even when the figure comes out right.

  Each request does a bounded amount of work, and backs off when Ola says so.

Nothing here reaches the network. Ola is replaced at route_km._post, and
urlopen itself is made to stop the test if anything gets past that.
"""
import contextlib
import datetime
import hashlib
import io
import itertools
import json
import os
import time
import urllib.error
import urllib.parse
from decimal import Decimal
from unittest import mock

from django.core.management import call_command
from django.db.models.query import QuerySet
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from authentication.models import User
from employees.models import Employee

from . import olamaps, route_km
from .models import Case, CaseVisit, DutySession, LocationPing, RouteKmDay

# The key in every test that has one. Never the real one: manage.py loads .env,
# so the real key is in the environment unless a test empties it -- see
# ola_off().
TEST_KEY = "route-km-test-key"

_case_numbers = itertools.count(1)


def make_engineer(name="Route Tester", branch="Hosur"):
    return Employee.objects.create(
        employee_name=name,
        email=f"{name.replace(' ', '.').lower()}@example.com",
        role="Field Engineer",
        department="Service",
        branch=branch,
        salary=Decimal("20000"),
    )


def at(day, hour, minute=0, second=0):
    """A moment on `day`, on the clock people read (IST)."""
    return timezone.make_aware(
        datetime.datetime.combine(day, datetime.time(hour, minute, second)),
        timezone.get_current_timezone(),
    )


def ping(engineer, when, lat, lon=77.80):
    return LocationPing.objects.create(
        engineer=engineer, latitude=lat, longitude=lon, accuracy=8, timestamp=when
    )


def duty(engineer, start, end=None, auto_closed=False):
    """A Login, and its Logout if there was one, the way the app writes them."""
    return DutySession.objects.create(
        engineer=engineer, started_at=start, ended_at=end, auto_closed=auto_closed
    )


def check_in(engineer, when, lat, lon, **extra):
    """A call the engineer checked into at `when`, standing at lat, lon."""
    number = "OC-%06d" % next(_case_numbers)
    return Case.objects.create(
        case_number=number,
        external_ref=f"WO-{number[3:]}",
        customer_name="A customer",
        title=f"Service call {number}",
        assigned_to=engineer,
        reached_at=when,
        punch_in_lat=lat,
        punch_in_lon=lon,
        **extra,
    )


def marked_day(engineer, day, checkins=1):
    """A Login at 09:00, `checkins` calls on the hour from 10:00, a Logout at 18:00.

    Every point placed, so the day has 2 + checkins points and as many legs
    less one.
    """
    duty(engineer, at(day, 9), at(day, 18))
    ping(engineer, at(day, 9), 12.70, 77.70)
    ping(engineer, at(day, 18), 12.90, 77.90)
    for n in range(checkins):
        check_in(engineer, at(day, 10 + n), round(12.75 + n * 0.01, 5), round(77.75 + n * 0.01, 5))


def encode_polyline(coordinates, precision=5):
    """decode_polyline in reverse, so the fake Ola can answer in Ola's format."""
    factor = 10**precision
    out = []
    last_lat = last_lon = 0
    for lat, lon in coordinates:
        lat_i, lon_i = round(lat * factor), round(lon * factor)
        for delta in (lat_i - last_lat, lon_i - last_lon):
            value = ~(delta << 1) if delta < 0 else delta << 1
            while value >= 0x20:
                out.append(chr((0x20 | (value & 0x1F)) + 63))
                value >>= 5
            out.append(chr(value + 63))
        last_lat, last_lon = lat_i, lon_i
    return "".join(out)


@contextlib.contextmanager
def ola_off():
    """No key anywhere. api_key() falls back from settings to the environment,
    and manage.py fills the environment from .env, so both are emptied."""
    with override_settings(OLA_MAPS_API_KEY=""), mock.patch.dict(
        os.environ, {"OLA_MAPS_API_KEY": ""}
    ):
        yield


class ReachedTheNetwork(BaseException):
    """What urlopen raises in these tests. A BaseException on purpose: _post
    turns every Exception into SnapUnavailable, which would hide a missed fake
    as an ordinary failed cell instead of stopping the test."""


class FakeOla:
    """Ola's Directions, standing in at route_km._post.

    Answers each request with one leg of `leg_metres` per pair of points and a
    route line through the points themselves, so a test can see what each
    request drew. Records the points each request carried -- never the URL,
    which carries the key. Set `fail` to an exception to have it raised.
    """

    def __init__(self, leg_metres=1000):
        self.leg_metres = leg_metres
        self.fail = None
        self.requests = []
        self.carried_the_key = []

    def __call__(self, url):
        query = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        sent = [query["origin"][0]]
        if "waypoints" in query:
            sent += query["waypoints"][0].split("|")
        sent.append(query["destination"][0])
        coordinates = [tuple(float(part) for part in pair.split(",")) for pair in sent]
        self.requests.append(coordinates)
        self.carried_the_key.append(query.get("api_key") == [TEST_KEY])
        if self.fail is not None:
            raise self.fail
        return {
            "status": "SUCCESS",
            "routes": [
                {
                    "legs": [{"distance": self.leg_metres} for _ in coordinates[1:]],
                    "overview_polyline": encode_polyline(coordinates),
                }
            ],
        }


class NoNetwork:
    """Mixed into every test class here.

    urlopen stops the test if anything reaches it unasked; time.sleep is a mock,
    so the pause between batches costs nothing and can be checked; and the 429
    cooldown, which lives in the module, starts and ends clear.
    """

    def setUp(self):
        super().setUp()
        guard = mock.patch("urllib.request.urlopen", side_effect=ReachedTheNetwork)
        guard.start()
        self.addCleanup(guard.stop)
        sleeper = mock.patch("time.sleep")
        self.sleep = sleeper.start()
        self.addCleanup(sleeper.stop)
        route_km._cooldown_until = 0.0
        self.addCleanup(setattr, route_km, "_cooldown_until", 0.0)


class WithFakeOla(NoNetwork):
    """A key, a fake Ola behind it, and the default per-minute allowance."""

    def setUp(self):
        super().setUp()
        self.ola = FakeOla()
        post = mock.patch.object(route_km, "_post", self.ola)
        post.start()
        self.addCleanup(post.stop)
        allowance = mock.patch.dict(os.environ, {"ROUTE_KM_PER_MINUTE": ""})
        allowance.start()
        self.addCleanup(allowance.stop)


# -- the points ---------------------------------------------------------------


class KeyPointsTests(NoNetwork, TestCase):
    """Login, every Check in, Logout -- where the engineer marked their day."""

    def setUp(self):
        super().setUp()
        self.engineer = make_engineer()
        self.day = timezone.localdate() - datetime.timedelta(days=1)

    def points(self):
        return route_km.key_points(self.engineer, self.day)

    def test_login_and_logout_take_the_nearest_fix(self):
        duty(self.engineer, at(self.day, 9, 2), at(self.day, 18, 5))
        ping(self.engineer, at(self.day, 8, 55), 12.10)  # 7 min before Login
        ping(self.engineer, at(self.day, 9, 4), 12.20)  # 2 min after: nearest
        ping(self.engineer, at(self.day, 18, 0), 12.30)  # 5 min before Logout: nearest
        ping(self.engineer, at(self.day, 18, 15), 12.40)  # 10 min after

        points = self.points()
        self.assertEqual(
            [(p["kind"], p["latitude"]) for p in points], [("login", 12.20), ("logout", 12.30)]
        )
        self.assertIsNone(points[0]["case_number"])

    def test_a_tie_goes_to_the_earlier_fix(self):
        duty(self.engineer, at(self.day, 9, 0))
        ping(self.engineer, at(self.day, 8, 55), 12.10)
        ping(self.engineer, at(self.day, 9, 5), 12.20)
        self.assertEqual(self.points()[0]["latitude"], 12.10)

    def no_office(self):
        """An engineer whose branch has no office in OFFICES, so a tap with no
        fix near it has nowhere to go and is left out."""
        self.engineer = make_engineer("No Office Tester", branch="Bengaluru")

    def test_fifteen_minutes_away_is_near_enough_and_a_second_more_is_not(self):
        self.no_office()
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 17, 0))
        ping(self.engineer, at(self.day, 9, 15), 12.10)  # exactly 15 min after Login
        ping(self.engineer, at(self.day, 17, 15, 1), 12.20)  # 15 min 1 s after Logout

        points = self.points()
        self.assertEqual([(p["kind"], p["latitude"]) for p in points], [("login", 12.10)])

    def test_a_tap_with_no_fix_near_it_is_left_out(self):
        self.no_office()
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 18, 0))
        ping(self.engineer, at(self.day, 13, 0), 12.10)  # hours from either tap
        self.assertEqual(self.points(), [])

    def test_a_fix_from_the_day_before_is_not_used(self):
        """The timeline places an entry from the day's own fixes only."""
        self.no_office()
        duty(self.engineer, at(self.day, 0, 5))
        ping(self.engineer, at(self.day - datetime.timedelta(days=1), 23, 58), 12.10)
        self.assertEqual(self.points(), [])

    def test_an_auto_closed_logout_is_not_a_point(self):
        """The system wrote that Logout; the engineer marked nothing there."""
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 19, 0), auto_closed=True)
        ping(self.engineer, at(self.day, 9, 1), 12.10)
        ping(self.engineer, at(self.day, 19, 0), 12.20)  # right where it was stamped
        self.assertEqual([p["kind"] for p in self.points()], ["login"])

    def test_still_on_duty_has_no_logout_yet(self):
        duty(self.engineer, at(self.day, 9, 0))
        ping(self.engineer, at(self.day, 9, 1), 12.10)
        ping(self.engineer, at(self.day, 17, 0), 12.20)
        self.assertEqual([p["kind"] for p in self.points()], ["login"])

    def test_check_ins_come_in_time_order_with_their_case_numbers(self):
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 18, 0))
        ping(self.engineer, at(self.day, 9, 0), 12.00)
        ping(self.engineer, at(self.day, 18, 0), 12.90)
        later = check_in(self.engineer, at(self.day, 14, 30), 12.50, 77.50)
        earlier = check_in(self.engineer, at(self.day, 11, 0), 12.40, 77.40)

        points = self.points()
        self.assertEqual(
            [(p["kind"], p["case_number"]) for p in points],
            [
                ("login", None),
                ("checkin", earlier.case_number),
                ("checkin", later.case_number),
                ("logout", None),
            ],
        )
        self.assertEqual((points[1]["latitude"], points[1]["longitude"]), (12.40, 77.40))
        self.assertEqual((points[2]["latitude"], points[2]["longitude"]), (12.50, 77.50))

    def test_a_check_in_with_no_position_is_left_out(self):
        placed = check_in(self.engineer, at(self.day, 10, 0), 12.40, 77.40)
        check_in(self.engineer, at(self.day, 11, 0), None, None)
        check_in(self.engineer, at(self.day, 12, 0), 12.60, None)
        self.assertEqual([p["case_number"] for p in self.points()], [placed.case_number])

    def test_check_outs_are_not_points(self):
        check_in(
            self.engineer,
            at(self.day, 10, 0),
            12.40,
            77.40,
            completed_at=at(self.day, 11, 0),
            punch_out_lat=12.60,
            punch_out_lon=77.60,
        )
        points = self.points()
        self.assertEqual([(p["kind"], p["latitude"]) for p in points], [("checkin", 12.40)])

    def test_the_days_own_visit_answers_for_a_call_sent_out_again(self):
        """A second dispatch overwrites the case's columns. The day's visit row
        keeps the check-in made that day, and that is the one used."""
        case = check_in(
            self.engineer, at(self.day + datetime.timedelta(days=1), 10, 0), 13.00, 78.00
        )
        CaseVisit.objects.create(
            case=case,
            engineer=self.engineer,
            plan_date=self.day,
            checked_in_at=at(self.day, 10, 0),
            punch_in_lat=12.40,
            punch_in_lon=77.40,
        )
        points = self.points()
        self.assertEqual(
            [(p["kind"], p["latitude"], p["case_number"]) for p in points],
            [("checkin", 12.40, case.case_number)],
        )

    def test_two_duties_are_two_logins_and_two_logouts(self):
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 12, 0))
        duty(self.engineer, at(self.day, 14, 0), at(self.day, 18, 0))
        for hour, lat in ((9, 12.10), (12, 12.20), (14, 12.30), (18, 12.40)):
            ping(self.engineer, at(self.day, hour, 0), lat)

        self.assertEqual(
            [(p["kind"], p["latitude"]) for p in self.points()],
            [("login", 12.10), ("logout", 12.20), ("login", 12.30), ("logout", 12.40)],
        )

    def test_at_is_an_iso_string_of_the_moment_itself(self):
        start = at(self.day, 9, 2)
        duty(self.engineer, start)
        ping(self.engineer, at(self.day, 9, 3), 12.10)

        point = self.points()[0]
        self.assertEqual(
            set(point), {"kind", "at", "latitude", "longitude", "case_number", "office"}
        )
        self.assertIs(point["office"], False)
        self.assertIsInstance(point["at"], str)
        self.assertEqual(parse_datetime(point["at"]), start)

    def test_a_check_in_at_the_moment_of_login_comes_after_it(self):
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 10, 0))
        ping(self.engineer, at(self.day, 9, 0), 12.10)
        ping(self.engineer, at(self.day, 10, 0), 12.30)
        check_in(self.engineer, at(self.day, 9, 0), 12.20, 77.20)
        check_in(self.engineer, at(self.day, 10, 0), 12.25, 77.25)
        self.assertEqual(
            [p["kind"] for p in self.points()], ["login", "checkin", "checkin", "logout"]
        )


class ParityWithTheDayTimelineTests(NoNetwork, TestCase):
    """The map beside a figure and the day rail must put Login and Logout in
    the same place. Asked of /day itself, on the same day, rather than of a
    copy of its rule."""

    def setUp(self):
        super().setUp()
        self.office = User.objects.create_user(
            username="office-route", password="x", role="superadmin", is_superuser=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.engineer = make_engineer("Parity Tester")
        self.day = timezone.localdate() - datetime.timedelta(days=1)
        engineer, day = self.engineer, self.day

        # A Login with a fix either side of it, the nearer one after.
        duty(engineer, at(day, 9, 2), at(day, 12, 30))
        ping(engineer, at(day, 8, 56), 12.70, 77.70)
        ping(engineer, at(day, 9, 4), 12.71, 77.71)
        # Its Logout between two equally near fixes: the earlier one wins.
        ping(engineer, at(day, 12, 25), 12.72, 77.72)
        ping(engineer, at(day, 12, 35), 12.73, 77.73)
        # A second duty: a Login whose only fix is exactly fifteen minutes away,
        # and a Logout with nothing near it at all.
        duty(engineer, at(day, 14, 0), at(day, 18, 0))
        ping(engineer, at(day, 14, 15), 12.74, 77.74)
        # A third, closed by the system: its Logout is on the rail, placed, but
        # it is not somewhere the engineer marked.
        duty(engineer, at(day, 19, 0), at(day, 20, 0), auto_closed=True)
        ping(engineer, at(day, 19, 1), 12.75, 77.75)
        ping(engineer, at(day, 20, 0), 12.76, 77.76)
        # And a call checked into in between.
        check_in(engineer, at(day, 11, 0), 12.80, 77.80)

    def test_login_logout_and_check_in_sit_where_the_timeline_puts_them(self):
        with ola_off():
            response = self.client.get(
                "/api/tracking/day/", {"engineer": self.engineer.id, "date": self.day.isoformat()}
            )
        self.assertEqual(response.status_code, 200, response.content[:300])
        events = response.json()["events"]

        taps = [e for e in events if e["type"] in ("duty_start", "duty_end")]
        self.assertIn(
            "Auto-closed (no Logout)",
            [e["label"] for e in taps],
            "the fixture's auto-closed duty is not on the rail",
        )
        timeline = [
            ("login" if e["type"] == "duty_start" else "logout", e["latitude"], e["longitude"])
            for e in taps
            if e["label"] != "Auto-closed (no Logout)" and e.get("latitude") is not None
        ]
        points = route_km.key_points(self.engineer, self.day)
        mine = [
            (p["kind"], p["latitude"], p["longitude"])
            for p in points
            if p["kind"] in ("login", "logout") and not p["office"]
        ]
        self.assertEqual(mine, timeline)
        # The taps the rail could place nowhere -- and only those -- are at the
        # engineer's office.
        unplaced = [
            ("login" if e["type"] == "duty_start" else "logout", parse_datetime(e["at"]))
            for e in taps
            if e["label"] != "Auto-closed (no Logout)" and e.get("latitude") is None
        ]
        at_office = [(p["kind"], parse_datetime(p["at"])) for p in points if p["office"]]
        self.assertEqual(at_office, unplaced)
        self.assertEqual(unplaced, [("logout", at(self.day, 18, 0))])
        self.assertEqual(
            [(p["latitude"], p["longitude"]) for p in points if p["office"]],
            [route_km.OFFICES["hosur"]],
        )
        # And what that list is, so a rule changed on both sides at once still
        # shows up here.
        self.assertEqual(
            mine,
            [
                ("login", 12.71, 77.71),
                ("logout", 12.72, 77.72),
                ("login", 12.74, 77.74),
                ("login", 12.75, 77.75),
            ],
        )

        reached = [(e["latitude"], e["longitude"]) for e in events if e["type"] == "reached"]
        checkins = [(p["latitude"], p["longitude"]) for p in points if p["kind"] == "checkin"]
        self.assertEqual(checkins, reached)
        self.assertEqual(checkins, [(12.80, 77.80)])


# -- signature and polyline ---------------------------------------------------


class OfficeTests(WithFakeOla, TestCase):
    """A Login or Logout the phone gave no position for is put at the
    engineer's office (asked for on 14 Oct 2026); one with a fix keeps it."""

    def setUp(self):
        super().setUp()
        self.engineer = make_engineer("Office Tester", branch="Hosur")
        self.day = timezone.localdate() - datetime.timedelta(days=3)

    def points(self):
        return route_km.key_points(self.engineer, self.day)

    def test_a_login_and_logout_with_no_fix_are_at_the_office(self):
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 18, 0))
        check_in(self.engineer, at(self.day, 11, 0), 12.80, 77.80)
        self.assertEqual(
            [(p["kind"], p["latitude"], p["longitude"], p["office"]) for p in self.points()],
            [
                ("login", 12.7242996, 77.826677, True),
                ("checkin", 12.80, 77.80, False),
                ("logout", 12.7242996, 77.826677, True),
            ],
        )

    def test_a_tap_with_a_fix_keeps_its_fix(self):
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 18, 0))
        ping(self.engineer, at(self.day, 9, 3), 12.71, 77.71)
        self.assertEqual(
            [(p["kind"], p["latitude"], p["office"]) for p in self.points()],
            [("login", 12.71, False), ("logout", 12.7242996, True)],
        )

    def test_every_region_has_its_office_whatever_the_spelling_of_its_case(self):
        for branch, office in (
            ("Chennai", (13.0544285, 80.177887)),
            ("  HOSUR ", (12.7242996, 77.826677)),
            ("kanchipuram", (12.8377362, 79.7045409)),
            ("Salem", (11.6700898, 78.1422186)),
            ("Vellore", (12.9491539, 79.1368408)),
        ):
            with self.subTest(branch=branch):
                engineer = make_engineer("Region %s" % branch.strip(), branch=branch)
                self.assertEqual(route_km.office_of(engineer), office)
        self.assertIsNone(route_km.office_of(make_engineer("Elsewhere", branch="Bengaluru")))
        self.assertIsNone(route_km.office_of(make_engineer("Nowhere", branch="")))

    def test_an_auto_closed_logout_is_not_put_at_the_office(self):
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 19, 0), auto_closed=True)
        self.assertEqual([(p["kind"], p["office"]) for p in self.points()], [("login", True)])

    def test_a_check_in_with_no_position_is_still_left_out(self):
        """The office stands in for a Login or Logout only."""
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 18, 0))
        check_in(self.engineer, at(self.day, 11, 0), None, None)
        self.assertEqual([p["kind"] for p in self.points()], ["login", "logout"])

    def test_the_office_is_routed_through_like_any_point(self):
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 18, 0))
        check_in(self.engineer, at(self.day, 11, 0), 12.80, 77.80)
        self.assertEqual(route_km.refresh([self.engineer.id], [self.day]), 1)

        self.assertEqual(
            self.ola.requests,
            [[(12.7242996, 77.826677), (12.80, 77.80), (12.7242996, 77.826677)]],
        )
        row = RouteKmDay.objects.get(engineer=self.engineer, day=self.day)
        self.assertEqual((row.status, row.km), ("ready", 2.0))
        self.assertEqual([p["office"] for p in row.points], [True, False, True])

    def test_a_day_at_the_office_and_back_is_zero_km_without_asking_ola(self):
        duty(self.engineer, at(self.day, 9, 0), at(self.day, 18, 0))
        route_km.refresh([self.engineer.id], [self.day])

        self.assertEqual(self.ola.requests, [])
        row = RouteKmDay.objects.get(engineer=self.engineer, day=self.day)
        self.assertEqual((row.status, row.km, len(row.points)), ("ready", 0.0, 2))
        self.assertEqual(row.path, [[12.7242996, 77.826677]])


class SignatureTests(SimpleTestCase):
    def point(self, kind, lat, lon, at="2026-09-25T03:30:00+00:00"):
        return {"kind": kind, "at": at, "latitude": lat, "longitude": lon, "case_number": None}

    def test_it_is_the_sha1_of_the_kinds_and_places_in_order(self):
        points = [self.point("login", 12.757671, 77.81), self.point("checkin", 12.8, 77.9)]
        expected = hashlib.sha1(b"login:12.75767,77.81000|checkin:12.80000,77.90000").hexdigest()
        self.assertEqual(route_km.signature(points), expected)
        self.assertEqual(len(expected), 40)

    def test_when_does_not_change_it(self):
        a = [self.point("login", 12.75767, 77.81), self.point("logout", 12.8, 77.9)]
        later = [
            self.point("login", 12.75767, 77.81, at="2026-09-25T04:00:00+00:00"),
            self.point("logout", 12.8, 77.9),
        ]
        self.assertEqual(route_km.signature(a), route_km.signature(later))

    def test_a_move_past_the_fifth_decimal_changes_it_and_less_does_not(self):
        a = [self.point("login", 12.757671, 77.81)]
        nudged = [self.point("login", 12.757674, 77.81)]  # a third of a metre
        moved = [self.point("login", 12.757771, 77.81)]  # about eleven metres
        self.assertEqual(route_km.signature(a), route_km.signature(nudged))
        self.assertNotEqual(route_km.signature(a), route_km.signature(moved))

    def test_kind_and_order_matter(self):
        login = self.point("login", 12.7, 77.7)
        call = self.point("checkin", 12.8, 77.8)
        self.assertNotEqual(
            route_km.signature([login, call]), route_km.signature([call, login])
        )
        self.assertNotEqual(
            route_km.signature([login]), route_km.signature([self.point("logout", 12.7, 77.7)])
        )


class DecodePolylineTests(SimpleTestCase):
    def test_googles_own_example(self):
        self.assertEqual(
            route_km.decode_polyline("_p~iF~ps|U_ulLnnqC_mqNvxq`@"),
            [[38.5, -120.2], [40.7, -120.95], [43.252, -126.453]],
        )

    def test_an_empty_line_is_no_points(self):
        self.assertEqual(route_km.decode_polyline(""), [])

    def test_another_precision(self):
        encoded = encode_polyline([(12.123456, 77.654321), (12.2, 77.7)], precision=6)
        self.assertEqual(
            route_km.decode_polyline(encoded, precision=6), [[12.123456, 77.654321], [12.2, 77.7]]
        )

    def test_a_garbled_line_is_refused(self):
        for garbled in ("_p~i", "_p~iF~ps|U_ulL", " ", "_p~iF~ps|U\x7f"):
            with self.subTest(garbled=garbled):
                with self.assertRaises(ValueError):
                    route_km.decode_polyline(garbled)


# -- measuring ------------------------------------------------------------------


@override_settings(OLA_MAPS_API_KEY=TEST_KEY)
class MeasureTests(NoNetwork, SimpleTestCase):
    def points(self, count):
        return [
            {
                "kind": "checkin",
                "at": "2026-09-25T05:00:00+00:00",
                "latitude": round(12.70 + i * 0.01, 5),
                "longitude": round(77.70 + i * 0.01, 5),
                "case_number": None,
            }
            for i in range(count)
        ]

    def answer(self, legs, line=((12.7, 77.7), (12.8, 77.8))):
        return {
            "status": "SUCCESS",
            "routes": [
                {
                    "legs": [{"distance": metres} for metres in legs],
                    "overview_polyline": encode_polyline(line),
                }
            ],
        }

    def test_thirty_points_are_two_requests_that_share_a_point(self):
        ola = FakeOla(leg_metres=1000)
        with mock.patch.object(route_km, "_post", ola):
            km, path = route_km.measure(self.points(30))

        self.assertEqual([len(sent) for sent in ola.requests], [26, 5])
        self.assertEqual(ola.requests[0][-1], ola.requests[1][0], "no leg may be lost at the seam")
        self.assertEqual(km, 29.0)
        # 26 + 5 drawn, the shared point once.
        self.assertEqual(path, [[p["latitude"], p["longitude"]] for p in self.points(30)])
        self.sleep.assert_called_once_with(0.4)
        self.assertEqual(ola.carried_the_key, [True, True])

    def test_an_ordinary_day_is_one_request(self):
        ola = FakeOla(leg_metres=2500)
        with mock.patch.object(route_km, "_post", ola):
            km, path = route_km.measure(self.points(5))
        self.assertEqual([len(sent) for sent in ola.requests], [5])
        self.assertEqual(km, 10.0)
        self.assertEqual(len(path), 5)
        self.sleep.assert_not_called()

    def test_the_km_is_every_legs_distance_added_and_rounded(self):
        with mock.patch.object(route_km, "_post", return_value=self.answer([1234, 5678.4, 1])):
            km, _ = route_km.measure(self.points(4))
        self.assertEqual(km, 6.91)

    def test_it_is_a_post_with_the_points_in_order(self):
        seen = {}

        def urlopen(request, timeout=None):
            seen["method"] = request.get_method()
            seen["query"] = urllib.parse.parse_qs(urllib.parse.urlsplit(request.full_url).query)
            seen["timeout"] = timeout
            return io.BytesIO(json.dumps(self.answer([1000, 1000])).encode())

        with mock.patch("urllib.request.urlopen", urlopen):
            route_km.measure(self.points(3))

        self.assertEqual(seen["method"], "POST")
        # Shorter than the snapper's: it runs inside a request being polled.
        self.assertEqual(seen["timeout"], route_km.TIMEOUT_SECONDS)
        self.assertEqual(route_km.TIMEOUT_SECONDS, 10)
        self.assertEqual(seen["query"]["origin"], ["12.7,77.7"])
        self.assertEqual(seen["query"]["waypoints"], ["12.71,77.71"])
        self.assertEqual(seen["query"]["destination"], ["12.72,77.72"])
        self.assertEqual(seen["query"]["api_key"], [TEST_KEY])

    def test_a_429_is_rate_limited_and_names_neither_the_key_nor_the_url(self):
        error = urllib.error.HTTPError(
            url="https://api.olamaps.io/routing/v1/directions?api_key=" + TEST_KEY,
            code=429,
            msg="Too Many Requests",
            hdrs=None,
            fp=io.BytesIO(b'{"message": "Too many requests"}'),
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(olamaps.RateLimited) as caught:
                route_km.measure(self.points(2))

        message = str(caught.exception)
        self.assertIn("Too many requests", message)
        self.assertNotIn(TEST_KEY, message)
        self.assertNotIn("olamaps.io", message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_an_error_body_that_quotes_the_key_is_withheld(self):
        body = ('{"error": "invalid api_key %s"}' % TEST_KEY).encode()
        error = urllib.error.HTTPError(
            url="x", code=401, msg="Unauthorized", hdrs=None, fp=io.BytesIO(body)
        )
        with mock.patch("urllib.request.urlopen", side_effect=error):
            with self.assertRaises(olamaps.SnapUnavailable) as caught:
                route_km.measure(self.points(2))

        self.assertNotIsInstance(caught.exception, olamaps.RateLimited)
        self.assertIn("HTTP 401", str(caught.exception))
        self.assertNotIn(TEST_KEY, str(caught.exception))

    def test_an_error_that_quotes_the_url_is_withheld(self):
        def urlopen(request, timeout=None):
            raise ValueError("could not open %s" % request.full_url)

        with mock.patch("urllib.request.urlopen", urlopen):
            with self.assertRaises(olamaps.SnapUnavailable) as caught:
                route_km.measure(self.points(2))

        message = str(caught.exception)
        self.assertNotIn(TEST_KEY, message)
        self.assertNotIn("olamaps.io", message)
        self.assertIsNone(caught.exception.__cause__)
        self.assertTrue(caught.exception.__suppress_context__)

    def test_an_ordinary_network_error_keeps_its_reason(self):
        with mock.patch("urllib.request.urlopen", side_effect=urllib.error.URLError("timed out")):
            with self.assertRaises(olamaps.SnapUnavailable) as caught:
                route_km.measure(self.points(2))
        self.assertIn("timed out", str(caught.exception))

    def test_a_body_that_is_not_json_is_unavailable(self):
        with mock.patch("urllib.request.urlopen", return_value=io.BytesIO(b"<html>busy</html>")):
            with self.assertRaises(olamaps.SnapUnavailable) as caught:
                route_km.measure(self.points(2))
        self.assertIn("not JSON", str(caught.exception))

    def test_no_route_is_unavailable(self):
        with mock.patch.object(
            route_km, "_post", return_value={"routes": [], "status": "ZERO_RESULTS"}
        ):
            with self.assertRaises(olamaps.SnapUnavailable) as caught:
                route_km.measure(self.points(3))
        self.assertIn("no route", str(caught.exception))

    def test_a_route_with_a_leg_missing_is_refused_not_added_up_short(self):
        with mock.patch.object(route_km, "_post", return_value=self.answer([1000])):
            with self.assertRaises(olamaps.SnapUnavailable):
                route_km.measure(self.points(3))

    def test_a_leg_with_no_distance_is_refused(self):
        answer = self.answer([1000, 1000])
        del answer["routes"][0]["legs"][1]["distance"]
        with mock.patch.object(route_km, "_post", return_value=answer):
            with self.assertRaises(olamaps.SnapUnavailable):
                route_km.measure(self.points(3))

    def test_a_line_that_does_not_decode_is_refused(self):
        answer = self.answer([1000])
        answer["routes"][0]["overview_polyline"] = "_p~i"
        with mock.patch.object(route_km, "_post", return_value=answer):
            with self.assertRaises(olamaps.SnapUnavailable):
                route_km.measure(self.points(2))

    def test_no_key_asks_ola_nothing(self):
        ola = FakeOla()
        with ola_off(), mock.patch.object(route_km, "_post", ola):
            with self.assertRaises(olamaps.SnapUnavailable):
                route_km.measure(self.points(3))
        self.assertEqual(ola.requests, [])

    def test_fewer_than_two_points_asks_ola_nothing(self):
        ola = FakeOla()
        with mock.patch.object(route_km, "_post", ola):
            for count in (0, 1):
                with self.subTest(count=count):
                    with self.assertRaises(olamaps.SnapUnavailable):
                        route_km.measure(self.points(count))
        self.assertEqual(ola.requests, [])


# -- the cells --------------------------------------------------------------------


@override_settings(OLA_MAPS_API_KEY=TEST_KEY)
class RefreshTests(WithFakeOla, TestCase):
    def setUp(self):
        super().setUp()
        self.ola.leg_metres = 1500
        self.engineer = make_engineer("Refresh Tester")
        self.today = timezone.localdate()
        self.yesterday = self.today - datetime.timedelta(days=1)
        # Four days back: a day stays open for late fixes until 49 h past its
        # midnight, so three days back would not be settled before 01:00.
        self.past = self.today - datetime.timedelta(days=4)

    def row(self, day, engineer=None):
        return RouteKmDay.objects.get(engineer=engineer or self.engineer, day=day)

    def test_a_day_with_one_place_is_no_points_and_final_once_settled(self):
        duty(self.engineer, at(self.past, 9))
        ping(self.engineer, at(self.past, 9), 12.70)

        measured = route_km.refresh([self.engineer.id], [self.past])

        row = self.row(self.past)
        self.assertEqual(measured, 0)
        self.assertEqual(row.status, "no_points")
        self.assertIsNone(row.km)
        self.assertEqual(row.path, [])
        self.assertEqual([p["kind"] for p in row.points], ["login"])
        self.assertTrue(row.final)
        self.assertIsNone(row.measured_at)
        self.assertEqual(self.ola.requests, [])

    def test_a_marked_day_is_measured_and_kept(self):
        marked_day(self.engineer, self.past, checkins=2)
        now = timezone.now()

        measured = route_km.refresh([self.engineer.id], [self.past], now=now)

        row = self.row(self.past)
        self.assertEqual(measured, 1)
        self.assertEqual(row.status, "ready")
        self.assertEqual(row.km, 4.5)  # three legs of 1.5 km
        self.assertEqual([p["kind"] for p in row.points], ["login", "checkin", "checkin", "logout"])
        self.assertEqual(row.path, [[p["latitude"], p["longitude"]] for p in row.points])
        self.assertEqual(row.signature, route_km.signature(row.points))
        self.assertEqual(row.error, "")
        self.assertEqual(row.checked_at, now)
        self.assertEqual(row.measured_at, now)
        self.assertTrue(row.final)
        self.assertEqual([len(sent) for sent in self.ola.requests], [4])

    def test_yesterday_is_final_only_once_it_has_settled(self):
        marked_day(self.engineer, self.yesterday)
        two_days_on = self.today + datetime.timedelta(days=2)

        route_km.refresh([self.engineer.id], [self.yesterday], now=at(self.today, 3, 0))
        self.assertFalse(self.row(self.yesterday).final, "final while fixes may still arrive")

        route_km.refresh([self.engineer.id], [self.yesterday], now=at(two_days_on, 1, 0))
        self.assertTrue(self.row(self.yesterday).final)
        self.assertEqual(len(self.ola.requests), 1, "the same points were measured twice")

        # And a final row is not even read again.
        with mock.patch.object(route_km, "key_points", wraps=route_km.key_points) as read:
            route_km.refresh([self.engineer.id], [self.yesterday], now=at(two_days_on, 9, 0))
        self.assertEqual(read.call_count, 0)

    def test_a_day_settles_once_late_fixes_can_no_longer_arrive(self):
        """Payroll takes a fix up to MAX_BACKLOG_HOURS late; the day settles an
        hour after the last of its fixes could come in."""
        self.assertEqual(route_km.SETTLE_HOURS, 49)
        two_days_on = self.today + datetime.timedelta(days=2)
        self.assertFalse(route_km.settled(self.yesterday, at(two_days_on, 0, 59, 59)))
        self.assertTrue(route_km.settled(self.yesterday, at(two_days_on, 1, 0)))
        self.assertFalse(route_km.settled(self.today, at(self.today, 23, 59, 59)))

    def test_today_is_never_final(self):
        marked_day(self.engineer, self.today)
        route_km.refresh([self.engineer.id], [self.today], now=at(self.today, 23, 59))
        row = self.row(self.today)
        self.assertEqual(row.status, "ready")
        self.assertFalse(row.final)

    def test_a_cell_read_in_the_last_five_minutes_is_left_alone(self):
        marked_day(self.engineer, self.today)
        # Midday, so five minutes on is still today.
        now = at(self.today, 12)
        route_km.refresh([self.engineer.id], [self.today], now=now)

        with mock.patch.object(route_km, "key_points", wraps=route_km.key_points) as read:
            route_km.refresh(
                [self.engineer.id], [self.today], now=now + datetime.timedelta(minutes=4)
            )
            self.assertEqual(read.call_count, 0, "read again inside five minutes")
            route_km.refresh(
                [self.engineer.id], [self.today], now=now + datetime.timedelta(minutes=5)
            )
            self.assertEqual(read.call_count, 1)

        row = self.row(self.today)
        self.assertEqual(row.checked_at, now + datetime.timedelta(minutes=5))
        self.assertEqual(row.measured_at, now)
        self.assertEqual(len(self.ola.requests), 1, "the same points were measured twice")

    def test_an_earlier_open_day_is_read_again_hourly(self):
        marked_day(self.engineer, self.yesterday)
        now = at(self.today, 12)
        route_km.refresh([self.engineer.id], [self.yesterday], now=now)

        with mock.patch.object(route_km, "key_points", wraps=route_km.key_points) as read:
            route_km.refresh(
                [self.engineer.id], [self.yesterday], now=now + datetime.timedelta(minutes=59)
            )
            self.assertEqual(read.call_count, 0, "an earlier day read again inside the hour")
            route_km.refresh(
                [self.engineer.id], [self.yesterday], now=now + datetime.timedelta(minutes=60)
            )
            self.assertEqual(read.call_count, 1)
        self.assertFalse(self.row(self.yesterday).final)

    def test_earlier_open_days_do_not_crowd_out_today(self):
        """Read as often as today, a large team's earlier open days would take
        every poll's reads, oldest first, and today's -- what the report waits
        on -- would never be reached."""
        for day in (self.yesterday, self.today):
            marked_day(self.engineer, day)
        now = at(self.today, 12)
        route_km.refresh([self.engineer.id], [self.yesterday, self.today], now=now)
        later = now + datetime.timedelta(minutes=30)

        route_km.refresh([self.engineer.id], [self.yesterday, self.today], now=later, work_limit=1)

        self.assertEqual(self.row(self.today).checked_at, later)
        self.assertEqual(self.row(self.yesterday).checked_at, now)

    def test_a_new_check_in_is_measured_again(self):
        marked_day(self.engineer, self.today)
        # Midday, so six minutes on is still today.
        now = at(self.today, 12)
        route_km.refresh([self.engineer.id], [self.today], now=now)
        before = self.row(self.today)

        check_in(self.engineer, at(self.today, 15), 12.85, 77.85)
        route_km.refresh([self.engineer.id], [self.today], now=now + datetime.timedelta(minutes=6))

        after = self.row(self.today)
        self.assertEqual(len(self.ola.requests), 2)
        self.assertNotEqual(after.signature, before.signature)
        self.assertEqual(len(after.points), len(before.points) + 1)
        self.assertEqual(after.km, before.km + 1.5)

    def test_the_minute_allowance_counts_every_workers_measurements(self):
        now = timezone.now()
        # Measured by another worker half a minute ago.
        RouteKmDay.objects.create(
            engineer=make_engineer("Elsewhere"),
            day=self.past,
            status="ready",
            km=1.0,
            measured_at=now - datetime.timedelta(seconds=30),
        )
        days = [self.past - datetime.timedelta(days=n) for n in range(4)]
        for day in days:
            marked_day(self.engineer, day)

        with mock.patch.dict(os.environ, {"ROUTE_KM_PER_MINUTE": "3"}):
            measured = route_km.refresh([self.engineer.id], days, now=now, measure_limit=10)

        self.assertEqual(measured, 2)
        self.assertEqual(len(self.ola.requests), 2)
        # The two it did not reach have no row: still pending.
        self.assertEqual(RouteKmDay.objects.filter(engineer=self.engineer).count(), 2)

    def test_a_measurement_over_a_minute_old_is_not_counted(self):
        now = timezone.now()
        RouteKmDay.objects.create(
            engineer=make_engineer("Elsewhere"),
            day=self.past,
            status="ready",
            km=1.0,
            measured_at=now - datetime.timedelta(seconds=61),
        )
        marked_day(self.engineer, self.past)
        with mock.patch.dict(os.environ, {"ROUTE_KM_PER_MINUTE": "1"}):
            self.assertEqual(route_km.refresh([self.engineer.id], [self.past], now=now), 1)

    def test_the_allowance_reads_the_environment_and_never_falls_below_one(self):
        for value, expected in (("", 6), ("9", 9), ("0", 1), ("-4", 1), ("lots", 6)):
            with self.subTest(value=value):
                with mock.patch.dict(os.environ, {"ROUTE_KM_PER_MINUTE": value}):
                    self.assertEqual(route_km.per_minute(), expected)

    def test_a_429_stops_measuring_and_makes_every_worker_wait(self):
        days = [self.past - datetime.timedelta(days=n) for n in range(3)]
        for day in days:
            marked_day(self.engineer, day)
        self.ola.fail = olamaps.RateLimited("rate limited by Ola: slow down")
        now = timezone.now()

        measured = route_km.refresh([self.engineer.id], days, now=now)

        self.assertEqual(measured, 0)
        self.assertEqual(len(self.ola.requests), 1, "kept asking after a 429")
        self.assertAlmostEqual(route_km._cooldown_until - time.monotonic(), 120, delta=5)
        # The refused cell says so, and is still waiting to be measured.
        refused = RouteKmDay.objects.get(engineer=self.engineer)
        self.assertEqual(
            (refused.status, refused.km, refused.error), ("pending", None, route_km.RATE_LIMITED)
        )

        # Another worker -- no cooldown of its own -- asks Ola nothing while the
        # wait written in the database lasts...
        self.ola.fail = None
        route_km._cooldown_until = 0.0
        later = now + datetime.timedelta(seconds=90)
        self.assertEqual(route_km.refresh([self.engineer.id], days, now=later), 0)
        self.assertEqual(len(self.ola.requests), 1)

        # ...and measures again once it is over.
        over = now + datetime.timedelta(seconds=route_km.COOLDOWN_SECONDS + 1)
        self.assertEqual(route_km.refresh([self.engineer.id], days, now=over), 3)
        self.assertEqual(
            set(RouteKmDay.objects.values_list("status", "error")), {("ready", "")}
        )

    def test_a_failed_day_is_tried_again_only_after_an_hour(self):
        marked_day(self.engineer, self.past)
        self.ola.fail = olamaps.SnapUnavailable("HTTP 400 from Ola: bad waypoint")
        now = timezone.now()

        self.assertEqual(route_km.refresh([self.engineer.id], [self.past], now=now), 1)

        row = self.row(self.past)
        self.assertEqual(row.status, "failed")
        self.assertEqual(row.error, "HTTP 400 from Ola: bad waypoint")
        self.assertIsNone(row.km)
        self.assertEqual(row.path, [])
        self.assertEqual(row.measured_at, now)
        self.assertFalse(row.final, "a failure must stay open to a retry")

        self.ola.fail = None
        route_km.refresh([self.engineer.id], [self.past], now=now + datetime.timedelta(minutes=59))
        self.assertEqual(len(self.ola.requests), 1, "retried inside the hour")

        route_km.refresh([self.engineer.id], [self.past], now=now + datetime.timedelta(minutes=60))
        self.assertEqual(len(self.ola.requests), 2)
        row = self.row(self.past)
        self.assertEqual((row.status, row.error), ("ready", ""))
        self.assertTrue(row.final)

    def test_a_long_reason_is_cut_to_fit(self):
        marked_day(self.engineer, self.past)
        self.ola.fail = olamaps.SnapUnavailable("x" * 500)
        route_km.refresh([self.engineer.id], [self.past])
        self.assertEqual(self.row(self.past).error, "x" * 200)

    def test_cells_not_reached_are_left_for_the_next_poll(self):
        days = [self.past - datetime.timedelta(days=n) for n in range(3)]
        for day in days:
            marked_day(self.engineer, day)

        self.assertEqual(route_km.refresh([self.engineer.id], days, measure_limit=1), 1)
        self.assertEqual(list(RouteKmDay.objects.values_list("day", flat=True)), [min(days)])

        self.assertEqual(route_km.refresh([self.engineer.id], days, measure_limit=1), 1)
        self.assertEqual(RouteKmDay.objects.count(), 2)

    def test_a_poll_reads_at_most_work_limit_cells_oldest_day_first(self):
        engineers = [self.engineer, make_engineer("Second"), make_engineer("Third")]
        for engineer in engineers:
            duty(engineer, at(self.past, 9))
            ping(engineer, at(self.past, 9), 12.70)

        route_km.refresh([e.id for e in engineers], [self.yesterday, self.past], work_limit=4)

        lowest = min(e.id for e in engineers)
        self.assertEqual(
            set(RouteKmDay.objects.values_list("engineer_id", "day")),
            {(e.id, self.past) for e in engineers} | {(lowest, self.yesterday)},
        )

    def test_out_of_time_reads_and_measures_nothing_and_leaves_it_all_pending(self):
        """Once nothing more can be measured the reading stops too: the next poll
        reads those cells anyway, and reading them twice is a query wasted."""
        marked_day(self.engineer, self.past)
        one_place = make_engineer("One Place")
        duty(one_place, at(self.past, 9))
        ping(one_place, at(self.past, 9), 12.70)

        with mock.patch.object(route_km, "key_points", wraps=route_km.key_points) as read:
            measured = route_km.refresh(
                [self.engineer.id, one_place.id], [self.past], time_budget=0
            )

        self.assertEqual(measured, 0)
        self.assertEqual(self.ola.requests, [])
        self.assertEqual(read.call_count, 0)
        self.assertFalse(RouteKmDay.objects.exists())

    def test_an_unknown_engineer_is_passed_over(self):
        self.assertEqual(route_km.refresh([987654321], [self.past]), 0)
        self.assertFalse(RouteKmDay.objects.exists())

    def test_two_polls_creating_one_cell_at_once_leave_one_row(self):
        """Neither finds a row to update; the other poll's create lands first, so
        this one's is refused by the unique constraint -- and it updates instead."""
        RouteKmDay.objects.create(engineer=self.engineer, day=self.past)
        real_update = QuerySet.update
        updates = []

        def first_update_finds_nothing(queryset, **fields):
            updates.append(fields)
            if len(updates) == 1:
                return 0
            return real_update(queryset, **fields)

        with mock.patch.object(QuerySet, "update", first_update_finds_nothing):
            route_km._store(
                self.engineer.id, self.past, status="no_points", checked_at=timezone.now()
            )

        self.assertEqual(len(updates), 2)
        self.assertEqual(RouteKmDay.objects.filter(engineer=self.engineer, day=self.past).count(), 1)
        self.assertEqual(self.row(self.past).status, "no_points")


# -- the endpoints ----------------------------------------------------------------


@override_settings(OLA_MAPS_API_KEY=TEST_KEY)
class RouteKmEndpointTests(WithFakeOla, TestCase):
    url = "/api/tracking/route_km/"

    def setUp(self):
        super().setUp()
        self.office = User.objects.create_user(
            username="office-routekm", password="x", role="superadmin", is_superuser=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.engineer = make_engineer("Endpoint Tester")
        self.today = timezone.localdate()
        # Four days back: a day stays open for late fixes until 49 h past its
        # midnight, so three days back would not be settled before 01:00.
        self.past = self.today - datetime.timedelta(days=4)

    def body(self, **changes):
        body = {
            "engineers": [self.engineer.id],
            "from": self.past.isoformat(),
            "to": self.past.isoformat(),
        }
        body.update(changes)
        return body

    def post(self, body):
        return self.client.post(self.url, body, format="json")

    def test_only_staff_may_ask(self):
        engineer_login = User.objects.create_user(
            username="an-engineer", password="x", role="employee"
        )
        self.client.force_authenticate(engineer_login)
        self.assertEqual(self.post(self.body()).status_code, 403)

    def test_a_bad_body_is_400(self):
        too_long = (self.past - datetime.timedelta(days=62)).isoformat()
        for name, body in (
            ("no engineers", {"from": self.past.isoformat(), "to": self.past.isoformat()}),
            ("engineers not a list", self.body(engineers=str(self.engineer.id))),
            ("a fraction", self.body(engineers=[1.5])),
            ("a bool", self.body(engineers=[True])),
            ("a null", self.body(engineers=[None])),
            ("a word", self.body(engineers=["abc"])),
            ("a negative", self.body(engineers=[-1])),
            ("a negative string", self.body(engineers=["-1"])),
            ("a non-ascii digit", self.body(engineers=["²"])),
            ("an object", self.body(engineers=[{"id": 1}])),
            ("past the integer", self.body(engineers=[2**63])),
            ("301 ids", self.body(engineers=list(range(1, 302)))),
            ("no from", {"engineers": [self.engineer.id], "to": self.past.isoformat()}),
            ("no to", {"engineers": [self.engineer.id], "from": self.past.isoformat()}),
            ("an impossible date", self.body(**{"from": "2026-13-01"})),
            ("a word for a date", self.body(**{"from": "yesterday"})),
            ("a number for a date", self.body(**{"from": 20261001})),
            ("from after to", self.body(**{"to": (self.past - datetime.timedelta(days=1)).isoformat()})),
            ("63 days", self.body(**{"from": too_long})),
        ):
            with self.subTest(name):
                response = self.post(body)
                self.assertEqual(response.status_code, 400, response.content[:200])
                self.assertIn("detail", response.json())
        self.assertEqual(self.ola.requests, [])

    def test_a_body_that_is_not_an_object_is_400(self):
        response = self.client.post(self.url, [self.engineer.id], format="json")
        self.assertEqual(response.status_code, 400)

    def test_sixty_two_days_and_three_hundred_ids_are_allowed(self):
        response = self.post(
            self.body(
                engineers=list(range(1, 301)),
                **{"from": (self.past - datetime.timedelta(days=61)).isoformat()},
            )
        )
        self.assertEqual(response.status_code, 200, response.content[:200])
        self.assertEqual(len(response.json()["days"]), 62)

    def test_without_a_key_nothing_is_measured(self):
        marked_day(self.engineer, self.past)
        with ola_off():
            response = self.post(self.body())
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "configured": False,
                "days": [self.past.isoformat()],
                "engineers": [],
                "pending": 0,
                "rechecking": 0,
                "measured_now": 0,
            },
        )
        self.assertEqual(self.ola.requests, [])
        self.assertFalse(RouteKmDay.objects.exists())

    def test_the_days_run_oldest_first_and_stop_at_today(self):
        response = self.post(
            self.body(
                **{
                    "from": (self.today - datetime.timedelta(days=2)).isoformat(),
                    "to": (self.today + datetime.timedelta(days=5)).isoformat(),
                }
            )
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json()["days"],
            [(self.today - datetime.timedelta(days=n)).isoformat() for n in (2, 1, 0)],
        )

    def test_a_range_wholly_after_today_has_no_days(self):
        response = self.post(
            self.body(
                **{
                    "from": (self.today + datetime.timedelta(days=1)).isoformat(),
                    "to": (self.today + datetime.timedelta(days=3)).isoformat(),
                }
            )
        )
        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["days"], [])
        self.assertEqual(payload["engineers"], [{"engineer_id": self.engineer.id, "days": {}}])
        self.assertEqual(payload["pending"], 0)

    def test_the_answer_has_a_cell_for_every_engineer_and_day(self):
        marked_day(self.engineer, self.past)  # Login, one call, Logout: two legs
        one_place = self.past - datetime.timedelta(days=1)
        duty(self.engineer, at(one_place, 9))
        ping(self.engineer, at(one_place, 9), 12.70)
        nothing = self.past - datetime.timedelta(days=2)

        response = self.post(self.body(**{"from": nothing.isoformat()}))

        self.assertEqual(response.status_code, 200, response.content[:300])
        self.assertEqual(
            response.json(),
            {
                "configured": True,
                "days": [nothing.isoformat(), one_place.isoformat(), self.past.isoformat()],
                "engineers": [
                    {
                        "engineer_id": self.engineer.id,
                        "days": {
                            nothing.isoformat(): {
                                "km": None, "points": 0, "status": "no_points", "office": False,
                                "final": True,
                            },
                            one_place.isoformat(): {
                                "km": None, "points": 1, "status": "no_points", "office": False,
                                "final": True,
                            },
                            self.past.isoformat(): {
                                "km": 2.0, "points": 3, "status": "ready", "office": False,
                                "final": True,
                            },
                        },
                    }
                ],
                "pending": 0,
                "rechecking": 0,
                "measured_now": 1,
            },
        )

    def test_pending_counts_what_is_left_and_polling_finishes_it(self):
        start = self.past - datetime.timedelta(days=4)
        days = [start + datetime.timedelta(days=n) for n in range(5)]
        for day in days:
            marked_day(self.engineer, day)

        first = self.post(self.body(**{"from": start.isoformat()})).json()
        self.assertEqual((first["measured_now"], first["pending"]), (3, 2))
        cells = first["engineers"][0]["days"]
        self.assertEqual(
            [cells[day.isoformat()]["status"] for day in days],
            ["ready", "ready", "ready", "pending", "pending"],
        )
        self.assertEqual(
            cells[days[-1].isoformat()],
            {"km": None, "points": 0, "status": "pending", "office": False, "final": False},
        )

        second = self.post(self.body(**{"from": start.isoformat()})).json()
        self.assertEqual((second["measured_now"], second["pending"]), (2, 0))

    def test_a_day_ola_could_not_route_says_so(self):
        marked_day(self.engineer, self.past)
        self.ola.fail = olamaps.SnapUnavailable("HTTP 400 from Ola: bad point")
        cell = self.post(self.body()).json()["engineers"][0]["days"][self.past.isoformat()]
        self.assertEqual(
            cell, {"km": None, "points": 3, "status": "failed", "office": False, "final": False}
        )

    def test_only_engineers_this_user_may_see_are_in_the_answer(self):
        hosur_office = User.objects.create_user(
            username="hosur-office",
            password="x",
            role="hr",
            allowed_sections={"attendance": ["Hosur"]},
        )
        self.client.force_authenticate(hosur_office)
        chennai = make_engineer("Chennai Engineer", branch="Chennai")

        response = self.post(self.body(engineers=[chennai.id, self.engineer.id, 987654321]))

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["engineer_id"] for row in response.json()["engineers"]], [self.engineer.id]
        )
        self.assertFalse(RouteKmDay.objects.filter(engineer=chennai).exists())

    def test_ids_may_be_strings_and_repeat(self):
        response = self.post(self.body(engineers=[str(self.engineer.id), self.engineer.id]))
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [row["engineer_id"] for row in response.json()["engineers"]], [self.engineer.id]
        )


@override_settings(OLA_MAPS_API_KEY=TEST_KEY)
class RouteKmDayEndpointTests(WithFakeOla, TestCase):
    url = "/api/tracking/route_km_day/"

    def setUp(self):
        super().setUp()
        self.office = User.objects.create_user(
            username="office-routeday", password="x", role="superadmin", is_superuser=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.engineer = make_engineer("Day Cell Tester")
        self.today = timezone.localdate()
        # Four days back: a day stays open for late fixes until 49 h past its
        # midnight, so three days back would not be settled before 01:00.
        self.past = self.today - datetime.timedelta(days=4)

    def get(self, engineer=None, date=None):
        params = {
            "engineer": self.engineer.id if engineer is None else engineer,
            "date": self.past.isoformat() if date is None else date,
        }
        return self.client.get(self.url, params)

    def test_only_staff_may_ask(self):
        self.client.force_authenticate(
            User.objects.create_user(username="engineer-day", password="x", role="employee")
        )
        self.assertEqual(self.get().status_code, 403)

    def test_a_bad_request_is_400(self):
        tomorrow = (self.today + datetime.timedelta(days=1)).isoformat()
        for name, params in (
            ("no engineer", {"date": self.past.isoformat()}),
            ("a word", {"engineer": "abc", "date": self.past.isoformat()}),
            ("a negative", {"engineer": "-1", "date": self.past.isoformat()}),
            ("a non-ascii digit", {"engineer": "²", "date": self.past.isoformat()}),
            ("past the integer", {"engineer": str(2**63), "date": self.past.isoformat()}),
            ("no date", {"engineer": self.engineer.id}),
            ("an impossible date", {"engineer": self.engineer.id, "date": "2026-13-01"}),
            ("a word for a date", {"engineer": self.engineer.id, "date": "yesterday"}),
            ("tomorrow", {"engineer": self.engineer.id, "date": tomorrow}),
        ):
            with self.subTest(name):
                response = self.client.get(self.url, params)
                self.assertEqual(response.status_code, 400, response.content[:200])
                self.assertIn("detail", response.json())

    def test_an_unknown_engineer_is_404(self):
        self.assertEqual(self.get(engineer=987654321).status_code, 404)

    def test_another_branch_is_403(self):
        self.client.force_authenticate(
            User.objects.create_user(
                username="chennai-office",
                password="x",
                role="hr",
                allowed_sections={"attendance": ["Chennai"]},
            )
        )
        self.assertEqual(self.get().status_code, 403)

    def test_without_a_key(self):
        marked_day(self.engineer, self.past)
        with ola_off():
            response = self.get()
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.json(),
            {
                "configured": False,
                "engineer_id": self.engineer.id,
                "date": self.past.isoformat(),
                "status": "pending",
                "km": None,
                "points": [],
                "path": [],
                "error": "",
                "final": False,
            },
        )
        self.assertEqual(self.ola.requests, [])

    def test_a_measured_day(self):
        marked_day(self.engineer, self.past)
        response = self.get()
        self.assertEqual(response.status_code, 200, response.content[:300])
        payload = response.json()

        self.assertEqual(
            {k: payload[k] for k in ("configured", "engineer_id", "date", "status", "km", "error")},
            {
                "configured": True,
                "engineer_id": self.engineer.id,
                "date": self.past.isoformat(),
                "status": "ready",
                "km": 2.0,
                "error": "",
            },
        )
        self.assertEqual([p["kind"] for p in payload["points"]], ["login", "checkin", "logout"])
        self.assertEqual(
            set(payload["points"][0]),
            {"kind", "at", "latitude", "longitude", "case_number", "office"},
        )
        self.assertIsNotNone(payload["points"][1]["case_number"])
        self.assertEqual(payload["path"], [[12.70, 77.70], [12.75, 77.75], [12.90, 77.90]])

    def test_a_day_ola_could_not_route(self):
        marked_day(self.engineer, self.past)
        self.ola.fail = olamaps.SnapUnavailable("HTTP 400 from Ola: bad point")
        payload = self.get().json()
        self.assertEqual(payload["status"], "failed")
        self.assertIsNone(payload["km"])
        self.assertEqual(payload["path"], [])
        self.assertEqual(payload["error"], "HTTP 400 from Ola: bad point")
        self.assertEqual(len(payload["points"]), 3)

    def test_a_day_with_one_place(self):
        duty(self.engineer, at(self.past, 9))
        ping(self.engineer, at(self.past, 9), 12.70)
        payload = self.get().json()
        self.assertEqual(
            (payload["status"], payload["km"], payload["path"], payload["error"]),
            ("no_points", None, [], ""),
        )
        self.assertEqual([p["kind"] for p in payload["points"]], ["login"])

    def test_no_allowance_left_is_pending_with_the_points_read_now(self):
        RouteKmDay.objects.create(
            engineer=make_engineer("Elsewhere"),
            day=self.past,
            status="ready",
            km=1.0,
            measured_at=timezone.now() - datetime.timedelta(seconds=10),
        )
        marked_day(self.engineer, self.past)
        with mock.patch.dict(os.environ, {"ROUTE_KM_PER_MINUTE": "1"}):
            payload = self.get().json()

        self.assertEqual(
            (payload["status"], payload["km"], payload["path"], payload["error"]),
            ("pending", None, [], ""),
        )
        self.assertEqual([p["kind"] for p in payload["points"]], ["login", "checkin", "logout"])
        self.assertEqual(self.ola.requests, [])
        self.assertFalse(RouteKmDay.objects.filter(engineer=self.engineer).exists())

    def test_only_the_cell_asked_about_is_touched(self):
        marked_day(self.engineer, self.past)
        marked_day(self.engineer, self.past - datetime.timedelta(days=1))
        self.get()
        self.assertEqual(
            list(RouteKmDay.objects.values_list("engineer_id", "day")),
            [(self.engineer.id, self.past)],
        )


# -- the command --------------------------------------------------------------------


@override_settings(OLA_MAPS_API_KEY=TEST_KEY)
class RouteKmCommandTests(WithFakeOla, TestCase):
    def setUp(self):
        super().setUp()
        self.ola.leg_metres = 1200
        self.engineer = make_engineer("Command Tester")
        self.day = timezone.localdate() - datetime.timedelta(days=2)
        marked_day(self.engineer, self.day)

    def run_command(self, who):
        out = io.StringIO()
        call_command("route_km", engineer=who, date=self.day.isoformat(), stdout=out)
        return out.getvalue()

    def test_it_prints_the_points_the_km_and_a_google_link_and_changes_nothing(self):
        text = self.run_command(str(self.engineer.id))

        case_number = Case.objects.get(assigned_to=self.engineer).case_number
        self.assertIn("3 points", text)
        self.assertIn(case_number, text)
        self.assertIn("09:00:00", text)
        self.assertIn("2.40 km", text)
        self.assertIn(
            "https://www.google.com/maps/dir/12.7,77.7/12.75,77.75/12.9,77.9", text
        )
        self.assertNotIn(TEST_KEY, text)
        self.assertFalse(RouteKmDay.objects.exists(), "the command wrote a row")

    def test_an_engineer_can_be_named(self):
        self.assertIn("3 points", self.run_command("Command Tes"))


# -- what the review found ----------------------------------------------------


class ReviewFixTests(WithFakeOla, TestCase):
    """What the review of ROUTE KM found, held in place."""

    def setUp(self):
        super().setUp()
        # No office for this branch: every point is where it was marked.
        self.engineer = make_engineer("Review Tester", branch="Bengaluru")
        self.today = timezone.localdate()
        self.past = self.today - datetime.timedelta(days=4)
        self.office = User.objects.create_user(
            username="office-review", password="x", role="superadmin", is_superuser=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.office)

    def row(self, day, engineer=None):
        return RouteKmDay.objects.get(engineer=engineer or self.engineer, day=day)

    def visit(self, case, engineer, day, hour, lat, lon):
        return CaseVisit.objects.create(
            case=case,
            engineer=engineer,
            plan_date=day,
            checked_in_at=at(day, hour),
            punch_in_lat=lat,
            punch_in_lon=lon,
        )

    def call(self, number, engineer):
        return Case.objects.create(
            case_number=number,
            external_ref=f"WO-{number}",
            customer_name="A customer",
            title=f"Service call {number}",
            assigned_to=engineer,
        )

    # Check-ins belong to whoever made the visit.

    def test_a_check_in_stays_with_whoever_made_it_after_the_call_moves_on(self):
        nextday = make_engineer("Next Day Engineer", branch="Bengaluru")
        case = self.call("OC-MOVED", self.engineer)
        self.visit(case, self.engineer, self.past, 11, 12.80, 77.80)
        # Sent on to someone else the next day: the case is theirs now.
        case.assigned_to = nextday
        case.save(update_fields=["assigned_to"])

        self.assertEqual(
            [(p["kind"], p["case_number"]) for p in route_km.key_points(self.engineer, self.past)],
            [("checkin", "OC-MOVED")],
        )
        self.assertEqual(route_km.key_points(nextday, self.past), [])

    def test_a_visit_with_no_engineer_falls_back_to_the_case(self):
        case = self.call("OC-NOBODY", self.engineer)
        self.visit(case, None, self.past, 11, 12.80, 77.80)
        self.assertEqual(
            [p["case_number"] for p in route_km.key_points(self.engineer, self.past)], ["OC-NOBODY"]
        )

    def test_a_call_with_no_visit_answers_from_its_own_columns(self):
        check_in(self.engineer, at(self.past, 11), 12.80, 77.80)
        self.assertEqual([p["kind"] for p in route_km.key_points(self.engineer, self.past)], ["checkin"])

    # A failed day does not go on for ever, nor ahead of new ones.

    def test_a_settled_day_that_failed_every_try_stops_being_tried(self):
        marked_day(self.engineer, self.past)
        self.ola.fail = olamaps.SnapUnavailable("HTTP 400 from Ola: no road")
        now = timezone.now()
        for attempt in range(route_km.MAX_FAILED_ATTEMPTS):
            route_km.refresh(
                [self.engineer.id], [self.past], now=now + datetime.timedelta(minutes=61 * attempt)
            )
        row = self.row(self.past)
        self.assertEqual((row.status, row.attempts, row.final), ("failed", 3, True))

        asked = len(self.ola.requests)
        route_km.refresh([self.engineer.id], [self.past], now=now + datetime.timedelta(days=1))
        self.assertEqual(len(self.ola.requests), asked)

    def test_today_failing_stays_open_whatever_its_tries(self):
        marked_day(self.engineer, self.today)
        self.ola.fail = olamaps.SnapUnavailable("HTTP 503 from Ola: down")
        now = timezone.now()
        for attempt in range(route_km.MAX_FAILED_ATTEMPTS + 1):
            route_km.refresh(
                [self.engineer.id], [self.today], now=now + datetime.timedelta(minutes=61 * attempt)
            )
        row = self.row(self.today)
        self.assertEqual((row.status, row.final), ("failed", False))

    def test_a_success_clears_the_count_of_failures(self):
        marked_day(self.engineer, self.past)
        now = timezone.now()
        self.ola.fail = olamaps.SnapUnavailable("HTTP 503 from Ola: down")
        route_km.refresh([self.engineer.id], [self.past], now=now)
        self.ola.fail = None
        route_km.refresh([self.engineer.id], [self.past], now=now + datetime.timedelta(minutes=61))
        row = self.row(self.past)
        self.assertEqual((row.status, row.attempts), ("ready", 0))

    def test_a_day_never_measured_goes_before_a_failed_one_due_again(self):
        older = self.past - datetime.timedelta(days=1)
        marked_day(self.engineer, older)
        marked_day(self.engineer, self.past)
        two_hours_ago = timezone.now() - datetime.timedelta(hours=2)
        RouteKmDay.objects.create(
            engineer=self.engineer,
            day=older,
            status="failed",
            attempts=1,
            measured_at=two_hours_ago,
            checked_at=two_hours_ago,
        )

        route_km.refresh([self.engineer.id], [older, self.past], measure_limit=1)

        self.assertEqual(self.row(self.past).status, "ready")
        self.assertEqual(self.row(older).status, "failed")

    # The allowance per minute, across workers.

    def test_a_day_in_one_place_is_zero_km_even_with_the_minute_spent(self):
        """No request is spent on it, so the allowance does not hold it up."""
        RouteKmDay.objects.create(
            engineer=make_engineer("Elsewhere"), day=self.past, status="ready", measured_at=timezone.now()
        )
        office_day = make_engineer("Office Day", branch="Hosur")
        duty(office_day, at(self.past, 9), at(self.past, 18))
        with mock.patch.dict(os.environ, {"ROUTE_KM_PER_MINUTE": "1"}):
            self.assertEqual(route_km.refresh([office_day.id], [self.past]), 0)
        row = self.row(self.past, office_day)
        self.assertEqual((row.status, row.km), ("ready", 0.0))
        self.assertEqual(self.ola.requests, [])

    def test_a_cell_another_worker_is_measuring_is_left_to_it(self):
        marked_day(self.engineer, self.past)
        now = timezone.now()
        # Claimed by another worker twenty seconds ago, its answer not back yet.
        RouteKmDay.objects.create(
            engineer=self.engineer, day=self.past, measured_at=now - datetime.timedelta(seconds=20)
        )
        self.assertEqual(route_km.refresh([self.engineer.id], [self.past], now=now), 0)
        self.assertEqual(self.ola.requests, [])

    def test_claiming_a_new_cell_never_undoes_another_workers_answer(self):
        """The row is made only if it is still absent: one measured since this
        worker looked keeps its status and km."""
        marked_day(self.engineer, self.past)
        RouteKmDay.objects.create(
            engineer=self.engineer,
            day=self.past,
            status="ready",
            km=4.2,
            measured_at=timezone.now() - datetime.timedelta(seconds=10),
        )
        self.assertEqual(route_km._claim(self.engineer.id, self.past, None, timezone.now()), "taken")
        row = self.row(self.past)
        self.assertEqual((row.status, row.km), ("ready", 4.2))

    # Pending is what asking again may still change.

    def test_a_stored_day_whose_points_moved_keeps_the_report_polling(self):
        # Today, read six minutes ago with only a Login.
        duty(self.engineer, at(self.today, 0, 1))
        ping(self.engineer, at(self.today, 0, 2), 12.70)
        six_minutes_ago = timezone.now() - datetime.timedelta(minutes=6)
        RouteKmDay.objects.create(
            engineer=self.engineer,
            day=self.today,
            status="no_points",
            checked_at=six_minutes_ago,
            points=[],
        )
        check_in(self.engineer, at(self.today, 0, 3), 12.80, 77.80)
        # The allowance for this minute spent elsewhere: it cannot be measured now.
        RouteKmDay.objects.create(
            engineer=make_engineer("Elsewhere"), day=self.past, status="ready", measured_at=timezone.now()
        )
        with mock.patch.dict(os.environ, {"ROUTE_KM_PER_MINUTE": "1"}):
            answer = self.client.post(
                "/api/tracking/route_km/",
                {
                    "engineers": [self.engineer.id],
                    "from": self.today.isoformat(),
                    "to": self.today.isoformat(),
                },
                format="json",
            ).json()
        self.assertEqual(
            (answer["pending"], answer["rechecking"]),
            (0, 1),
            "today's day whose points moved was answered as settled",
        )
        self.assertEqual(self.ola.requests, [])

    def test_is_pending(self):
        now = timezone.now()
        recent = now - datetime.timedelta(minutes=1)
        old = now - datetime.timedelta(minutes=route_km.RECHECK_MINUTES + 1)
        today = timezone.localdate(now)
        before = today - datetime.timedelta(days=1)
        # Waiting on Ola: never measured, or failed and due a retry.
        self.assertTrue(route_km.is_pending(None, now))
        self.assertTrue(route_km.is_pending(RouteKmDay(status="pending"), now))
        self.assertFalse(route_km.is_pending(RouteKmDay(status="ready", day=today, checked_at=old), now))
        self.assertFalse(route_km.is_pending(RouteKmDay(status="failed", measured_at=recent), now))
        self.assertTrue(
            route_km.is_pending(
                RouteKmDay(status="failed", measured_at=now - datetime.timedelta(minutes=61)), now
            )
        )
        self.assertFalse(
            route_km.is_pending(
                RouteKmDay(status="failed", final=True, measured_at=now - datetime.timedelta(hours=9)),
                now,
            )
        )
        # To be read again: today's measured cells, once RECHECK_MINUTES old.
        self.assertTrue(route_km.is_rechecking(RouteKmDay(status="ready", day=today, checked_at=old), now))
        self.assertTrue(
            route_km.is_rechecking(RouteKmDay(status="no_points", day=today, checked_at=old), now)
        )
        self.assertFalse(
            route_km.is_rechecking(RouteKmDay(status="ready", day=today, checked_at=recent), now)
        )
        self.assertFalse(
            route_km.is_rechecking(RouteKmDay(status="ready", day=before, checked_at=old), now)
        )
        self.assertFalse(route_km.is_rechecking(RouteKmDay(status="failed", day=today, checked_at=old), now))
        self.assertFalse(route_km.is_rechecking(None, now))

    # The answer.

    def test_a_cell_says_when_its_km_rests_on_the_office(self):
        office_day = make_engineer("Office Day", branch="Hosur")
        duty(office_day, at(self.past, 9), at(self.past, 18))
        check_in(office_day, at(self.past, 11), 12.80, 77.80)
        answer = self.client.post(
            "/api/tracking/route_km/",
            {"engineers": [office_day.id], "from": self.past.isoformat(), "to": self.past.isoformat()},
            format="json",
        ).json()
        cell = answer["engineers"][0]["days"][self.past.isoformat()]
        self.assertEqual((cell["status"], cell["office"]), ("ready", True))

    def test_an_id_too_long_to_read_is_a_400_not_a_500(self):
        response = self.client.post(
            "/api/tracking/route_km/",
            {"engineers": ["9" * 5000], "from": self.past.isoformat(), "to": self.past.isoformat()},
            format="json",
        )
        self.assertEqual(response.status_code, 400)


class WhoMadeTheVisitTests(WithFakeOla, TestCase):
    """A check-in is credited to whoever made it -- once, to one engineer --
    however the call was booked and re-booked."""

    def setUp(self):
        super().setUp()
        self.bot = User.objects.create_user(
            username="opencall-bot-who", password="x", role="admin", is_staff=True
        )
        self.bot_client = APIClient()
        self.bot_client.force_authenticate(self.bot)
        self.first = self.engineer("First Booked", "first-booked")
        self.second = self.engineer("Second Booked", "second-booked")
        self.today = timezone.localdate()

    def engineer(self, name, username):
        user = User.objects.create_user(username=username, password="x", role="employee")
        employee = Employee.objects.create(
            user=user,
            employee_name=name,
            email=f"{username}@example.com",
            role="Service engineer",
            department="Service",
            branch="Bengaluru",
            salary=Decimal("20000"),
        )
        employee.client = APIClient()
        employee.client.force_authenticate(user)
        return employee

    def dispatch(self, engineer, plan_date, ref="WO-WHO-1"):
        body = {
            "plan_date": plan_date.isoformat(),
            "cases": [
                {
                    "external_ref": ref,
                    "title": f"Service call ({ref})",
                    "customer_name": "A customer",
                    "engineer_name": engineer.employee_name,
                    "status": "assigned",
                }
            ],
        }
        response = self.bot_client.post("/api/cases/bulk_dispatch/", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return Case.objects.get(external_ref=ref)

    def test_a_call_moved_on_the_same_day_is_credited_to_whoever_checked_in(self):
        self.dispatch(self.first, self.today)
        case = self.dispatch(self.second, self.today)
        self.assertEqual(case.assigned_to, self.second)

        response = self.second.client.post(
            f"/api/cases/{case.id}/punch_in/",
            {"latitude": 12.80, "longitude": 77.80, "accuracy": 9},
            format="json",
        )
        self.assertIn(response.status_code, (200, 201), getattr(response, "data", None))

        visit = case.visits.get(plan_date=self.today)
        self.assertEqual(visit.engineer, self.second)
        self.assertEqual(
            [p["kind"] for p in route_km.key_points(self.second, self.today)], ["checkin"]
        )
        self.assertEqual(route_km.key_points(self.first, self.today), [])

    def test_a_check_in_from_before_a_move_is_not_also_read_off_the_case(self):
        """The case's columns still hold the first engineer's punch after it is
        sent on; the visit that day says whose it was, so the columns are not
        read as well."""
        day = self.today - datetime.timedelta(days=4)
        case = Case.objects.create(
            case_number="OC-WHO-2",
            external_ref="WO-WHO-2",
            customer_name="A customer",
            title="Service call",
            assigned_to=self.first,
            reached_at=at(day, 11),
            punch_in_lat=12.80,
            punch_in_lon=77.80,
        )
        CaseVisit.objects.create(
            case=case,
            engineer=self.first,
            plan_date=day,
            checked_in_at=at(day, 11),
            punch_in_lat=12.80,
            punch_in_lon=77.80,
        )
        case.assigned_to = self.second
        case.save(update_fields=["assigned_to"])

        self.assertEqual(
            [p["case_number"] for p in route_km.key_points(self.first, day)], ["OC-WHO-2"]
        )
        self.assertEqual(route_km.key_points(self.second, day), [])

    # Rows production already holds: before the check-in named its maker, a
    # call moved on the same day kept the first-booked engineer on its visit.

    def test_an_old_row_naming_the_first_booked_engineer_goes_to_who_checked_in(self):
        day = self.today - datetime.timedelta(days=4)
        case = Case.objects.create(
            case_number="OC-WHO-3",
            external_ref="WO-WHO-3",
            customer_name="A customer",
            title="Service call",
            assigned_to=self.second,
            # Handed to the second engineer at 10:00, before they checked in.
            assigned_at=at(day, 10),
            reached_at=at(day, 11),
            punch_in_lat=12.80,
            punch_in_lon=77.80,
        )
        CaseVisit.objects.create(
            case=case,
            engineer=self.first,  # the first booking, as the old check-in left it
            plan_date=day,
            checked_in_at=at(day, 11),
            punch_in_lat=12.80,
            punch_in_lon=77.80,
        )

        self.assertEqual(
            [p["case_number"] for p in route_km.key_points(self.second, day)], ["OC-WHO-3"]
        )
        self.assertEqual(route_km.key_points(self.first, day), [])

    def test_a_check_in_made_before_the_call_was_handed_on_stays_with_its_maker(self):
        day = self.today - datetime.timedelta(days=4)
        case = Case.objects.create(
            case_number="OC-WHO-4",
            external_ref="WO-WHO-4",
            customer_name="A customer",
            title="Service call",
            assigned_to=self.second,
            # Handed on the next day, after the first engineer's check-in.
            assigned_at=at(day + datetime.timedelta(days=1), 9),
        )
        CaseVisit.objects.create(
            case=case,
            engineer=self.first,
            plan_date=day,
            checked_in_at=at(day, 11),
            punch_in_lat=12.80,
            punch_in_lon=77.80,
        )

        self.assertEqual(
            [p["case_number"] for p in route_km.key_points(self.first, day)], ["OC-WHO-4"]
        )
        self.assertEqual(route_km.key_points(self.second, day), [])

    def test_a_429_on_a_failed_days_retry_keeps_its_reason_and_its_hour(self):
        day = self.today - datetime.timedelta(days=4)
        marked_day(self.first, day)
        two_hours_ago = timezone.now() - datetime.timedelta(hours=2)
        RouteKmDay.objects.create(
            engineer=self.first,
            day=day,
            status="failed",
            error="HTTP 400 from Ola: no road",
            attempts=1,
            measured_at=two_hours_ago,
            checked_at=two_hours_ago,
        )
        self.ola.fail = olamaps.RateLimited("rate limited by Ola: slow down")

        self.assertEqual(route_km.refresh([self.first.id], [day]), 0)

        row = RouteKmDay.objects.get(engineer=self.first, day=day)
        self.assertEqual(
            (row.status, row.error, row.attempts, row.measured_at),
            ("failed", "HTTP 400 from Ola: no road", 1, two_hours_ago),
        )
