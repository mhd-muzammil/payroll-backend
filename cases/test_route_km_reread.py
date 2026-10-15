"""route_km_reread: settled days measured again for the 15 Oct 2026 points rule
(an auto-closed Logout and a Check in with no position became points).

What these tests hold it to:

  Without --apply it changes nothing and asks Ola nothing.

  With it, only final cells the new rule changes are measured again -- and
  only when the rule is the whole difference: a cell whose points moved for
  another reason (a branch changed, a call handed on) is listed and never
  touched. A cell measured again ends final with the new points and km, through
  the table's own refresh.

  Nothing is lost: a cell Ola fails, or never reaches, or the run is stopped
  in the middle of, is put back exactly as it was; three failures in a row stop
  the run. Open days and never-measured cells are the report's.

Nothing here reaches the network: Ola is test_route_km's FakeOla.
"""
import datetime
import io
import itertools
from unittest import mock

from django.core.management import CommandError, call_command
from django.test import SimpleTestCase, TestCase, override_settings
from django.utils import timezone

from fieldez_km.models import FieldezKmDay
from fieldez_km.points import fieldez_points
from fieldez_km.tests import FIELDEZ_PLACE, FieldezMixin, order, resolution

from . import olamaps, route_km
from .management.commands import route_km_reread
from .models import Case, CaseVisit, RouteKmDay
from .test_route_km import TEST_KEY, at, check_in, duty, make_engineer, ping

DAY = datetime.timedelta(days=1)
LONG_AGO = timezone.now() - datetime.timedelta(days=3)

_numbers = itertools.count(1)


def visited(engineer, when, wo):
    """A call checked into at `when` through the day's visit row, the phone at
    12.80, 77.80 -- fieldez_km.tests.visit with a case number of its own:
    numbered by Case.save from its pk, it could meet one of check_in's."""
    given = when - datetime.timedelta(hours=1)
    case = Case.objects.create(
        case_number="OC-RR-%06d" % next(_numbers),
        external_ref=wo,
        customer_name="A customer",
        title="Service call",
        assigned_to=engineer,
        assigned_at=given,
    )
    CaseVisit.objects.create(
        case=case,
        engineer=engineer,
        plan_date=timezone.localtime(when).date(),
        assigned_at=given,
        checked_in_at=when,
        checked_out_at=when + datetime.timedelta(minutes=30),
        punch_in_lat=12.80,
        punch_in_lon=77.80,
    )
    return case


def as_measured_before(model, engineer, day, points, km=5.0):
    """A final cell as the old rule left it: measured through `points`."""
    return model.objects.create(
        engineer=engineer,
        day=day,
        status="ready",
        km=km,
        path=[[points[0]["latitude"], points[0]["longitude"]]],
        points=points,
        signature=route_km.signature(points),
        checked_at=LONG_AGO,
        measured_at=LONG_AGO,
        final=True,
    )


def forgot_to_log_out(engineer, day):
    """Login at 09:00, a call at 11:00, and a duty the system closed at its last
    fix, 19:41 -- Mohan's 13 Oct, in short."""
    duty(engineer, at(day, 9, 0), at(day, 19, 41), auto_closed=True)
    ping(engineer, at(day, 9, 0), 12.70, 77.70)
    ping(engineer, at(day, 19, 41), 12.90, 77.90)
    return check_in(engineer, at(day, 11, 0), 12.80, 77.80)


def old_rule(engineer, day):
    return route_km.key_points(engineer, day, as_before_15_oct=True)


@override_settings(OLA_MAPS_API_KEY=TEST_KEY)
class RereadTests(FieldezMixin, TestCase):
    def setUp(self):
        super().setUp()
        # A cell not read on the first try is given up at once: time.sleep is
        # a mock here, so a real wait would spin for CELL_WAIT_SECONDS.
        wait = mock.patch.object(route_km_reread, "CELL_WAIT_SECONDS", 0)
        wait.start()
        self.addCleanup(wait.stop)
        self.engineer = make_engineer("Reread Tester", branch="Hosur")
        self.today = timezone.localdate()
        # Four days back: settled.
        self.day = self.today - 4 * DAY
        self.case = forgot_to_log_out(self.engineer, self.day)
        self.new_points = route_km.key_points(self.engineer, self.day)
        self.assertEqual([p["kind"] for p in self.new_points], ["login", "checkin", "logout"])
        self.old = as_measured_before(
            RouteKmDay, self.engineer, self.day, old_rule(self.engineer, self.day)
        )
        self.assertEqual([p["kind"] for p in self.old.points], ["login", "checkin"])

    def run_it(self, *extra, day=None):
        out = io.StringIO()
        day = (day or self.day).isoformat()
        call_command("route_km_reread", "--from", day, "--to", day, *extra, stdout=out)
        return out.getvalue()

    def snapshot(self, model=RouteKmDay):
        return list(model.objects.order_by("pk").values())

    def test_without_apply_it_lists_the_change_and_changes_nothing(self):
        before = self.snapshot()

        out = self.run_it()

        self.assertIn("Changed by the 15 Oct rule (1):", out)
        self.assertIn(
            "1. ROUTE KM %s  Reread Tester (%d)  5.00 km  + Logout 19:41"
            % (self.day.isoformat(), self.engineer.id),
            out,
        )
        self.assertIn("1 cell(s) to measure again", out)
        self.assertIn("Nothing was changed", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ola.requests, [])

    def test_apply_measures_it_again_through_the_new_points_and_settles_it(self):
        out = self.run_it("--apply")

        self.assertEqual(self.ola.requests, [[(12.70, 77.70), (12.80, 77.80), (12.90, 77.90)]])
        row = RouteKmDay.objects.get(pk=self.old.pk)
        self.assertEqual((row.status, row.km, row.final), ("ready", 2.0, True))
        self.assertEqual(row.points, self.new_points)
        self.assertEqual(row.signature, route_km.signature(self.new_points))
        self.assertIn("5.00 km -> 2.00 km", out)
        self.assertIn("Done: 1 measured again, 0 kept as they were.", out)

        # And a second run finds nothing left to do.
        self.assertIn("Nothing to measure again", self.run_it("--apply"))
        self.assertEqual(len(self.ola.requests), 1)

    def test_a_cell_whose_points_are_the_same_is_left_as_it_was(self):
        other = make_engineer("Same Points", branch="Hosur")
        duty(other, at(self.day, 9, 0), at(self.day, 18, 0))
        ping(other, at(self.day, 9, 0), 12.71, 77.71)
        ping(other, at(self.day, 18, 0), 12.91, 77.91)
        same = as_measured_before(RouteKmDay, other, self.day, route_km.key_points(other, self.day))
        before = RouteKmDay.objects.values().get(pk=same.pk)

        self.run_it("--apply")

        self.assertEqual(RouteKmDay.objects.values().get(pk=same.pk), before)
        self.assertEqual(len(self.ola.requests), 1)  # the changed one only

    def test_a_cell_changed_for_another_reason_is_listed_and_never_touched(self):
        # Moved: the engineer's branch changed after the day settled, so a
        # Login the old rule put at the Hosur office now stands at Chennai's.
        moved = make_engineer("Moved Branch", branch="Hosur")
        duty(moved, at(self.day, 9, 0), at(self.day, 19, 41), auto_closed=True)
        ping(moved, at(self.day, 19, 41), 12.90, 77.90)
        check_in(moved, at(self.day, 11, 0), 12.80, 77.80)
        row = as_measured_before(RouteKmDay, moved, self.day, old_rule(moved, self.day))
        self.assertTrue(row.points[0]["office"])
        moved.branch = "Chennai"
        moved.save(update_fields=["branch"])
        before = RouteKmDay.objects.values().get(pk=row.pk)

        out = self.run_it("--apply")

        self.assertIn("Changed for another reason: NOT touched (1):", out)
        self.assertIn("Moved Branch", out)
        self.assertIn("- Login 09:00 (office)", out)
        self.assertEqual(RouteKmDay.objects.values().get(pk=row.pk), before)
        # Only the Reread Tester's cell, which the rule alone changed, was measured.
        self.assertEqual(len(self.ola.requests), 1)

    def test_an_open_day_is_the_reports(self):
        yesterday = self.today - DAY
        forgot_to_log_out(self.engineer, yesterday)
        # Final, so only its day being open keeps it out.
        as_measured_before(RouteKmDay, self.engineer, yesterday, old_rule(self.engineer, yesterday))
        before = self.snapshot()

        out = self.run_it("--apply", day=yesterday)

        self.assertIn("1 day(s) asked for are not final yet", out)
        self.assertIn("Nothing to measure again", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ola.requests, [])

    def test_a_cell_never_measured_is_left_to_the_report(self):
        # As _claim makes one: pending, never read, not final.
        RouteKmDay.objects.filter(pk=self.old.pk).update(
            status="pending", km=None, points=[], signature="", checked_at=None, measured_at=None, final=False
        )
        before = self.snapshot()

        out = self.run_it("--apply")

        self.assertIn("Nothing to measure again", out)
        self.assertNotIn("Reread Tester", out)  # not even listed
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ola.requests, [])

    def test_only_the_engineer_asked_for(self):
        other = make_engineer("Someone Else", branch="Hosur")
        forgot_to_log_out(other, self.day)
        theirs = as_measured_before(RouteKmDay, other, self.day, old_rule(other, self.day))
        mine = RouteKmDay.objects.values().get(pk=self.old.pk)

        out = self.run_it("--apply", "--engineer", str(other.id))

        self.assertIn("Someone Else", out)
        self.assertNotIn("Reread Tester", out)
        self.assertNotEqual(RouteKmDay.objects.get(pk=theirs.pk).signature, theirs.signature)
        self.assertEqual(RouteKmDay.objects.values().get(pk=self.old.pk), mine)

    def test_a_cell_ola_will_not_take_now_is_put_back_and_taken_next_run(self):
        # Another cell, of another day, measured this minute; an allowance of one.
        busy = make_engineer("Busy", branch="Hosur")
        RouteKmDay.objects.create(
            engineer=busy, day=self.day - DAY, status="ready", km=1.0, measured_at=timezone.now()
        )
        before = RouteKmDay.objects.values().get(pk=self.old.pk)
        with mock.patch.dict("os.environ", {"ROUTE_KM_PER_MINUTE": "1"}):
            out = self.run_it("--apply")

        self.assertIn("Ola stayed busy. Kept 5.00 km", out)
        self.assertIn("Done: 0 measured again, 1 kept as they were.", out)
        self.assertEqual(self.ola.requests, [])
        # Exactly as it was: final, its km, its points.
        self.assertEqual(RouteKmDay.objects.values().get(pk=self.old.pk), before)

        # The next run takes it.
        self.assertIn("Done: 1 measured again, 0 kept", self.run_it("--apply"))
        row = RouteKmDay.objects.get(pk=self.old.pk)
        self.assertEqual((row.km, row.final, row.points), (2.0, True, self.new_points))

    def test_a_cell_ola_fails_keeps_its_km_and_stays_final(self):
        self.ola.fail = olamaps.SnapUnavailable("no route")
        before = RouteKmDay.objects.values().get(pk=self.old.pk)

        out = self.run_it("--apply")

        self.assertIn("Ola could not measure it (no route). Kept 5.00 km.", out)
        self.assertIn("Done: 0 measured again, 1 kept as they were.", out)
        row = RouteKmDay.objects.values().get(pk=self.old.pk)
        changed = {key for key in before if before[key] != row[key]}
        # Only when Ola was last asked, and why it failed: they pace every worker.
        self.assertTrue(changed <= {"measured_at", "error"}, changed)
        # A run minutes later -- its claim of this minute run out -- tries it again.
        self.ola.fail = None
        RouteKmDay.objects.filter(pk=self.old.pk).update(measured_at=LONG_AGO)
        self.assertIn("Done: 1 measured again", self.run_it("--apply"))

    def test_three_failures_in_a_row_stop_the_run(self):
        engineers = [make_engineer("Fails %d" % n, branch="Hosur") for n in range(3)]
        for engineer in engineers:
            forgot_to_log_out(engineer, self.day)
            as_measured_before(RouteKmDay, engineer, self.day, old_rule(engineer, self.day))
        self.ola.fail = olamaps.SnapUnavailable("Ola is down")
        before = self.snapshot()

        out = self.run_it("--apply")

        self.assertIn("4 cell(s) to measure again", out)
        self.assertIn("Stopped: Ola failed 3 cells in a row.", out)
        self.assertEqual(len(self.ola.requests), 3)  # the fourth was never asked
        after = self.snapshot()
        for row_before, row_after in zip(before, after):
            changed = {key for key in row_before if row_before[key] != row_after[key]}
            self.assertTrue(changed <= {"measured_at", "error"}, changed)

    def test_a_run_stopped_in_the_middle_puts_the_cell_back(self):
        before = RouteKmDay.objects.values().get(pk=self.old.pk)
        with mock.patch.object(route_km, "refresh", side_effect=KeyboardInterrupt):
            out = self.run_it("--apply")

        self.assertIn("Stopped after 0", out)
        self.assertEqual(RouteKmDay.objects.values().get(pk=self.old.pk), before)

    def test_a_cell_that_failed_within_the_hour_is_left_for_later(self):
        RouteKmDay.objects.filter(pk=self.old.pk).update(
            status="failed", km=None, attempts=3, measured_at=timezone.now() - datetime.timedelta(minutes=5)
        )
        before = self.snapshot()

        out = self.run_it("--apply")

        self.assertIn("Failed less than an hour ago: run again later (1):", out)
        self.assertEqual(self.snapshot(), before)
        self.assertEqual(self.ola.requests, [])

    def test_cells_are_paced(self):
        other = make_engineer("Second Cell", branch="Hosur")
        forgot_to_log_out(other, self.day)
        as_measured_before(RouteKmDay, other, self.day, old_rule(other, self.day))

        self.run_it("--apply")

        self.assertEqual(len(self.ola.requests), 2)
        self.assertIn(mock.call(route_km_reread.PAUSE_SECONDS), self.sleep.call_args_list)

    def test_apply_needs_an_ola_key_and_changes_nothing_without_one(self):
        before = self.snapshot()
        with override_settings(OLA_MAPS_API_KEY=""), mock.patch.dict("os.environ", {"OLA_MAPS_API_KEY": ""}):
            with self.assertRaises(CommandError):
                self.run_it("--apply")
            # The dry run asks Ola nothing, so it works without one.
            self.assertIn("1 cell(s) to measure again", self.run_it())
        self.assertEqual(self.snapshot(), before)

    def fieldez_day(self):
        """FIELDEZ's own cell for a day: Login, the call at FieldEZ's place,
        and no Logout, as the old rule left it. (engineer, new points, row)"""
        engineer = make_engineer("Fieldez Reread", branch="Hosur")
        duty(engineer, at(self.day, 9, 0), at(self.day, 19, 41), auto_closed=True)
        ping(engineer, at(self.day, 9, 0), 12.70, 77.70)
        ping(engineer, at(self.day, 19, 41), 12.90, 77.90)
        visited(engineer, at(self.day, 12, 0), "WO-000000801")
        order("WO-000000801", resolutions=[resolution(at(self.day, 12, 20))])
        new = fieldez_points(engineer, self.day, timezone.now())[0]
        self.assertEqual([p["kind"] for p in new], ["login", "checkin", "logout"])
        old = [p for p in new if p["kind"] != "logout"]
        return engineer, new, as_measured_before(FieldezKmDay, engineer, self.day, old)

    def test_fieldez_takes_the_same_logout(self):
        engineer, new, old = self.fieldez_day()

        out = self.run_it("--apply", "--engineer", str(engineer.id))

        self.assertIn(
            "1. FIELDEZ  %s  Fieldez Reread (%d)  5.00 km  + Logout 19:41"
            % (self.day.isoformat(), engineer.id),
            out,
        )
        row = FieldezKmDay.objects.get(pk=old.pk)
        self.assertEqual((row.status, row.km, row.final), ("ready", 2.0, True))
        self.assertEqual(row.points, new)
        self.assertEqual(self.ola.requests, [[(12.70, 77.70), FIELDEZ_PLACE, (12.90, 77.90)]])
        # ROUTE KM had no cell for them, and was given none.
        self.assertFalse(RouteKmDay.objects.filter(engineer=engineer).exists())

    def test_both_tables_in_one_run_fieldez_waits_out_route_kms_minute(self):
        """The usual run: ROUTE KM's cell first, then FIELDEZ's -- which gives
        way while ROUTE KM has read or measured anything in the last minute
        (fieldez_km.km._giving_way), and is measured once that minute is up."""
        engineer, new, old = self.fieldez_day()
        route = as_measured_before(RouteKmDay, engineer, self.day, old_rule(engineer, self.day))
        clock = [0.0]

        def wait(seconds):
            clock[0] += seconds
            if seconds == route_km_reread.RETRY_SECONDS:
                # A minute goes by for the database too.
                for row in RouteKmDay.objects.all():
                    RouteKmDay.objects.filter(pk=row.pk).update(
                        measured_at=row.measured_at and row.measured_at - datetime.timedelta(seconds=61),
                        checked_at=row.checked_at and row.checked_at - datetime.timedelta(seconds=61),
                    )

        self.sleep.side_effect = wait
        with mock.patch.object(route_km_reread, "CELL_WAIT_SECONDS", 600), mock.patch.object(
            route_km_reread.time, "monotonic", lambda: clock[0]
        ):
            out = self.run_it("--apply", "--engineer", str(engineer.id))

        self.assertIn("Done: 2 measured again, 0 kept as they were.", out)
        self.assertIn(mock.call(route_km_reread.RETRY_SECONDS), self.sleep.call_args_list)
        self.assertEqual(
            self.ola.requests,
            [
                [(12.70, 77.70), (12.80, 77.80), (12.90, 77.90)],
                [(12.70, 77.70), FIELDEZ_PLACE, (12.90, 77.90)],
            ],
        )
        for row in (RouteKmDay.objects.get(pk=route.pk), FieldezKmDay.objects.get(pk=old.pk)):
            self.assertEqual((row.status, row.km, row.final), ("ready", 2.0, True))
            self.assertEqual([p["kind"] for p in row.points], ["login", "checkin", "logout"])

    def test_an_engineer_named_in_full_is_that_engineer(self):
        mohan = make_engineer("Mohan", branch="Hosur")
        make_engineer("Mohan Kumar", branch="Hosur")
        forgot_to_log_out(mohan, self.day)
        as_measured_before(RouteKmDay, mohan, self.day, old_rule(mohan, self.day))

        out = self.run_it("--engineer", "mohan")

        self.assertIn("Mohan (%d)" % mohan.id, out)
        self.assertNotIn("Reread Tester", out)
        with self.assertRaises(CommandError):
            self.run_it("--engineer", "Moh")  # both, by part of the name

    def test_a_fieldez_cell_left_waiting_on_ola_is_put_back(self):
        engineer, new, old = self.fieldez_day()
        before = FieldezKmDay.objects.values().get(pk=old.pk)
        # ROUTE KM read a cell this minute, so FIELDEZ gives way to it -- and
        # its refresh stores the cell pending, its km hidden, meanwhile.
        RouteKmDay.objects.create(
            engineer=make_engineer("Route Busy"), day=self.today, status="ready", checked_at=timezone.now()
        )

        out = self.run_it("--apply", "--engineer", str(engineer.id))

        self.assertIn("Ola stayed busy. Kept 5.00 km", out)
        self.assertEqual(self.ola.requests, [])
        self.assertEqual(FieldezKmDay.objects.values().get(pk=old.pk), before)

    def test_a_fieldez_cell_a_killed_run_left_pending_is_taken_up(self):
        engineer, new, old = self.fieldez_day()
        # As _waits_on_ola leaves it when the run is killed outright: pending
        # through the new points, the old km and signature kept, never read.
        FieldezKmDay.objects.filter(pk=old.pk).update(
            status="pending", points=new, final=False, checked_at=None
        )

        out = self.run_it("--apply", "--engineer", str(engineer.id))

        self.assertIn("Done: 1 measured again", out)
        row = FieldezKmDay.objects.get(pk=old.pk)
        self.assertEqual((row.status, row.km, row.final, row.points), ("ready", 2.0, True, new))

    def test_bad_arguments(self):
        with self.assertRaises(CommandError):
            call_command("route_km_reread", "--from", "2026-10-10", "--to", "2026-10-01")
        with self.assertRaises(CommandError):
            call_command("route_km_reread", "--from", "2026-01-01", "--to", "2026-12-31")
        with self.assertRaises(CommandError):
            call_command("route_km_reread", "--from", "13-10-2026", "--to", "2026-10-13")


class DifferenceTests(SimpleTestCase):
    def point(self, kind, lat, at="2026-10-13T14:11:00+00:00", case_number=None, office=False):
        return {
            "kind": kind,
            "at": at,
            "latitude": lat,
            "longitude": 77.0,
            "case_number": case_number,
            "office": office,
        }

    def test_what_is_new_and_what_is_gone_in_words(self):
        login = self.point("login", 12.1, at="2026-10-13T03:33:00+00:00")
        old = [login, self.point("checkin", 12.2, case_number="OC-1")]
        new = [login, self.point("checkin", 12.3, case_number="OC-1"), self.point("logout", 12.4)]
        self.assertEqual(
            route_km_reread.difference(old, new),
            ["+ Check in OC-1 19:41", "+ Logout 19:41", "- Check in OC-1 19:41"],
        )

    def test_an_office_point_says_so(self):
        old = [self.point("login", 12.1, office=True)]
        new = [self.point("login", 13.1, office=True)]
        self.assertEqual(
            route_km_reread.difference(old, new),
            ["+ Login 19:41 (office)", "- Login 19:41 (office)"],
        )

    def test_the_same_points_are_no_difference(self):
        points = [self.point("login", 12.1), self.point("logout", 12.1)]
        self.assertEqual(route_km_reread.difference(points, list(points)), [])
