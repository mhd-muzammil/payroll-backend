"""FIELDEZ: ROUTE KM's road, with each check-in where FieldEZ placed its WO's
Problem Resolution.

What these tests hold it to:

  A check-in stands at the Problem Resolution FieldEZ recorded, never at the
  phone's fix; one FieldEZ has no place for is left out and named with why.
  Login and Logout are ROUTE KM's own.

  A day whose WOs the worker has not read waits -- and is read again as soon
  as the worker has read them, not an hour later (found by the end-to-end run
  of 15 Oct 2026). A day is not final while FieldEZ may still place a check-in
  it left out.

  FIELDEZ comes after ROUTE KM for Ola, and never writes ROUTE KM's table.

Nothing here reaches the network: Ola is cases.test_route_km's FakeOla, and
urlopen stops the test if anything gets past it.
"""
import datetime
import json
import os
from unittest import mock

from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone
from django.utils.dateparse import parse_datetime
from rest_framework.test import APIClient

from authentication.models import User
from cases import olamaps, route_km
from cases.models import Case, CaseVisit, RouteKmDay
from cases.test_route_km import (
    TEST_KEY,
    NoNetwork,
    WithFakeOla,
    at,
    duty,
    make_engineer,
    ola_off,
    ping,
)

from . import km
from .models import FieldezKmDay, FieldezWorkOrder
from .points import (
    AWAY_KM,
    MAX_WANTED_DAYS,
    away_from_checkin,
    choose_resolution,
    clean_resolution,
    fieldez_points,
    readable,
    wanted_work_orders,
    wo_key,
)
from .views import _save_result

MINUTE = datetime.timedelta(minutes=1)
DAY = datetime.timedelta(days=1)

# Where the phone was at each check-in, and where FieldEZ put the Problem
# Resolution: far enough apart that a test can tell which one was used.
PHONE = (12.80, 77.80)
FIELDEZ_PLACE = (12.95, 77.95)


def visit(engineer, when, wo, lat=PHONE[0], lon=PHONE[1]):
    """A call `engineer` checked into at `when` with the phone at lat, lon, the
    way the app records it now: a case with its WO, and a visit."""
    case = Case.objects.create(
        external_ref=wo or "",
        customer_name="A customer",
        title="Service call",
        assigned_to=engineer,
        assigned_at=when - datetime.timedelta(hours=1),
    )
    CaseVisit.objects.create(
        case=case,
        engineer=engineer,
        plan_date=timezone.localtime(when).date(),
        assigned_at=when - datetime.timedelta(hours=1),
        checked_in_at=when,
        checked_out_at=when + datetime.timedelta(minutes=30),
        punch_in_lat=lat,
        punch_in_lon=lon,
    )
    return case


def resolution(when, lat=FIELDEZ_PLACE[0], lon=FIELDEZ_PLACE[1], **extra):
    """One Problem Resolution, as the worker posts it."""
    return {"at": when.isoformat(), "user": "An engineer", "latitude": lat, "longitude": lon, "address": "A road", **extra}


def order(wo, status="found", resolutions=None, attempts=0, fetched_at=None):
    """What the worker read for `wo`, as stored."""
    return FieldezWorkOrder.objects.create(
        wo_key=wo,
        status=status,
        resolutions=[clean_resolution(entry) for entry in resolutions or []],
        attempts=attempts,
        fetched_at=fetched_at or timezone.now(),
    )


def logged_day(engineer, day, login=(12.70, 77.70), logout=(12.90, 77.90)):
    """A Login at 09:00 and a Logout at 18:00, each with a fix."""
    duty(engineer, at(day, 9), at(day, 18))
    ping(engineer, at(day, 9), *login)
    ping(engineer, at(day, 18), *logout)


class FieldezMixin(WithFakeOla):
    """A fake Ola, FIELDEZ's own default allowance, and its 429 clock clear."""

    def setUp(self):
        super().setUp()
        allowance = mock.patch.dict(os.environ, {"FIELDEZ_KM_PER_MINUTE": ""})
        allowance.start()
        self.addCleanup(allowance.stop)
        km._cooldown_until = 0.0
        self.addCleanup(setattr, km, "_cooldown_until", 0.0)


# -- a WO, and a Problem Resolution -------------------------------------------


class WoKeyTests(SimpleTestCase):
    def test_the_ways_a_wo_is_written_are_one_wo(self):
        for text in ("WO-036165368", "wo036165368", "WO 036 165 368", " wo-036165368 ", "Wo-036165368"):
            self.assertEqual(wo_key(text), "WO-036165368", text)

    def test_a_revisit_is_its_own_wo(self):
        self.assertEqual(wo_key("WO-035260625-1"), "WO-035260625-1")
        self.assertNotEqual(wo_key("WO-035260625-1"), wo_key("WO-035260625"))

    def test_what_is_not_a_wo(self):
        for text in (None, "", "   ", 36165368, "036165368", "WO-12345", "SN-036165368", "WO-036165368-", "WO--036165368", "WO-٠٣٦١٦٥٣٦٨"):
            self.assertIsNone(wo_key(text), repr(text))

    def test_a_key_too_long_to_store_is_not_a_wo(self):
        self.assertIsNone(wo_key("WO-" + "1" * 40))


class ResolutionTests(SimpleTestCase):
    def test_a_resolution_is_kept_in_utc_with_its_text_trimmed(self):
        self.assertEqual(
            clean_resolution(
                {"at": "2026-10-12T10:57:55+05:30", "user": " Lava ", "latitude": 13.09206, "longitude": 79.997763, "address": None}
            ),
            {"at": "2026-10-12T05:27:55+00:00", "user": "Lava", "latitude": 13.09206, "longitude": 79.997763, "address": ""},
        )

    def test_a_resolution_that_cannot_place_anything_is_refused(self):
        good = resolution(at(datetime.date(2026, 10, 12), 11))
        for name, change in (
            ("0 latitude: FieldEZ's no location", {"latitude": 0}),
            ("0 longitude", {"longitude": 0.0}),
            ("not finite", {"latitude": float("nan")}),
            ("infinite", {"longitude": float("inf")}),
            ("out of range", {"latitude": 91}),
            ("a bool", {"latitude": True}),
            ("a string", {"latitude": "13.09"}),
            ("a time with no offset", {"at": "2026-10-12T10:57:55"}),
            ("past the worker's years", {"at": "9999-12-31T23:00:00+00:00"}),
            ("before them", {"at": "1999-12-31T23:00:00+00:00"}),
            ("not a time", {"at": "yesterday"}),
            ("no time", {"at": None}),
            ("a user that is not text", {"user": 7}),
        ):
            self.assertIsNone(clean_resolution({**good, **change}), name)
        self.assertIsNone(clean_resolution("not a dict"))

    def test_what_postgresql_cannot_store_is_taken_out_of_the_text(self):
        # NUL, and half a surrogate pair, would make the save a 500 on
        # PostgreSQL. Emoji and Tamil stay as they are.
        kept = clean_resolution(
            {**resolution(at(datetime.date(2026, 10, 12), 11)), "user": "Lava\x00 kumar \ud83d", "address": "\udc00தமிழ் Road 🙂\x00"}
        )
        self.assertEqual((kept["user"], kept["address"]), ("Lava kumar", "தமிழ் Road 🙂"))

    def test_the_check_ins_own_ist_day_comes_first(self):
        # 04:00 IST on 13 Oct is 12 Oct in UTC. The resolution 4 h before is
        # nearer but on 12 Oct in IST; the one 6 h after is on the same IST day.
        checked_in = at(datetime.date(2026, 10, 13), 4)
        before = resolution(at(datetime.date(2026, 10, 12), 23, 59), lat=11.0)
        same_day = resolution(at(datetime.date(2026, 10, 13), 10), lat=12.0)
        self.assertEqual(choose_resolution([before, same_day], checked_in)["latitude"], 12.0)

    def test_then_the_nearest_in_time_and_the_earlier_on_a_tie(self):
        day = datetime.date(2026, 10, 13)
        checked_in = at(day, 11)
        self.assertEqual(
            choose_resolution([resolution(at(day, 9), lat=11.0), resolution(at(day, 12), lat=12.0)], checked_in)["latitude"],
            12.0,
        )
        self.assertEqual(
            choose_resolution([resolution(at(day, 12), lat=12.0), resolution(at(day, 10), lat=10.0)], checked_in)["latitude"],
            10.0,
        )

    def test_another_days_resolution_when_the_day_has_none(self):
        checked_in = at(datetime.date(2026, 10, 13), 11)
        chosen = choose_resolution([resolution(at(datetime.date(2026, 10, 15), 11), lat=15.0)], checked_in)
        self.assertEqual(chosen["latitude"], 15.0)

    def test_nothing_usable_is_no_resolution(self):
        checked_in = at(datetime.date(2026, 10, 13), 11)
        self.assertIsNone(choose_resolution([], checked_in))
        self.assertIsNone(choose_resolution(None, checked_in))
        self.assertIsNone(choose_resolution([{"at": "2026-10-13T11:00:00", "latitude": 1, "longitude": 1}], checked_in))


# -- the points ---------------------------------------------------------------



class AwayTests(SimpleTestCase):
    """A Problem Resolution pressed away from the call (asked 16 Oct 2026):
    Mohan's 13 Oct, where the Anna Nagar call's was pressed at 8:30 PM from
    the next call in Madha Nagar."""

    def point(self, kind, at, lat, lon, case_number=None, **extra):
        return {"kind": kind, "at": at, "latitude": lat, "longitude": lon, "case_number": case_number, **extra}

    def test_a_check_in_placed_more_than_a_km_from_where_it_was_made_is_named(self):
        route = [
            self.point("login", "2026-10-13T03:33:00+00:00", 13.0055, 80.1094),
            self.point("checkin", "2026-10-13T08:05:00+00:00", 13.0711963, 80.2412456, "OC-004698"),
            self.point("checkin", "2026-10-13T11:01:00+00:00", 13.0906842, 80.2233519, "OC-004075"),
        ]
        fieldez = [
            self.point("login", "2026-10-13T03:33:00+00:00", 13.0055, 80.1094),
            # At the customer's: 2 m from the check-in.
            self.point(
                "checkin", "2026-10-13T08:05:00+00:00", 13.0712056, 80.2412661, "OC-004698",
                wo="WO-036176428", resolved_at="2026-10-13T08:00:29+00:00", address="Gee Gee Universal",
            ),
            # From Madha Nagar, hours later.
            self.point(
                "checkin", "2026-10-13T11:01:00+00:00", 13.018215, 80.1449683, "OC-004075",
                wo="WO-035990164", resolved_at="2026-10-13T15:00:39+00:00",
                address="Madha Nagar Main Road, Mugalivakkam",
            ),
        ]
        self.assertEqual(
            away_from_checkin(fieldez, route),
            [
                {
                    "case_number": "OC-004075",
                    "wo": "WO-035990164",
                    "at": "2026-10-13T11:01:00+00:00",
                    "resolved_at": "2026-10-13T15:00:39+00:00",
                    "address": "Madha Nagar Main Road, Mugalivakkam",
                    "away_km": 11.7,
                }
            ],
        )

    def test_just_over_a_km_is_away_and_just_under_is_not(self):
        # A degree of latitude is 111.195 km on haversine_km's earth.
        step = AWAY_KM / 111.195
        route = [self.point("checkin", "2026-10-13T08:00:00+00:00", 13.0, 80.0, "OC-1")]
        over = [self.point("checkin", "2026-10-13T08:00:00+00:00", 13.0 + step * 1.01, 80.0, "OC-1")]
        under = [self.point("checkin", "2026-10-13T08:00:00+00:00", 13.0 + step * 0.99, 80.0, "OC-1")]
        self.assertEqual([item["away_km"] for item in away_from_checkin(over, route)], [1.0])
        self.assertEqual(away_from_checkin(under, route), [])

    def test_only_the_same_check_in_is_compared(self):
        """Matched by case number and moment: a call checked into twice in a
        day is two check-ins, and one ROUTE KM has no place for is not judged."""
        route = [
            self.point("checkin", "2026-10-13T05:00:00+00:00", 13.0, 80.0, "OC-1"),
            self.point("checkin", "2026-10-13T09:00:00+00:00", 13.1, 80.1, "OC-1"),
        ]
        fieldez = [
            # Both stand where the second visit was: only the first is away.
            self.point("checkin", "2026-10-13T05:00:00+00:00", 13.1, 80.1, "OC-1"),
            self.point("checkin", "2026-10-13T09:00:00+00:00", 13.1, 80.1, "OC-1"),
            # ROUTE KM has no point for this one: not judged.
            self.point("checkin", "2026-10-13T10:00:00+00:00", 14.0, 81.0, "OC-2"),
            # Not check-ins at all.
            self.point("logout", "2026-10-13T12:00:00+00:00", 15.0, 82.0),
        ]
        self.assertEqual(
            [item["at"] for item in away_from_checkin(fieldez, route)], ["2026-10-13T05:00:00+00:00"]
        )

    def test_the_same_moment_written_two_ways_is_one_moment(self):
        route = [self.point("checkin", "2026-10-13T08:05:00+00:00", 13.0, 80.0, "OC-1")]
        fieldez = [self.point("checkin", "2026-10-13T13:35:00+05:30", 13.1, 80.0, "OC-1")]
        self.assertEqual(len(away_from_checkin(fieldez, route)), 1)

    def test_nothing_to_compare_is_nothing_away(self):
        self.assertEqual(away_from_checkin([], []), [])
        self.assertEqual(away_from_checkin(None, None), [])


class PointsTests(NoNetwork, TestCase):
    def setUp(self):
        super().setUp()
        self.engineer = make_engineer("Fieldez Points", branch="Hosur")
        self.now = timezone.now()
        self.day = timezone.localdate(self.now) - 4 * DAY
        logged_day(self.engineer, self.day)

    def points(self, day=None, now=None):
        return fieldez_points(self.engineer, day or self.day, now or self.now)

    def test_login_and_logout_are_route_kms_own(self):
        visit(self.engineer, at(self.day, 11), "WO-000000101")
        order("WO-000000101", resolutions=[resolution(at(self.day, 11, 20))])

        points, _, _, _ = self.points()
        ends = [point for point in points if point["kind"] != "checkin"]
        route = [point for point in route_km.key_points(self.engineer, self.day) if point["kind"] != "checkin"]
        self.assertEqual(ends, route)
        self.assertEqual([point["kind"] for point in ends], ["login", "logout"])

    def test_a_check_in_stands_at_its_problem_resolution_not_the_phone(self):
        case = visit(self.engineer, at(self.day, 11), "wo 000000101")
        order("WO-000000101", resolutions=[resolution(at(self.day, 11, 20))])

        points, missing, waiting, checkins = self.points()
        self.assertEqual([point["kind"] for point in points], ["login", "checkin", "logout"])
        checkin = points[1]
        self.assertEqual((checkin["latitude"], checkin["longitude"]), FIELDEZ_PLACE)
        self.assertEqual(parse_datetime(checkin["at"]), at(self.day, 11))
        self.assertEqual(checkin["case_number"], case.case_number)
        self.assertEqual(checkin["wo"], "WO-000000101")
        self.assertEqual(checkin["resolved_at"], at(self.day, 11, 20).astimezone(datetime.timezone.utc).isoformat())
        self.assertEqual((checkin["address"], checkin["user"], checkin["office"]), ("A road", "An engineer", False))
        self.assertEqual((missing, waiting, checkins), ([], 0, 1))

    def test_a_check_in_with_no_phone_fix_is_still_placed_by_fieldez(self):
        visit(self.engineer, at(self.day, 11), "WO-000000101", lat=None, lon=None)
        order("WO-000000101", resolutions=[resolution(at(self.day, 11, 20))])

        points, _, _, _ = self.points()
        self.assertEqual([(p["kind"], p["latitude"]) for p in points][1], ("checkin", FIELDEZ_PLACE[0]))

    def test_every_reason_a_check_in_is_left_out_and_every_wait(self):
        calls = {
            "no_wo": visit(self.engineer, at(self.day, 10), ""),
            "not_in_fieldez": visit(self.engineer, at(self.day, 11), "WO-000000102"),
            "no_problem_resolution": visit(self.engineer, at(self.day, 12), "WO-000000103"),
            "fieldez_failed": visit(self.engineer, at(self.day, 13), "WO-000000104"),
            "found_but_unusable": visit(self.engineer, at(self.day, 14), "WO-000000105"),
        }
        order("WO-000000102", status="not_found")
        order("WO-000000103", status="no_resolution")
        order("WO-000000104", status="failed", attempts=route_km.MAX_FAILED_ATTEMPTS)
        # Found, with a place no longer usable: as good as none.
        FieldezWorkOrder.objects.create(
            wo_key="WO-000000105", status="found", fetched_at=self.now, resolutions=[{"at": "2026-10-13T11:00:00", "latitude": 1, "longitude": 1}]
        )
        # Waiting: never read, read as pending, a failed read with tries left.
        visit(self.engineer, at(self.day, 15), "WO-000000106")
        visit(self.engineer, at(self.day, 16), "WO-000000107")
        visit(self.engineer, at(self.day, 17), "WO-000000108")
        order("WO-000000107", status="pending")
        order("WO-000000108", status="failed", attempts=1)

        points, missing, waiting, checkins = self.points()
        self.assertEqual([point["kind"] for point in points], ["login", "logout"])
        self.assertEqual(
            missing,
            [
                {"case_number": calls["no_wo"].case_number, "wo": None, "reason": "no_wo"},
                {"case_number": calls["not_in_fieldez"].case_number, "wo": "WO-000000102", "reason": "not_in_fieldez"},
                {"case_number": calls["no_problem_resolution"].case_number, "wo": "WO-000000103", "reason": "no_problem_resolution"},
                {"case_number": calls["fieldez_failed"].case_number, "wo": "WO-000000104", "reason": "fieldez_failed"},
                {"case_number": calls["found_but_unusable"].case_number, "wo": "WO-000000105", "reason": "no_problem_resolution"},
            ],
        )
        self.assertEqual((waiting, checkins), (3, 8))

    def test_past_the_window_nothing_waits(self):
        old = timezone.localdate(self.now) - MAX_WANTED_DAYS * DAY
        logged_day(self.engineer, old)
        never = visit(self.engineer, at(old, 11), "WO-000000201")
        failed = visit(self.engineer, at(old, 12), "WO-000000202")
        order("WO-000000202", status="failed", attempts=1)

        _, missing, waiting, _ = self.points(day=old)
        self.assertEqual(waiting, 0)
        self.assertEqual(
            missing,
            [
                {"case_number": never.case_number, "wo": "WO-000000201", "reason": "not_read"},
                {"case_number": failed.case_number, "wo": "WO-000000202", "reason": "fieldez_failed"},
            ],
        )

    def test_the_window_is_31_ist_days_counting_today(self):
        today = timezone.localdate(self.now)
        self.assertTrue(readable(today, self.now))
        self.assertTrue(readable(today - (MAX_WANTED_DAYS - 1) * DAY, self.now))
        self.assertFalse(readable(today - MAX_WANTED_DAYS * DAY, self.now))

    def test_check_ins_in_time_order_each_at_its_own_place(self):
        visit(self.engineer, at(self.day, 14), "WO-000000302")
        visit(self.engineer, at(self.day, 11), "WO-000000301")
        order("WO-000000301", resolutions=[resolution(at(self.day, 11, 5), lat=12.91)])
        order("WO-000000302", resolutions=[resolution(at(self.day, 14, 5), lat=12.92)])

        points, _, _, _ = self.points()
        self.assertEqual([(p["kind"], p.get("wo"), p["latitude"]) for p in points[1:3]], [
            ("checkin", "WO-000000301", 12.91),
            ("checkin", "WO-000000302", 12.92),
        ])

    def test_a_check_in_belongs_to_whoever_made_it(self):
        # Handed on to someone else after the check-in: still this engineer's.
        case = visit(self.engineer, at(self.day, 11), "WO-000000401")
        order("WO-000000401", resolutions=[resolution(at(self.day, 11, 5))])
        someone_else = make_engineer("Someone Else", branch="Hosur")
        Case.objects.filter(pk=case.pk).update(assigned_to=someone_else, assigned_at=at(self.day, 15))

        mine, _, _, _ = self.points()
        theirs, _, _, their_checkins = fieldez_points(someone_else, self.day, self.now)
        self.assertEqual([p["kind"] for p in mine], ["login", "checkin", "logout"])
        self.assertEqual((theirs, their_checkins), ([], 0))


# -- measuring ----------------------------------------------------------------


@override_settings(OLA_MAPS_API_KEY=TEST_KEY)
class RefreshTests(FieldezMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.engineer = make_engineer("Fieldez Refresh", branch="Hosur")
        self.now = timezone.now()
        self.today = timezone.localdate(self.now)
        # Four days back: settled (a day stays open until 49 h past its midnight).
        self.past = self.today - 4 * DAY
        self.yesterday = self.today - DAY

    def row(self, day, engineer=None):
        return FieldezKmDay.objects.get(engineer=engineer or self.engineer, day=day)

    def refresh(self, days, now, engineers=None):
        return km.refresh([e.id for e in engineers or [self.engineer]], days, now=now)

    def test_a_day_waiting_on_fieldez_is_stored_waiting_never_measured_never_final(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 11), "WO-000000501")

        self.assertEqual(self.refresh([self.past], self.now), 0)
        row = self.row(self.past)
        self.assertEqual((row.status, row.km, row.final, row.checkins), ("waiting", None, False, 1))
        self.assertEqual([p["kind"] for p in row.points], ["login", "logout"])
        self.assertEqual(self.ola.requests, [])

    def test_a_waiting_day_is_read_again_as_soon_as_the_worker_has_read_its_wo(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 11), "WO-000000501")
        self.refresh([self.past], self.now)

        # Five minutes on, nothing read: inside its hour, the day is left alone.
        self.refresh([self.past], self.now + 5 * MINUTE)
        self.assertEqual(self.row(self.past).checked_at, self.now)

        # The worker reads the WO; the next look reads the day again, though
        # its hour is not up, and measures it through FieldEZ's place.
        order("WO-000000501", resolutions=[resolution(at(self.past, 11, 20))], fetched_at=self.now + 6 * MINUTE)
        self.assertEqual(self.refresh([self.past], self.now + 7 * MINUTE), 1)
        row = self.row(self.past)
        self.assertEqual(row.status, "ready")
        self.assertEqual([p["kind"] for p in row.points], ["login", "checkin", "logout"])
        self.assertEqual(self.ola.requests, [[(12.70, 77.70), FIELDEZ_PLACE, (12.90, 77.90)]])
        self.assertTrue(row.final)

    def test_a_measured_day_is_read_again_when_its_own_wo_is_read_again_only(self):
        logged_day(self.engineer, self.yesterday)
        visit(self.engineer, at(self.yesterday, 11), "WO-000000601")
        order("WO-000000601", resolutions=[resolution(at(self.yesterday, 11, 20))], fetched_at=self.now - 2 * MINUTE)
        self.refresh([self.yesterday], self.now)
        self.assertEqual(self.row(self.yesterday).status, "ready")

        # Another WO read: this day is not touched.
        order("WO-000000999", status="not_found", fetched_at=self.now + 2 * MINUTE)
        self.refresh([self.yesterday], self.now + 3 * MINUTE)
        self.assertEqual(self.row(self.yesterday).checked_at, self.now)

        # Its own WO read again: the day is read again -- and, its places the
        # same, not measured again.
        FieldezWorkOrder.objects.filter(wo_key="WO-000000601").update(fetched_at=self.now + 4 * MINUTE)
        self.assertEqual(self.refresh([self.yesterday], self.now + 5 * MINUTE), 0)
        self.assertEqual(self.row(self.yesterday).checked_at, self.now + 5 * MINUTE)
        self.assertEqual(len(self.ola.requests), 1)

    def test_a_waiting_day_that_leaves_the_window_is_read_again_and_settles(self):
        day = self.today - 60 * DAY
        logged_day(self.engineer, day)
        never = visit(self.engineer, at(day, 11), "WO-000000701")
        last_readable = at(day + (MAX_WANTED_DAYS - 1) * DAY, 23, 55)
        self.refresh([day], last_readable)
        self.assertEqual(self.row(day).status, "waiting")

        # Ten minutes on, the day is past the window: nothing will be read for
        # it now, so it is read again at once, and settles without the call.
        self.refresh([day], last_readable + 10 * MINUTE)
        row = self.row(day)
        self.assertEqual(row.missing, [{"case_number": never.case_number, "wo": "WO-000000701", "reason": "not_read"}])
        self.assertEqual((row.status, row.final), ("ready", True))

    def test_a_day_with_a_check_in_fieldez_may_still_place_is_not_final(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 11), "WO-000000801")
        visit(self.engineer, at(self.past, 12), "WO-000000802")
        order("WO-000000801", resolutions=[resolution(at(self.past, 11, 20))])
        order("WO-000000802", status="not_found")

        self.refresh([self.past], self.now)
        row = self.row(self.past)
        self.assertEqual((row.status, [m["reason"] for m in row.missing]), ("ready", ["not_in_fieldez"]))
        self.assertFalse(row.final, "final while the worker still reads WO-000000802 every half hour")

        # FieldEZ has it now: the day takes it, and is final.
        FieldezWorkOrder.objects.filter(wo_key="WO-000000802").update(
            status="found",
            resolutions=[clean_resolution(resolution(at(self.past, 12, 20), lat=12.99))],
            fetched_at=self.now + 30 * MINUTE,
        )
        self.refresh([self.past], self.now + 31 * MINUTE)
        row = self.row(self.past)
        self.assertEqual(([p["kind"] for p in row.points], row.missing, row.final), (["login", "checkin", "checkin", "logout"], [], True))

    def test_left_out_for_good_is_final(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 11), "")
        visit(self.engineer, at(self.past, 12), "WO-000000901")
        order("WO-000000901", status="failed", attempts=route_km.MAX_FAILED_ATTEMPTS)

        self.refresh([self.past], self.now)
        row = self.row(self.past)
        self.assertEqual([m["reason"] for m in row.missing], ["no_wo", "fieldez_failed"])
        self.assertTrue(row.final)

    def test_past_the_window_a_not_found_check_in_no_longer_holds_the_day_open(self):
        day = self.today - 40 * DAY
        logged_day(self.engineer, day)
        visit(self.engineer, at(day, 11), "WO-000001001")
        order("WO-000001001", status="not_found")

        self.refresh([day], self.now)
        self.assertTrue(self.row(day).final)

    def test_one_place_is_zero_km_and_one_point_is_no_points_without_ola(self):
        logged_day(self.engineer, self.past, login=(12.7, 77.7), logout=(12.7, 77.7))
        duty(self.engineer, at(self.yesterday, 9))
        ping(self.engineer, at(self.yesterday, 9), 12.7, 77.7)

        self.refresh([self.past, self.yesterday], self.now)
        self.assertEqual((self.row(self.past).status, self.row(self.past).km), ("ready", 0.0))
        self.assertEqual((self.row(self.yesterday).status, self.row(self.yesterday).km), ("no_points", None))
        self.assertEqual(self.ola.requests, [])

    def test_unchanged_places_keep_their_km(self):
        logged_day(self.engineer, self.yesterday)
        visit(self.engineer, at(self.yesterday, 11), "WO-000001101")
        order("WO-000001101", resolutions=[resolution(at(self.yesterday, 11, 20))], fetched_at=self.now - MINUTE)
        self.refresh([self.yesterday], self.now)
        km_before = self.row(self.yesterday).km

        self.refresh([self.yesterday], self.now + 61 * MINUTE)
        self.assertEqual(self.row(self.yesterday).checked_at, self.now + 61 * MINUTE)
        self.assertEqual(self.row(self.yesterday).km, km_before)
        self.assertEqual(len(self.ola.requests), 1)

    def test_route_kms_own_places_take_route_kms_km_without_asking_ola(self):
        logged_day(self.engineer, self.past)
        route_km.refresh([self.engineer.id], [self.past], now=self.now)
        self.assertEqual(len(self.ola.requests), 1)

        # Seconds later: ROUTE KM has just measured, so FIELDEZ may not ask
        # Ola -- and does not need to.
        self.refresh([self.past], self.now + datetime.timedelta(seconds=5))
        route = RouteKmDay.objects.get(engineer=self.engineer, day=self.past)
        row = self.row(self.past)
        self.assertEqual((row.status, row.km, row.path), ("ready", route.km, route.path))
        self.assertEqual(len(self.ola.requests), 1)

    def test_route_kms_km_is_not_borrowed_for_other_places(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 11), "WO-000001201")
        order("WO-000001201", resolutions=[resolution(at(self.past, 11, 20))])
        route_km.refresh([self.engineer.id], [self.past], now=self.now - 2 * MINUTE)

        self.refresh([self.past], self.now)
        self.assertEqual(self.ola.requests[-1], [(12.70, 77.70), FIELDEZ_PLACE, (12.90, 77.90)])
        self.assertEqual(len(self.ola.requests), 2)

    def measurable(self, engineer, day):
        logged_day(engineer, day)
        visit(engineer, at(day, 11), f"WO-{engineer.id:09d}")
        order(f"WO-{engineer.id:09d}", resolutions=[resolution(at(day, 11, 20))])

    def test_fieldez_gives_way_while_route_km_has_measured_in_the_last_minute(self):
        self.measurable(self.engineer, self.past)
        other = make_engineer("Route Km Only", branch="Hosur")
        RouteKmDay.objects.create(engineer=other, day=self.past, status="ready", km=1.0, measured_at=self.now - 30 * datetime.timedelta(seconds=1))

        # Stored pending, with the places it will be measured through.
        self.assertEqual(self.refresh([self.past], self.now), 0)
        row = self.row(self.past)
        self.assertEqual((row.status, row.km, len(row.points)), ("pending", None, 3))
        self.assertEqual(self.ola.requests, [])

        self.assertEqual(self.refresh([self.past], self.now + 31 * datetime.timedelta(seconds=1)), 1)
        self.assertEqual(self.row(self.past).status, "ready")

    def test_a_fresh_429_on_either_table_holds_fieldez_back(self):
        self.measurable(self.engineer, self.past)
        other = make_engineer("Rate Limited", branch="Hosur")
        for table in (RouteKmDay, FieldezKmDay):
            RouteKmDay.objects.all().delete()
            FieldezKmDay.objects.all().delete()
            table.objects.create(engineer=other, day=self.yesterday, status="pending", error=route_km.RATE_LIMITED, measured_at=self.now - 90 * datetime.timedelta(seconds=1))
            self.assertEqual(self.refresh([self.past], self.now), 0, table.__name__)
            self.assertEqual(self.ola.requests, [], table.__name__)

    def test_fieldez_measures_at_most_its_own_allowance_a_minute(self):
        others = [make_engineer(f"Allowance {n}", branch="Hosur") for n in range(2)]
        for engineer in [self.engineer, *others]:
            self.measurable(engineer, self.past)

        with mock.patch.dict(os.environ, {"FIELDEZ_KM_PER_MINUTE": "1"}):
            self.assertEqual(self.refresh([self.past], self.now, engineers=[self.engineer, *others]), 1)
        self.assertEqual(len(self.ola.requests), 1)

        # The default is 2 a minute, and two more cannot start in the same minute.
        self.assertEqual(self.refresh([self.past], self.now, engineers=[self.engineer, *others]), 1)
        self.assertEqual(len(self.ola.requests), 2)

    def test_a_429_is_written_on_the_cell_so_every_worker_waits(self):
        self.measurable(self.engineer, self.past)
        self.ola.fail = olamaps.RateLimited("rate limited by Ola: slow down")

        self.assertEqual(self.refresh([self.past], self.now), 0)
        row = self.row(self.past)
        self.assertEqual((row.status, row.error, row.measured_at), ("pending", route_km.RATE_LIMITED, self.now))

        self.ola.fail = None
        km._cooldown_until = 0.0
        self.assertEqual(self.refresh([self.past], self.now + 60 * datetime.timedelta(seconds=1)), 0, "inside the cooldown")
        self.assertEqual(self.refresh([self.past], self.now + 121 * datetime.timedelta(seconds=1)), 1)

    def test_a_day_ola_cannot_route_fails_and_settles_after_three_tries(self):
        self.measurable(self.engineer, self.past)
        self.ola.fail = olamaps.SnapUnavailable("HTTP 400 from Ola: bad point")

        for attempt in range(1, route_km.MAX_FAILED_ATTEMPTS + 1):
            self.refresh([self.past], self.now + (attempt - 1) * 61 * MINUTE)
            row = self.row(self.past)
            self.assertEqual((row.status, row.attempts), ("failed", attempt))
            self.assertEqual(row.final, attempt == route_km.MAX_FAILED_ATTEMPTS)

    def test_fieldez_gives_way_while_route_km_is_reading_too(self):
        """ROUTE KM writes checked_at on every cell it reads, before it claims
        one: FIELDEZ waits from then, not only once ROUTE KM has asked Ola."""
        self.measurable(self.engineer, self.past)
        other = make_engineer("Route Km Reading", branch="Hosur")
        RouteKmDay.objects.create(engineer=other, day=self.past, status="no_points", checked_at=self.now - 30 * datetime.timedelta(seconds=1))

        self.assertEqual(self.refresh([self.past], self.now), 0)
        self.assertEqual((self.row(self.past).status, self.ola.requests), ("pending", []))
        self.assertEqual(self.refresh([self.past], self.now + 31 * datetime.timedelta(seconds=1)), 1)

    def test_while_ola_is_closed_the_days_that_need_no_ola_are_still_read(self):
        # An older day that needs Ola sorts first; behind it, a day whose WO
        # the worker has just read and which needs no Ola: all in one place.
        older = self.past - DAY
        self.measurable(self.engineer, older)
        other = make_engineer("No Ola Needed", branch="Hosur")
        logged_day(other, self.past, login=(12.7, 77.7), logout=(12.7, 77.7))
        visit(other, at(self.past, 11), "WO-000009901")
        self.refresh([self.past], self.now - 2 * MINUTE, engineers=[other])
        self.assertEqual(self.row(self.past, other).status, "waiting")
        order("WO-000009901", status="not_found", fetched_at=self.now - MINUTE)
        RouteKmDay.objects.create(engineer=other, day=older, status="ready", km=1.0, measured_at=self.now - 10 * datetime.timedelta(seconds=1))

        self.refresh([older, self.past], self.now, engineers=[self.engineer, other])
        self.assertEqual(self.row(older).status, "pending")
        self.assertEqual((self.row(self.past, other).status, self.row(self.past, other).km), ("ready", 0.0))
        self.assertEqual(self.ola.requests, [])

        # Closed from the start -- Ola has said 429 -- the day known to need
        # Ola is not even read.
        RouteKmDay.objects.filter(engineer=other, day=older).update(error=route_km.RATE_LIMITED)
        with mock.patch.object(km, "fieldez_points", wraps=km.fieldez_points) as read:
            self.refresh([older, self.past], self.now + 5 * datetime.timedelta(seconds=1), engineers=[self.engineer, other])
        self.assertEqual(read.call_count, 0)
        self.assertEqual(self.row(older).status, "pending")

    def test_today_and_the_newest_days_are_read_again_first(self):
        # Two days back is the oldest day that has not settled.
        days = [self.today - DAY, self.today - 2 * DAY]
        for day in days:
            logged_day(self.engineer, day)
        self.refresh(days, self.now)
        self.assertTrue(all(not self.row(day).final for day in days), "neither has settled")

        later = self.now + 61 * MINUTE
        km.refresh([self.engineer.id], days, now=later, work_limit=1)
        self.assertEqual((self.row(days[0]).checked_at, self.row(days[1]).checked_at), (later, self.now))

    def test_a_waiting_day_keeps_its_km_and_gets_it_back_without_ola(self):
        logged_day(self.engineer, self.yesterday)
        visit(self.engineer, at(self.yesterday, 11), "WO-000009801")
        order("WO-000009801", resolutions=[resolution(at(self.yesterday, 11, 20))], fetched_at=self.now - MINUTE)
        self.refresh([self.yesterday], self.now)
        measured_km = self.row(self.yesterday).km
        self.assertEqual(len(self.ola.requests), 1)

        # A new check-in on a WO not read yet: the day waits, showing no km...
        visit(self.engineer, at(self.yesterday, 15), "WO-000009802")
        self.refresh([self.yesterday], self.now + 61 * MINUTE)
        row = self.row(self.yesterday)
        self.assertEqual((row.status, row.km), ("waiting", measured_km))
        self.client = APIClient()
        self.client.force_authenticate(User.objects.create_user(username="km-viewer", password="x", role="superadmin", is_superuser=True))
        cell = self.client.post(
            "/api/fieldez_km/range/",
            {"engineers": [self.engineer.id], "from": self.yesterday.isoformat(), "to": self.yesterday.isoformat()},
            format="json",
        ).json()["engineers"][0]["days"][self.yesterday.isoformat()]
        self.assertEqual((cell["status"], cell["km"]), ("waiting", None))

        # ...and FieldEZ has no place for it: the same places, the same km, no request.
        order("WO-000009802", status="no_resolution", fetched_at=self.now + 62 * MINUTE)
        self.refresh([self.yesterday], self.now + 63 * MINUTE)
        row = self.row(self.yesterday)
        self.assertEqual((row.status, row.km, [m["reason"] for m in row.missing]), ("ready", measured_km, ["no_problem_resolution"]))
        self.assertEqual(len(self.ola.requests), 1)

    def test_a_settled_day_ola_cannot_route_is_not_retried_every_hour_while_held_open(self):
        self.measurable(self.engineer, self.past)
        visit(self.engineer, at(self.past, 12), "WO-000009701")
        order("WO-000009701", status="not_found")
        self.ola.fail = olamaps.SnapUnavailable("HTTP 400 from Ola: bad point")

        for attempt in range(route_km.MAX_FAILED_ATTEMPTS):
            self.refresh([self.past], self.now + attempt * 61 * MINUTE)
        row = self.row(self.past)
        self.assertEqual((row.status, row.attempts, row.final), ("failed", 3, False))
        self.assertEqual(len(self.ola.requests), 3)

        # Held open by the WO FieldEZ may yet have, but Ola is not asked again
        # through the same places, nor is the day counted as pending.
        later = self.now + 4 * 61 * MINUTE
        self.refresh([self.past], later)
        row = self.row(self.past)
        self.assertEqual((row.status, row.attempts, row.checked_at), ("failed", 3, later))
        self.assertEqual(len(self.ola.requests), 3)
        self.assertFalse(km.is_pending(row, later))

        # FieldEZ places the other check-in: new places, three new tries.
        FieldezWorkOrder.objects.filter(wo_key="WO-000009701").update(
            status="found", resolutions=[clean_resolution(resolution(at(self.past, 12, 20), lat=12.99))], fetched_at=later + MINUTE
        )
        self.refresh([self.past], later + 2 * MINUTE)
        row = self.row(self.past)
        self.assertEqual((row.status, row.attempts, len(row.points)), ("failed", 1, 4))
        self.assertEqual(len(self.ola.requests), 4)

    def test_the_cell_carrying_a_fresh_429_keeps_it_while_ola_is_closed(self):
        logged_day(self.engineer, self.yesterday)
        self.refresh([self.yesterday], self.now)
        # A 429 written on it a minute before the next look: fresh.
        FieldezKmDay.objects.filter(engineer=self.engineer, day=self.yesterday).update(
            error=route_km.RATE_LIMITED, measured_at=self.now + 60 * MINUTE
        )

        self.refresh([self.yesterday], self.now + 61 * MINUTE)
        row = self.row(self.yesterday)
        self.assertEqual((row.error, row.checked_at), (route_km.RATE_LIMITED, self.now))

    def test_a_cell_finished_by_another_request_is_never_left_final_and_pending(self):
        # A poll that read the day as waiting reaches it after a /day click
        # measured and finished it: what it writes must not be final.
        logged_day(self.engineer, self.past)
        self.refresh([self.past], self.now)
        finished = self.row(self.past)
        self.assertEqual((finished.status, finished.final), ("ready", True))
        stale = FieldezKmDay(engineer=self.engineer, day=self.past, status="waiting", km=None)
        km._waits_on_ola(self.engineer.id, self.past, stale, finished.points, [], 0)
        self.assertEqual((self.row(self.past).status, self.row(self.past).final), ("pending", False))

        # The next look puts it back, with its km, and asks Ola nothing.
        self.refresh([self.past], self.now + 2 * MINUTE)
        row = self.row(self.past)
        self.assertEqual((row.status, row.km, row.final), ("ready", finished.km, True))
        self.assertEqual(len(self.ola.requests), 1)

    def test_a_measured_day_whose_places_moved_waits_as_pending_while_ola_is_closed(self):
        logged_day(self.engineer, self.yesterday)
        visit(self.engineer, at(self.yesterday, 11), "WO-000009601")
        order("WO-000009601", status="no_resolution", fetched_at=self.now - MINUTE)
        self.refresh([self.yesterday], self.now)
        self.assertEqual(self.row(self.yesterday).status, "ready")

        # FieldEZ places the check-in while ROUTE KM is at work.
        FieldezWorkOrder.objects.filter(wo_key="WO-000009601").update(
            status="found", resolutions=[clean_resolution(resolution(at(self.yesterday, 11, 20)))], fetched_at=self.now + MINUTE
        )
        other = make_engineer("Route Km Busy", branch="Hosur")
        RouteKmDay.objects.create(engineer=other, day=self.yesterday, status="ready", km=1.0, measured_at=self.now + MINUTE)
        self.refresh([self.yesterday], self.now + 2 * MINUTE)
        row = self.row(self.yesterday)
        self.assertEqual((row.status, len(row.points)), ("pending", 3))
        self.assertTrue(km.is_pending(row, self.now + 2 * MINUTE))
        self.assertEqual(len(self.ola.requests), 1)

    def test_a_late_save_inside_a_look_still_counts_as_after_it(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 11), "WO-000009501")
        self.refresh([self.past], self.now)
        # Stamped with its request's start, a little before the look's.
        order("WO-000009501", resolutions=[resolution(at(self.past, 11, 20))], fetched_at=self.now - 10 * datetime.timedelta(seconds=1))
        self.refresh([self.past], self.now + 5 * MINUTE)
        self.assertEqual(self.row(self.past).status, "ready")

    def test_a_failed_fieldez_read_does_not_buy_ola_new_tries(self):
        self.measurable(self.engineer, self.past)
        visit(self.engineer, at(self.past, 12), "WO-000009401")
        order("WO-000009401", status="not_found")
        self.ola.fail = olamaps.SnapUnavailable("HTTP 400 from Ola: bad point")
        for attempt in range(route_km.MAX_FAILED_ATTEMPTS):
            self.refresh([self.past], self.now + attempt * 61 * MINUTE)
        self.assertEqual((self.row(self.past).status, self.row(self.past).attempts), ("failed", 3))

        # The held-open WO's re-read fails for a moment: the day waits...
        later = self.now + 4 * 61 * MINUTE
        FieldezWorkOrder.objects.filter(wo_key="WO-000009401").update(status="failed", attempts=1, fetched_at=later)
        self.refresh([self.past], later + MINUTE)
        self.assertEqual((self.row(self.past).status, self.row(self.past).attempts), ("waiting", 3))
        # ...and when it reads not_found again, the same places are not tried again.
        FieldezWorkOrder.objects.filter(wo_key="WO-000009401").update(status="not_found", attempts=0, fetched_at=later + 2 * MINUTE)
        self.refresh([self.past], later + 3 * MINUTE)
        self.assertEqual((self.row(self.past).status, self.row(self.past).attempts), ("failed", 3))
        self.assertEqual(len(self.ola.requests), 3)

    def test_a_pending_day_is_always_due(self):
        # Pending waits only on Ola: read a minute ago or not, it is measured
        # as soon as Ola may be asked.
        self.measurable(self.engineer, self.past)
        FieldezKmDay.objects.create(engineer=self.engineer, day=self.past, status="pending", checked_at=self.now - MINUTE)
        self.assertEqual(self.refresh([self.past], self.now), 1)
        self.assertEqual(self.row(self.past).status, "ready")

    def test_route_kms_table_is_never_written(self):
        logged_day(self.engineer, self.past)
        route_km.refresh([self.engineer.id], [self.past], now=self.now - 10 * MINUTE)
        visit(self.engineer, at(self.past, 11), "WO-000001301")
        visit(self.engineer, at(self.past, 12), "WO-000001302")
        order("WO-000001301", resolutions=[resolution(at(self.past, 11, 20))])
        before = list(RouteKmDay.objects.order_by("pk").values())

        self.refresh([self.past, self.yesterday, self.today], self.now)
        order("WO-000001302", status="not_found", fetched_at=self.now + MINUTE)
        self.refresh([self.past, self.yesterday, self.today], self.now + 2 * MINUTE)
        self.ola.fail = olamaps.RateLimited("rate limited by Ola: slow down")
        FieldezKmDay.objects.all().delete()
        self.refresh([self.past], self.now + 5 * MINUTE)

        self.assertEqual(list(RouteKmDay.objects.order_by("pk").values()), before)


# -- the endpoints ------------------------------------------------------------


@override_settings(OLA_MAPS_API_KEY=TEST_KEY)
class EndpointTests(FieldezMixin, TestCase):
    def setUp(self):
        super().setUp()
        self.office = User.objects.create_user(username="office-fieldez", password="x", role="superadmin", is_superuser=True)
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.engineer = make_engineer("Fieldez Endpoint", branch="Hosur")
        self.today = timezone.localdate()
        self.past = self.today - 4 * DAY

    def range(self, **changes):
        body = {"engineers": [self.engineer.id], "from": self.past.isoformat(), "to": self.past.isoformat(), **changes}
        return self.client.post("/api/fieldez_km/range/", body, format="json")

    def day(self, engineer=None, date=None):
        engineer = self.engineer.id if engineer is None else engineer
        date = (date or self.past).isoformat() if not isinstance(date, str) else date
        return self.client.get("/api/fieldez_km/day/", {"engineer": engineer, "date": date})

    def post_results(self, results):
        return self.client.post("/api/fieldez_km/work_orders/", {"results": results}, format="json")

    def branch_hr(self, branch="Hosur"):
        return User.objects.create_user(username=f"hr-{branch}", password="x", role="hr", allowed_sections={"attendance": [branch]})

    # range and day

    def test_from_waiting_to_measured_through_the_endpoints(self):
        """The end-to-end run of 15 Oct 2026, with a fake Ola: a day waits,
        the worker posts, the next look measures it, and /day agrees."""
        logged_day(self.engineer, self.past)
        found = visit(self.engineer, at(self.past, 11), "WO-036165368")
        no_place = visit(self.engineer, at(self.past, 12, 30), "wo 035446997")
        found_2 = visit(self.engineer, at(self.past, 14), "WO-035362112")
        absent = visit(self.engineer, at(self.past, 15, 30), "WO-099999999")
        no_wo = visit(self.engineer, at(self.past, 17), "")

        first = self.range().json()
        self.assertEqual(first["waiting"], 1)
        self.assertEqual(first["engineers"][0]["days"][self.past.isoformat()], {
            "km": None, "points": 2, "status": "waiting", "office": False, "final": False, "missing": 1, "away": 0,
        })

        answer = self.post_results([
            {"wo": "WO-036165368", "status": "found", "fieldez_ticket_id": "1089604", "error": "", "resolutions": [resolution(at(self.past - DAY, 10, 57), lat=13.09206, lon=79.997763)]},
            {"wo": "WO-035446997", "status": "no_resolution", "fieldez_ticket_id": "1023712", "error": "", "resolutions": []},
            {"wo": "WO-035362112", "status": "found", "fieldez_ticket_id": "1015602", "error": "", "resolutions": [resolution(at(self.past - 47 * DAY, 15, 59), lat=13.054051, lon=80.177763)]},
            {"wo": "WO-099999999", "status": "not_found", "fieldez_ticket_id": "", "error": "", "resolutions": []},
        ]).json()
        self.assertEqual(answer, {"saved": 4, "rejected": 0})

        second = self.range().json()
        cell = second["engineers"][0]["days"][self.past.isoformat()]
        self.assertEqual((second["waiting"], second["measured_now"]), (0, 1))
        self.assertEqual(cell, {"km": 3.0, "points": 4, "status": "ready", "office": False, "final": False, "missing": 3, "away": 0})
        self.assertEqual(self.ola.requests[-1], [(12.70, 77.70), (13.09206, 79.997763), (13.054051, 80.177763), (12.90, 77.90)])

        day = self.day().json()
        self.assertEqual((day["status"], day["km"], len(day["points"]), day["waiting"]), ("ready", 3.0, 4, 0))
        self.assertEqual([p.get("case_number") for p in day["points"]], [None, found.case_number, found_2.case_number, None])
        self.assertEqual(day["missing"], [
            {"case_number": no_place.case_number, "wo": "WO-035446997", "reason": "no_problem_resolution"},
            {"case_number": absent.case_number, "wo": "WO-099999999", "reason": "not_in_fieldez"},
            {"case_number": no_wo.case_number, "wo": None, "reason": "no_wo"},
        ])

    def test_the_day_asked_right_after_the_worker_posts_is_measured_too(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 11), "WO-000001401")
        self.assertEqual(self.day().json()["status"], "waiting")

        self.post_results([{"wo": "WO-000001401", "status": "found", "resolutions": [resolution(at(self.past, 11, 20))]}])
        day = self.day().json()
        self.assertEqual((day["status"], day["km"], day["missing"]), ("ready", 2.0, []))
        self.assertEqual(self.range().json()["engineers"][0]["days"][self.past.isoformat()]["status"], "ready")

    def test_a_waiting_day_tells_the_map_its_places_and_what_it_waits_for(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 11), "WO-000001501")
        visit(self.engineer, at(self.past, 12), "")

        day = self.day().json()
        self.assertEqual((day["status"], day["km"], day["waiting"], day["path"]), ("waiting", None, 1, []))
        self.assertEqual([p["kind"] for p in day["points"]], ["login", "logout"])
        self.assertEqual([m["reason"] for m in day["missing"]], ["no_wo"])


    def test_a_problem_resolution_pressed_away_from_the_call_is_marked(self):
        """Mohan's 13 Oct: the Chetpet call's Problem Resolution at the
        customer's, the Anna Nagar call's at 8:30 PM from Madha Nagar."""
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 13, 35), "WO-036176428", lat=13.0711963, lon=80.2412456)
        far = visit(self.engineer, at(self.past, 16, 31), "WO-035990164", lat=13.0906842, lon=80.2233519)
        order("WO-036176428", resolutions=[resolution(at(self.past, 13, 30), lat=13.0712056, lon=80.2412661)])
        order(
            "WO-035990164",
            resolutions=[
                resolution(
                    at(self.past, 20, 30), lat=13.018215, lon=80.1449683,
                    address="Madha Nagar Main Road, Mugalivakkam",
                )
            ],
        )
        self.route_km_cell()

        cell = self.range().json()["engineers"][0]["days"][self.past.isoformat()]
        self.assertEqual((cell["status"], cell["away"]), ("ready", 1))

        away = self.day().json()["away"]
        self.assertEqual(len(away), 1)
        self.assertEqual(
            (away[0]["case_number"], away[0]["wo"], away[0]["address"], away[0]["away_km"]),
            (far.case_number, "WO-035990164", "Madha Nagar Main Road, Mugalivakkam", 11.7),
        )
        self.assertEqual(parse_datetime(away[0]["at"]), at(self.past, 16, 31))
        self.assertEqual(parse_datetime(away[0]["resolved_at"]), at(self.past, 20, 30))

    def route_km_cell(self):
        """ROUTE KM's own cell for the day, as the report asks both tables
        for it: where each check-in was made. Measured minutes ago, so FIELDEZ
        does not give way to it."""
        checked_in = route_km.key_points(self.engineer, self.past)
        RouteKmDay.objects.create(
            engineer=self.engineer,
            day=self.past,
            status="ready",
            km=40.0,
            points=checked_in,
            signature=route_km.signature(checked_in),
            checked_at=timezone.now() - 5 * MINUTE,
            measured_at=timezone.now() - 5 * MINUTE,
            final=True,
        )

    def test_with_no_route_km_cell_nothing_is_marked(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 16, 31), "WO-035990164", lat=13.0906842, lon=80.2233519)
        order("WO-035990164", resolutions=[resolution(at(self.past, 20, 30), lat=13.018215, lon=80.1449683)])

        self.assertEqual(self.range().json()["engineers"][0]["days"][self.past.isoformat()]["away"], 0)
        self.assertEqual(self.day().json()["away"], [])

    def test_a_day_waiting_on_fieldez_names_what_it_can(self):
        logged_day(self.engineer, self.past)
        visit(self.engineer, at(self.past, 16, 31), "WO-035990164", lat=13.0906842, lon=80.2233519)
        visit(self.engineer, at(self.past, 17, 0), "WO-000001601")  # not read yet
        order("WO-035990164", resolutions=[resolution(at(self.past, 20, 30), lat=13.018215, lon=80.1449683)])
        self.route_km_cell()

        day = self.day().json()
        self.assertEqual(day["status"], "waiting")
        self.assertEqual([item["wo"] for item in day["away"]], ["WO-035990164"])

    def test_only_staff_may_ask(self):
        self.client.force_authenticate(User.objects.create_user(username="an-engineer", password="x", role="employee"))
        self.assertEqual(self.range().status_code, 403)
        self.assertEqual(self.day().status_code, 403)

    def test_a_branch_login_sees_its_own_branch_only(self):
        elsewhere = make_engineer("Salem Engineer", branch="Salem")
        self.client.force_authenticate(self.branch_hr("Hosur"))

        answer = self.range(engineers=[self.engineer.id, elsewhere.id]).json()
        self.assertEqual([row["engineer_id"] for row in answer["engineers"]], [self.engineer.id])
        self.assertEqual(self.day(engineer=elsewhere.id).status_code, 403)
        self.assertEqual(self.day().status_code, 200)

    def test_a_bad_question_is_400_as_on_route_km(self):
        for name, body in (
            ("engineers not a list", {"engineers": str(self.engineer.id)}),
            ("a bool id", {"engineers": [True]}),
            ("a negative id", {"engineers": [-1]}),
            ("301 ids", {"engineers": list(range(1, 302))}),
            ("an impossible date", {"from": "2026-13-01"}),
            ("from after to", {"to": (self.past - DAY).isoformat()}),
            ("63 days", {"from": (self.past - 62 * DAY).isoformat()}),
        ):
            self.assertEqual(self.range(**body).status_code, 400, name)
        self.assertEqual(self.day(engineer="7a").status_code, 400)
        self.assertEqual(self.day(date="2026-02-30").status_code, 400)
        self.assertEqual(self.day(date=self.today + DAY).status_code, 400)
        self.assertEqual(self.day(engineer=999999).status_code, 404)

    def test_days_after_today_are_not_asked_about(self):
        answer = self.range(**{"from": self.today.isoformat(), "to": (self.today + 3 * DAY).isoformat()}).json()
        self.assertEqual(answer["days"], [self.today.isoformat()])

    def test_without_an_ola_key_it_says_so(self):
        with ola_off():
            self.assertEqual(self.range().json(), {
                "configured": False, "days": [self.past.isoformat()], "engineers": [],
                "pending": 0, "rechecking": 0, "waiting": 0, "measured_now": 0,
            })
            day = self.day().json()
        self.assertEqual((day["configured"], day["status"], day["points"], day["missing"], day["waiting"]), (False, "pending", [], [], 0))
        self.assertFalse(FieldezKmDay.objects.exists())

    # wanted

    def wanted(self, days=None):
        return self.client.get("/api/fieldez_km/wanted/", {} if days is None else {"days": days})

    def test_wanted_is_for_a_login_that_sees_every_branch(self):
        self.assertEqual(self.wanted().status_code, 200)
        self.client.force_authenticate(self.branch_hr("Hosur"))
        self.assertEqual(self.wanted().status_code, 403)
        self.client.force_authenticate(User.objects.create_user(username="an-engineer", password="x", role="employee"))
        self.assertEqual(self.wanted().status_code, 403)

    def test_wanted_days_are_1_to_31_and_31_when_not_said(self):
        self.assertEqual(self.wanted().json()["days"], 31)
        self.assertEqual(self.wanted(days=1).json()["days"], 1)
        for bad in ("0", "32", "abc", "1.5", "-1", "٣"):
            self.assertEqual(self.wanted(days=bad).status_code, 400, bad)

    def test_which_work_orders_are_wanted(self):
        now = timezone.now()
        hours_ago = lambda hours: now - datetime.timedelta(hours=hours)
        visit(self.engineer, hours_ago(9), "WO-000002001")  # never read
        visit(self.engineer, hours_ago(8), "WO-000002002")  # not found 10 min ago
        visit(self.engineer, hours_ago(7), "WO-000002003")  # not found 31 min ago
        visit(self.engineer, hours_ago(6), "WO-000002004")  # failed 30 min ago
        visit(self.engineer, hours_ago(5), "WO-000002005")  # failed 61 min ago
        visit(self.engineer, hours_ago(4), "WO-000002006")  # failed for good
        visit(self.engineer, hours_ago(3), "WO-000002007")  # found after its check-in
        visit(self.engineer, hours_ago(2), "WO-000002008")  # found before a newer check-in
        visit(self.engineer, hours_ago(10), "WO-000002008")
        visit(self.engineer, now - 40 * DAY, "WO-000002009")  # outside the window
        order("WO-000002002", status="not_found", fetched_at=now - 10 * MINUTE)
        order("WO-000002003", status="no_resolution", fetched_at=now - 31 * MINUTE)
        order("WO-000002004", status="failed", attempts=1, fetched_at=now - 30 * MINUTE)
        order("WO-000002005", status="failed", attempts=2, fetched_at=now - 61 * MINUTE)
        order("WO-000002006", status="failed", attempts=route_km.MAX_FAILED_ATTEMPTS, fetched_at=now - 5 * DAY)
        order("WO-000002007", resolutions=[resolution(hours_ago(3))], fetched_at=hours_ago(2.5))
        order("WO-000002008", resolutions=[resolution(hours_ago(10))], fetched_at=hours_ago(9))

        # Never read first, then re-reads newest check-in first, then failed reads.
        self.assertEqual(self.wanted().json()["work_orders"], ["WO-000002001", "WO-000002008", "WO-000002003", "WO-000002005"])

    def test_a_failed_read_about_to_leave_the_window_goes_with_the_never_read(self):
        now = timezone.now()
        first = timezone.make_aware(datetime.datetime.combine(timezone.localdate(now) - 30 * DAY, datetime.time.min))
        visit(self.engineer, first + DAY + 11 * datetime.timedelta(hours=1), "WO-000007301")  # edge, failed
        visit(self.engineer, now - 10 * DAY, "WO-000007302")  # not near the edge, failed
        visit(self.engineer, now - 3 * datetime.timedelta(hours=1), "WO-000007303")  # re-read
        visit(self.engineer, now - MINUTE, "WO-000007304")  # never read
        order("WO-000007301", status="failed", attempts=1, fetched_at=now - 61 * MINUTE)
        order("WO-000007302", status="failed", attempts=1, fetched_at=now - 61 * MINUTE)
        order("WO-000007303", status="no_resolution", fetched_at=now - 31 * MINUTE)

        self.assertEqual(
            wanted_work_orders(first, now, ["All"]),
            ["WO-000007301", "WO-000007304", "WO-000007303", "WO-000007302"],
        )

    def test_a_read_is_due_again_a_pass_after_it_was_posted(self):
        # Posted near the end of a pass, read again by the next one: 30
        # minutes less the slack, not a full 30 after the post.
        now = timezone.now()
        visit(self.engineer, now - 3 * datetime.timedelta(hours=1), "WO-000007401")
        visit(self.engineer, now - 3 * datetime.timedelta(hours=1), "WO-000007402")
        order("WO-000007401", status="no_resolution", fetched_at=now - 20 * MINUTE)
        order("WO-000007402", status="no_resolution", fetched_at=now - 10 * MINUTE)
        self.assertEqual(self.wanted().json()["work_orders"], ["WO-000007401"])

    def test_a_new_check_in_is_read_before_any_re_read(self):
        """Oldest first throughout, WOs FieldEZ never places took every read
        and today's were never read (review, 15 Oct 2026)."""
        now = timezone.now()
        for n in range(10):
            visit(self.engineer, now - (20 - n) * DAY, f"WO-00000700{n}")
            order(f"WO-00000700{n}", status="no_resolution", fetched_at=now - 25 * datetime.timedelta(hours=1))
        visit(self.engineer, now - 3 * datetime.timedelta(hours=1), "WO-000007100")
        order("WO-000007100", status="no_resolution", fetched_at=now - 31 * MINUTE)
        visit(self.engineer, now - MINUTE, "WO-000007200")  # never read
        first = timezone.make_aware(datetime.datetime.combine(timezone.localdate(now) - 30 * DAY, datetime.time.min))

        self.assertEqual(
            wanted_work_orders(first, now, ["All"], limit=3),
            ["WO-000007200", "WO-000007100", "WO-000007009"],
        )

    def test_a_wo_still_without_a_place_is_asked_about_daily_once_its_check_in_is_old(self):
        now = timezone.now()
        visit(self.engineer, now - 3 * DAY, "WO-000008001")
        visit(self.engineer, now - 3 * datetime.timedelta(hours=1), "WO-000008002")
        order("WO-000008001", status="not_found", fetched_at=now - 31 * MINUTE)
        order("WO-000008002", status="no_resolution", fetched_at=now - 31 * MINUTE)
        self.assertEqual(self.wanted().json()["work_orders"], ["WO-000008002"])

        FieldezWorkOrder.objects.filter(wo_key="WO-000008001").update(fetched_at=now - 25 * datetime.timedelta(hours=1))
        self.assertEqual(self.wanted().json()["work_orders"], ["WO-000008002", "WO-000008001"])

    def test_wanted_is_at_most_the_limit_oldest_first(self):
        now = timezone.now()
        for n in range(3):
            visit(self.engineer, now - datetime.timedelta(hours=3 - n), f"WO-00000300{n}")
        first = timezone.make_aware(datetime.datetime.combine(timezone.localdate(now) - DAY, datetime.time.min))
        self.assertEqual(wanted_work_orders(first, now, ["All"], limit=2), ["WO-000003000", "WO-000003001"])

    # work_orders

    def test_work_orders_are_for_a_login_that_sees_every_branch(self):
        self.client.force_authenticate(self.branch_hr("Hosur"))
        self.assertEqual(self.post_results([{"wo": "WO-000004001", "status": "not_found"}]).status_code, 403)
        self.assertFalse(FieldezWorkOrder.objects.exists())

    def test_a_bad_result_is_refused_and_the_rest_kept(self):
        good = {"wo": "WO-000004001", "status": "found", "fieldez_ticket_id": 1089604, "resolutions": [resolution(at(self.past, 11))]}
        answer = self.post_results([
            good,
            "not a dict",
            {"wo": "not a wo", "status": "not_found"},
            {"wo": "WO-000004002", "status": "lost"},
            {"wo": "WO-000004003", "status": "found", "resolutions": []},
            {"wo": "WO-000004004", "status": "not_found", "resolutions": [resolution(at(self.past, 11))]},
            {"wo": "WO-000004005", "status": "found", "resolutions": [resolution(at(self.past, 11), latitude=0)]},
            {"wo": "WO-000004006", "status": "not_found", "fieldez_ticket_id": ["x"]},
            {"wo": "WO-000004007", "status": "failed", "error": {"not": "text"}},
            {"wo": "WO-000004008", "status": "not_found", "fieldez_ticket_id": "9" * 33},
        ])
        self.assertEqual((answer.status_code, answer.json()), (200, {"saved": 1, "rejected": 9}))
        stored = FieldezWorkOrder.objects.get()
        self.assertEqual((stored.wo_key, stored.status, stored.fieldez_ticket_id), ("WO-000004001", "found", "1089604"))

    def test_an_error_is_kept_without_what_cannot_be_stored_and_a_ticket_id_is_digits(self):
        # As the worker's JSON.stringify sends them: escaped.
        body = json.dumps({"results": [
            {"wo": "WO-000004101", "status": "failed", "error": "search answered\x00 500\udc00"},
            {"wo": "WO-000004102", "status": "not_found", "fieldez_ticket_id": "12a"},
            {"wo": "WO-000004103", "status": "not_found", "fieldez_ticket_id": " 1023712 "},
        ]})
        self.assertIn("\\u0000", body)
        answer = self.client.post("/api/fieldez_km/work_orders/", body, content_type="application/json")
        self.assertEqual(answer.json(), {"saved": 2, "rejected": 1})
        self.assertEqual(FieldezWorkOrder.objects.get(wo_key="WO-000004101").error, "search answered 500")
        self.assertEqual(FieldezWorkOrder.objects.get(wo_key="WO-000004103").fieldez_ticket_id, "1023712")

    def test_a_body_that_is_not_a_list_of_results_is_400(self):
        for body in ({"results": "x"}, {"results": [{}] * 201}, {}, ["x"]):
            self.assertEqual(self.client.post("/api/fieldez_km/work_orders/", body, format="json").status_code, 400, str(body)[:40])

    def test_one_row_per_wo_with_failed_reads_counted_in_a_row(self):
        for status in ("failed", "failed"):
            self.post_results([{"wo": "wo 000005001", "status": status, "error": "search answered 500"}])
        stored = FieldezWorkOrder.objects.get()
        self.assertEqual((stored.wo_key, stored.attempts, stored.error), ("WO-000005001", 2, "search answered 500"))

        before = timezone.now()
        self.post_results([{"wo": "WO-000005001", "status": "no_resolution"}])
        stored = FieldezWorkOrder.objects.get()
        self.assertEqual((stored.status, stored.attempts, stored.error), ("no_resolution", 0, ""))
        self.assertGreaterEqual(stored.fetched_at, before)

    def test_save_result_creates_then_updates(self):
        now = timezone.now()
        fields = {"status": "not_found", "fieldez_ticket_id": "", "error": "", "resolutions": []}
        _save_result("WO-000006001", fields, now)
        _save_result("WO-000006001", {**fields, "status": "failed"}, now + MINUTE)
        stored = FieldezWorkOrder.objects.get()
        self.assertEqual((stored.status, stored.attempts, stored.fetched_at), ("failed", 1, now + MINUTE))
