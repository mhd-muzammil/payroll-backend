"""Give the rows the office marked by hand their employee back.

Mark Attendance and Add Record post a NAME. The serializer had `employee` in
read_only_fields, so the link was never written and the row was saved belonging
to nobody. Read back it has no employee id, no email, and a branch of "Chennai"
whoever the person is -- so the Attendance page listed the same employee twice,
one card for the days somebody punched and another for the day the office
marked, and a Hosur absence was counted against Chennai.

New rows are linked as they are saved now. This is for the ones already in the
table.

Matching is by name, and only an unambiguous match is taken: one active
employee with that name, else one employee with that name, else the row is left
exactly as it is and printed as ambiguous or unknown. Two people with one name
is precisely where a guess does damage -- it would move somebody's absence onto
a colleague's record.

    python manage.py link_attendance_rows                  # dry run, changes nothing
    python manage.py link_attendance_rows --apply
    python manage.py link_attendance_rows --name "Vijayananth M" --apply

Nothing is deleted and nothing else on a row is touched: only the employee
link, and only where it is empty.
"""

from collections import defaultdict

from django.core.management.base import BaseCommand
from django.db import transaction
from django.utils import timezone

from attendance.models import Attendance
from employees.models import Employee


def _rows(count):
    return "1 row" if count == 1 else "%d rows" % count


class Command(BaseCommand):
    help = "Link attendance rows that were saved without an employee, by name."

    def add_arguments(self, parser):
        parser.add_argument(
            "--name",
            help="Only rows with this employee_name (exact, case-insensitive).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the links. Without it, nothing is saved.",
        )

    def handle(self, *args, **options):
        rows = Attendance.objects.filter(employee__isnull=True)
        if options.get("name"):
            rows = rows.filter(employee_name__iexact=options["name"].strip())
        rows = list(rows.order_by("employee_name", "intime"))

        if not rows:
            self.stdout.write("Every attendance row already belongs to an employee.")
            return

        # One lookup per distinct name rather than per row: a year of a
        # branch's absences is thousands of rows and a handful of names.
        by_name = defaultdict(list)
        for row in rows:
            by_name[(row.employee_name or "").strip().lower()].append(row)

        linkable = {}
        ambiguous = {}
        unknown = {}
        for name, group in by_name.items():
            if not name:
                unknown[name] = group
                continue
            named = Employee.objects.filter(employee_name__iexact=name)
            active = list(named.exclude(status="relieved")[:3])
            matches = active if len(active) == 1 else list(named[:3])
            if len(matches) == 1:
                linkable[name] = (matches[0], group)
            elif matches:
                ambiguous[name] = (matches, group)
            else:
                unknown[name] = group

        self.stdout.write(
            "Attendance rows belonging to nobody: %d, across %d name%s."
            % (len(rows), len(by_name), "" if len(by_name) == 1 else "s")
        )

        for name, (employee, group) in sorted(linkable.items()):
            # Local dates. A midnight-IST row is the previous day in UTC, and
            # printing that would send somebody to check the wrong day.
            days = ", ".join(
                sorted(
                    {
                        timezone.localtime(r.intime or r.outtime).date().isoformat()
                        for r in group
                        if (r.intime or r.outtime)
                    }
                )
            )
            self.stdout.write(
                "  link  %-28s -> %s (%s, id %d)  %s%s"
                % (
                    group[0].employee_name or "(blank)",
                    employee.employee_name,
                    employee.branch,
                    employee.id,
                    _rows(len(group)),
                    ("  " + days) if days else "",
                )
            )

        for name, (matches, group) in sorted(ambiguous.items()):
            where = ", ".join("%s/%s id %d" % (e.employee_name, e.branch, e.id) for e in matches)
            self.stdout.write(
                self.style.WARNING(
                    "  skip  %-28s %s: %d employees have this name (%s)"
                    % (name, _rows(len(group)), len(matches), where)
                )
            )

        for name, group in sorted(unknown.items()):
            self.stdout.write(
                self.style.WARNING(
                    "  skip  %-28s %s: no employee has this name"
                    % (name or "(blank)", _rows(len(group)))
                )
            )

        to_write = [(employee, group) for employee, group in linkable.values()]
        total = sum(len(group) for _, group in to_write)

        if not options["apply"]:
            self.stdout.write(
                self.style.NOTICE(
                    "\nDry run. %d rows would be linked. Re-run with --apply to write them."
                    % total
                )
            )
            return

        with transaction.atomic():
            for employee, group in to_write:
                for row in group:
                    row.employee = employee
                Attendance.objects.bulk_update(group, ["employee"])

        self.stdout.write(self.style.SUCCESS("\nLinked %s." % _rows(total)))
