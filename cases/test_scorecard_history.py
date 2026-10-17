"""An engineer's own past days: km, Assigned / Attended / Closed and targets.

Payroll kept today only: one scorecard row per engineer, overwritten on every
sync. The history is a row per engineer per day beside it -- written for today
by the live push, and for the days before by OpenCall's history push, which
never overwrites today's live row -- and read back by the engineer alone, each
day beside the km the office's KM report shows for it.
"""
import datetime
import os
from decimal import Decimal
from unittest import mock

from django.db import IntegrityError, transaction
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from authentication.models import User
from cases.models import EngineerScorecard, EngineerScorecardDay, LocationPing
from cases.views import MAX_HISTORY_DAYS, MAX_SCORECARD_DAYS, _DayKm, _js_round2
from employees.models import Employee

from .test_km_replay import DAY, KM, NEW_RULE, KmReplay, make_engineer

ONE_DAY = datetime.timedelta(days=1)

# The history views read "today" off the clock; these tests set it.
TODAY = datetime.date(2026, 10, 17)
NOW = timezone.make_aware(datetime.datetime.combine(TODAY, datetime.time(12, 0)))

KEPT = "today's numbers come from the live push."


def make_person(username, name, **extra):
    user = User.objects.create_user(username=username, password="x", role="employee")
    employee = Employee.objects.create(
        user=user,
        employee_name=name,
        email=f"{username}@example.com",
        role="Field Engineer",
        department="Service",
        branch="Chennai",
        salary=Decimal("20000"),
        **extra,
    )
    return user, employee


def numbers(row):
    return (row.assigned, row.attended, row.closed, row.daily_target, row.monthly_target)


class ScorecardDayModelTests(TestCase):
    def test_one_row_per_engineer_per_day(self):
        _, ravi = make_person("model-ravi", "Model Ravi")
        _, mani = make_person("model-mani", "Model Mani")
        EngineerScorecardDay.objects.create(engineer=ravi, day=DAY, source="live")
        with self.assertRaises(IntegrityError), transaction.atomic():
            EngineerScorecardDay.objects.create(engineer=ravi, day=DAY, source="history")

        # Another day of his, and somebody else's same day, are rows of their own.
        EngineerScorecardDay.objects.create(engineer=ravi, day=DAY - ONE_DAY, source="history")
        EngineerScorecardDay.objects.create(engineer=mani, day=DAY, source="live")
        self.assertEqual(ravi.scorecard_days.count(), 2)
        self.assertEqual(EngineerScorecardDay.objects.count(), 3)

    def test_a_count_is_never_negative(self):
        _, ravi = make_person("model-ravi", "Model Ravi")
        with self.assertRaises(IntegrityError), transaction.atomic():
            EngineerScorecardDay.objects.create(engineer=ravi, day=DAY, closed=-1, source="live")


class PushTestCase(APITestCase):
    """OpenCall's two pushes, as the sync account sends them."""

    def setUp(self):
        self.admin = User.objects.create_user(
            username="sync-bot", password="x", role="superadmin", is_superuser=True
        )
        self.ravi_user, self.ravi = make_person("ravi", "Ravi Kumar", phone="+91 98400 55221")
        self.mani_user, self.mani = make_person("mani", "Mani S")
        self.today = timezone.localdate()

    def _live(self, rows, as_of=None, daily=7, monthly=175):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/cases/scorecards/",
            {
                "as_of": str(as_of or self.today),
                "daily_target": daily,
                "monthly_target": monthly,
                "rows": rows,
            },
            format="json",
        )
        self.client.force_authenticate(None)
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def _history(self, days, user=None):
        self.client.force_authenticate(user or self.admin)
        response = self.client.post("/api/cases/scorecard_days/", {"days": days}, format="json")
        self.client.force_authenticate(None)
        return response

    def _day(self, day, who="ravi@example.com", assigned=0, attended=0, closed=0, daily=7, monthly=175):
        return {
            "engineer_email": who,
            "day": day.isoformat(),
            "assigned": assigned,
            "attended": attended,
            "closed": closed,
            "daily_target": daily,
            "monthly_target": monthly,
        }

    def _row(self, engineer, day):
        return EngineerScorecardDay.objects.get(engineer=engineer, day=day)


class LivePushTests(PushTestCase):
    """POST scorecards/ files today's numbers under today, beside the card."""

    def test_a_live_push_files_the_day_beside_the_card(self):
        response = self._live([
            {"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5,
             "closed": 3, "month_closed": 48},
        ])
        # The push answers exactly as it did.
        self.assertEqual(response.data, {"saved": 1, "zeroed": 0, "skipped": []})

        row = self._row(self.ravi, self.today)
        self.assertEqual(numbers(row), (6, 5, 3, 7, 175))
        self.assertEqual(row.source, "live")
        card = EngineerScorecard.objects.get(engineer=self.ravi)
        self.assertEqual((card.assigned, card.attended, card.closed), (6, 5, 3))

    def test_a_second_push_rewrites_the_days_row_in_place(self):
        self._live([{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5, "closed": 3}])
        self._live([{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 6, "closed": 4}])

        self.assertEqual(EngineerScorecardDay.objects.count(), 1)
        self.assertEqual(numbers(self._row(self.ravi, self.today)), (6, 6, 4, 7, 175))

    def test_each_day_keeps_its_own_row(self):
        yesterday = self.today - ONE_DAY
        self._live(
            [{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5, "closed": 3}],
            as_of=yesterday,
        )
        self._live([{"engineer_email": "ravi@example.com", "assigned": 2, "attended": 1, "closed": 1}])

        self.assertEqual(numbers(self._row(self.ravi, yesterday)), (6, 5, 3, 7, 175))
        self.assertEqual(numbers(self._row(self.ravi, self.today)), (2, 1, 1, 7, 175))

    def test_the_cards_zeroing_never_touches_a_day(self):
        """Ravi is off today's plan: his card is zeroed, as always. His
        yesterday stays as it was, and no zero row is invented for today."""
        yesterday = self.today - ONE_DAY
        self._live(
            [{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5, "closed": 3}],
            as_of=yesterday,
        )
        response = self._live(
            [{"engineer_email": "mani@example.com", "assigned": 2, "attended": 2, "closed": 2}]
        )
        self.assertEqual(response.data["zeroed"], 1)
        card = EngineerScorecard.objects.get(engineer=self.ravi)
        self.assertEqual((card.as_of, card.assigned), (self.today, 0))

        self.assertEqual(numbers(self._row(self.ravi, yesterday)), (6, 5, 3, 7, 175))
        self.assertFalse(EngineerScorecardDay.objects.filter(engineer=self.ravi, day=self.today).exists())
        self.assertEqual(numbers(self._row(self.mani, self.today)), (2, 2, 2, 7, 175))

    def test_junk_counts_are_filed_as_the_card_reads_them(self):
        self._live([
            {"engineer_email": "ravi@example.com", "assigned": "six", "attended": None, "closed": -4},
        ])
        self.assertEqual(numbers(self._row(self.ravi, self.today)), (0, 0, 0, 7, 175))


class HistoryPushTests(PushTestCase):
    """POST scorecard_days/: OpenCall rewriting the days before today."""

    def test_only_staff_may_push(self):
        day = self._day(self.today - ONE_DAY, assigned=3)
        self.assertEqual(self._history([day], user=self.ravi_user).status_code, 403)
        response = self.client.post("/api/cases/scorecard_days/", {"days": [day]}, format="json")
        self.assertIn(response.status_code, (401, 403))
        self.assertFalse(EngineerScorecardDay.objects.exists())

    def test_past_days_are_stored_then_rewritten_in_place(self):
        first, second = self.today - 2 * ONE_DAY, self.today - ONE_DAY
        response = self._history([
            self._day(first, assigned=6, attended=5, closed=3),
            self._day(second, assigned=5, attended=5, closed=5, monthly=180),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data,
            {"stored": 2, "updated": 0, "unmatched": [], "rejected": [], "zeroed": 0},
        )
        self.assertEqual(numbers(self._row(self.ravi, first)), (6, 5, 3, 7, 175))
        self.assertEqual(numbers(self._row(self.ravi, second)), (5, 5, 5, 7, 180))
        self.assertEqual(self._row(self.ravi, first).source, "history")

        # A late Evening edit, pushed again: the same rows, new numbers.
        response = self._history([
            self._day(first, assigned=6, attended=6, closed=4),
            self._day(second, assigned=5, attended=5, closed=5, monthly=180),
        ])
        self.assertEqual((response.data["stored"], response.data["updated"]), (0, 2))
        self.assertEqual(EngineerScorecardDay.objects.count(), 2)
        self.assertEqual(numbers(self._row(self.ravi, first)), (6, 6, 4, 7, 175))

    def test_the_live_card_is_never_touched(self):
        self._live([{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5,
                     "closed": 3, "month_closed": 48}])
        before = EngineerScorecard.objects.get(engineer=self.ravi)

        response = self._history([
            self._day(self.today - ONE_DAY, assigned=9, attended=9, closed=9),
            self._day(self.today - ONE_DAY, who="mani@example.com", assigned=1),
        ])
        self.assertEqual(response.data["stored"], 2, response.data)

        after = EngineerScorecard.objects.get(engineer=self.ravi)
        self.assertEqual(
            (after.as_of, after.assigned, after.attended, after.closed, after.month_closed,
             after.updated_at),
            (before.as_of, 6, 5, 3, 48, before.updated_at),
        )
        # Nor is a card made for somebody the history push names.
        self.assertFalse(EngineerScorecard.objects.filter(engineer=self.mani).exists())

    def test_todays_live_row_is_kept(self):
        self._live([{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5, "closed": 3}])

        response = self._history([self._day(self.today, assigned=1, attended=1, closed=1)])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data,
            {
                "stored": 0,
                "updated": 0,
                "unmatched": [],
                "rejected": [{"index": 0, "reason": KEPT}],
                "zeroed": 0,
            },
        )
        row = self._row(self.ravi, self.today)
        self.assertEqual((numbers(row), row.source), ((6, 5, 3, 7, 175), "live"))

    # -------------------------------------------- a day pushed as complete

    def _complete(self, days, complete_days):
        self.client.force_authenticate(self.admin)
        response = self.client.post(
            "/api/cases/scorecard_days/",
            {"days": days, "complete_days": complete_days},
            format="json",
        )
        self.client.force_authenticate(None)
        self.assertEqual(response.status_code, 200, response.data)
        return response

    def test_a_complete_day_zeroes_whoever_dropped_off_it(self):
        """The review's case: Ravi's calls went to Mani later that day.

        The live push filed Ravi's morning count under the day; OpenCall's day,
        pushed again, lists only Mani. Without the zeroing Ravi's History kept
        3 assigned for a day Productivity gives him nothing -- an overcount.
        """
        yesterday = self.today - ONE_DAY
        self._live(
            [
                {"engineer_email": "ravi@example.com", "assigned": 3, "attended": 1, "closed": 0},
                {"engineer_email": "mani@example.com", "assigned": 2, "attended": 2, "closed": 1},
            ],
            as_of=yesterday,
        )
        response = self._complete(
            [self._day(yesterday, who="mani@example.com", assigned=5, attended=4, closed=3)],
            [yesterday.isoformat()],
        )
        self.assertEqual(response.data["zeroed"], 1, response.data)
        ravi = self._row(self.ravi, yesterday)
        self.assertEqual(numbers(ravi), (0, 0, 0, 7, 175), "counts zeroed, targets kept")
        self.assertEqual(ravi.source, "history")
        self.assertEqual(numbers(self._row(self.mani, yesterday)), (5, 4, 3, 7, 175))
        # Zeroed, never deleted: the day is still there for him, at nothing.
        self.assertEqual(EngineerScorecardDay.objects.filter(day=yesterday).count(), 2)

    def test_without_complete_days_nobody_is_zeroed(self):
        """An older OpenCall, or a day split over requests: today's behaviour."""
        yesterday = self.today - ONE_DAY
        self._live(
            [{"engineer_email": "ravi@example.com", "assigned": 3, "attended": 1, "closed": 0}],
            as_of=yesterday,
        )
        response = self._history([self._day(yesterday, who="mani@example.com", assigned=5)])
        self.assertEqual(response.data["zeroed"], 0, response.data)
        self.assertEqual(numbers(self._row(self.ravi, yesterday))[:3], (3, 1, 0))

    def test_today_is_never_zeroed_even_if_called_complete(self):
        self._live([{"engineer_email": "ravi@example.com", "assigned": 3, "attended": 1, "closed": 0}])
        response = self._complete(
            [self._day(self.today, who="mani@example.com", assigned=5)], [self.today.isoformat()]
        )
        self.assertEqual(response.data["zeroed"], 0, response.data)
        self.assertEqual(numbers(self._row(self.ravi, self.today))[:3], (3, 1, 0))

    def test_a_complete_day_nobody_matched_on_zeroes_nobody(self):
        """Every name failing to match is a lookup that broke, not an empty day."""
        yesterday = self.today - ONE_DAY
        self._live(
            [{"engineer_email": "ravi@example.com", "assigned": 3, "attended": 1, "closed": 0}],
            as_of=yesterday,
        )
        response = self._complete(
            [self._day(yesterday, who="nobody@example.com", assigned=5)], [yesterday.isoformat()]
        )
        self.assertEqual(response.data["zeroed"], 0, response.data)
        self.assertEqual(response.data["unmatched"], ["nobody@example.com"])
        self.assertEqual(numbers(self._row(self.ravi, yesterday))[:3], (3, 1, 0))

    def test_only_the_named_complete_day_is_zeroed(self):
        two_back, yesterday = self.today - 2 * ONE_DAY, self.today - ONE_DAY
        for day in (two_back, yesterday):
            self._live(
                [{"engineer_email": "ravi@example.com", "assigned": 3, "attended": 1, "closed": 0}],
                as_of=day,
            )
        self._complete(
            [
                self._day(two_back, who="mani@example.com", assigned=5),
                self._day(yesterday, who="mani@example.com", assigned=5),
            ],
            [yesterday.isoformat()],
        )
        self.assertEqual(numbers(self._row(self.ravi, yesterday))[:3], (0, 0, 0))
        self.assertEqual(numbers(self._row(self.ravi, two_back))[:3], (3, 1, 0))

    def test_today_before_any_live_push_is_written_and_the_live_push_takes_it_over(self):
        response = self._history([self._day(self.today, assigned=1, attended=1, closed=1)])
        self.assertEqual(response.data["stored"], 1, response.data)
        self.assertEqual(self._row(self.ravi, self.today).source, "history")

        # The live push always wins today...
        self._live([{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5, "closed": 3}])
        row = self._row(self.ravi, self.today)
        self.assertEqual((numbers(row), row.source), ((6, 5, 3, 7, 175), "live"))

        # ...and from then on today is its own.
        response = self._history([self._day(self.today, assigned=1, attended=1, closed=1)])
        self.assertEqual(response.data["rejected"], [{"index": 0, "reason": KEPT}])
        self.assertEqual(numbers(self._row(self.ravi, self.today)), (6, 5, 3, 7, 175))

    def test_a_past_days_live_row_is_rewritten(self):
        """Yesterday's last live push was not its final numbers."""
        yesterday = self.today - ONE_DAY
        self._live(
            [{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5, "closed": 3}],
            as_of=yesterday,
        )
        response = self._history([self._day(yesterday, assigned=6, attended=6, closed=5)])
        self.assertEqual((response.data["stored"], response.data["updated"]), (0, 1))
        row = self._row(self.ravi, yesterday)
        self.assertEqual((numbers(row), row.source), ((6, 6, 5, 7, 175), "history"))

    def test_losing_the_race_to_the_live_push_keeps_its_row(self):
        """The history push read no row for today, and the live push's insert
        landed before its own: the insert fails on the unique pair, the row is
        read again, and it is today's live row -- kept."""
        EngineerScorecardDay.objects.create(
            engineer=self.ravi, day=self.today, assigned=6, attended=5, closed=3,
            daily_target=7, monthly_target=175, source="live",
        )
        manager = EngineerScorecardDay.objects
        locked_read = manager.select_for_update
        reads = []

        def first_read_misses_it():
            reads.append(1)
            return manager.none() if len(reads) == 1 else locked_read()

        with mock.patch.object(manager, "select_for_update", side_effect=first_read_misses_it):
            response = self._history([self._day(self.today, assigned=1, attended=1, closed=1)])

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(reads), 2)
        self.assertEqual(response.data["rejected"], [{"index": 0, "reason": KEPT}])
        row = self._row(self.ravi, self.today)
        self.assertEqual((numbers(row), row.source), ((6, 5, 3, 7, 175), "live"))

    def test_an_unmatched_engineer_is_named_once_and_nobody_else_loses_a_day(self):
        yesterday = self.today - ONE_DAY
        response = self._history([
            {**self._day(yesterday), "engineer_email": None, "engineer_name": "Nobody At All"},
            {**self._day(yesterday - ONE_DAY), "engineer_email": None,
             "engineer_name": "Nobody At All"},
            self._day(yesterday, who="ghost@example.com"),
            self._day(yesterday, assigned=4),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["unmatched"], ["Nobody At All", "ghost@example.com"])
        self.assertEqual((response.data["stored"], response.data["rejected"]), (1, []))
        self.assertEqual(self._row(self.ravi, yesterday).assigned, 4)

    def test_engineers_are_matched_as_the_live_push_matches_them(self):
        """By phone across formatting, under a name spelt differently."""
        response = self._history([
            {**self._day(self.today - ONE_DAY, assigned=4), "engineer_email": None,
             "engineer_name": "spelt differently", "engineer_phone": "9840055221"},
        ])
        self.assertEqual(response.data["stored"], 1, response.data)
        self.assertEqual(self._row(self.ravi, self.today - ONE_DAY).assigned, 4)

    def test_a_bad_item_is_rejected_by_its_index(self):
        response = self._history([
            "not an object",
            {**self._day(self.today), "day": "2026-02-30"},
            {**self._day(self.today), "day": None},
            self._day(self.today + ONE_DAY),
            self._day(self.today - datetime.timedelta(days=401)),
            self._day(self.today - datetime.timedelta(days=400), assigned=2),
            {**self._day(self.today - ONE_DAY), "engineer_email": "  "},
            self._day(self.today - ONE_DAY, assigned=3),
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["rejected"],
            [
                {"index": 0, "reason": "each day must be an object."},
                {"index": 1, "reason": "day must be YYYY-MM-DD."},
                {"index": 2, "reason": "day must be YYYY-MM-DD."},
                {"index": 3, "reason": "day is in the future."},
                {"index": 4, "reason": "day is older than 400 days."},
                {"index": 6, "reason": "no engineer given."},
            ],
        )
        self.assertEqual(response.data["stored"], 2)
        self.assertEqual(
            sorted(EngineerScorecardDay.objects.values_list("day", "assigned")),
            [(self.today - datetime.timedelta(days=400), 2), (self.today - ONE_DAY, 3)],
        )

    def test_the_body_must_carry_a_list_of_days(self):
        self.client.force_authenticate(self.admin)
        for body in ({}, {"days": "all of them"}, {"days": {"day": "2026-10-16"}}, [self._day(self.today)]):
            response = self.client.post("/api/cases/scorecard_days/", body, format="json")
            self.assertEqual(response.status_code, 400, body)
        self.assertFalse(EngineerScorecardDay.objects.exists())

    def test_at_most_2000_days_a_push(self):
        future = self._day(self.today + ONE_DAY)
        response = self._history([future] * (MAX_SCORECARD_DAYS + 1))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["max_days"], 2000)

        # 2000 is allowed (each refused on its own merits here, cheaply).
        response = self._history([future] * MAX_SCORECARD_DAYS)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.data["rejected"]), 2000)


class MyHistoryTests(APITestCase):
    """GET my_history/: the caller's own days, and nobody else's."""

    def setUp(self):
        patcher = mock.patch.dict(os.environ, NEW_RULE)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.ravi_user, self.ravi = make_person("ravi", "Ravi Kumar")
        self.mani_user, self.mani = make_person("mani", "Mani S")

    def _ask(self, user, params):
        self.client.force_authenticate(user)
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            response = self.client.get("/api/cases/my_history/", params)
        self.client.force_authenticate(None)
        return response

    def _span(self, first, last):
        return {"from": first.isoformat(), "to": last.isoformat()}

    def _fix(self, engineer, day, hour, minute, latitude):
        taken = timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))
        LocationPing.objects.create(
            engineer=engineer, latitude=latitude, longitude=78.0, accuracy=8,
            timestamp=taken, received_at=taken,
        )

    def _score(self, engineer, day, assigned, attended, closed, monthly=175, source="history"):
        EngineerScorecardDay.objects.create(
            engineer=engineer, day=day, assigned=assigned, attended=attended, closed=closed,
            daily_target=7, monthly_target=monthly, source=source,
        )

    def test_every_day_of_the_range_oldest_first(self):
        first, middle = TODAY - 2 * ONE_DAY, TODAY - ONE_DAY
        self._score(self.ravi, first, 6, 5, 3)
        self._score(self.ravi, TODAY, 4, 2, 1, monthly=180, source="live")
        for step in range(3):  # two kilometres the day before
            self._fix(self.ravi, middle, 10, step * 10, 11.0 + step * KM)
        for step in range(2):  # one this morning
            self._fix(self.ravi, TODAY, 9, step * 10, 12.0 + step * KM)

        response = self._ask(self.ravi_user, self._span(first, TODAY))
        self.assertEqual(response.status_code, 200, response.data)
        data = response.data
        self.assertEqual((data["from"], data["to"]), (first.isoformat(), TODAY.isoformat()))
        self.assertEqual(
            [entry["day"] for entry in data["days"]],
            [first.isoformat(), middle.isoformat(), TODAY.isoformat()],
        )
        scored, quiet, today = data["days"]
        self.assertEqual(
            scored,
            {"day": first.isoformat(), "km": 0.0, "assigned": 6, "attended": 5, "closed": 3,
             "daily_target": 7, "has_score": True},
        )
        # A day nobody pushed numbers for: zeros, and says so.
        self.assertEqual(
            {key: quiet[key] for key in ("assigned", "attended", "closed", "daily_target", "has_score")},
            {"assigned": 0, "attended": 0, "closed": 0, "daily_target": 0, "has_score": False},
        )
        self.assertAlmostEqual(quiet["km"], 2.0, delta=0.01)
        self.assertAlmostEqual(today["km"], 1.0, delta=0.01)
        self.assertTrue(today["has_score"])

        totals = data["totals"]
        self.assertEqual(totals["km"], _js_round2(0.0 + quiet["km"] + today["km"]))
        self.assertEqual(totals["days_tracked"], 2)
        self.assertEqual((totals["assigned"], totals["attended"], totals["closed"]), (10, 7, 4))
        # The newest day's target, not the first's.
        self.assertEqual(totals["monthly_target"], 180)

    def test_only_the_callers_own_days(self):
        """There is no engineer to pass; one passed anyway is not read."""
        self._score(self.mani, TODAY, 9, 9, 9)
        for step in range(3):
            self._fix(self.mani, TODAY, 9, step * 10, 13.0 + step * KM)

        response = self._ask(
            self.ravi_user, {**self._span(TODAY, TODAY), "engineer": self.mani.id}
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(
            response.data["days"],
            [{"day": TODAY.isoformat(), "km": 0.0, "assigned": 0, "attended": 0, "closed": 0,
              "daily_target": 0, "has_score": False}],
        )
        self.assertEqual(
            response.data["totals"],
            {"km": 0.0, "days_tracked": 0, "assigned": 0, "attended": 0, "closed": 0,
             "monthly_target": 0},
        )

        his = self._ask(self.mani_user, self._span(TODAY, TODAY)).data["days"][0]
        self.assertEqual((his["assigned"], his["has_score"]), (9, True))
        self.assertAlmostEqual(his["km"], 2.0, delta=0.01)

    def test_a_login_with_no_employee_record_is_told_so(self):
        loose = User.objects.create_user(username="loose", password="x", role="employee")
        response = self._ask(loose, self._span(TODAY, TODAY))
        self.assertEqual(response.status_code, 409)

    def test_reading_needs_a_login(self):
        response = self.client.get("/api/cases/my_history/", self._span(TODAY, TODAY))
        self.assertIn(response.status_code, (401, 403))

    def test_a_range_is_at_most_62_days(self):
        first = TODAY - datetime.timedelta(days=MAX_HISTORY_DAYS - 1)
        response = self._ask(self.ravi_user, self._span(first, TODAY))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["days"]), 62)
        self.assertEqual(response.data["days"][0]["day"], first.isoformat())
        self.assertEqual(response.data["days"][-1]["day"], TODAY.isoformat())

        response = self._ask(self.ravi_user, self._span(first - ONE_DAY, TODAY))
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["max_days"], 62)

        # Counted after `to` stops at today: only the days it covers.
        response = self._ask(self.ravi_user, self._span(first, TODAY + 10 * ONE_DAY))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(len(response.data["days"]), 62)

    def test_bad_bounds_are_refused(self):
        for params in (
            {},
            {"from": "2026-10-01"},
            {"to": "2026-10-01"},
            {"from": "01-10-2026", "to": "2026-10-02"},
            {"from": "2026-02-30", "to": "2026-03-01"},
            {"from": "2026-10-05", "to": "2026-10-01"},
        ):
            response = self._ask(self.ravi_user, params)
            self.assertEqual(response.status_code, 400, params)

    def test_to_stops_at_today(self):
        response = self._ask(self.ravi_user, self._span(TODAY - 2 * ONE_DAY, TODAY + 5 * ONE_DAY))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["to"], TODAY.isoformat())
        self.assertEqual(
            [entry["day"] for entry in response.data["days"]],
            [(TODAY - 2 * ONE_DAY).isoformat(), (TODAY - ONE_DAY).isoformat(), TODAY.isoformat()],
        )

    def test_a_range_wholly_after_today_has_no_days(self):
        response = self._ask(self.ravi_user, self._span(TODAY + ONE_DAY, TODAY + 3 * ONE_DAY))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["days"], [])
        self.assertEqual(response.data["totals"]["km"], 0.0)
        self.assertEqual(response.data["totals"]["days_tracked"], 0)

    def test_the_total_rounds_halves_up_like_the_km_report(self):
        """The KM report adds a row's days and takes Math.round(x * 100) / 100.
        Python's round() would make 1.125 + 1.0 come to 2.12."""
        with mock.patch.object(_DayKm, "km", side_effect=[1.125, 1.0]):
            response = self._ask(self.ravi_user, self._span(TODAY - ONE_DAY, TODAY))
        self.assertEqual([entry["km"] for entry in response.data["days"]], [1.125, 1.0])
        self.assertEqual(response.data["totals"]["km"], 2.13)
        self.assertEqual(round(1.125 + 1.0, 2), 2.12)

    def test_rounding_is_javascripts(self):
        # Each pair checked against node: Math.round(x * 100) / 100.
        for value, rounded in (
            (2.125, 2.13),
            (0.125, 0.13),
            (2.675, 2.68),
            (1.005, 1.0),
            (0.1 + 0.2, 0.3),
            (158.99, 158.99),
            (0.0, 0.0),
        ):
            self.assertEqual(_js_round2(value), rounded, value)


class HistoryParityTests(TestCase):
    """A day's km in the history is the office roster's distance_km for it
    and, today, the phone's "Travelled today": one helper reads all three."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.engineer = make_engineer("history-parity", "History Hari")
        cls.office = User.objects.create_user(
            username="history-office", password="x", role="admin"
        )

    def setUp(self):
        patcher = mock.patch.dict(os.environ, NEW_RULE)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _board(self, office, day):
        roster = office.get("/api/tracking/roster/", {"date": day.isoformat()}).data
        return next(row for row in roster if row["engineer_id"] == self.engineer.id)

    def test_history_roster_and_duty_agree_across_a_login_and_a_late_batch(self):
        day = KmReplay(self.engineer, self.user)
        day.login(day.at(9, 0))
        for step, minute in enumerate(range(5, 61, 5)):
            day.fix(day.at(9, 0) + datetime.timedelta(minutes=minute), 11.0 + step * KM)
        # Five fixes held on the phone, sent behind a newer one.
        held = [day.make_fix(day.at(10, 5 + n), 11.0 + (12 + n) * KM) for n in range(5)]
        day.fix(day.at(10, 15), 11.0 + 18 * KM)
        day.batch(held, arrives=day.at(10, 30))
        day.fix(day.at(12, 0), 11.0 + 19 * KM)
        day.logout(day.at(12, 1))
        # Twenty kilometres off duty, then a Login: the drive to it is no work.
        day.login(day.at(15, 0))
        day.fix(day.at(15, 1), 11.0 + 39 * KM, after_gap=True)
        day.fix(day.at(15, 6), 11.0 + 40 * KM)
        readings = day.run(self)

        office = APIClient()
        office.force_authenticate(self.office)
        with mock.patch("django.utils.timezone.now", return_value=day.at(23)):
            duty = day.phone.get("/api/tracking/duty/").data["today_km"]
            board = self._board(office, DAY)
            history = day.phone.get(
                "/api/cases/my_history/",
                {"from": (DAY - ONE_DAY).isoformat(), "to": DAY.isoformat()},
            ).data

        self.assertEqual(history["days"][-1]["day"], DAY.isoformat())
        self.assertEqual(history["days"][-1]["km"], board["distance_km"])
        self.assertEqual(history["days"][-1]["km"], duty)
        self.assertEqual(readings[-1][2], duty)
        self.assertEqual(history["totals"]["km"], duty)
        self.assertEqual(history["totals"]["days_tracked"], 1)
        # Eleven kilometres of morning, eight through the held stretch to
        # noon, one after the Login -- never the twenty driven to it.
        self.assertAlmostEqual(duty, 11 + 8 + 1, delta=0.3)

        # Next morning the day is history, and still the roster's figure --
        # after the open duty it ended on has been closed as forgotten.
        next_morning = day.at(23) + datetime.timedelta(hours=11)
        with mock.patch("django.utils.timezone.now", return_value=next_morning):
            board = self._board(office, DAY)
            later = day.phone.get(
                "/api/cases/my_history/",
                {"from": DAY.isoformat(), "to": (DAY + ONE_DAY).isoformat()},
            ).data
        self.assertEqual(later["days"][0]["km"], board["distance_km"])
        self.assertEqual(later["days"][0]["km"], duty)
        self.assertEqual(later["days"][1]["km"], 0.0)

    def test_a_day_is_its_own_fixes_to_the_microsecond(self):
        """Asked for between the day's two instants: the very fixes
        timestamp__date found, midnight in and the next midnight out."""
        midnight = timezone.make_aware(datetime.datetime.combine(DAY, datetime.time.min))
        tick = datetime.timedelta(microseconds=1)
        for step, taken in enumerate(
            (midnight - tick, midnight, midnight + datetime.timedelta(hours=12),
             midnight + ONE_DAY - tick, midnight + ONE_DAY)
        ):
            LocationPing.objects.create(
                engineer=self.engineer, latitude=11.0 + step * KM, longitude=78.0, accuracy=8,
                timestamp=taken, received_at=taken,
            )
        by_date = LocationPing.objects.filter(
            engineer=self.engineer, timestamp__date=DAY
        ).order_by("timestamp", "id")
        trail = _DayKm([self.engineer.id], DAY).trails[self.engineer.id]
        self.assertEqual([p.id for p in trail], [p.id for p in by_date])
        self.assertEqual(len(trail), 3)
