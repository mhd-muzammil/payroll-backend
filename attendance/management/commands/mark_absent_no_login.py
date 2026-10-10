"""Mark everybody who has not logged in by 10am Absent.

The office's rule, run once a day at 10am. Who counts, and every way somebody
is left out, is in attendance/absence.py -- the WhatsApp reminders at 9:11 and
9:37 read the same list, so the people they name are the people this marks.

TODAY ONLY, and never before 10am. An Absent row is a day's pay, so this does
not take a date: run for a past day it would mark Absent everybody whose
attendance was simply never entered, and run at 9am it would mark everybody
still on their way in. Both are refused rather than left to a cron line.

Somebody who logs in after 10am stays Absent for the day -- the office's
decision -- but the Login itself is accepted and recorded on their row, so
the app still starts their duty and their km. See AttendanceViewSet.check_in.

    python manage.py mark_absent_no_login             # mark today's no-shows
    python manage.py mark_absent_no_login --dry-run   # say who, change nothing

Scheduled daily at 10am IST. The server runs on UTC, so that is 04:30 UTC --
`30 4 * * *`. The command reads the day and the time in IST itself.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from attendance.absence import CUTOFF, mark_absent, reason_to_skip


class Command(BaseCommand):
    help = "Mark everybody who has not logged in by 10am today Absent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Say who would be marked and write nothing.",
        )

    def handle(self, *args, **options):
        now = timezone.localtime()
        day = now.date()
        if now.time() < CUTOFF:
            raise CommandError(
                f"It is {now:%H:%M}. Nobody is marked Absent before {CUTOFF:%H:%M} -- "
                "they still have time to log in."
            )

        skip = reason_to_skip(day)
        if skip:
            self.stdout.write(f"{day}: nobody marked -- {skip}.")
            return

        dry_run = options["dry_run"]
        people = mark_absent(day, dry_run=dry_run)
        verb = "would be marked" if dry_run else "marked"
        self.stdout.write(f"{day}: {len(people)} {verb} Absent (no Login by {CUTOFF:%H:%M}).")

        by_branch = {}
        for person in people:
            by_branch.setdefault(person.branch or "Chennai", []).append(person.employee_name)
        for branch in sorted(by_branch):
            names = by_branch[branch]
            self.stdout.write(f"    {branch} ({len(names)}): {', '.join(names)}")
