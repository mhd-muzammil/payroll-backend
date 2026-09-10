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
from decimal import Decimal
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from cases.models import Case, DutySession, LocationPing, PlaceName
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


class DarkStretchTests(TestCase):
    """Where the day went dark, and which kind of dark it was.

    The office could see short kilometres and not why. Two different facts:
    location switched off (the stretch is genuinely unmeasured, and counts as
    zero km) versus no network (the kilometres are honest, only the live board
    was empty). Both were in the database and shown nowhere.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            username="office-dark", password="x", role="superadmin", is_superuser=True
        )
        self.engineer = Employee.objects.create(
            employee_name="Dark Day", role="Service engineer", department="Service",
            branch="Salem", salary=30000, email="dark@test.local",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.tz = timezone.get_current_timezone()
        self.today = timezone.localdate()

    def _at(self, hour, minute=0):
        return timezone.make_aware(
            datetime.datetime.combine(self.today, datetime.time(hour, minute)), self.tz
        )

    def _local(self, iso):
        """The wall-clock time an entry carries, in the timezone people read.

        The payload is UTC, and 14:00 IST serialises as 08:30Z -- comparing the
        string against the local hour is how the first version of these tests
        failed while the code was right.
        """
        from django.utils.dateparse import parse_datetime

        return timezone.localtime(parse_datetime(iso))

    def _ping(self, hour, minute, *, lat=11.0, after_gap=False, received=None):
        ping = LocationPing.objects.create(
            engineer=self.engineer, latitude=lat, longitude=78.0, accuracy=8,
            after_gap=after_gap,
        )
        LocationPing.objects.filter(pk=ping.pk).update(
            timestamp=self._at(hour, minute),
            received_at=received if received is not None else self._at(hour, minute),
        )
        return ping

    def _events(self, kind=None):
        response = self.client.get(
            f"/api/tracking/day/?engineer={self.engineer.id}&date={self.today}"
        )
        self.assertEqual(response.status_code, 200, response.content)
        events = response.json()["events"]
        return [e for e in events if kind is None or e["type"] == kind]

    def test_location_switched_off_is_named_with_how_long(self):
        self._ping(9, 0, lat=11.00)
        self._ping(9, 30, lat=11.05)
        # Off at 09:30, back at 14:00 somewhere else entirely.
        self._ping(14, 0, lat=11.60, after_gap=True)

        dark = self._events("location_off")
        self.assertEqual(len(dark), 1, dark)
        self.assertEqual(dark[0]["label"], "Location off · 4h 30m")
        self.assertEqual(dark[0]["minutes"], 270)

    def test_the_moment_location_came_back_is_on_the_rail_too(self):
        """A hole that opens has to be shown closing.

        Without this the rail says "Location off 4h 30m" at 09:30 and nothing
        else, so whether the phone returned at two o'clock or stayed dark until
        the logout has to be worked out by eye -- and that difference is
        whether anybody needs chasing.
        """
        self._ping(9, 0, lat=11.00)
        self._ping(9, 30, lat=11.05)
        self._ping(14, 0, lat=11.60, after_gap=True)

        back = self._events("location_back")
        self.assertEqual(len(back), 1, back)
        self.assertEqual(back[0]["label"], "Location back on")
        # At the moment it started reporting again, not where it stopped.
        came_back = self._local(back[0]["at"])
        self.assertEqual((came_back.hour, came_back.minute), (14, 0), back[0]["at"])
        self.assertEqual(back[0]["latitude"], 11.60)

    def test_the_two_ends_are_one_stretch_read_in_order(self):
        self._ping(9, 0)
        self._ping(9, 30)
        self._ping(14, 0, after_gap=True)

        pair = [e for e in self._events() if e["type"] in ("location_off", "location_back")]
        self.assertEqual([e["type"] for e in pair], ["location_off", "location_back"], pair)
        self.assertLess(pair[0]["at"], pair[1]["at"])
        # Both carry the length of the same stretch, so either entry answers
        # "how long" without the reader holding two times in their head.
        self.assertEqual(pair[0]["minutes"], pair[1]["minutes"])

    def test_jitter_produces_neither_end(self):
        """The five-minute floor holds for the far end as well as the near one."""
        self._ping(9, 0)
        self._ping(9, 2, after_gap=True)
        self.assertEqual(self._events("location_back"), [])

    def test_the_network_coming_back_is_its_own_entry(self):
        held_from = self._at(11, 11)
        delivered = self._at(11, 30)
        first = self._ping(11, 11, received=delivered)
        self._ping(11, 20, received=delivered)
        self.assertIsNotNone(first)

        back = self._events("network_back")
        self.assertEqual(len(back), 1, back)
        self.assertEqual(back[0]["label"], "Network back")
        # Stamped when the held fixes actually arrived.
        arrived = self._local(back[0]["at"])
        self.assertEqual((arrived.hour, arrived.minute), (11, 30), back[0]["at"])
        self.assertGreater(arrived, held_from)

    def test_the_network_coming_back_is_placed_by_the_nearest_fix(self):
        """Where the phone was when the delivery went through.

        Deliberately not the place the last held fix was TAKEN -- the engineer
        has moved since. The day view's nearest-fix pass answers it, and this
        pins that the entry is placed rather than left blank when there is a
        fix close enough in time.
        """
        delivered = self._at(11, 30)
        self._ping(11, 11, lat=11.10, received=delivered)
        self._ping(11, 31, lat=11.40)

        back = self._events("network_back")[0]
        self.assertEqual(back["latitude"], 11.40, back)

    def test_a_silence_the_app_never_admitted_to_is_still_shown(self):
        """An app that is killed flags nothing.

        "Location off" needs the phone to say tracking stopped. Swipe the app
        away and there is no error to catch -- the fixes simply stop -- so one
        engineer's day carried a hundred and thirteen minutes of nothing and
        the board said not a word about it.
        """
        self._ping(9, 0, lat=11.00)
        # Quiet for two hours, and a long way off when it comes back.
        self._ping(11, 0, lat=11.30)

        dark = self._events("untracked")
        self.assertEqual(len(dark), 1, dark)
        self.assertEqual(dark[0]["label"], "Not tracked · 2h")
        self.assertEqual(dark[0]["minutes"], 120)
        # How far they had got by the time it came back: the size of what
        # nothing was watching.
        self.assertGreater(dark[0]["moved_km"], 30)

    def test_standing_at_a_customer_is_not_called_a_hole(self):
        """THE one that would make this unusable.

        A parked phone reports nothing either -- it only speaks every ten
        metres -- so an engineer at a customer for two hours produces exactly
        the same silence. That is a stop, the board already draws it as
        Waiting, and calling it "not tracked" would put a red line against
        every visit anybody makes.
        """
        self._ping(9, 0, lat=11.00)
        self._ping(11, 0, lat=11.00)  # same place, two hours later
        self.assertEqual(self._events("untracked"), [])

    def test_a_flagged_stretch_is_not_reported_twice(self):
        """Location off says it better; this must not say it again."""
        self._ping(9, 0, lat=11.00)
        self._ping(11, 0, lat=11.30, after_gap=True)
        self.assertEqual(len(self._events("location_off")), 1)
        self.assertEqual(self._events("untracked"), [])

    def test_a_short_quiet_moment_is_not_an_entry(self):
        self._ping(9, 0, lat=11.00)
        self._ping(9, 2, lat=11.30)  # two minutes, under the floor
        self.assertEqual(self._events("untracked"), [])

    def test_a_wild_reading_cannot_invent_a_journey(self):
        """A cold GPS throws one fix a district away.

        Read off the accurate fixes only, so that reading cannot become an
        hour of untracked travel on somebody's record.
        """
        self._ping(9, 0, lat=11.00)
        wild = LocationPing.objects.create(
            engineer=self.engineer, latitude=12.5, longitude=78.0, accuracy=400,
        )
        LocationPing.objects.filter(pk=wild.pk).update(
            timestamp=self._at(10, 0), received_at=self._at(10, 0)
        )
        self._ping(10, 1, lat=11.001)
        self.assertEqual(self._events("untracked"), [])

    def test_a_moment_of_jitter_is_not_an_entry(self):
        """A phone handing the GPS back in two minutes is not a lost stretch."""
        self._ping(9, 0)
        self._ping(9, 2, after_gap=True)
        self.assertEqual(self._events("location_off"), [])

    def test_the_first_fix_of_the_day_is_not_a_gap(self):
        """Coming on duty flags the first fix, and there is nothing before it."""
        self._ping(9, 0, after_gap=True)
        self._ping(9, 30)
        self.assertEqual(self._events("location_off"), [])

    def test_an_outage_is_one_entry_however_many_fixes_it_held(self):
        self._ping(9, 0)
        # Twenty fixes taken through the outage, all delivered at 10:05.
        for i in range(20):
            self._ping(9, 20 + i, received=self._at(10, 5))
        self._ping(10, 10)

        offline = self._events("no_network")
        self.assertEqual(len(offline), 1, offline)
        self.assertEqual(offline[0]["label"], "No network · 45 min")

    def test_a_late_fix_and_a_lost_stretch_are_different_entries(self):
        self._ping(9, 0)
        self._ping(9, 10, received=self._at(9, 55))
        self._ping(13, 0, lat=11.4, after_gap=True)

        kinds = {e["type"] for e in self._events()}
        self.assertIn("no_network", kinds)
        self.assertIn("location_off", kinds)

    def test_an_ordinary_day_says_nothing_about_darkness(self):
        for minute in (0, 30):
            self._ping(9, minute)
        self.assertEqual(self._events("location_off"), [])
        self.assertEqual(self._events("no_network"), [])


class TimelineAddressTests(TestCase):
    """Where each entry happened, in words.

    Most are free: a call carries the customer's address already. Only a stop
    away from any customer, and the place the trail went dark, need a
    coordinate resolved -- and Ola is metered, so those are cached and budgeted.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            username="office-addr", password="x", role="superadmin", is_superuser=True
        )
        self.engineer = Employee.objects.create(
            employee_name="Addressed", role="Service engineer", department="Service",
            branch="Hosur", salary=30000, email="addr@test.local",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.today = timezone.localdate()
        self.tz = timezone.get_current_timezone()

    def _at(self, hour, minute=0):
        return timezone.make_aware(
            datetime.datetime.combine(self.today, datetime.time(hour, minute)), self.tz
        )

    def _events(self):
        response = self.client.get(
            f"/api/tracking/day/?engineer={self.engineer.id}&date={self.today}"
        )
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()["events"]

    def test_a_call_says_where_without_asking_anybody(self):
        """The customer's address is on the case, so this costs no lookup."""
        Case.objects.create(
            case_number="OC-004001", external_ref="WO-004001", customer_name="Naveen",
            title="Service call", address="P.B.NO 4, HARITHA Krishnagiri - 635109",
            assigned_to=self.engineer, in_current_plan=True, plan_date=self.today,
            status="completed", assigned_at=self._at(9), reached_at=self._at(10),
            completed_at=self._at(11),
        )
        addressed = [e for e in self._events() if e.get("address")]
        self.assertTrue(addressed, self._events())
        self.assertTrue(
            all(e["address"] == "P.B.NO 4, HARITHA Krishnagiri - 635109" for e in addressed)
        )

    def test_a_cached_place_is_used_and_nothing_is_fetched(self):
        PlaceName.objects.create(
            lat_key=Decimal("11.0000"), lon_key=Decimal("78.0000"),
            address="Kirana Store, Hosur, Tamil Nadu",
        )
        for minute in (0, 30, 45):
            ping = LocationPing.objects.create(
                engineer=self.engineer, latitude=11.0, longitude=78.0, accuracy=8,
            )
            LocationPing.objects.filter(pk=ping.pk).update(timestamp=self._at(9, minute))

        with mock.patch("cases.places._fetch_address") as fetch:
            events = self._events()
        fetch.assert_not_called()
        stops = [e for e in events if e["type"] == "stop"]
        self.assertTrue(stops, events)
        self.assertEqual(stops[0]["address"], "Kirana Store, Hosur, Tamil Nadu")

    def test_an_unknown_place_is_looked_up_once_and_kept(self):
        for minute in (0, 30, 45):
            ping = LocationPing.objects.create(
                engineer=self.engineer, latitude=12.5, longitude=77.5, accuracy=8,
            )
            LocationPing.objects.filter(pk=ping.pk).update(timestamp=self._at(9, minute))

        with mock.patch("cases.places._fetch_address", return_value="Some Road, Hosur") as fetch:
            first = self._events()
            self.assertEqual(fetch.call_count, 1)
            # Asked again, it comes from the row that first call wrote.
            second = self._events()
            self.assertEqual(fetch.call_count, 1, "a cached place must not be fetched twice")

        self.assertEqual(PlaceName.objects.count(), 1)
        for events in (first, second):
            stops = [e for e in events if e["type"] == "stop"]
            self.assertEqual(stops[0]["address"], "Some Road, Hosur")

    def test_a_place_that_cannot_be_resolved_is_simply_absent(self):
        """No address is a missing line, never an error and never an empty one."""
        for minute in (0, 30, 45):
            ping = LocationPing.objects.create(
                engineer=self.engineer, latitude=13.9, longitude=79.9, accuracy=8,
            )
            LocationPing.objects.filter(pk=ping.pk).update(timestamp=self._at(9, minute))

        with mock.patch("cases.places._fetch_address", return_value=None):
            events = self._events()
        stops = [e for e in events if e["type"] == "stop"]
        self.assertTrue(stops, events)
        self.assertNotIn("address", stops[0])
        self.assertEqual(PlaceName.objects.count(), 0)


class EveryEntryHasAPlaceTests(TestCase):
    """Every entry can be opened on the map, because every entry has a place.

    Only Waiting had one: stops carried coordinates and nothing else did. The
    others always had a place, it just was not put on the event -- the punch
    coordinates for the two taps at a customer, and the nearest fix in the
    trail for Login and Logout.
    """

    def setUp(self):
        self.staff = User.objects.create_user(
            username="office-place", password="x", role="superadmin", is_superuser=True
        )
        self.engineer = Employee.objects.create(
            employee_name="Placed", role="Service engineer", department="Service",
            branch="Hosur", salary=30000, email="placed@test.local",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.staff)
        self.tz = timezone.get_current_timezone()
        self.today = timezone.localdate()

    def _at(self, hour, minute=0):
        return timezone.make_aware(
            datetime.datetime.combine(self.today, datetime.time(hour, minute)), self.tz
        )

    def _events(self, kind):
        response = self.client.get(
            f"/api/tracking/day/?engineer={self.engineer.id}&date={self.today}"
        )
        self.assertEqual(response.status_code, 200, response.content)
        return [e for e in response.json()["events"] if e["type"] == kind]

    def test_check_in_opens_where_they_stood(self):
        Case.objects.create(
            case_number="OC-004001", external_ref="WO-1", customer_name="C", title="t",
            assigned_to=self.engineer, in_current_plan=True, plan_date=self.today,
            status="working", assigned_at=self._at(9), reached_at=self._at(10, 41),
            latitude=12.700, longitude=77.700,
            punch_in_lat=12.75767, punch_in_lon=77.81050,
        )
        entry = self._events("reached")[0]
        self.assertAlmostEqual(entry["latitude"], 12.75767, places=5)
        self.assertAlmostEqual(entry["longitude"], 77.81050, places=5)

    def test_a_call_with_no_punch_falls_back_to_the_customer(self):
        Case.objects.create(
            case_number="OC-004002", external_ref="WO-2", customer_name="C", title="t",
            assigned_to=self.engineer, in_current_plan=True, plan_date=self.today,
            status="completed", assigned_at=self._at(9), completed_at=self._at(11, 17),
            latitude=12.700, longitude=77.700,
        )
        entry = self._events("completed")[0]
        self.assertAlmostEqual(entry["latitude"], 12.700, places=3)

    def test_login_takes_the_nearest_fix(self):
        session = DutySession.objects.create(engineer=self.engineer)
        DutySession.objects.filter(pk=session.pk).update(started_at=self._at(9, 2))
        ping = LocationPing.objects.create(
            engineer=self.engineer, latitude=12.9, longitude=77.9, accuracy=8
        )
        LocationPing.objects.filter(pk=ping.pk).update(timestamp=self._at(9, 4))

        entry = self._events("duty_start")[0]
        self.assertAlmostEqual(entry["latitude"], 12.9, places=3)

    def test_a_fix_an_hour_away_is_not_its_place(self):
        """Fifteen minutes of driving is a different town."""
        session = DutySession.objects.create(engineer=self.engineer)
        DutySession.objects.filter(pk=session.pk).update(started_at=self._at(9, 2))
        ping = LocationPing.objects.create(
            engineer=self.engineer, latitude=12.9, longitude=77.9, accuracy=8
        )
        LocationPing.objects.filter(pk=ping.pk).update(timestamp=self._at(11, 30))

        entry = self._events("duty_start")[0]
        self.assertIsNone(entry.get("latitude"))
