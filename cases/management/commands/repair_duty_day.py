"""Write the duty session a day is missing, for engineers who plainly worked it.

Needed once, for the day the bug was found on. An engineer who never tapped
Logout left last night's session open; the app read that as "already on duty",
so Login wrote no session for today, and the office's board -- which asks for
sessions STARTED today -- showed them off duty all day beside a live position
and a climbing distance.

Login fixes itself from the next one onward (cases/views.py start_duty closes a
forgotten session and opens one dated today). It cannot fix the day already in
progress: those engineers have punched in, so their app offers Logout, not
Login, and pressing it would end their shift.

Judged only on what the day itself shows: an attendance punch, or a position
sent. Both come from the engineer, neither is guessed. An engineer with a
session already, or with no sign of the day at all, is left alone.

    python manage.py repair_duty_day             # say what it would do
    python manage.py repair_duty_day --apply     # do it
    python manage.py repair_duty_day --date 2026-09-03 --apply
"""

from datetime import datetime, time

from django.core.management.base import BaseCommand
from django.db.models import Q
from django.utils import timezone

from attendance.models import Attendance
from cases.models import DutySession, LocationPing
from employees.models import Employee


class Command(BaseCommand):
    help = "Open the missing duty session for engineers who worked a day without one."

    def add_arguments(self, parser):
        parser.add_argument(
            "--date",
            help="The day to repair, YYYY-MM-DD. Defaults to today.",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Write the changes. Without it, nothing is saved.",
        )

    def handle(self, *args, **options):
        day = timezone.localdate()
        if options.get("date"):
            day = datetime.strptime(options["date"], "%Y-%m-%d").date()
        apply_changes = bool(options.get("apply"))

        tz = timezone.get_current_timezone()
        day_start = timezone.make_aware(datetime.combine(day, time.min), tz)

        # Everyone the day itself says was working.
        worked = Employee.objects.filter(
            Q(attendances__intime__date=day) | Q(location_pings__timestamp__date=day)
        ).distinct()

        repaired = 0
        skipped = 0
        for employee in worked.order_by("employee_name"):
            if DutySession.objects.filter(engineer=employee, started_at__date=day).exists():
                skipped += 1
                continue

            attendance = (
                Attendance.objects.filter(employee=employee, intime__date=day)
                .order_by("intime")
                .first()
            )
            first_ping = (
                LocationPing.objects.filter(engineer=employee, timestamp__date=day)
                .order_by("timestamp")
                .first()
            )
            # The earliest thing they actually did that day: the punch usually,
            # a position if they were tracking before punching in.
            candidates = [c for c in (attendance.intime if attendance else None,
                                      first_ping.timestamp if first_ping else None) if c]
            if not candidates:
                skipped += 1
                continue
            started_at = min(candidates)

            # A session left open from an earlier day is what caused this. Close
            # it at its own last fix before the day began -- stamping it with
            # this morning would record a night nobody worked.
            stale = (
                DutySession.objects.filter(
                    engineer=employee, ended_at__isnull=True, started_at__lt=day_start
                )
                .order_by("-started_at")
                .first()
            )
            stale_note = ""
            if stale:
                last = (
                    LocationPing.objects.filter(
                        engineer=employee,
                        timestamp__gte=stale.started_at,
                        timestamp__lt=day_start,
                    )
                    .order_by("-timestamp")
                    .first()
                )
                ends_at = last.timestamp if last else stale.started_at
                stale_note = (
                    f" | closes #{stale.pk} "
                    f"({timezone.localtime(stale.started_at):%d-%m %H:%M}"
                    f" -> {timezone.localtime(ends_at):%d-%m %H:%M})"
                )
                if apply_changes:
                    stale.ended_at = ends_at
                    stale.auto_closed = True
                    stale.save(update_fields=["ended_at", "auto_closed"])

            # If they have already punched out, the day is over: record it as a
            # finished session rather than leaving one open behind them.
            ended_at = attendance.outtime if attendance and attendance.outtime else None

            if apply_changes:
                DutySession.objects.create(
                    engineer=employee,
                    started_at=started_at,
                    ended_at=ended_at,
                    auto_closed=bool(ended_at),
                )

            finish = (
                f"{timezone.localtime(ended_at):%H:%M}" if ended_at else "still on duty"
            )
            self.stdout.write(
                f"{employee.employee_name:<28} "
                f"{timezone.localtime(started_at):%H:%M} -> {finish}"
            )
            if stale_note:
                self.stdout.write(f"{'':<28} {stale_note.strip(' |')}")
            repaired += 1

        verb = "Repaired" if apply_changes else "Would repair"
        self.stdout.write(
            self.style.SUCCESS(
                f"\n{verb} {repaired} duty session(s) for {day}; {skipped} already fine."
            )
        )
        if not apply_changes and repaired:
            self.stdout.write("Nothing was saved. Run it again with --apply.")
