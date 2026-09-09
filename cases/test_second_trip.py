"""A call sent again the next day is a second trip, not the first one over.

The office dispatches the same ticket again -- the part did not arrive, the
customer was out, the job needs a second visit. The engineer's phone shows the
call, and there is NO Check In button on it: the case still says `completed`
from yesterday, and the sync deliberately leaves an engineer's own status
alone, so the card offers nothing to press. The engineer is standing at the
customer with no way to record that they are there.

What has to be true:

  * the second day offers a Check In, and it works;
  * the same sync repeating during ONE day never re-opens a call the engineer
    has already finished (it runs every couple of minutes);
  * and yesterday keeps its own check in and check out after today's punches,
    because a Case has room for exactly one pair of times and the day board
    reads them.
"""
import datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from authentication.models import User
from employees.models import Employee

from .models import Case


def _at(day, hour, minute=0):
    """An aware datetime at a wall-clock time on `day`, in the local zone.

    Built from the local date on purpose: every reader of these stamps groups
    them with timezone.localtime(...).date(), and a UTC-built time drifts onto
    the wrong day for half of every night in IST.
    """
    return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))


class SecondTripTests(APITestCase):
    def setUp(self):
        self.bot = User.objects.create_user(
            username="opencall-bot-trip", password="x", role="admin", is_staff=True
        )
        self.engineer_user = User.objects.create_user(
            username="trip-engineer", password="x", role="employee"
        )
        self.engineer = Employee.objects.create(
            user=self.engineer_user,
            employee_name="Praveen S",
            email="praveen.trip@example.com",
            role="Service engineer",
            department="Service",
            branch="Chennai",
            salary=Decimal("20000"),
        )
        self.office = User.objects.create_user(
            username="office-trip", password="x", role="superadmin", is_superuser=True
        )

        self.bot_client = APIClient()
        self.bot_client.force_authenticate(self.bot)
        self.engineer_client = APIClient()
        self.engineer_client.force_authenticate(self.engineer_user)
        self.office_client = APIClient()
        self.office_client.force_authenticate(self.office)

        self.today = timezone.localdate()
        self.yesterday = self.today - datetime.timedelta(days=1)

    # ------------------------------------------------------------------ helpers

    def _sync(self, plan_date, ref="WO-2201", status="assigned"):
        body = {
            "plan_date": plan_date.isoformat(),
            "cases": [
                {
                    "external_ref": ref,
                    "title": f"Service call ({ref})",
                    "customer_name": "Renderways",
                    "engineer_name": "Praveen S",
                    "status": status,
                }
            ],
        }
        response = self.bot_client.post("/api/cases/bulk_dispatch/", body, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        return Case.objects.get(external_ref=ref)

    def _engineer_card(self, ref="WO-2201"):
        """The case as the engineer's own screen receives it."""
        response = self.engineer_client.get("/api/cases/")
        self.assertEqual(response.status_code, 200, response.data)
        rows = response.data["results"] if isinstance(response.data, dict) and "results" in response.data else response.data
        return next((row for row in rows if row.get("external_ref") == ref), None)

    def _punch(self, case, which, **body):
        return self.engineer_client.post(f"/api/cases/{case.id}/{which}/", body, format="json")

    def _yesterdays_trip(self, ref="WO-2201"):
        """Dispatched yesterday, checked in and out yesterday, and finished."""
        case = self._sync(self.yesterday, ref=ref)
        case.status = "completed"
        case.reached_at = _at(self.yesterday, 10, 20)
        case.completed_at = _at(self.yesterday, 12, 5)
        case.punch_in_lat, case.punch_in_lon = 13.0827, 80.2707
        case.punch_out_lat, case.punch_out_lon = 13.0830, 80.2710
        case.save()
        # The day's own row, the way both the punch endpoints and the migration
        # that backfilled the trips already made write it.
        visit = case.visits.get(plan_date=self.yesterday)
        visit.checked_in_at = case.reached_at
        visit.checked_out_at = case.completed_at
        visit.punch_in_lat, visit.punch_in_lon = case.punch_in_lat, case.punch_in_lon
        visit.punch_out_lat, visit.punch_out_lon = case.punch_out_lat, case.punch_out_lon
        visit.save()
        return case

    # ------------------------------------------------------- the reported bug

    def test_the_second_day_offers_a_check_in(self):
        case = self._yesterdays_trip()

        # The office sends the same ticket out again today.
        case = self._sync(self.today)

        card = self._engineer_card()
        self.assertIsNotNone(card, "the call has to be on today's list at all")
        self.assertNotEqual(
            case.status,
            "completed",
            "a call sent out again is not a finished call -- with no status to "
            "punch from, the card shows the engineer no button",
        )
        response = self._punch(case, "punch_in", latitude=13.0827, longitude=80.2707, accuracy=9)
        self.assertEqual(
            response.status_code,
            200,
            f"the engineer is at the customer and cannot say so: {response.data}",
        )

    def test_the_second_day_can_be_checked_out_of_too(self):
        case = self._yesterdays_trip()
        case = self._sync(self.today)
        self._punch(case, "punch_in", latitude=13.0827, longitude=80.2707)
        response = self._punch(case, "punch_out", latitude=13.0827, longitude=80.2707)
        self.assertEqual(response.status_code, 200, response.data)
        case.refresh_from_db()
        self.assertEqual(case.status, "completed")

    def test_the_card_shows_todays_trip_not_yesterdays(self):
        """What the buttons are decided from.

        The card hides Check In once there is a check-in time on the case, so
        yesterday's time would leave today's trip showing Check Out before the
        engineer had arrived anywhere.
        """
        self._yesterdays_trip()
        self._sync(self.today)

        card = self._engineer_card()
        self.assertIsNone(
            card.get("visit_checked_in_at"),
            "today's trip has not been checked into yet",
        )
        self.assertIsNone(card.get("visit_checked_out_at"))

    # ------------------------------------------- and what must NOT change

    def test_the_same_day_sync_never_reopens_a_finished_call(self):
        """It runs every couple of minutes.

        If a repeat within the day reset the status, a call the engineer
        finished at 11am would be back on their list as unstarted at 11:02.
        """
        case = self._sync(self.today)
        self._punch(case, "punch_in", latitude=13.0827, longitude=80.2707)
        self._punch(case, "punch_out", latitude=13.0827, longitude=80.2707)

        case = self._sync(self.today)
        self.assertEqual(case.status, "completed")

        card = self._engineer_card()
        self.assertIsNotNone(card["visit_checked_out_at"], "today's trip is finished")

    def test_an_upstream_completed_ticket_stays_completed(self):
        """OpenCall saying the call is closed still wins."""
        self._yesterdays_trip()
        case = self._sync(self.today, status="completed")
        self.assertEqual(case.status, "completed")

    def test_yesterday_keeps_its_own_check_in_and_check_out(self):
        """The day board reads the punch times, and there is one pair per case.

        Once the second trip can be punched, today's times overwrite
        yesterday's -- so yesterday's board would lose the two entries it has
        been showing since it happened.
        """
        case = self._yesterdays_trip()
        case = self._sync(self.today)
        self._punch(case, "punch_in", latitude=13.0827, longitude=80.2707)
        self._punch(case, "punch_out", latitude=13.0830, longitude=80.2710)

        def board(day):
            response = self.office_client.get(
                "/api/tracking/day/", {"engineer": self.engineer.id, "date": day.isoformat()}
            )
            self.assertEqual(response.status_code, 200, response.data)
            return [
                (event["type"], event.get("case_ref"))
                for event in response.data.get("events", [])
            ]

        yesterday_events = board(self.yesterday)
        self.assertIn(("reached", "WO-2201"), yesterday_events, yesterday_events)
        self.assertIn(("completed", "WO-2201"), yesterday_events, yesterday_events)

        today_events = board(self.today)
        self.assertIn(("reached", "WO-2201"), today_events, today_events)
        self.assertIn(("completed", "WO-2201"), today_events, today_events)
