"""Sunday is a day off, so the register should say so without anybody typing it.

Nobody is asked to mark a weekly off, and nobody does: Sunday came out of the
attendance page as a blank column, which reads as a day the office forgot
rather than a day the company was shut. The Excel import has always written
Sunday as Leave for anybody with no punch on it -- this is the same rule for
the days nobody imports.

ONE SUNDAY at a time, and by default the most recent one. The rules are the
same shape as close_forgotten_logouts, and for the same reasons:

  * a row that already exists is never touched. Somebody who actually came in
    on a Sunday has a punch, and an engineer's real day must not be rewritten
    into a holiday -- that is the import's oldest rule (a real punch always
    wins) and it is the one that matters most here;
  * only Sundays. Handed any other date the command refuses rather than
    marking a working day as leave for the whole company -- a cron line typed
    one field out of place would otherwise do exactly that;
  * only people who were employed on the day. Running this over past months
    would otherwise invent attendance for somebody who had not joined yet;
  * anybody relieved is left out, as they are on every working screen;
  * and running it twice changes nothing, so a cron that fires again, or a
    catch-up after a week of downtime, is safe.

    python manage.py mark_sunday_leave                  # the most recent Sunday
    python manage.py mark_sunday_leave --dry-run        # show, change nothing
    python manage.py mark_sunday_leave --date 2026-10-04
    python manage.py mark_sunday_leave --since 2026-09-01   # every Sunday from then

Scheduled weekly. The server runs on UTC and the command reads the day in IST,
so Monday 00:30 IST is `0 19 * * 0` in UTC -- a Sunday evening in UTC, which
is already Monday in India, with the Sunday itself safely finished. Running it
on the Sunday would be fine too; it simply means somebody punching in later
that day already has their row and keeps it.
"""

from datetime import datetime, time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from attendance.models import Attendance
from employees.models import Employee

SUNDAY = 6  # datetime.weekday()

# A day with no punch is stored as midnight on that date, with no logout. That
# is what the Add Record form writes, what the Excel import writes, and what
# every list on the page reads the date from.
DAY_MARK = time(0, 0)


def _sunday_on_or_before(day):
    """The most recent Sunday, which is `day` itself when that is one."""
    return day - timedelta(days=(day.weekday() - SUNDAY) % 7)


class Command(BaseCommand):
    help = "Mark every employee Leave on a Sunday, except those who already have a record."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="The Sunday to mark, YYYY-MM-DD. Defaults to the most recent one (IST).",
        )
        parser.add_argument(
            "--since",
            help="Mark every Sunday from this date up to the most recent one.",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Say what would be written and write nothing.",
        )

    def handle(self, *args, **options):
        today = timezone.localdate()
        last_sunday = _sunday_on_or_before(today)

        if options.get("date") and options.get("since"):
            raise CommandError("Use either --date or --since, not both.")

        if options.get("date"):
            try:
                day = datetime.strptime(options["date"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("--date has to be YYYY-MM-DD.")
            if day.weekday() != SUNDAY:
                raise CommandError(
                    f"{day} is a {day.strftime('%A')}, not a Sunday. "
                    "This command marks the whole company as on leave; it will not do that to a working day."
                )
            sundays = [day]
        elif options.get("since"):
            try:
                start = datetime.strptime(options["since"], "%Y-%m-%d").date()
            except ValueError:
                raise CommandError("--since has to be YYYY-MM-DD.")
            if start > last_sunday:
                raise CommandError(f"No Sunday between {start} and {last_sunday}.")
            first = start + timedelta(days=(SUNDAY - start.weekday()) % 7)
            sundays = []
            while first <= last_sunday:
                sundays.append(first)
                first += timedelta(days=7)
        else:
            sundays = [last_sunday]

        dry_run = options["dry_run"]
        total = 0
        for sunday in sundays:
            total += self._mark(sunday, dry_run)

        if len(sundays) > 1:
            self.stdout.write(
                f"{len(sundays)} Sundays, {total} {'would be ' if dry_run else ''}marked Leave."
            )
        return None

    def _mark(self, sunday, dry_run):
        # Everybody still on the books. 'inactive' and 'onleave' are people who
        # are still employed, and their Sunday is a Sunday like anybody else's.
        people = Employee.objects.exclude(status="relieved").order_by("employee_name")

        # Whoever already has something on that date keeps it, whatever it says.
        #
        # By name as well as by id: a row the office marked by hand can be
        # saved belonging to nobody (see _link_office_record), and matching
        # only on the id would give that person a second row for the same day.
        on_the_day = Attendance.objects.filter(
            Q(intime__date=sunday) | Q(intime__isnull=True, outtime__date=sunday)
        )
        taken = set(
            on_the_day.exclude(employee=None).values_list("employee_id", flat=True)
        )
        taken_names = {
            (name or "").strip().lower()
            for name in on_the_day.filter(employee=None).values_list("employee_name", flat=True)
        }
        taken_names.discard("")

        marked_at = timezone.make_aware(datetime.combine(sunday, DAY_MARK))
        rows = []
        skipped_joined = 0
        for person in people:
            if person.id in taken:
                continue
            if person.employee_name.strip().lower() in taken_names:
                continue
            # Not yet employed on that Sunday. The real hire date only --
            # NOT `joining_date`, which is stamped when the record is created
            # and says nothing about when the person started. Read as a hire
            # date it would skip the whole company on any Sunday before the
            # system was set up, and quietly: a blank column nobody can
            # explain is exactly what this command exists to stop.
            joined = person.date_of_joining
            if joined and sunday < joined:
                skipped_joined += 1
                continue
            rows.append(
                Attendance(
                    employee=person,
                    employee_name=person.employee_name,
                    role=person.role,
                    department=person.department,
                    salary=person.salary,
                    intime=marked_at,
                    outtime=None,
                    status="Leave",
                )
            )

        kept = len(taken) + len(taken_names)
        if dry_run:
            self.stdout.write(
                f"{sunday}: would mark {len(rows)} Leave, {kept} already have a record"
                + (f", {skipped_joined} had not joined" if skipped_joined else "")
            )
            for row in rows[:10]:
                self.stdout.write(f"    {row.employee_name}")
            if len(rows) > 10:
                self.stdout.write(f"    ... and {len(rows) - 10} more")
            return len(rows)

        with transaction.atomic():
            Attendance.objects.bulk_create(rows)
        self.stdout.write(
            f"{sunday}: {len(rows)} marked Leave, {kept} already had a record"
            + (f", {skipped_joined} had not joined" if skipped_joined else "")
        )
        return len(rows)
