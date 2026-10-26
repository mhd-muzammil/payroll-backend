"""Km imported from the old app's report, for days the app did not record.

An ImportedKmDay is read by _DayKm, the one place an engineer-day's km is
read, so the roster (the KM report, Engineer Productivity, the board) and the
engineer's own history all show it -- and today, a day still going on, never
does. `import_old_app_km` finds each engineer by their own September km and
writes nothing unless it finds every one of them and lowers nobody's own km.
"""
import datetime
import os
from io import StringIO
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from cases.management.commands import import_old_app_km as command
from cases.models import ImportedKmDay, LocationPing
from cases.views import _DayKm

from .test_km_replay import KM, NEW_RULE, make_engineer

ONE_DAY = datetime.timedelta(days=1)

# "Today" for the readers' tests; the command's days are all long past.
TODAY = datetime.date(2026, 10, 26)
NOW = timezone.make_aware(datetime.datetime.combine(TODAY, datetime.time(12, 0)))
PAST = datetime.date(2026, 8, 25)


def drive(engineer, day, kilometres, latitude=11.0, hour=10):
    """About `kilometres` km north on `day`, a fix a kilometre."""
    for step in range(kilometres + 1):
        taken = timezone.make_aware(
            datetime.datetime.combine(day, datetime.time(hour, step % 60, step // 60))
        )
        LocationPing.objects.create(
            engineer=engineer, latitude=latitude + step * KM, longitude=78.0, accuracy=8,
            timestamp=taken, received_at=taken,
        )


def imported(engineer, day, km, source=command.SOURCE):
    return ImportedKmDay.objects.create(engineer=engineer, day=day, km=km, source=source)


class NewRuleTestCase(TestCase):
    def setUp(self):
        patcher = mock.patch.dict(os.environ, NEW_RULE)
        patcher.start()
        self.addCleanup(patcher.stop)


class DayKmTests(NewRuleTestCase):
    """_DayKm: an imported day's km is the import; every other day its fixes."""

    def setUp(self):
        super().setUp()
        _, self.ravi = make_engineer("imp-ravi", "Ravi Kumar")
        _, self.mani = make_engineer("imp-mani", "Mani S")

    def _km(self, engineer, day):
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            day_km = _DayKm([self.ravi.id, self.mani.id], day)
            return day_km.km(engineer.id), day_km.trail_km(engineer.id)

    def test_an_imported_day_is_the_import_not_its_fixes(self):
        # The app's own short test Login that day, and the report's full day.
        drive(self.ravi, PAST, 1)
        imported(self.ravi, PAST, 10.5)
        km, own = self._km(self.ravi, PAST)
        self.assertEqual(km, 10.5)
        self.assertAlmostEqual(own, 1.0, delta=0.01)

    def test_a_day_with_nothing_imported_is_its_fixes(self):
        drive(self.ravi, PAST + ONE_DAY, 2)
        imported(self.ravi, PAST, 10.5)
        km, own = self._km(self.ravi, PAST + ONE_DAY)
        self.assertEqual(km, own)
        self.assertAlmostEqual(km, 2.0, delta=0.01)

    def test_only_that_engineers_day(self):
        drive(self.mani, PAST, 3, latitude=12.0)
        imported(self.ravi, PAST, 10.5)
        km, own = self._km(self.mani, PAST)
        self.assertEqual(km, own)
        self.assertAlmostEqual(km, 3.0, delta=0.01)
        self.assertEqual(self._km(self.ravi, PAST)[0], 10.5)

    def test_today_is_never_imported(self):
        drive(self.ravi, TODAY, 2)
        imported(self.ravi, TODAY, 99.0)
        km, own = self._km(self.ravi, TODAY)
        self.assertEqual(km, own)
        self.assertAlmostEqual(km, 2.0, delta=0.01)
        # Yesterday is a past day like any other.
        imported(self.ravi, TODAY - ONE_DAY, 7.5)
        self.assertEqual(self._km(self.ravi, TODAY - ONE_DAY)[0], 7.5)


class ReadersTests(NewRuleTestCase):
    """Every reader of a day's km shows the import: the office's roster, the
    engineer's own history; and the phone's "Travelled today" never does."""

    def setUp(self):
        super().setUp()
        self.ravi_user, self.ravi = make_engineer("imp-read-ravi", "Ravi Kumar")
        self.office = APIClient()
        self.office.force_authenticate(
            User.objects.create_user(username="imp-office", password="x", role="admin")
        )
        self.phone = APIClient()
        self.phone.force_authenticate(self.ravi_user)
        self.phone.credentials(HTTP_X_PAYROLL_CLIENT="app")

    def _roster_km(self, day):
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            rows = self.office.get("/api/tracking/roster/", {"date": day.isoformat()}).data
        return next(row["distance_km"] for row in rows if row["engineer_id"] == self.ravi.id)

    def test_the_roster_shows_the_import_on_its_day_only(self):
        imported(self.ravi, PAST, 113.8)
        drive(self.ravi, PAST + ONE_DAY, 2)
        self.assertEqual(self._roster_km(PAST), 113.8)
        self.assertAlmostEqual(self._roster_km(PAST + ONE_DAY), 2.0, delta=0.01)

    def test_the_engineers_history_shows_the_import(self):
        imported(self.ravi, PAST, 113.8)
        drive(self.ravi, PAST + ONE_DAY, 2)
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            history = self.phone.get(
                "/api/cases/my_history/",
                {"from": PAST.isoformat(), "to": (PAST + ONE_DAY).isoformat()},
            ).data
        first, second = history["days"]
        self.assertEqual(first["km"], 113.8)
        self.assertAlmostEqual(second["km"], 2.0, delta=0.01)
        self.assertEqual(history["totals"]["days_tracked"], 2)
        self.assertAlmostEqual(history["totals"]["km"], 115.8, delta=0.01)

    def test_travelled_today_is_the_phones_own(self):
        drive(self.ravi, TODAY, 2)
        imported(self.ravi, TODAY, 99.0)
        imported(self.ravi, TODAY - ONE_DAY, 50.0)
        with mock.patch("django.utils.timezone.now", return_value=NOW):
            today_km = self.phone.get("/api/tracking/duty/").data["today_km"]
        self.assertAlmostEqual(today_km, 2.0, delta=0.01)
        self.assertEqual(self._roster_km(TODAY), today_km)


# The command, on a small table of its own. September days identify; the
# August days are imported. All long past, whenever the tests run.
F1, F2 = datetime.date(2026, 9, 3), datetime.date(2026, 9, 4)
D1, D2 = datetime.date(2026, 8, 25), datetime.date(2026, 8, 26)
SMALL_KM = {
    "Ravi": ("RAVI K", {D1.isoformat(): 12.5, D2.isoformat(): 30}),
    "Mani": ("Mani S", {D1.isoformat(): 8.2}),
}
SMALL_FINGERPRINT = {
    "Ravi": {F1.isoformat(): 3.0, F2.isoformat(): 5.0},
    "Mani": {F1.isoformat(): 2.0, F2.isoformat(): 4.0},
}


class CommandTests(NewRuleTestCase):
    def setUp(self):
        super().setUp()
        _, self.ravi = make_engineer("imp-cmd-ravi", "Ravi Kumar")
        _, self.mani = make_engineer("imp-cmd-mani", "Mani S")
        _, self.other = make_engineer("imp-cmd-other", "Other Person")
        drive(self.ravi, F1, 3, latitude=11.0)
        drive(self.ravi, F2, 5, latitude=11.0)
        drive(self.mani, F1, 2, latitude=12.0)
        drive(self.mani, F2, 4, latitude=12.0)
        # Somebody else out on the same days, who must not be picked.
        drive(self.other, F1, 7, latitude=13.0)
        drive(self.other, F2, 2, latitude=13.0)
        self.tables(SMALL_KM, SMALL_FINGERPRINT)

    def tables(self, km, fingerprint):
        for name, value in (("KM", km), ("FINGERPRINT", fingerprint)):
            patcher = mock.patch.object(command, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_command(self, *args):
        out = StringIO()
        call_command("import_old_app_km", *args, stdout=out)
        return out.getvalue()

    def stored(self):
        return {
            (row.engineer_id, row.day, row.km, row.source)
            for row in ImportedKmDay.objects.all()
        }

    def test_a_dry_run_names_everyone_and_saves_nothing(self):
        out = self.run_command()
        self.assertIn(
            f"Ravi (report: RAVI K) -> #{self.ravi.id} Ravi Kumar (Chennai); "
            "September km 3.00 5.00 = KM report 3.0 5.0",
            out,
        )
        self.assertIn(
            f"Mani (report: Mani S) -> #{self.mani.id} Mani S (Chennai); "
            "September km 2.00 4.00 = KM report 2.0 4.0",
            out,
        )
        self.assertIn("2026-08-25   app   0.00 ->   12.5   new", out)
        self.assertIn("Would write 3 day(s). Nothing was saved.", out)
        self.assertEqual(ImportedKmDay.objects.count(), 0)

    def test_apply_writes_every_day_and_the_readers_show_it(self):
        out = self.run_command("--apply")
        self.assertIn("IMPORTED: wrote 3 day(s).", out)
        self.assertEqual(
            self.stored(),
            {
                (self.ravi.id, D1, 12.5, command.SOURCE),
                (self.ravi.id, D2, 30.0, command.SOURCE),
                (self.mani.id, D1, 8.2, command.SOURCE),
            },
        )
        self.assertEqual(_DayKm([self.ravi.id], D2).km(self.ravi.id), 30.0)

    def test_running_it_again_changes_nothing(self):
        self.run_command("--apply")
        before = list(ImportedKmDay.objects.order_by("id").values())
        out = self.run_command("--apply")
        self.assertIn("already imported", out)
        self.assertIn("Nothing to do: every day is imported already.", out)
        self.assertEqual(list(ImportedKmDay.objects.order_by("id").values()), before)

    def test_a_corrected_figure_replaces_the_one_stored(self):
        self.run_command("--apply")
        self.tables(
            {**SMALL_KM, "Ravi": ("RAVI K", {D1.isoformat(): 13.0, D2.isoformat(): 30})},
            SMALL_FINGERPRINT,
        )
        out = self.run_command()
        self.assertIn("2026-08-25   app   0.00 ->   13.0   was 12.5", out)
        self.assertEqual(ImportedKmDay.objects.get(engineer=self.ravi, day=D1).km, 12.5)
        self.run_command("--apply")
        self.assertEqual(ImportedKmDay.objects.get(engineer=self.ravi, day=D1).km, 13.0)
        self.assertEqual(ImportedKmDay.objects.count(), 3)

    def test_the_apps_own_km_is_replaced_never_added_to(self):
        drive(self.ravi, D1, 1, latitude=14.0)
        out = self.run_command("--apply")
        self.assertIn("2026-08-25   app   1.00 ->   12.5   new", out)
        self.assertEqual(_DayKm([self.ravi.id], D1).km(self.ravi.id), 12.5)

    def _refused(self, *args):
        with self.assertRaises(CommandError) as raised:
            self.run_command(*args)
        self.assertEqual(ImportedKmDay.objects.count(), 0)
        return str(raised.exception)

    def test_a_day_the_app_has_more_km_on_is_refused_and_nothing_is_written(self):
        drive(self.ravi, D1, 15, latitude=14.0)
        message = self._refused("--apply")
        self.assertIn("STOP: the app already has more km", message)
        self.assertIn("Ravi 2026-08-25", message)

    def test_a_name_whose_september_km_nobody_has_stops_it(self):
        self.tables(SMALL_KM, {**SMALL_FINGERPRINT, "Mani": {F1.isoformat(): 2.0, F2.isoformat(): 9.0}})
        message = self._refused("--apply")
        self.assertIn("STOP: could not tell who is who", message)
        self.assertIn("Mani: 0 employee(s)", message)

    def test_two_employees_with_the_same_september_km_stop_it(self):
        _, twin = make_engineer("imp-cmd-twin", "Ravi Twin")
        drive(twin, F1, 3, latitude=15.0)
        drive(twin, F2, 5, latitude=15.0)
        message = self._refused("--apply")
        self.assertIn("Ravi: 2 employee(s)", message)

    def test_two_names_for_one_employee_stop_it(self):
        self.tables(
            {**SMALL_KM, "Ravi again": ("RAVI", {D2.isoformat(): 1.0})},
            {**SMALL_FINGERPRINT, "Ravi again": SMALL_FINGERPRINT["Ravi"]},
        )
        message = self._refused("--apply")
        self.assertIn(f"Ravi and Ravi again are the same employee #{self.ravi.id}", message)

    def test_a_close_figure_is_the_same_figure_a_far_one_is_not(self):
        # Ravi's 3 km of fixes read 3.0: shown to one decimal, rounded or cut,
        # that is never more than 0.1 away; 2.85 is someone else.
        self.tables(SMALL_KM, {**SMALL_FINGERPRINT, "Ravi": {F1.isoformat(): 2.91, F2.isoformat(): 5.0}})
        self.assertIn("September km 3.00 5.00 = KM report 2.9 5.0", self.run_command())
        self.tables(SMALL_KM, {**SMALL_FINGERPRINT, "Ravi": {F1.isoformat(): 2.85, F2.isoformat(): 5.0}})
        self.assertIn("Ravi: 0 employee(s)", self._refused())

    def test_remove_takes_out_this_import_only(self):
        self.run_command("--apply")
        other = imported(self.other, D1, 4.0, source="another report")
        out = self.run_command("--remove")
        self.assertIn("Would take out 3 day(s). Nothing was saved.", out)
        self.assertEqual(ImportedKmDay.objects.count(), 4)
        out = self.run_command("--remove", "--apply")
        self.assertIn("REMOVED: 3 day(s).", out)
        self.assertEqual(list(ImportedKmDay.objects.all()), [other])
        self.assertIn("Nothing to take out", self.run_command("--remove", "--apply"))


class ReportTableTests(TestCase):
    """The table the command carries: the PDF's figures for 25 Aug - 1 Sep."""

    def test_the_figures(self):
        self.assertEqual(len(command.KM), 15)
        self.assertEqual(sum(len(kms) for _, kms in command.KM.values()), 93)
        self.assertAlmostEqual(
            sum(sum(kms.values()) for _, kms in command.KM.values()), 6951.1, places=6
        )
        for _, kms in command.KM.values():
            for day, km in kms.items():
                self.assertTrue("2026-08-25" <= day <= "2026-09-01", day)
                self.assertGreater(km, 0)

    def test_every_name_is_identified_by_days_the_import_never_touches(self):
        self.assertEqual(set(command.FINGERPRINT), set(command.KM))
        for name, kms in command.FINGERPRINT.items():
            self.assertEqual(len(kms), 3, name)
            for day, km in kms.items():
                self.assertGreater(day, "2026-09-01", name)
                self.assertGreater(km, 0, name)
