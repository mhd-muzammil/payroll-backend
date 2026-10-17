"""The phone's own word about itself, and what the board makes of it.

Three things the board could not tell apart: a phone that is recording and
standing still, a phone whose app stopped recording while its page kept
talking, and a phone with Location switched off. The phone now posts what it
notices (/api/tracking/events/), every post of it -- an empty one is the
heartbeat -- and every new fix is contact; and the board reads "No signal",
"GPS off" and the age of the last fix from that.
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.db import DatabaseError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from cases.models import DutySession, LocationPing, PhoneEvent
from cases.pings import MAX_EVENTS
from employees.models import Employee


def make_engineer(username, name):
    user = User.objects.create_user(username=username, password="x", role="employee")
    engineer = Employee.objects.create(
        user=user,
        employee_name=name,
        email=f"{username}@example.com",
        role="Field Engineer",
        department="Service",
        branch="Chennai",
        salary=Decimal("20000"),
    )
    return user, engineer


class EventsEndpointTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.engineer = make_engineer("evented", "Evented Elango")

    def setUp(self):
        self.phone = APIClient()
        self.phone.force_authenticate(self.user)
        self.phone.credentials(HTTP_X_PAYROLL_CLIENT="app")
        self.now = timezone.now()

    def _post(self, events, client=None):
        return (client or self.phone).post(
            "/api/tracking/events/", {"events": events}, format="json"
        )

    def _event(self, kind="gps_off", minutes_ago=1, key=None, **extra):
        return {
            "kind": kind,
            "at": (self.now - datetime.timedelta(minutes=minutes_ago)).isoformat(),
            "client_key": key or f"{kind}-{minutes_ago}",
            **extra,
        }

    def test_events_are_stored_with_when_we_got_them(self):
        response = self._post(
            [
                self._event("gps_off", 5, detail="switched", latitude=12.75, longitude=77.81),
                self._event("gps_on", 1, detail="switched"),
            ]
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data, {"stored": 2, "duplicates": 0, "rejected": []})

        off = PhoneEvent.objects.get(kind="gps_off")
        self.assertEqual(off.engineer, self.engineer)
        self.assertEqual(off.detail, "switched")
        self.assertAlmostEqual(off.latitude, 12.75)
        self.assertGreaterEqual(off.received_at, self.now)
        self.assertLess(off.at, off.received_at)

    def test_every_kind_the_phone_may_send(self):
        kinds = PhoneEvent.KINDS
        response = self._post([self._event(kind, n + 1) for n, kind in enumerate(kinds)])
        self.assertEqual(response.data["stored"], len(kinds), response.data)
        self.assertEqual(
            set(PhoneEvent.objects.values_list("kind", flat=True)),
            {"gps_off", "gps_on", "net_off", "net_on", "tracker_stopped", "tracker_started", "app_opened"},
        )

    def test_an_event_posted_twice_is_stored_once(self):
        first = self._post([self._event(key="one")])
        again = self._post([self._event(key="one"), self._event("net_off", key="two")])
        self.assertEqual(first.data["stored"], 1)
        self.assertEqual(again.data, {"stored": 1, "duplicates": 1, "rejected": []})
        self.assertEqual(PhoneEvent.objects.count(), 2)

    def test_a_post_that_repeats_itself_is_deduped(self):
        response = self._post([self._event(key="same"), self._event(key="same")])
        self.assertEqual(response.data, {"stored": 1, "duplicates": 1, "rejected": []})

    def test_a_race_between_two_posts_of_the_same_event_is_a_duplicate_not_an_error(self):
        """Both posts pass the check for what we already hold; the second to
        insert loses on the database's constraint, and says so as a
        duplicate rather than failing the phone's whole post."""
        # The other post's row, committed after this one looked and before it
        # inserted: the look is made to miss it.
        PhoneEvent.objects.create(
            engineer=self.engineer, kind="gps_off", at=self.now, received_at=self.now,
            client_key="raced",
        )
        with mock.patch.object(PhoneEvent.objects, "filter", return_value=PhoneEvent.objects.none()):
            response = self._post([self._event(key="raced"), self._event("net_on", key="fine")])

        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data, {"stored": 1, "duplicates": 1, "rejected": []})
        self.assertEqual(
            sorted(PhoneEvent.objects.values_list("client_key", flat=True)), ["fine", "raced"]
        )

    def test_a_bad_event_does_not_lose_the_rest(self):
        response = self._post(
            [
                self._event(key="good"),
                {**self._event(key="odd"), "kind": "phone_exploded"},
                {"kind": "gps_off", "at": self.now.isoformat()},
                self._event(key="ahead", minutes_ago=-10),
                self._event(key="ancient", minutes_ago=60 * 49),
                {**self._event(key="garbled"), "at": "yesterday"},
                "not an event",
            ]
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["stored"], 1)
        reasons = {item["index"]: item["reason"] for item in response.data["rejected"]}
        self.assertEqual(sorted(reasons), [1, 2, 3, 4, 5, 6])
        self.assertIn("kind", reasons[1])
        self.assertIn("client_key", reasons[2])
        # The phone retries what was refused as "in the future": its clock
        # catches up with ours.
        self.assertIn("in the future", reasons[3])
        self.assertIn("older than", reasons[4])

    def test_an_impossible_moment_is_refused_not_a_crash(self):
        """Shaped like a time but not one. A 500 here failed the whole post,
        and the phone would have sent it again with every heartbeat."""
        response = self._post(
            [{**self._event(key="impossible"), "at": "2026-10-17T25:61:00Z"}, self._event(key="good")]
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["stored"], 1)
        self.assertEqual([item["index"] for item in response.data["rejected"]], [0])
        self.assertIn("ISO 8601", response.data["rejected"][0]["reason"])

    def test_an_oversized_post_is_refused_with_the_limit(self):
        response = self._post([self._event(key=f"k{n}") for n in range(MAX_EVENTS + 1)])
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["max_batch"], MAX_EVENTS)
        self.assertFalse(PhoneEvent.objects.exists())

    def test_a_post_that_is_not_a_list_is_refused(self):
        for body in ({"events": "everything"}, {}, {"pings": []}):
            response = self.phone.post("/api/tracking/events/", body, format="json")
            self.assertEqual(response.status_code, 400, body)

    def test_an_empty_list_is_the_heartbeat(self):
        response = self._post([])
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data, {"stored": 0, "duplicates": 0, "rejected": []})

    def test_a_browser_may_not_post_events(self):
        """Two of these change the kilometres, so the same door as a fix."""
        browser = APIClient()
        browser.force_authenticate(self.user)
        response = self._post([self._event()], client=browser)
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.data.get("app_only"))
        self.assertFalse(PhoneEvent.objects.exists())

    def test_a_login_with_no_employee_is_refused(self):
        stranger = User.objects.create_user(username="no-employee", password="x", role="employee")
        client = APIClient()
        client.force_authenticate(stranger)
        client.credentials(HTTP_X_PAYROLL_CLIENT="app")
        self.assertEqual(self._post([self._event()], client=client).status_code, 400)

    def test_events_land_on_the_engineer_who_posted_them(self):
        _, other = make_engineer("not-me", "Not Me")
        self._post([self._event(key="mine")])
        self.assertEqual(PhoneEvent.objects.get().engineer, self.engineer)
        self.assertFalse(PhoneEvent.objects.filter(engineer=other).exists())


class ContactTests(TestCase):
    """Contact is what proves something is RECORDING: a fix we had not got,
    or a post to events/. Not a page reading its duty, not a Login, not a
    re-sent fix -- an old page does all three with its tracker stopped."""

    @classmethod
    def setUpTestData(cls):
        cls.user, cls.engineer = make_engineer("contact", "Contact Kumar")

    def setUp(self):
        self.phone = APIClient()
        self.phone.force_authenticate(self.user)
        self.phone.credentials(HTTP_X_PAYROLL_CLIENT="app")
        self.session = DutySession.objects.create(engineer=self.engineer)

    def _contact(self):
        self.session.refresh_from_db()
        return self.session.last_contact_at

    def _fix(self, key, **extra):
        return self.phone.post(
            "/api/tracking/ping/",
            {"latitude": 13.08, "longitude": 80.27, "accuracy": 8, "client_key": key, **extra},
            format="json",
        )

    def test_a_new_fix_is_contact(self):
        self.assertIsNone(self._contact())
        self.assertEqual(self._fix("k1").status_code, 201)
        self.assertIsNotNone(self._contact())

    def test_a_re_sent_fix_is_not(self):
        self._fix("k1")
        DutySession.objects.filter(pk=self.session.pk).update(last_contact_at=None)
        self.assertEqual(self._fix("k1").status_code, 200)
        self.assertIsNone(self._contact())

    def test_a_batch_with_something_new_is_contact_and_one_of_repeats_is_not(self):
        fix = {
            "latitude": 13.08, "longitude": 80.27,
            "timestamp": (timezone.now() - datetime.timedelta(minutes=5)).isoformat(),
            "client_key": "held-1",
        }
        self.phone.post("/api/tracking/ping/batch/", {"pings": [fix]}, format="json")
        self.assertIsNotNone(self._contact())

        DutySession.objects.filter(pk=self.session.pk).update(last_contact_at=None)
        again = self.phone.post("/api/tracking/ping/batch/", {"pings": [fix]}, format="json")
        self.assertEqual(again.data["duplicates"], 1)
        self.assertIsNone(self._contact())

    def test_reading_the_duty_or_logging_in_is_not(self):
        self.assertEqual(self.phone.get("/api/tracking/duty/").status_code, 200)
        self.assertEqual(self.phone.post("/api/tracking/start_duty/").status_code, 201)
        self.assertIsNone(self._contact())

    def test_the_heartbeat_is_contact(self):
        self.phone.post("/api/tracking/events/", {"events": []}, format="json")
        self.assertIsNotNone(self._contact())

    def test_contact_is_written_at_most_every_half_minute(self):
        now = timezone.now()
        with mock.patch("django.utils.timezone.now", return_value=now):
            self.phone.post("/api/tracking/events/", {"events": []}, format="json")
        with mock.patch("django.utils.timezone.now", return_value=now + datetime.timedelta(seconds=20)):
            self.phone.post("/api/tracking/events/", {"events": []}, format="json")
        self.assertEqual(self._contact(), now)
        later = now + datetime.timedelta(seconds=31)
        with mock.patch("django.utils.timezone.now", return_value=later):
            self.phone.post("/api/tracking/events/", {"events": []}, format="json")
        self.assertEqual(self._contact(), later)

    def test_only_the_open_session_is_touched(self):
        DutySession.objects.filter(pk=self.session.pk).update(ended_at=timezone.now())
        self._fix("after-logout")
        self.assertIsNone(self._contact())

    def test_failing_to_note_contact_never_loses_the_fix(self):
        with mock.patch("cases.views.DutySession.objects.filter", side_effect=DatabaseError("down")):
            with self.assertLogs("cases.views", level="ERROR"):
                response = self._fix("kept")
        self.assertEqual(response.status_code, 201, response.data)
        self.assertTrue(LocationPing.objects.filter(client_key="kept").exists())


class BoardSignalTests(TestCase):
    """What the board shows for an engineer on duty: live, No signal, GPS off."""

    @classmethod
    def setUpTestData(cls):
        cls.office = User.objects.create_user(username="signal-office", password="x", role="admin")
        cls.user, cls.engineer = make_engineer("signal", "Signal Siva")

    def setUp(self):
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        # Noon on a fixed day, read through a clock the endpoints are given,
        # so no row here can fall on the other side of a midnight.
        self.now = timezone.make_aware(datetime.datetime(2026, 10, 16, 12, 0))

    def _ago(self, minutes):
        return self.now - datetime.timedelta(minutes=minutes)

    def _duty(self, minutes_ago, contact_minutes_ago=None):
        return DutySession.objects.create(
            engineer=self.engineer,
            started_at=self._ago(minutes_ago),
            last_contact_at=None if contact_minutes_ago is None else self._ago(contact_minutes_ago),
        )

    def _fix(self, minutes_ago, latitude=13.0, received_minutes_ago=None):
        return LocationPing.objects.create(
            engineer=self.engineer, latitude=latitude, longitude=80.0, accuracy=8,
            timestamp=self._ago(minutes_ago),
            received_at=self._ago(minutes_ago if received_minutes_ago is None else received_minutes_ago),
        )

    def _gps(self, kind, minutes_ago, detail="switched"):
        return PhoneEvent.objects.create(
            engineer=self.engineer, kind=kind, at=self._ago(minutes_ago),
            received_at=self._ago(minutes_ago), client_key=f"{kind}-{minutes_ago}", detail=detail,
        )

    def _rows(self):
        with mock.patch("django.utils.timezone.now", return_value=self.now):
            roster = self.client.get("/api/tracking/roster/").data
            live = self.client.get("/api/tracking/live/").data
        board = next(r for r in roster if r["engineer_id"] == self.engineer.id)
        mine = [r for r in live if r["engineer_id"] == self.engineer.id]
        return board, (mine[0] if mine else None)

    def test_a_parked_phone_with_its_heartbeat_is_live(self):
        """Standing still at a customer: no new fix for half an hour, but the
        phone is heard from every minute. The original complaint."""
        self._duty(minutes_ago=60, contact_minutes_ago=1)
        self._fix(minutes_ago=30)
        for row in self._rows():
            self.assertFalse(row["stale"], row)
            self.assertEqual(row["last_seen_minutes"], 1)
            self.assertEqual(row["last_fix_minutes"], 30)
            self.assertIsNone(row["gps_off_since"])
            self.assertIsNotNone(row["last_contact_at"])

    def test_nothing_heard_for_ten_minutes_is_no_signal(self):
        self._duty(minutes_ago=60, contact_minutes_ago=11)
        self._fix(minutes_ago=30)
        for row in self._rows():
            self.assertTrue(row["stale"], row)
            self.assertEqual(row["last_seen_minutes"], 11)

    def test_a_page_talking_with_no_fix_since_the_login_is_no_signal(self):
        """A tracker that stopped at the first fix: the page keeps posting,
        and nothing is recorded. "No signal · no fix yet", as before."""
        self._duty(minutes_ago=20, contact_minutes_ago=0)
        for row in self._rows():
            self.assertTrue(row["stale"], row)
            self.assertIsNone(row["last_fix_minutes"])
            self.assertIsNone(row["latitude"])

    def test_a_fresh_login_with_no_fix_yet_but_a_heartbeat_is_not(self):
        self._duty(minutes_ago=5, contact_minutes_ago=0)
        for row in self._rows():
            self.assertFalse(row["stale"], row)

    def test_a_login_never_heard_from_is_no_signal(self):
        """No contact at all, as a phone on the old app reads before its first
        fix: what the board said before."""
        self._duty(minutes_ago=1)
        for row in self._rows():
            self.assertTrue(row["stale"], row)
            self.assertIsNone(row["last_seen_minutes"])
            self.assertIsNone(row["last_contact_at"])

    def test_a_fix_before_the_login_is_not_one_since_it(self):
        self._duty(minutes_ago=30, contact_minutes_ago=0)
        self._fix(minutes_ago=40)
        board, _ = self._rows()
        self.assertTrue(board["stale"])

    def test_location_off_says_since_when(self):
        self._duty(minutes_ago=60, contact_minutes_ago=0)
        self._fix(minutes_ago=20)
        self._gps("gps_off", minutes_ago=15)
        for row in self._rows():
            self.assertTrue(row["stale"], "an older board must not read it as live")
            self.assertEqual(
                timezone.localtime(_parse(row["gps_off_since"])), timezone.localtime(self._ago(15))
            )

    def test_location_found_off_at_login_counts_too(self):
        """The phone found it off when the app looked, a moment before the
        Login by its own clock."""
        self._duty(minutes_ago=10, contact_minutes_ago=0)
        self._gps("gps_off", minutes_ago=11, detail="found_off")
        for row in self._rows():
            self.assertIsNotNone(row["gps_off_since"], row)

    def test_location_back_on_clears_it(self):
        self._duty(minutes_ago=60, contact_minutes_ago=0)
        self._fix(minutes_ago=20)
        self._gps("gps_off", minutes_ago=15)
        self._gps("gps_on", minutes_ago=5)
        for row in self._rows():
            self.assertIsNone(row["gps_off_since"], row)
            self.assertFalse(row["stale"], row)

    def test_a_fix_after_it_clears_it_before_the_gps_on_arrives(self):
        self._duty(minutes_ago=60, contact_minutes_ago=0)
        self._gps("gps_off", minutes_ago=15)
        self._fix(minutes_ago=2)
        for row in self._rows():
            self.assertIsNone(row["gps_off_since"], row)

    def test_a_switch_off_from_an_earlier_duty_is_not_this_ones(self):
        self._gps("gps_off", minutes_ago=300)
        self._duty(minutes_ago=60, contact_minutes_ago=0)
        self._fix(minutes_ago=20)
        for row in self._rows():
            self.assertIsNone(row["gps_off_since"], row)

    def test_a_checked_out_engineer_is_never_flagged(self):
        session = self._duty(minutes_ago=60)
        DutySession.objects.filter(pk=session.pk).update(ended_at=self._ago(30))
        self._gps("gps_off", minutes_ago=40)
        board, live = self._rows()
        self.assertFalse(board["stale"])
        self.assertIsNone(board["gps_off_since"])
        self.assertIsNone(live)

    def test_a_clock_running_ahead_is_not_minus_three_minutes(self):
        self._duty(minutes_ago=60, contact_minutes_ago=0)
        self._fix(minutes_ago=-3, received_minutes_ago=0)
        for row in self._rows():
            self.assertEqual(row["last_fix_minutes"], 0)

    def test_the_marker_is_the_newest_fix_taken_not_the_last_stored(self):
        """A late batch is stored after the live fixes it was taken before.
        Reading the highest id moved the marker back down the road and called
        a live phone stale."""
        self._duty(minutes_ago=60, contact_minutes_ago=0)
        self._fix(minutes_ago=1, latitude=13.5)
        for minutes in (40, 35, 30):
            self._fix(minutes_ago=minutes, latitude=13.0, received_minutes_ago=0)
        for row in self._rows():
            self.assertEqual(row["latitude"], 13.5, row)
            self.assertEqual(row["last_fix_minutes"], 1)
            self.assertFalse(row["stale"], row)

    def test_names_nobody_answers_to_carry_the_new_fields_empty(self):
        with mock.patch("django.utils.timezone.now", return_value=self.now):
            rows = self.client.post(
                "/api/tracking/roster/", {"names": ["Nobody Here"]}, format="json"
            ).data
        self.assertEqual(
            {key: rows[0][key] for key in ("last_fix_minutes", "gps_off_since", "last_contact_at")},
            {"last_fix_minutes": None, "gps_off_since": None, "last_contact_at": None},
        )


class RosterEveryLoginTests(TestCase):
    """The board holds every Login of the day, not only the one it shows."""

    @classmethod
    def setUpTestData(cls):
        cls.office = User.objects.create_user(username="logins-office", password="x", role="admin")
        cls.user, cls.engineer = make_engineer("logins", "Logins Lakshmi")

    def test_the_board_leaves_out_the_drive_to_a_second_login_like_the_day_view(self):
        day = timezone.localdate() - datetime.timedelta(days=1)

        def at(hour, minute=0):
            return timezone.make_aware(datetime.datetime.combine(day, datetime.time(hour, minute)))

        DutySession.objects.create(engineer=self.engineer, started_at=at(9), ended_at=at(12))
        DutySession.objects.create(engineer=self.engineer, started_at=at(15), ended_at=at(18))
        for moment, latitude in ((at(10), 11.0), (at(11), 11.009), (at(15, 5), 11.5), (at(16), 11.509)):
            LocationPing.objects.create(
                engineer=self.engineer, latitude=latitude, longitude=78.0, accuracy=8,
                timestamp=moment, received_at=moment,
            )
        client = APIClient()
        client.force_authenticate(self.office)
        with mock.patch.dict("os.environ", {"KM_RULE_FROM": "2000-01-01T00:00:00+00:00"}), mock.patch(
            "cases.olamaps.is_configured", return_value=False
        ):
            board = next(
                r for r in client.get("/api/tracking/roster/", {"date": day.isoformat()}).data
                if r["engineer_id"] == self.engineer.id
            )
            panel = client.get(
                "/api/tracking/day/", {"engineer": self.engineer.id, "date": day.isoformat()}
            ).data
        self.assertAlmostEqual(board["distance_km"], 2.0, delta=0.05)
        self.assertEqual(board["distance_km"], panel["total_km"])


def _parse(value):
    from django.utils.dateparse import parse_datetime

    return value if isinstance(value, datetime.datetime) else parse_datetime(value)
