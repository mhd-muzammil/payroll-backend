"""The day timeline has to account for every case on the engineer's list.

The office reads this against the engineer's own Cases screen: five cards there
and one entry here is the screen contradicting itself. The five come from the
day's PLAN, which is what both that list and OpenCall's Assigned column are
built from; the timeline was built from assigned_at, stamped once on first
dispatch, so a call pushed yesterday and worked today produced nothing at all.

Run against the old view and test_every_case_on_the_days_list_appears fails
with 1 entry instead of 4.
"""
import datetime

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from cases.models import Case, DutySession
from employees.models import Employee


class DayTimelineTests(TestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="office", password="x", is_staff=True, is_superuser=True
        )
        self.engineer = Employee.objects.create(
            employee_name="Prashanth K", role="Service engineer", department="Service",
            branch="Salem", salary=30000, email="prashanth.k@test.local",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)
        self.today = timezone.localdate()
        self.yesterday = self.today - datetime.timedelta(days=1)

    def _case(self, number, ref, **kwargs):
        return Case.objects.create(
            case_number=number,
            external_ref=ref,
            customer_name="A customer",
            title=f"Service call ({ref})",
            assigned_to=self.engineer,
            plan_date=kwargs.pop("plan_date", self.today),
            **kwargs,
        )

    def _events(self, date=None):
        response = self.client.get(
            "/api/tracking/day/",
            {"engineer": self.engineer.id, "date": (date or self.today).isoformat()},
        )
        self.assertEqual(response.status_code, 200, response.content[:300])
        return response.json()["events"]

    def test_every_case_on_the_days_list_appears(self):
        at = lambda h, m: timezone.make_aware(
            datetime.datetime.combine(self.today, datetime.time(h, m))
        )
        yesterday_evening = timezone.make_aware(
            datetime.datetime.combine(self.yesterday, datetime.time(18, 10))
        )

        # Given today.
        self._case("OC-003326", "WO-035714663", assigned_at=at(11, 2))
        # Given yesterday, worked today -- the case that used to vanish.
        self._case("OC-002962", "WO-035595447", assigned_at=yesterday_evening,
                   reached_at=at(11, 55), completed_at=at(13, 19))
        # Given yesterday, still on today's list, not touched yet.
        self._case("OC-003277", "WO-035694713", assigned_at=yesterday_evening)
        # Created by hand in Payroll: no plan owns it, and the engineer's list
        # shows it, so the timeline must too.
        self._case("OC-000163", "WO-035168198", assigned_at=yesterday_evening, plan_date=None)

        events = self._events()
        listed = {e["case_number"] for e in events if e.get("case_number")}
        self.assertEqual(
            listed,
            {"OC-003326", "OC-002962", "OC-003277", "OC-000163"},
            f"every case on the day's list must appear: {events}",
        )

    def test_the_wo_number_rides_along(self):
        at = timezone.make_aware(datetime.datetime.combine(self.today, datetime.time(11, 2)))
        self._case("OC-003326", "WO-035714663", assigned_at=at)

        events = self._events()
        assigned = [e for e in events if e["type"] == "assigned"]
        self.assertEqual(len(assigned), 1, events)
        self.assertEqual(assigned[0]["case_number"], "OC-003326")
        self.assertEqual(assigned[0]["case_ref"], "WO-035714663")

    def test_a_carried_over_case_opens_the_day_and_says_when_it_came(self):
        given = timezone.make_aware(
            datetime.datetime.combine(self.yesterday, datetime.time(18, 10))
        )
        self._case("OC-002962", "WO-035595447", assigned_at=given)
        DutySession.objects.create(engineer=self.engineer)

        events = self._events()
        # Not every event carries a case -- "Started duty" does not.
        carried = [e for e in events if e.get("case_number") == "OC-002962"]
        self.assertEqual(len(carried), 1, events)
        self.assertEqual(carried[0]["type"], "carried")
        self.assertTrue(
            carried[0]["label"].startswith("On the list from"),
            f"the label must say it came earlier: {carried[0]['label']}",
        )
        self.assertIn(f"{given:%b}", carried[0]["label"])
        # It opens the day: before the duty that started this morning.
        self.assertEqual(events[0].get("case_number"), "OC-002962")

    def test_a_case_given_today_is_not_also_listed_as_carried_over(self):
        at = timezone.make_aware(datetime.datetime.combine(self.today, datetime.time(9, 30)))
        self._case("OC-003326", "WO-035714663", assigned_at=at)

        events = self._events()
        entries = [e for e in events if e.get("case_number") == "OC-003326"]
        self.assertEqual(len(entries), 1, entries)
        self.assertEqual(entries[0]["label"], "Case assigned")

    def test_the_cases_real_status_rides_along(self):
        """A call closed on an earlier day is still closed today.

        Four of this engineer's five cases were completed before today, so this
        day records no event for them at all. Reading their state from the day
        alone would say "not started" beside an app that says DONE.
        """
        given = timezone.make_aware(
            datetime.datetime.combine(self.yesterday, datetime.time(18, 10))
        )
        done = self._case("OC-000163", "WO-035168198", assigned_at=given, status="completed")
        open_one = self._case("OC-003277", "WO-035694713", assigned_at=given, status="assigned")

        by_case = {e["case_number"]: e for e in self._events() if e.get("case_number")}
        self.assertEqual(by_case[done.case_number]["case_status"], "completed")
        self.assertEqual(by_case[open_one.case_number]["case_status"], "assigned")

    def test_a_cancelled_case_stays_off_the_list(self):
        self._case(
            "OC-009999", "WO-099999999", status="cancelled",
            assigned_at=timezone.make_aware(
                datetime.datetime.combine(self.yesterday, datetime.time(9, 0))
            ),
        )
        self.assertEqual(self._events(), [])

    def test_a_past_day_is_read_from_that_days_plan_only(self):
        # Today's plan must not leak into yesterday's timeline.
        self._case("OC-003326", "WO-035714663", assigned_at=timezone.make_aware(
            datetime.datetime.combine(self.today, datetime.time(11, 0))
        ))
        self._case("OC-001111", "WO-011111111", plan_date=self.yesterday,
                   assigned_at=timezone.make_aware(
                       datetime.datetime.combine(self.yesterday, datetime.time(10, 0))
                   ))

        listed = {e["case_number"] for e in self._events(self.yesterday) if e.get("case_number")}
        self.assertEqual(listed, {"OC-001111"})


class PastDayWorkloadTests(TestCase):
    """A finished day cannot be asked about the CURRENT plan.

    Productivity said Lingeshwaran M had five cases yesterday and the tracking
    panel listed three; Vijayakumar R, five and one; Praveen, four and two.
    Everybody else matched -- and the three who did not were exactly the three
    who worked nothing that day. plan_date marks the plan the sync LAST pushed
    and the sync renews it, so yesterday lost every call still open today and
    kept only what the four timestamps caught.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            username="office-past", password="x", role="superadmin", is_superuser=True
        )
        self.engineer = Employee.objects.create(
            employee_name="Lingeshwaran M", role="Service engineer", department="Service",
            branch="Kanchipuram", salary=30000, email="lingesh@test.local",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.today = timezone.localdate()
        self.yesterday = self.today - datetime.timedelta(days=1)
        self.tz = timezone.get_current_timezone()

    def _at(self, day, hour=10, minute=0):
        return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)), self.tz)

    def _case(self, number, **kwargs):
        fields = {
            "case_number": number,
            "external_ref": f"WO-{number[-6:]}",
            "customer_name": "Customer",
            "title": f"Service call ({number})",
            "assigned_to": self.engineer,
            "in_current_plan": True,
            # What the sync leaves behind: the plan renewed to TODAY.
            "plan_date": self.today,
            "status": "assigned",
        }
        fields.update(kwargs)
        return Case.objects.create(**fields)

    def _cases_on(self, day):
        response = self.client.get(f"/api/tracking/day/?engineer={self.engineer.id}&date={day}")
        self.assertEqual(response.status_code, 200, response.content)
        return {e["case_number"] for e in response.json()["events"] if e.get("case_number")}

    def test_a_call_closed_before_the_day_is_not_on_it(self):
        self._case(
            "OC-003300",
            status="completed",
            assigned_at=self._at(self.today - datetime.timedelta(days=5)),
            completed_at=self._at(self.today - datetime.timedelta(days=4)),
        )
        self.assertNotIn("OC-003300", self._cases_on(self.yesterday))

    def test_a_call_closed_on_the_day_is_on_it(self):
        self._case(
            "OC-003254",
            status="completed",
            assigned_at=self._at(self.today - datetime.timedelta(days=2)),
            completed_at=self._at(self.yesterday, 15),
        )
        self.assertIn("OC-003254", self._cases_on(self.yesterday))

    def test_a_call_given_after_the_day_is_not_on_it(self):
        self._case("OC-003400", assigned_at=self._at(self.today, 9))
        self.assertNotIn("OC-003400", self._cases_on(self.yesterday))

    def test_a_cancelled_call_stays_off_a_past_day(self):
        self._case(
            "OC-003401",
            status="cancelled",
            assigned_at=self._at(self.today - datetime.timedelta(days=3)),
        )
        self.assertNotIn("OC-003401", self._cases_on(self.yesterday))

    def test_today_still_reads_the_plan(self):
        """Today is unchanged: the plan is the truth and the engineer's own list.

        A call whose plan_date is yesterday's has left today's plan and must not
        appear on today, however open it is.
        """
        self._case("OC-003500", assigned_at=self._at(self.today, 8))
        self._case(
            "OC-003501",
            assigned_at=self._at(self.today - datetime.timedelta(days=4)),
            plan_date=self.yesterday,
            in_current_plan=False,
        )
        on_today = self._cases_on(self.today)
        self.assertIn("OC-003500", on_today)
        self.assertNotIn("OC-003501", on_today)

    def test_a_carried_call_is_missing_and_that_is_known(self):
        """The limit of what the plan can say, asserted so nobody is surprised.

        A call given three days ago, untouched since and still open, has had its
        plan_date renewed to today by the sync. Yesterday cannot see it -- the
        renewal overwrote the only record that it was ever on yesterday's list.
        Recording (case, plan_date) append-only is what fixes this; until then
        the screen under-reports rather than inventing, which is the cheaper of
        the two mistakes.
        """
        self._case("OC-003381", assigned_at=self._at(self.today - datetime.timedelta(days=3)))
        self.assertNotIn("OC-003381", self._cases_on(self.yesterday))
