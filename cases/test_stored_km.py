"""The stored-km reads: /api/tracking/route_km_stored/ and /api/fieldez_km/stored/.

What they must never do is what route_km's POST does on every poll: ask Ola, or
write a row. Every test here runs behind NoNetwork (urlopen stops the test), and
the measuring functions are replaced with ones that fail the test if called.
"""

import datetime
import json
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from fieldez_km.models import FieldezKmDay
from fieldez_km.points import MAX_WANTED_DAYS

from . import route_km
from .models import RouteKmDay
from .test_route_km import NoNetwork, make_engineer

DAY = datetime.timedelta(days=1)
ROUTE = "/api/tracking/route_km_stored/"
FIELDEZ = "/api/fieldez_km/stored/"

# What a point holds, and must never come out of these reads.
PLACE = {"kind": "checkin", "at": "2026-10-01T10:00:00+05:30", "latitude": 12.7243, "longitude": 77.8267, "case_number": "PC-1", "wo": "WO-036165368", "address": "Secret Street, Hosur", "user": "fz-user", "office": False}
LOGIN = {"kind": "login", "at": "2026-10-01T09:00:00+05:30", "latitude": 12.70, "longitude": 77.80, "case_number": None, "office": True}
LOGOUT = {"kind": "logout", "at": "2026-10-01T18:00:00+05:30", "latitude": 12.71, "longitude": 77.81, "case_number": None, "office": False}
LEAKS = ("latitude", "longitude", "address", "case_number", "WO-036165368", "Secret Street", "fz-user", "12.7243")


def never(*args, **kwargs):
    raise AssertionError("a stored-km read must not measure")


class StoredKmTests(NoNetwork, TestCase):
    def setUp(self):
        super().setUp()
        for target in ("cases.route_km.refresh", "fieldez_km.km.refresh", "cases.route_km.measure"):
            patcher = mock.patch(target, side_effect=never)
            patcher.start()
            self.addCleanup(patcher.stop)
        self.client = APIClient()
        self.client.force_authenticate(User.objects.create_user(username="km-reader", password="x", role="superadmin", is_superuser=True))
        self.engineer = make_engineer("Stored Reader", branch="Hosur")
        self.today = timezone.localdate()
        self.past = self.today - 4 * DAY

    def get(self, path, engineers=None, start=None, end=None):
        ids = [self.engineer.id] if engineers is None else engineers
        return self.client.get(
            path,
            {
                "engineers": ",".join(str(i) for i in ids),
                "from": (start or self.past).isoformat(),
                "to": (end or self.past).isoformat(),
            },
        )

    def cell(self, response, engineer=None, day=None):
        engineer = self.engineer.id if engineer is None else engineer
        row = next(e for e in response.json()["engineers"] if e["engineer_id"] == engineer)
        return row["days"][(day or self.past).isoformat()]

    def route_row(self, day=None, **fields):
        defaults = {"status": "ready", "km": 85.53, "points": [LOGIN, PLACE, LOGOUT], "signature": "s", "final": True, "measured_at": timezone.now()}
        return RouteKmDay.objects.create(engineer=self.engineer, day=day or self.past, **{**defaults, **fields})

    # ---- ROUTE KM ----

    def test_a_ready_day_gives_its_km_and_counts_only(self):
        self.route_row()
        response = self.get(ROUTE)
        self.assertEqual(response.status_code, 200)
        cell = self.cell(response)
        self.assertEqual(cell["km"], 85.53)
        self.assertEqual(cell["status"], "ready")
        self.assertEqual(cell["points"], 3)
        self.assertTrue(cell["office"])
        self.assertTrue(cell["final"])
        self.assertIsNotNone(cell["measured_at"])
        body = json.dumps(response.json())
        for leak in LEAKS:
            self.assertNotIn(leak, body)

    def test_a_day_never_measured_is_not_measured_with_no_km(self):
        cell = self.cell(self.get(ROUTE))
        self.assertEqual(cell, {"km": None, "status": "not_measured", "points": 0, "office": False, "final": False, "measured_at": None})

    def test_only_a_ready_day_has_a_km(self):
        for offset, status in ((1, "pending"), (2, "no_points"), (3, "failed")):
            self.route_row(day=self.past - offset * DAY, status=status, km=12.0, final=status == "failed")
        response = self.get(ROUTE, start=self.past - 3 * DAY, end=self.past)
        for offset, status in ((1, "pending"), (2, "no_points"), (3, "failed")):
            cell = self.cell(response, day=self.past - offset * DAY)
            self.assertEqual(cell["status"], status)
            self.assertIsNone(cell["km"])
        self.assertTrue(self.cell(response, day=self.past - 3 * DAY)["final"])

    def test_zero_km_is_an_answer(self):
        self.route_row(km=0.0)
        self.assertEqual(self.cell(self.get(ROUTE))["km"], 0.0)

    def test_reading_writes_nothing_and_asks_ola_nothing(self):
        self.route_row(status="pending", km=None, final=False, measured_at=None)
        before = list(RouteKmDay.objects.values())
        response = self.get(ROUTE, start=self.past - 10 * DAY, end=self.today)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(list(RouteKmDay.objects.values()), before)
        self.assertEqual(FieldezKmDay.objects.count(), 0)

    def test_days_stop_at_today(self):
        response = self.get(ROUTE, start=self.today - DAY, end=self.today + 5 * DAY)
        self.assertEqual(response.json()["days"], [(self.today - DAY).isoformat(), self.today.isoformat()])

    def test_the_request_is_checked_as_route_km_checks_it(self):
        for engineers, start, end in (
            (["x1"], None, None),
            ([-1], None, None),
            (list(range(1, route_km.MAX_ENGINEERS + 2)), None, None),
            (None, self.past, self.past - DAY),
            (None, self.past - route_km.MAX_DAYS * DAY, self.past),
        ):
            self.assertEqual(self.get(ROUTE, engineers, start, end).status_code, 400)
        self.assertEqual(self.client.get(ROUTE, {"engineers": str(self.engineer.id), "from": "2026-13-01", "to": "2026-10-01"}).status_code, 400)
        self.assertEqual(self.get(ROUTE, start=self.past - (route_km.MAX_DAYS - 1) * DAY, end=self.past).status_code, 200)

    def test_staff_only_and_only_the_branches_they_may_see(self):
        elsewhere = make_engineer("Salem Stored", branch="Salem")
        self.client.force_authenticate(User.objects.create_user(username="hr-hosur", password="x", role="hr", allowed_sections={"attendance": ["Hosur"]}))
        ids = [e["engineer_id"] for e in self.get(ROUTE, engineers=[self.engineer.id, elsewhere.id]).json()["engineers"]]
        self.assertEqual(ids, [self.engineer.id])
        self.client.force_authenticate(User.objects.create_user(username="an-engineer", password="x", role="employee"))
        self.assertEqual(self.get(ROUTE).status_code, 403)
        self.assertEqual(self.get(FIELDEZ).status_code, 403)

    def test_post_is_not_allowed(self):
        self.assertEqual(self.client.post(ROUTE, {}, format="json").status_code, 405)
        self.assertEqual(self.client.post(FIELDEZ, {}, format="json").status_code, 405)

    # ---- FIELDEZ ----

    def fieldez_row(self, day=None, **fields):
        defaults = {
            "status": "ready",
            "km": 91.2,
            "points": [LOGIN, PLACE, dict(PLACE, case_number="PC-2"), LOGOUT],
            "signature": "s",
            "final": False,
            "measured_at": timezone.now(),
            "checkins": 5,
            "missing": [
                {"case_number": "PC-3", "wo": "WO-036165370", "reason": "not_in_fieldez"},
                {"case_number": "PC-4", "wo": None, "reason": "no_wo"},
            ],
        }
        return FieldezKmDay.objects.create(engineer=self.engineer, day=day or self.past, **{**defaults, **fields})

    def test_a_fieldez_day_gives_its_km_and_counts_by_reason(self):
        self.fieldez_row()
        response = self.get(FIELDEZ)
        cell = self.cell(response)
        self.assertEqual(cell["km"], 91.2)
        self.assertEqual(cell["status"], "ready")
        self.assertEqual(cell["checkins"], 5)
        self.assertEqual(cell["placed"], 2)
        self.assertEqual(cell["waiting"], 1)
        self.assertEqual(cell["missing_by_reason"], {"no_wo": 1, "not_in_fieldez": 1, "no_problem_resolution": 0, "fieldez_failed": 0, "not_read": 0})
        self.assertFalse(cell["beyond_lookback"])
        body = json.dumps(response.json())
        for leak in LEAKS + ("WO-036165370", "PC-3"):
            self.assertNotIn(leak, body)

    def test_a_fieldez_day_never_measured_knows_no_counts(self):
        cell = self.cell(self.get(FIELDEZ))
        self.assertEqual(cell["status"], "not_measured")
        self.assertIsNone(cell["km"])
        self.assertIsNone(cell["checkins"])
        self.assertIsNone(cell["placed"])
        self.assertIsNone(cell["waiting"])

    def test_a_waiting_day_has_no_km(self):
        self.fieldez_row(status="waiting", km=None, checkins=3, missing=[], points=[LOGIN, PLACE, LOGOUT])
        cell = self.cell(self.get(FIELDEZ))
        self.assertEqual(cell["status"], "waiting")
        self.assertIsNone(cell["km"])
        self.assertEqual(cell["waiting"], 2)

    def test_the_lookback_boundary_is_the_workers_own(self):
        last_readable = self.today - (MAX_WANTED_DAYS - 1) * DAY
        first_beyond = last_readable - DAY
        response = self.get(FIELDEZ, start=first_beyond, end=last_readable)
        self.assertTrue(self.cell(response, day=first_beyond)["beyond_lookback"])
        self.assertFalse(self.cell(response, day=last_readable)["beyond_lookback"])

    def test_fieldez_reading_writes_nothing(self):
        self.fieldez_row(status="pending", km=None)
        before = list(FieldezKmDay.objects.values())
        self.assertEqual(self.get(FIELDEZ, start=self.past - 40 * DAY, end=self.today).status_code, 200)
        self.assertEqual(list(FieldezKmDay.objects.values()), before)
        self.assertEqual(RouteKmDay.objects.count(), 0)
