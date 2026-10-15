"""The KM report's case counts, held to the day timeline they count.

The Live Tracking board's "CASES" is the list /api/tracking/day/ gives for one
engineer-day, and its "Closed" rows the ones with a Check out that day. The KM
report asks the same of a whole cycle at once (day_cases.cases_by_day), so every
test here builds a set of days and asks /day/ itself for each engineer-day: the
counts must be what the timeline lists, not a second reading of the rules.
"""
import datetime

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User

from . import day_cases
from .models import Case, CaseVisit
from .test_route_km import NoNetwork, at, make_engineer

DAY = datetime.timedelta(days=1)

# The timeline's own entries for a call; a "stop" is the board's, from pings.
CASE_EVENTS = {"assigned", "started", "reached", "completed", "carried"}


def call(engineer, **fields):
    fields.setdefault("status", "assigned")
    # Off the plan unless a test puts it there: a call in the plan with no plan
    # day is on today's list.
    fields.setdefault("in_current_plan", False)
    return Case.objects.create(customer_name="A customer", title="Service call", assigned_to=engineer, **fields)


def visit(case, engineer, plan_date, **fields):
    return CaseVisit.objects.create(case=case, engineer=engineer, plan_date=plan_date, **fields)


class CasesByDayTests(NoNetwork, TestCase):
    def setUp(self):
        super().setUp()
        self.office = User.objects.create_user(username="office-cases", password="x", role="superadmin", is_superuser=True)
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.today = timezone.localdate()
        self.days = [self.today - n * DAY for n in range(5, -1, -1)]
        self.anu = make_engineer("Anu Cases", branch="Hosur")
        self.bala = make_engineer("Bala Cases", branch="Salem")

    def timeline(self, engineer, day):
        """(cases, closed, check-ins) as the board counts them from /day/: its
        CASES rows, its "Closed" rows, and its CHECK-INS tile -- the punches
        of kind "in"."""
        response = self.client.get("/api/tracking/day/", {"engineer": engineer.id, "date": day.isoformat()})
        self.assertEqual(response.status_code, 200)
        listed, closed = set(), set()
        for event in response.json()["events"]:
            key = event.get("case_number") or event.get("case_ref")
            if key and event.get("type") in CASE_EVENTS:
                listed.add(key)
                if event["type"] == "completed":
                    closed.add(key)
        checkins = sum(1 for punch in response.json()["punches"] if punch["kind"] == "in")
        return len(listed), len(closed), checkins

    def assert_matches_the_timeline(self, engineers):
        counts = day_cases.cases_by_day([e.id for e in engineers], self.days, today=self.today)
        for engineer in engineers:
            for day in self.days:
                with self.subTest(engineer=engineer.employee_name, day=day.isoformat()):
                    cases, closed, checkins = self.timeline(engineer, day)
                    self.assertEqual(
                        counts[(engineer.id, day)], {"cases": cases, "closed": closed, "checkins": checkins}
                    )
        return counts

    def test_every_way_a_call_belongs_to_a_day_counts_as_the_timeline_lists_it(self):
        today, d1, d2, d3, d4 = self.today, self.today - DAY, self.today - 2 * DAY, self.today - 3 * DAY, self.today - 4 * DAY
        # Planned: today's plan, a call with no plan day (today only), a past plan day.
        call(self.anu, in_current_plan=True, plan_date=today)
        call(self.anu, in_current_plan=True, plan_date=None)
        call(self.anu, in_current_plan=True, plan_date=d2)
        call(self.anu, in_current_plan=True, plan_date=d2, status="cancelled")  # not listed
        call(self.anu, in_current_plan=False, plan_date=d2)  # not in the plan
        # Touched by each stamp -- a cancelled call too, as the timeline lists it.
        call(self.anu, assigned_at=at(d1, 9))
        call(self.anu, started_at=at(d1, 10))
        call(self.anu, reached_at=at(d3, 11))
        call(self.anu, completed_at=at(d3, 12), status="completed")
        call(self.anu, assigned_at=at(d4, 9), status="cancelled")
        # Visited: planned for the day, checked into on it, checked out of on it.
        planned_visit = call(self.anu, assigned_at=at(d4 - 10 * DAY, 9))
        visit(planned_visit, self.anu, d4)
        checked_in = call(self.anu, assigned_at=at(d4 - 10 * DAY, 9))
        visit(checked_in, self.anu, d4 - DAY, checked_in_at=at(d3, 10))
        checked_out = call(self.anu, assigned_at=at(d4 - 10 * DAY, 9))
        visit(checked_out, self.anu, d4 - DAY, checked_out_at=at(d2, 16))
        cancelled_visit = call(self.anu, assigned_at=at(d4 - 10 * DAY, 9), status="cancelled")
        visit(cancelled_visit, self.anu, d1)  # not listed
        # Another engineer's day is theirs alone.
        call(self.bala, in_current_plan=True, plan_date=d1)
        call(self.bala, assigned_at=at(d1, 9), completed_at=at(d1, 15), status="completed")

        counts = self.assert_matches_the_timeline([self.anu, self.bala])
        self.assertEqual(counts[(self.anu.id, today)]["cases"], 2)
        self.assertEqual(counts[(self.anu.id, d2)]["cases"], 2)
        self.assertEqual(counts[(self.bala.id, d1)], {"cases": 2, "closed": 1, "checkins": 0})

    def test_closed_means_checked_out_that_day(self):
        d1, d2 = self.today - DAY, self.today - 2 * DAY
        # The call's own Check out on the day.
        call(self.anu, assigned_at=at(d1, 9), completed_at=at(d1, 17), status="completed")
        # The day's visit row checked out on it.
        by_visit = call(self.anu, assigned_at=at(d2, 9))
        visit(by_visit, self.anu, d1, checked_in_at=at(d1, 10), checked_out_at=at(d1, 11))
        # A visit row with no Check out of its own: the call's answers.
        falls_back = call(self.anu, assigned_at=at(d2, 9), completed_at=at(d1, 14), status="completed")
        visit(falls_back, self.anu, d1, checked_in_at=at(d1, 13))
        # Closed on an earlier day and still on this day's plan: "Closed earlier", not closed.
        call(self.anu, in_current_plan=True, plan_date=d1, completed_at=at(d2, 15), status="completed")
        # Checked in, not out.
        open_call = call(self.anu, assigned_at=at(d2, 9))
        visit(open_call, self.anu, d1, checked_in_at=at(d1, 15))

        counts = self.assert_matches_the_timeline([self.anu])
        self.assertEqual(counts[(self.anu.id, d1)], {"cases": 5, "closed": 3, "checkins": 3})

    def test_checkins_are_the_boards_check_ins(self):
        d1, d2 = self.today - DAY, self.today - 2 * DAY
        # A call with no visit row answers from its own Check in column.
        call(self.anu, reached_at=at(d1, 10))
        # Two trips to one call checked into on the same day: two check-ins.
        twice = call(self.anu, assigned_at=at(d2, 9))
        visit(twice, self.anu, d1, checked_in_at=at(d1, 11), checked_out_at=at(d1, 12))
        visit(twice, self.anu, d2, checked_in_at=at(d1, 14))
        # A visit row punched that day hides the call's own column for it.
        both = call(self.anu, reached_at=at(d1, 16))
        visit(both, self.anu, d1, checked_in_at=at(d1, 16))
        # Checked out that day, into it the day before: no check-in that day.
        out_only = call(self.anu, assigned_at=at(d2, 9))
        visit(out_only, self.anu, d2, checked_in_at=at(d2, 10), checked_out_at=at(d1, 9))

        counts = self.assert_matches_the_timeline([self.anu])
        self.assertEqual((counts[(self.anu.id, d1)]["checkins"], counts[(self.anu.id, d2)]["checkins"]), (4, 1))

    def test_the_days_visit_is_the_first_by_plan_day_as_the_timeline_takes_it(self):
        d1, d2 = self.today - DAY, self.today - 2 * DAY
        # Two rows match d1: yesterday's plan checked out on d1, and d1's own,
        # open. The timeline reads the first by plan day.
        twice = call(self.anu, assigned_at=at(d2, 9))
        visit(twice, self.anu, d2, checked_out_at=at(d1, 9))
        visit(twice, self.anu, d1)
        # The other way round: d1's own row checked out on d1, then a later one.
        later = call(self.anu, assigned_at=at(d2, 9))
        visit(later, self.anu, d1, checked_out_at=at(d1, 12))
        visit(later, self.anu, d1 + DAY, checked_in_at=at(d1, 18))

        self.assert_matches_the_timeline([self.anu])

    def test_a_call_handed_on_counts_for_whoever_holds_it_now_as_on_the_board(self):
        d1 = self.today - DAY
        handed = call(self.anu, in_current_plan=True, plan_date=d1, reached_at=at(d1, 11))
        Case.objects.filter(pk=handed.pk).update(assigned_to=self.bala)
        counts = self.assert_matches_the_timeline([self.anu, self.bala])
        self.assertEqual((counts[(self.anu.id, d1)]["cases"], counts[(self.bala.id, d1)]["cases"]), (0, 1))

    # the endpoint

    def post(self, **changes):
        body = {"engineers": [self.anu.id, self.bala.id], "from": self.days[0].isoformat(), "to": self.today.isoformat(), **changes}
        return self.client.post("/api/tracking/cases_by_day/", body, format="json")

    def test_the_endpoint_answers_every_engineer_and_day_with_zeros(self):
        call(self.anu, assigned_at=at(self.today - DAY, 9), completed_at=at(self.today - DAY, 16), status="completed")
        answer = self.post().json()
        self.assertEqual(answer["configured"], True)
        self.assertEqual(answer["days"], [day.isoformat() for day in self.days])
        self.assertEqual([row["engineer_id"] for row in answer["engineers"]], [self.anu.id, self.bala.id])
        anu = answer["engineers"][0]["days"]
        self.assertEqual(anu[(self.today - DAY).isoformat()], {"cases": 1, "closed": 1, "checkins": 0})
        self.assertEqual(anu[self.days[0].isoformat()], {"cases": 0, "closed": 0, "checkins": 0})
        self.assertEqual(set(answer["engineers"][1]["days"]), {day.isoformat() for day in self.days})

    def test_days_after_today_are_not_asked_about(self):
        answer = self.post(**{"from": self.today.isoformat(), "to": (self.today + 3 * DAY).isoformat()}).json()
        self.assertEqual(answer["days"], [self.today.isoformat()])

    def test_only_staff_and_only_their_branches(self):
        self.client.force_authenticate(User.objects.create_user(username="an-engineer", password="x", role="employee"))
        self.assertEqual(self.post().status_code, 403)
        self.client.force_authenticate(
            User.objects.create_user(username="hr-hosur", password="x", role="hr", allowed_sections={"attendance": ["Hosur"]})
        )
        self.assertEqual([row["engineer_id"] for row in self.post().json()["engineers"]], [self.anu.id])

    def test_a_bad_question_is_400_as_on_route_km(self):
        for name, change in (
            ("engineers not a list", {"engineers": str(self.anu.id)}),
            ("a bool id", {"engineers": [True]}),
            ("301 ids", {"engineers": list(range(1, 302))}),
            ("an impossible date", {"from": "2026-13-01"}),
            ("from after to", {"from": self.today.isoformat(), "to": (self.today - DAY).isoformat()}),
            ("63 days", {"from": (self.today - 62 * DAY).isoformat()}),
        ):
            self.assertEqual(self.post(**change).status_code, 400, name)
