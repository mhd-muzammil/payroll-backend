"""Close the day for people who forgot to press Logout.

They press Login in the morning and the day never gets closed: the register
keeps `outtime` empty, the row reads 0.0h for good, and the only way to fix it
is somebody editing the record by hand. The office asked for the obvious rule
-- if you logged in and never logged out, your day is closed at 11.59pm.

ONE DAY at a time, and by default the day it is run in. That is deliberate:

  * 11.59pm of the day the person logged in, never "now" -- run late, or run
    for an earlier date, and the register still says 11.59pm, not the time the
    command happened to run;
  * only rows with a real login and no logout. An Absent or Leave day is
    STORED as midnight with no logout -- that is how a day with no punch is
    written -- and stamping 11.59pm on those would turn every absence in the
    month into a fifteen-hour shift. They are excluded by status, and the
    tests pin it;
  * nothing already closed is touched, so running it twice changes nothing;
  * and no earlier day is swept unless you ask for it by date. An imported
    sheet leaves plenty of old rows with no logout, and quietly stamping those
    would rewrite months of hours that nobody asked about.

    python manage.py close_forgotten_logouts                 # today
    python manage.py close_forgotten_logouts --dry-run       # show, change nothing
    python manage.py close_forgotten_logouts --date 2026-09-08

Scheduled nightly at 11.59pm IST. Note for whoever sets that up: the server
runs on UTC, so 11.59pm IST is 18:29 UTC -- `29 18 * * *`. The command reads
the day in IST itself, so it files the rows on the right date either way.

Duty is NOT touched here. An engineer's duty session already closes itself
(DutySession.MAX_DURATION_HOURS), at their last known position rather than at
a clock time, which is the honest answer for a trail.
"""

from datetime import datetime, time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from attendance.models import Attendance

# A day with no punch is stored as midnight with no logout -- see the docstring.
# These are the statuses that means, and they are never closed.
NO_PUNCH_STATUSES = ("Absent", "Leave")

# The minute the day is closed at. Not midnight: midnight belongs to the next
# day, and every list on the page groups by the date it reads off a punch.
CLOSING_TIME = time(23, 59)


class Command(BaseCommand):
    help = "Close attendance rows for one day where somebody logged in and never logged out."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="The day to close, YYYY-MM-DD. Defaults to today (IST).",
        )
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="List what would be closed and change nothing.",
        )

    def handle(self, *args, **options):
        if options.get("date"):
            try:
                day = datetime.strptime(options["date"], "%Y-%m-%d").date()
            except ValueError as exc:
                raise CommandError("--date must be YYYY-MM-DD") from exc
        else:
            day = timezone.localdate()

        closing = timezone.make_aware(datetime.combine(day, CLOSING_TIME))

        forgotten = list(
            Attendance.objects.filter(
                intime__date=day,
                intime__isnull=False,
                outtime__isnull=True,
            )
            .exclude(status__in=NO_PUNCH_STATUSES)
            .order_by("employee_name")
        )

        if not forgotten:
            self.stdout.write(f"{day}: everybody who logged in logged out.")
            return

        for row in forgotten:
            self.stdout.write(
                "  %-28s logged in %s%s"
                % (
                    row.employee_name or "(no name)",
                    timezone.localtime(row.intime).strftime("%I:%M %p").lstrip("0"),
                    "" if row.status == "Present" else f"  [{row.status}]",
                )
            )

        if options["dry_run"]:
            self.stdout.write(
                self.style.NOTICE(
                    "\nDry run. %d row%s would be closed at 11.59pm on %s."
                    % (len(forgotten), "" if len(forgotten) == 1 else "s", day)
                )
            )
            return

        for row in forgotten:
            row.outtime = closing
            row.auto_closed_out = True
        Attendance.objects.bulk_update(forgotten, ["outtime", "auto_closed_out"])

        self.stdout.write(
            self.style.SUCCESS(
                "\nClosed %d row%s at 11.59pm on %s."
                % (len(forgotten), "" if len(forgotten) == 1 else "s", day)
            )
        )
