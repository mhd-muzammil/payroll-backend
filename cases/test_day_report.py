"""The Day Report, held to the day panel it reports.

Everything in a cell is what /api/tracking/day/ says about the same
engineer-day, so these tests build a day and ask /day/ itself: the Login and
Logout and where they were, the punches, the stops, the phone's GPS and Net
entries and the km must be the panel's, not a second reading of its rules.
"""
import datetime
from unittest import mock

from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User

from . import day_report, places
from .models import EngineerScorecardDay, ImportedKmDay, LocationPing, PhoneEvent, PlaceName
from .test_route_km import NoNetwork, at, check_in, duty, make_engineer, ola_off, ping

ONE_DAY = datetime.timedelta(days=1)
PHONE_KINDS = ("gps_off", "gps_on", "net_off", "net_on")


def event(engineer, kind, when, lat=None, lon=None, detail="switched"):
    return PhoneEvent.objects.create(
        engineer=engineer, kind=kind, at=when, received_at=when,
        client_key=f"{kind}-{when.isoformat()}", latitude=lat, longitude=lon, detail=detail,
    )


def tagged(engineer, when, lat, lon, case):
    return LocationPing.objects.create(
        engineer=engineer, latitude=lat, longitude=lon, accuracy=8, timestamp=when, case=case
    )


def moment(entry):
    """(when, lat, lon) of one entry, as JSON carries it."""
    return (
        datetime.datetime.fromisoformat(entry["at"]),
        entry.get("latitude"),
        entry.get("longitude"),
    )


class DayReportTestCase(NoNetwork, TestCase):
    def setUp(self):
        super().setUp()
        # No Ola key, so the day panel these tests ask never looks an address
        # up either. The report itself never does: see
        # test_no_address_is_ever_looked_up, which turns the key back on.
        self.enterContext(ola_off())
        self.office = User.objects.create_user(
            username="office-day-report", password="x", role="superadmin", is_superuser=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.today = timezone.localdate()
        self.day = self.today - ONE_DAY
        self.anu = make_engineer("Anu Report", branch="Hosur")
        self.bala = make_engineer("Bala Report", branch="Salem")

    def ask(self, engineers, first, last=None, client=None):
        return (client or self.client).post(
            "/api/tracking/day_report/",
            {
                "engineers": [engineer.id for engineer in engineers],
                "from": first.isoformat(),
                "to": (last or first).isoformat(),
            },
            format="json",
        )

    def cell(self, engineer, day):
        response = self.ask([engineer], day)
        self.assertEqual(response.status_code, 200, response.data)
        (row,) = response.json()["engineers"]
        return row["days"][day.isoformat()]

    def panel(self, engineer, day):
        response = self.client.get(
            "/api/tracking/day/", {"engineer": engineer.id, "date": day.isoformat()}
        )
        self.assertEqual(response.status_code, 200)
        return response.json()


class AWholeDayTests(DayReportTestCase):
    def setUp(self):
        super().setUp()
        day, anu = self.day, self.anu
        duty(anu, at(day, 9), at(day, 18))
        ping(anu, at(day, 9, 2), 12.700, 77.700)
        ping(anu, at(day, 9, 40), 12.740, 77.740)
        # At the customer from 10:00 to 10:30, the fixes tagged with the call.
        self.call = check_in(
            anu, at(day, 10, 5), 12.7501, 77.7501,
            address="12, Gandhi Street, Hosur", completed_at=at(day, 10, 25),
            punch_out_lat=12.7502, punch_out_lon=77.7502, status="completed",
        )
        for minute in range(0, 31, 5):
            tagged(anu, at(day, 10, minute), 12.7500, 77.7500, self.call)
        ping(anu, at(day, 11), 12.800, 77.800)
        ping(anu, at(day, 13), 12.850, 77.850)
        ping(anu, at(day, 17, 55), 12.900, 77.900)
        # GPS off for half an hour, Net off for ten minutes, a Net blip that
        # is no outage, and GPS off at 17:30 still off when they logged out.
        event(anu, "gps_off", at(day, 11, 0))
        event(anu, "gps_on", at(day, 11, 30), 12.810, 77.810)
        event(anu, "net_off", at(day, 12, 0), 12.820, 77.820)
        event(anu, "net_on", at(day, 12, 10), 12.830, 77.830)
        event(anu, "net_off", at(day, 13, 0), 12.850, 77.850)
        event(anu, "net_on", at(day, 13, 1), 12.850, 77.850)
        event(anu, "gps_off", at(day, 17, 30), 12.880, 77.880)
        event(anu, "gps_on", at(day, 19, 0), 12.950, 77.950)
        EngineerScorecardDay.objects.create(
            engineer=anu, day=day, assigned=6, attended=3, closed=2, source="history"
        )
        # The Logout's place is already in the cache.
        PlaceName.objects.create(lat_key="12.9000", lon_key="77.9000", address="Cached place, Hosur")

    def test_every_figure(self):
        cell = self.cell(self.anu, self.day)
        self.assertEqual([moment(e) for e in cell["logins"]], [(at(self.day, 9), 12.7, 77.7)])
        self.assertEqual(
            [(moment(e), e["auto"], e["address"]) for e in cell["logouts"]],
            [((at(self.day, 18), 12.9, 77.9), False, "Cached place, Hosur")],
        )
        self.assertFalse(cell["on_duty"])
        (check_in_entry,) = cell["check_ins"]
        self.assertEqual(moment(check_in_entry), (at(self.day, 10, 5), 12.7501, 77.7501))
        self.assertEqual(check_in_entry["case_number"], self.call.case_number)
        self.assertEqual(check_in_entry["case_ref"], self.call.external_ref)
        self.assertEqual(check_in_entry["address"], "12, Gandhi Street, Hosur")
        (check_out_entry,) = cell["check_outs"]
        self.assertEqual(moment(check_out_entry), (at(self.day, 10, 25), 12.7502, 77.7502))
        self.assertEqual((cell["assigned"], cell["attended"], cell["closed"]), (6, 3, 2))
        (wait,) = cell["waits"]
        self.assertEqual((moment(wait)[0], wait["minutes"]), (at(self.day, 10), 30))
        self.assertEqual(wait["case_number"], self.call.case_number)
        self.assertEqual(wait["address"], "12, Gandhi Street, Hosur")
        self.assertEqual(cell["waiting_minutes"], 30)
        # 11:00-11:30, and 17:30 to the Logout at 18:00: the half hour after
        # it, off duty, is not theirs to answer for.
        self.assertEqual(cell["gps_off_minutes"], 60)
        self.assertEqual(
            [moment(e)[0] for e in cell["gps_offs"]], [at(self.day, 11), at(self.day, 17, 30)]
        )
        self.assertEqual([moment(e)[0] for e in cell["gps_ons"]], [at(self.day, 11, 30)])
        self.assertEqual(cell["net_off_minutes"], 10)
        self.assertEqual([moment(e)[0] for e in cell["net_offs"]], [at(self.day, 12)])
        self.assertEqual([moment(e)[0] for e in cell["net_ons"]], [at(self.day, 12, 10)])
        self.assertGreater(cell["km"], 0)

    def test_it_is_what_the_day_panel_says(self):
        cell = self.cell(self.anu, self.day)
        panel = self.panel(self.anu, self.day)
        events = panel["events"]

        def panel_moments(kind):
            return [moment(e) for e in events if e["type"] == kind]

        self.assertEqual([moment(e) for e in cell["logins"]], panel_moments("duty_start"))
        self.assertEqual([moment(e) for e in cell["logouts"]], panel_moments("duty_end"))
        for kind, key in (("gps_off", "gps_offs"), ("gps_on", "gps_ons"), ("net_off", "net_offs"), ("net_on", "net_ons")):
            with self.subTest(kind=kind):
                self.assertEqual([moment(e) for e in cell[key]], panel_moments(kind))
        self.assertEqual(
            [moment(e) for e in cell["check_ins"] + cell["check_outs"]],
            [moment(p) for p in panel["punches"]],
        )
        self.assertEqual(
            [(moment(w)[0], w["minutes"]) for w in cell["waits"]],
            [(datetime.datetime.fromisoformat(s["arrived_at"]), s["minutes"]) for s in panel["stops"]],
        )
        self.assertEqual(cell["km"], panel["total_km"])
        # The panel's own words for where they were, wherever it has them.
        addressed = {
            (moment(e)[0], e["type"]): e["address"] for e in events if e.get("address")
        }
        self.assertEqual(addressed[(at(self.day, 18), "duty_end")], cell["logouts"][0]["address"])
        self.assertEqual(addressed[(at(self.day, 10, 5), "reached")], cell["check_ins"][0]["address"])

    def test_no_address_is_ever_looked_up(self):
        with mock.patch.object(places, "_fetch_address") as fetch, mock.patch(
            "cases.olamaps.is_configured", return_value=True
        ):
            cell = self.cell(self.anu, self.day)
        fetch.assert_not_called()
        # The Login's place is not cached: it is given by where it is.
        (login,) = cell["logins"]
        self.assertIsNone(login["address"])
        self.assertEqual((login["latitude"], login["longitude"]), (12.7, 77.7))


class OtherDaysTests(DayReportTestCase):
    def test_a_day_with_no_login_counts_the_whole_day(self):
        ping(self.anu, at(self.day, 10), 12.70, 77.70)
        event(self.anu, "gps_off", at(self.day, 10, 5))
        event(self.anu, "gps_on", at(self.day, 10, 25))
        cell = self.cell(self.anu, self.day)
        self.assertEqual((cell["logins"], cell["logouts"], cell["on_duty"]), ([], [], False))
        self.assertEqual(cell["gps_off_minutes"], 20)
        self.assertEqual((cell["assigned"], cell["attended"], cell["closed"]), (None, None, None))

    def test_a_duty_still_running_has_no_logout(self):
        now = at(self.today, 12)
        with mock.patch("django.utils.timezone.now", return_value=now):
            duty(self.anu, at(self.today, 9))
            ping(self.anu, at(self.today, 9, 1), 12.70, 77.70)
            event(self.anu, "gps_off", at(self.today, 11))
            cell = self.cell(self.anu, self.today)
        self.assertTrue(cell["on_duty"])
        self.assertEqual(cell["logouts"], [])
        self.assertEqual(cell["gps_off_minutes"], 60)  # 11:00 to now

    def test_the_km_is_the_km_reports_imported_or_not(self):
        ping(self.anu, at(self.day - ONE_DAY, 10), 12.70, 77.70)
        ping(self.anu, at(self.day - ONE_DAY, 11), 12.80, 77.70)
        ImportedKmDay.objects.create(engineer=self.anu, day=self.day, km=113.8, source="test")
        response = self.ask([self.anu], self.day - ONE_DAY, self.day)
        days = response.json()["engineers"][0]["days"]
        self.assertEqual(days[self.day.isoformat()]["km"], 113.8)
        self.assertEqual(
            days[(self.day - ONE_DAY).isoformat()]["km"],
            self.panel(self.anu, self.day - ONE_DAY)["total_km"],
        )

    def test_every_engineer_and_day_asked_for_in_order_and_nothing_after_today(self):
        response = self.ask([self.bala, self.anu], self.day, self.today + 3 * ONE_DAY)
        self.assertEqual(response.status_code, 200, response.data)
        data = response.json()
        self.assertEqual(data["days"], [self.day.isoformat(), self.today.isoformat()])
        self.assertEqual([row["engineer_id"] for row in data["engineers"]], [self.bala.id, self.anu.id])
        for row in data["engineers"]:
            self.assertEqual(list(row["days"]), data["days"])


class AskingTests(DayReportTestCase):
    def test_only_the_office(self):
        engineer_user = User.objects.create_user(username="day-report-eng", password="x", role="employee")
        client = APIClient()
        client.force_authenticate(engineer_user)
        self.assertEqual(self.ask([self.anu], self.day, client=client).status_code, 403)

    def test_bad_requests(self):
        for body in (
            {"engineers": "1", "from": "2026-10-01", "to": "2026-10-01"},
            {"engineers": [True], "from": "2026-10-01", "to": "2026-10-01"},
            {"engineers": ["x"], "from": "2026-10-01", "to": "2026-10-01"},
            {"engineers": [], "from": "01-10-2026", "to": "2026-10-01"},
            {"engineers": [], "from": "2026-10-02", "to": "2026-10-01"},
        ):
            with self.subTest(body=body):
                response = self.client.post("/api/tracking/day_report/", body, format="json")
                self.assertEqual(response.status_code, 400)

    def test_at_most_31_days(self):
        first = self.today - datetime.timedelta(days=day_report.MAX_DAYS - 1)
        self.assertEqual(self.ask([], first, self.today).status_code, 200)
        self.assertEqual(self.ask([], first - ONE_DAY, self.today).status_code, 400)
