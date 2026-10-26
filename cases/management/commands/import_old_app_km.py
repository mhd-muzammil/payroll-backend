"""Import the km of 25 Aug - 1 Sep 2026 from the old app's Monthly Report.

The app recorded nothing for the engineers on those days, the first eight of
the 25 Aug - 24 Sep bill cycle: another app was tracking them until then. Its
Monthly Reports for August and September are where these figures come from,
read cell by cell and checked against each report's own "Total KM" for the
month. A day the report shows as 0 is left out.

Each engineer is named as the KM report names them, and is found in Payroll by
the km the KM report showed for them on three days in September -- their own
GPS figure, which no spelling of a name can be mistaken for. All of them must
be found, each exactly once, or nothing is written.

A row becomes the day's km wherever _DayKm reads a day (ImportedKmDay). It
never lowers what the app recorded itself: a day on which the app already has
more km than the report is refused, and then nothing is written at all.

    python manage.py import_old_app_km                    # say what it would do
    python manage.py import_old_app_km --apply            # do it
    python manage.py import_old_app_km --remove           # say what it would take out
    python manage.py import_old_app_km --remove --apply   # take it out
"""

from datetime import date

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from cases import km_rule
from cases.models import ImportedKmDay, LocationPing
from cases.views import _DayKm
from employees.models import Employee

SOURCE = "Old app Monthly Report, Aug-Sep 2026"

# The KM report's name -> (the name in the old app's report, {day: km}).
KM = {
    "Santhosh kumar S": ("Santhosh kumar S", {"2026-08-25": 15.3, "2026-08-26": 13.3, "2026-08-27": 13.4, "2026-08-28": 97.3, "2026-08-29": 56.2, "2026-09-01": 28.5}),
    "Sarwana N": ("Sarwana N", {"2026-08-25": 175.2, "2026-08-26": 71, "2026-08-27": 45.8, "2026-08-28": 73.6, "2026-08-29": 30.2, "2026-08-31": 190.1, "2026-09-01": 36.1}),
    "Vishnu S": ("Vishnu S", {"2026-08-25": 82.9, "2026-08-26": 15.4, "2026-08-27": 61, "2026-08-28": 182.5, "2026-08-29": 21.3, "2026-08-31": 68.6, "2026-09-01": 59.5}),
    "Jayakumar": ("Jaya kumar S", {"2026-08-25": 57.2, "2026-08-26": 28.9, "2026-08-27": 26.9, "2026-08-28": 92.5, "2026-08-31": 95.9, "2026-09-01": 12.8}),
    "Prasanth": ("Prasanth K", {"2026-08-25": 96.9, "2026-08-26": 76.3, "2026-08-27": 67.3, "2026-08-31": 90.9, "2026-09-01": 74.5}),
    "Perumal": ("Perumal V", {"2026-08-25": 69.7, "2026-08-26": 116.4, "2026-08-28": 44.1, "2026-08-29": 103, "2026-08-31": 104.6, "2026-09-01": 86.8}),
    "Vijayakumar S": ("Vijayakumar S", {"2026-08-25": 93.1, "2026-08-26": 62.1, "2026-08-27": 64.1, "2026-08-28": 57.4, "2026-08-31": 76.2, "2026-09-01": 89}),
    "Vijayakumar R": ("VIJAYAKUMAR.R Arakkonam", {"2026-08-25": 27.4, "2026-08-26": 103.8, "2026-08-27": 70.2, "2026-08-28": 100.1, "2026-08-29": 88.9}),
    "Lingeshwaran M": ("Lingeshwaran", {"2026-08-25": 139.1, "2026-08-26": 68.4, "2026-08-27": 50.8, "2026-08-28": 71, "2026-08-29": 153, "2026-08-31": 101.8, "2026-09-01": 178.8}),
    "Lava kumar V": ("Lavakumar V", {"2026-08-25": 113.8, "2026-08-26": 162.5, "2026-08-28": 111.7, "2026-08-29": 94.7, "2026-08-31": 192.9, "2026-09-01": 167.5}),
    "Mohan": ("MOHAN R", {"2026-08-25": 40, "2026-08-26": 61, "2026-08-27": 13.6, "2026-08-28": 63, "2026-08-29": 62.5, "2026-09-01": 54.1}),
    "samim": ("Samim K", {"2026-08-25": 35.1, "2026-08-27": 26.5, "2026-08-28": 55.8, "2026-08-29": 26.7, "2026-08-31": 62.1, "2026-09-01": 63.8}),
    "Kausar Basha": ("Kausarbadshah M", {"2026-08-25": 29.1, "2026-08-26": 77.6, "2026-08-27": 91.3, "2026-08-28": 79.5, "2026-08-29": 50.7, "2026-08-31": 32.5, "2026-09-01": 10.5}),
    "vignesh raja": ("Vignesh R", {"2026-08-25": 96.2, "2026-08-26": 79.6, "2026-08-27": 23.9, "2026-08-28": 11.3, "2026-08-29": 14.4, "2026-08-31": 10.4, "2026-09-01": 43.7}),
    "Vijay Anand": ("Vijayananth M", {"2026-08-25": 22, "2026-08-26": 186.2, "2026-08-27": 95.3, "2026-08-28": 25.5, "2026-08-29": 195.4, "2026-09-01": 191.6}),
}

# The KM report's name -> the km it showed for them on three September days,
# to one decimal (read from OpenCall on 26 Oct 2026). Who they are in Payroll.
FINGERPRINT = {
    "Santhosh kumar S": {"2026-09-02": 58.4, "2026-09-03": 65.3, "2026-09-04": 32.6},
    "Sarwana N": {"2026-09-03": 98.4, "2026-09-04": 13.7, "2026-09-05": 24.7},
    "Vishnu S": {"2026-09-02": 53.5, "2026-09-03": 44.7, "2026-09-04": 110.8},
    "Jayakumar": {"2026-09-02": 70.1, "2026-09-03": 52.1, "2026-09-04": 147.2},
    "Prasanth": {"2026-09-03": 68.9, "2026-09-04": 56.5, "2026-09-05": 66.8},
    "Perumal": {"2026-09-03": 76.4, "2026-09-04": 84.4, "2026-09-05": 98.7},
    "Vijayakumar S": {"2026-09-03": 77.6, "2026-09-04": 84.6, "2026-09-05": 225.6},
    "Vijayakumar R": {"2026-09-03": 79.1, "2026-09-04": 68.8, "2026-09-07": 44.7},
    "Lingeshwaran M": {"2026-09-03": 36.7, "2026-09-04": 81.5, "2026-09-07": 79.6},
    "Lava kumar V": {"2026-09-02": 66.9, "2026-09-03": 115.5, "2026-09-04": 121.3},
    "Mohan": {"2026-09-02": 15.6, "2026-09-03": 38.4, "2026-09-07": 18.7},
    "samim": {"2026-09-04": 73.8, "2026-09-05": 27.2, "2026-09-07": 46.2},
    "Kausar Basha": {"2026-09-02": 43.3, "2026-09-03": 25.7, "2026-09-04": 32.6},
    "vignesh raja": {"2026-09-03": 3.5, "2026-09-04": 100.4, "2026-09-05": 2.0},
    "Vijay Anand": {"2026-09-03": 6.9, "2026-09-04": 15.4, "2026-09-05": 143.8},
}

# How far Payroll's figure may sit from the one-decimal figure above, whether
# that was rounded or cut to one decimal.
TOLERANCE = 0.1


def _days(table):
    return sorted({day for kms in table for day in kms})


class Command(BaseCommand):
    help = "Import the km of 25 Aug - 1 Sep 2026 from the old app's Monthly Report."

    def add_arguments(self, parser):
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without it, nothing is saved.",
        )
        parser.add_argument(
            "--remove",
            action="store_true",
            help="Take this import out again instead.",
        )

    def handle(self, *args, **options):
        apply_changes = bool(options.get("apply"))
        if options.get("remove"):
            self._remove(apply_changes)
            return

        employees, september = self._identify()
        ids = [employee.id for employee in employees.values()]
        days = _days(kms for _, kms in KM.values())

        # What the app recorded itself on each day, and what is imported already.
        app = {}
        for day in days:
            day_km = _DayKm(ids, date.fromisoformat(day))
            for engineer_id in ids:
                app[engineer_id, day] = day_km.trail_km(engineer_id)
        existing = {
            (row.engineer_id, row.day.isoformat()): row
            for row in ImportedKmDay.objects.filter(engineer_id__in=ids, day__in=days)
        }

        writes = []
        refused = []
        for name, (report_name, kms) in KM.items():
            employee = employees[name]
            theirs = " ".join(f"{km:.2f}" for km in september[name])
            shown = " ".join(f"{km:.1f}" for km in FINGERPRINT[name].values())
            self.stdout.write(
                f"\n{name} (report: {report_name}) -> #{employee.id} "
                f"{employee.employee_name} ({employee.branch}); "
                f"September km {theirs} = KM report {shown}"
            )
            for day, km in kms.items():
                own = app[employee.id, day]
                row = existing.get((employee.id, day))
                if own > km:
                    note = "REFUSED: the app has more"
                    refused.append(f"{name} {day}: the app has {own} km, the report {km}")
                elif row is None:
                    note = "new"
                    writes.append((employee, day, km))
                elif row.km != km:
                    note = f"was {row.km:g}"
                    writes.append((employee, day, km))
                else:
                    note = "already imported"
                self.stdout.write(f"   {day}   app {own:6.2f} -> {km:6.1f}   {note}")
            self.stdout.write(f"   total {sum(kms.values()):.1f} km")

        total = sum(sum(kms.values()) for _, kms in KM.values())
        cells = sum(len(kms) for _, kms in KM.values())
        self.stdout.write(f"\n{cells} day(s) for {len(KM)} engineer(s), {total:.1f} km in all.")
        if refused:
            raise CommandError(
                "STOP: the app already has more km on these days. Nothing was saved.\n"
                + "\n".join(refused)
            )
        if not writes:
            self.stdout.write(self.style.SUCCESS("Nothing to do: every day is imported already."))
            return
        if not apply_changes:
            self.stdout.write(f"Would write {len(writes)} day(s). Nothing was saved. Run it again with --apply.")
            return
        with transaction.atomic():
            for employee, day, km in writes:
                ImportedKmDay.objects.update_or_create(
                    engineer=employee,
                    day=date.fromisoformat(day),
                    defaults={"km": km, "source": SOURCE},
                )
        self.stdout.write(self.style.SUCCESS(f"IMPORTED: wrote {len(writes)} day(s)."))

    def _identify(self):
        """Each KM report name's Payroll employee: the one, and only one, whose
        km on its September days is what the KM report showed for that name.
        Returned with those km, as Payroll has them."""
        seen = {}
        for day in _days(FINGERPRINT.values()):
            the_day = date.fromisoformat(day)
            start, end = km_rule.day_bounds(the_day)
            ids = list(
                LocationPing.objects.filter(timestamp__gte=start, timestamp__lt=end)
                .order_by()
                .values_list("engineer_id", flat=True)
                .distinct()
            )
            day_km = _DayKm(ids, the_day)
            for engineer_id in ids:
                seen.setdefault(engineer_id, {})[day] = day_km.km(engineer_id)

        found = {}
        problems = []
        for name, kms in FINGERPRINT.items():
            matches = [
                engineer_id
                for engineer_id, theirs in seen.items()
                if all(abs(theirs.get(day, 0.0) - km) <= TOLERANCE for day, km in kms.items())
            ]
            if len(matches) == 1:
                found[name] = matches[0]
            else:
                problems.append(f"{name}: {len(matches)} employee(s) have the September km {kms}")
        names_by_id = {}
        for name, engineer_id in found.items():
            names_by_id.setdefault(engineer_id, []).append(name)
        for engineer_id, names in names_by_id.items():
            if len(names) > 1:
                problems.append(f"{' and '.join(names)} are the same employee #{engineer_id}")
        if problems:
            raise CommandError(
                "STOP: could not tell who is who. Nothing was saved.\n" + "\n".join(problems)
            )
        employees = Employee.objects.in_bulk(found.values())
        return (
            {name: employees[engineer_id] for name, engineer_id in found.items()},
            {
                name: [seen[engineer_id].get(day, 0.0) for day in FINGERPRINT[name]]
                for name, engineer_id in found.items()
            },
        )

    def _remove(self, apply_changes):
        rows = list(
            ImportedKmDay.objects.filter(source=SOURCE)
            .select_related("engineer")
            .order_by("engineer__employee_name", "day")
        )
        for row in rows:
            self.stdout.write(f"{row.engineer.employee_name:<28} {row.day}  {row.km:g} km")
        if not rows:
            self.stdout.write("Nothing to take out: no day of this import is stored.")
            return
        if not apply_changes:
            self.stdout.write(f"Would take out {len(rows)} day(s). Nothing was saved. Run it again with --apply.")
            return
        deleted, _ = ImportedKmDay.objects.filter(pk__in=[row.pk for row in rows]).delete()
        self.stdout.write(self.style.SUCCESS(f"REMOVED: {deleted} day(s)."))
