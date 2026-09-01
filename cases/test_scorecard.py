"""Carrying OpenCall's Assigned / Attended / Closed onto the engineer's phone.

The engineer's screen used to derive its own three numbers from the cases in
the list — To do / On site / Done. Honest about this app, useless as a
scorecard: the office measures an engineer on Assigned / Attended / Closed, and
those are decided by one function in OpenCall over the whole day's plan, not by
whether a punch button has been pressed. Two systems counting the same three
words differently is worse than one of them not showing them.

So the numbers are pushed across the bridge that already pushes the cases.
These tests hold the two things that make that safe: an engineer only ever sees
their own row, and a row that has stopped being updated shows nothing rather
than yesterday's figures wearing today's date.
"""
import datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from authentication.models import User
from cases.models import EngineerScorecard
from employees.models import Employee


class ScorecardTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="sync-bot", password="x", role="superadmin", is_superuser=True
        )

        self.eng_user = User.objects.create_user(
            username="ravi", password="x", role="employee"
        )
        self.engineer = Employee.objects.create(
            user=self.eng_user,
            employee_name="Ravi Kumar",
            email="ravi@example.com",
            phone="+91 98400 55221",
            role="Field Engineer",
            department="Service",
            branch="Chennai",
            salary=Decimal("20000"),
        )

        self.other_user = User.objects.create_user(
            username="mani", password="x", role="employee"
        )
        self.other = Employee.objects.create(
            user=self.other_user,
            employee_name="Mani S",
            email="mani@example.com",
            role="Field Engineer",
            department="Service",
            branch="Chennai",
            salary=Decimal("20000"),
        )

        self.today = timezone.localdate()

    def _push(self, rows, as_of=None, daily=7, monthly=175):
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
        return response

    # ------------------------------------------------------------- ingest

    def test_a_push_lands_on_the_right_engineer(self):
        response = self._push([
            {"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5,
             "closed": 3, "month_closed": 48},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["saved"], 1)

        card = EngineerScorecard.objects.get(engineer=self.engineer)
        self.assertEqual((card.assigned, card.attended, card.closed), (6, 5, 3))
        self.assertEqual(card.month_closed, 48)
        self.assertEqual(card.daily_target, 7)
        self.assertEqual(card.monthly_target, 175)
        self.assertEqual(card.as_of, self.today)

    def test_pushing_twice_replaces_rather_than_piles_up(self):
        """One row per engineer. An append-only table is what rotted the sync."""
        self._push([{"engineer_email": "ravi@example.com", "assigned": 6,
                     "attended": 5, "closed": 3, "month_closed": 48}])
        self._push([{"engineer_email": "ravi@example.com", "assigned": 6,
                     "attended": 6, "closed": 4, "month_closed": 49}])

        self.assertEqual(EngineerScorecard.objects.count(), 1)
        card = EngineerScorecard.objects.get(engineer=self.engineer)
        self.assertEqual((card.attended, card.closed, card.month_closed), (6, 4, 49))

    def test_an_unmatched_engineer_is_reported_not_fatal(self):
        """One unrecognised name must not cost everyone else their numbers."""
        response = self._push([
            {"engineer_name": "Nobody At All", "assigned": 2, "attended": 1, "closed": 0},
            {"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5, "closed": 3},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["saved"], 1)
        self.assertEqual(len(response.data["skipped"]), 1)
        self.assertEqual(response.data["skipped"][0]["engineer_name"], "Nobody At All")
        self.assertTrue(EngineerScorecard.objects.filter(engineer=self.engineer).exists())

    def test_phone_matches_across_formatting(self):
        response = self._push([
            {"engineer_name": "spelt differently", "engineer_phone": "9840055221",
             "assigned": 4, "attended": 2, "closed": 1},
        ])
        self.assertEqual(response.data["saved"], 1, response.data)
        self.assertTrue(EngineerScorecard.objects.filter(engineer=self.engineer).exists())

    def test_an_engineer_off_todays_plan_is_zeroed_not_left_stale(self):
        """Yesterday's 6/5/3 must not sit on the phone labelled today."""
        self._push(
            [{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5,
              "closed": 3, "month_closed": 48}],
            as_of=self.today - datetime.timedelta(days=1),
        )
        # Today's push has a different engineer on the plan.
        response = self._push([
            {"engineer_email": "mani@example.com", "assigned": 2, "attended": 2, "closed": 2},
        ])
        self.assertEqual(response.data["saved"], 1)
        self.assertEqual(response.data["zeroed"], 1)

        card = EngineerScorecard.objects.get(engineer=self.engineer)
        self.assertEqual((card.assigned, card.attended, card.closed), (0, 0, 0))
        self.assertEqual(card.as_of, self.today)

    def test_a_partial_push_cannot_blank_a_colleague_pushed_moments_ago(self):
        """Zeroing only ever touches rows stamped with an OLDER day."""
        self._push([{"engineer_email": "ravi@example.com", "assigned": 6,
                     "attended": 5, "closed": 3}])
        response = self._push([{"engineer_email": "mani@example.com", "assigned": 2,
                                "attended": 2, "closed": 2}])
        self.assertEqual(response.data["zeroed"], 0)
        self.assertEqual(EngineerScorecard.objects.get(engineer=self.engineer).assigned, 6)

    def test_only_staff_may_push(self):
        self.client.force_authenticate(self.eng_user)
        response = self.client.post("/api/cases/scorecards/", {"rows": []}, format="json")
        self.assertEqual(response.status_code, 403)

    def test_junk_counts_become_zero_rather_than_a_500(self):
        response = self._push([
            {"engineer_email": "ravi@example.com", "assigned": "six",
             "attended": None, "closed": -4},
        ])
        self.assertEqual(response.status_code, 200, response.data)
        card = EngineerScorecard.objects.get(engineer=self.engineer)
        self.assertEqual((card.assigned, card.attended, card.closed), (0, 0, 0))

    # --------------------------------------------------------------- read

    def test_an_engineer_reads_their_own_numbers(self):
        self._push([
            {"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5,
             "closed": 3, "month_closed": 48},
            {"engineer_email": "mani@example.com", "assigned": 9, "attended": 9,
             "closed": 9, "month_closed": 99},
        ])

        self.client.force_authenticate(self.eng_user)
        response = self.client.get("/api/cases/my_scorecard/")

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data["assigned"], 6)
        self.assertEqual(response.data["attended"], 5)
        self.assertEqual(response.data["closed"], 3)
        self.assertEqual(response.data["month_closed"], 48)
        self.assertEqual(response.data["daily_target"], 7)
        self.assertEqual(response.data["monthly_target"], 175)
        self.assertFalse(response.data["stale"])

    def test_an_engineer_cannot_read_anybody_elses(self):
        """There is no id to pass — the endpoint only ever answers about you."""
        self._push([{"engineer_email": "mani@example.com", "assigned": 9,
                     "attended": 9, "closed": 9}])

        self.client.force_authenticate(self.eng_user)
        response = self.client.get("/api/cases/my_scorecard/")
        self.assertEqual(response.data["assigned"], 0)
        self.assertIsNone(response.data["as_of"])

    def test_yesterdays_card_reads_as_stale_and_shows_no_figures(self):
        self._push(
            [{"engineer_email": "ravi@example.com", "assigned": 6, "attended": 5,
              "closed": 3, "month_closed": 48}],
            as_of=self.today - datetime.timedelta(days=1),
        )

        self.client.force_authenticate(self.eng_user)
        response = self.client.get("/api/cases/my_scorecard/")

        self.assertTrue(response.data["stale"])
        self.assertEqual(response.data["assigned"], 0)
        self.assertEqual(response.data["closed"], 0)
        self.assertEqual(response.data["month_closed"], 0)

    def test_no_card_at_all_is_answered_calmly(self):
        self.client.force_authenticate(self.eng_user)
        response = self.client.get("/api/cases/my_scorecard/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data["assigned"], 0)
        self.assertFalse(response.data["stale"])

    def test_reading_needs_a_login(self):
        response = self.client.get("/api/cases/my_scorecard/")
        self.assertIn(response.status_code, (401, 403))
