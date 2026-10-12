"""Mark everybody who has not logged in by 10:30am Absent -- by hand.

The office's rule. Day to day it is not run from here: the WhatsApp poster
asks for the same marking (POST /api/attendance/mark_absent/) at 10:30, just
before its 10:30 report, so the report always comes after it. This is the
same thing for a terminal -- on a day the poster was down, say. Who counts,
and every way somebody is left out, is in attendance/absence.py.

TODAY ONLY, and never before 10:30. An Absent row is a day's pay, so this
does not take a date: run for a past day it would mark Absent everybody whose
attendance was simply never entered, and run at 9am it would mark everybody
still on their way in. Running it again marks nobody twice.

Somebody who logs in after 10:30 stays Absent for the day -- the office's
decision -- but the Login itself is accepted and recorded on their row, so
the app still starts their duty and their km. See AttendanceViewSet.check_in.

    python manage.py mark_absent_no_login             # mark today's no-shows
    python manage.py mark_absent_no_login --dry-run   # say who, change nothing
"""

from django.core.management.base import BaseCommand, CommandError

from attendance.absence import CUTOFF, TooEarly, mark_today


class Command(BaseCommand):
    help = "Mark everybody who has not logged in by 10:30am today Absent."

    def add_arguments(self, parser):
        parser.add_argument(
            "--dry-run",
            action="store_true",
            help="Say who would be marked and write nothing.",
        )

    def handle(self, *args, **options):
        dry_run = options["dry_run"]
        try:
            day, skip, people = mark_today(dry_run=dry_run)
        except TooEarly as err:
            raise CommandError(str(err))

        if skip:
            self.stdout.write(f"{day}: nobody marked -- {skip}.")
            return

        verb = "would be marked" if dry_run else "marked"
        self.stdout.write(f"{day}: {len(people)} {verb} Absent (no Login by {CUTOFF:%H:%M}).")

        by_branch = {}
        for person in people:
            by_branch.setdefault(person.branch or "Chennai", []).append(person.employee_name)
        for branch in sorted(by_branch):
            names = by_branch[branch]
            self.stdout.write(f"    {branch} ({len(names)}): {', '.join(names)}")
