"""Who has not logged in today -- and, at 10:30am, marking them Absent.

The office's rule: anybody who has not pressed Login by 10:30am is Absent for
the day. (It was 10:00 for the first two days; the office moved it.) Leave
applied for in the app does not change that; the office corrects a leave day
by hand.

One rule, read in two places that have to agree. The WhatsApp reminders at
9:13, 9:38 and 10:05 name the people who have not logged in yet, and the
10:30 marking marks those same people Absent. A reminder that named somebody
the marking then skipped, or a marking of somebody no reminder had named,
would be worse than having neither -- so both ask `not_logged_in`, and
nothing else.

The marking is asked for by the WhatsApp poster, at 10:30, just before its
10:30 report -- so the report can never go out ahead of it -- through the
mark_absent endpoint. `manage.py mark_absent_no_login` does the same by hand.

Absent is not a label here, it is pay: a payslip's loss-of-pay days are the
count of Absent (and Off Day) rows in its cycle. So every rule below is about who must NOT
be marked:

  * Sunday. It is a day off, marked as one (Leave, shown as "Sunday") by
    mark_sundays below -- the same 10:30 call marks the cycle's Sundays that
    are over, so the Monday's marking writes the Sunday just gone.
  * A day nobody at all has logged in. That is a holiday, or the app was down,
    and a whole company of Absent rows is the one mistake that must not happen.
  * Anybody who could not have logged in: not 'active' (inactive, relieved,
    on leave in the employee record), not joined yet, no app account, or an
    account with no Login button (admin accounts).
  * Anybody who already has anything on the day -- a Login, or a row the
    office marked -- whatever it says.
"""

from datetime import datetime, time, timedelta

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from attendance.models import Attendance
from employees.models import Employee

# Not logged in by then is Absent.
CUTOFF = time(10, 30)

SUNDAY = 6  # datetime.weekday()

# A day with no punch is stored as midnight on that date with no logout -- the
# same as the Add Record form, the Excel import and the Sunday rule write it.
DAY_MARK = time(0, 0)

# The statuses a no-punch day is written with. Off Day is a day off the office
# marks: nobody punches on it, and a row of it is not somebody having worked.
NO_PUNCH_STATUSES = ("Absent", "Leave", "Off Day")

# App accounts that have a Login button. The admin accounts do not.
CAN_LOG_IN = ("employee", "hr")


def is_day_mark(row):
    """Whether this row holds only a date -- an Absent, Leave or Off Day day
    nobody punched -- rather than a Login."""
    if row.status not in NO_PUNCH_STATUSES or row.intime is None:
        return False
    return timezone.localtime(row.intime).time() == DAY_MARK


def expected(day):
    """Everybody who should have logged in on `day`."""
    return (
        Employee.objects.filter(
            status="active",
            user__isnull=False,
            user__is_active=True,
            user__role__in=CAN_LOG_IN,
        )
        # The real hire date only -- `joining_date` is stamped when the record
        # is made and says nothing about when the person started.
        .exclude(date_of_joining__gt=day)
        .order_by("employee_name")
    )


def not_logged_in(day):
    """Of those, the people with nothing at all on `day`."""
    on_the_day = Attendance.objects.filter(
        Q(intime__date=day) | Q(intime__isnull=True, outtime__date=day)
    )
    taken = set(on_the_day.exclude(employee=None).values_list("employee_id", flat=True))
    # By name as well as by id: a row the office marked by hand can be saved
    # belonging to nobody, and matching only on the id would give that person
    # a second row for the same day.
    taken_names = {
        (name or "").strip().lower()
        for name in on_the_day.filter(employee=None).values_list("employee_name", flat=True)
    }
    taken_names.discard("")
    return [
        person
        for person in expected(day)
        if person.id not in taken and person.employee_name.strip().lower() not in taken_names
    ]


def reason_to_skip(day):
    """Why nobody is to be chased or marked on `day`; None on a working day."""
    if day.weekday() == SUNDAY:
        return "Sunday"
    worked = (
        Attendance.objects.filter(intime__date=day)
        .exclude(status__in=NO_PUNCH_STATUSES)
        .exists()
    )
    if not worked:
        return "nobody has logged in today -- a holiday, or the app was down"
    return None


def mark_absent(day, dry_run=False):
    """Write an Absent row for everybody in `not_logged_in(day)`.

    Returns the people marked (or who would be, on a dry run). Running it
    again marks nobody twice: whoever was marked has a row now.
    """
    people = not_logged_in(day)
    if dry_run or not people:
        return people

    marked_at = timezone.make_aware(datetime.combine(day, DAY_MARK))
    with transaction.atomic():
        Attendance.objects.bulk_create(
            [
                Attendance(
                    employee=person,
                    employee_name=person.employee_name,
                    role=person.role,
                    department=person.department,
                    salary=person.salary,
                    intime=marked_at,
                    outtime=None,
                    status="Absent",
                )
                for person in people
            ]
        )
    return people


def cycle_start(day):
    """The 25th that opened the pay cycle `day` falls in."""
    if day.day >= 25:
        return day.replace(day=25)
    return (day.replace(day=1) - timedelta(days=1)).replace(day=25)


def sundays_gone_by(today):
    """Every Sunday of the current pay cycle that is already over.

    Over, so never today: on the Sunday itself somebody may still come in,
    and their day is a Login, not a day off -- the Sunday is marked the next
    time anybody asks, which is the Monday's 10:30 marking. The whole cycle
    rather than the last Sunday alone, so a week the poster was down is caught
    up, and a register started mid-cycle fills its earlier Sundays.
    """
    start = cycle_start(today)
    sunday = start + timedelta(days=(SUNDAY - start.weekday()) % 7)
    sundays = []
    while sunday < today:
        sundays.append(sunday)
        sunday += timedelta(days=7)
    return sundays


def mark_sunday(day, dry_run=False):
    """Write a Sunday for everybody in `not_logged_in(day)` -- the same people
    the 10:30 rule would mark Absent on a working day, so the week reads as one
    list of people.

    Stored as Leave at midnight, which is what a Sunday has always been in the
    register: the attendance page shows a Leave on a Sunday as "Sunday", and
    the payslip, which counts only Absent rows, pays it. Whoever has anything
    on the day already -- somebody who came in -- keeps it, and running it
    again marks nobody twice.
    """
    people = not_logged_in(day)
    if dry_run or not people:
        return people

    marked_at = timezone.make_aware(datetime.combine(day, DAY_MARK))
    with transaction.atomic():
        Attendance.objects.bulk_create(
            [
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
                for person in people
            ]
        )
    return people


def mark_sundays(today, dry_run=False):
    """Mark every Sunday of the cycle gone by. Returns {sunday: people marked}."""
    return {sunday: mark_sunday(sunday, dry_run=dry_run) for sunday in sundays_gone_by(today)}


class TooEarly(Exception):
    """Asked to mark before the cutoff: people still have time to log in."""


def mark_today(now=None, dry_run=False):
    """The 10:30 marking, for whoever asks for it -- the poster just before its
    10:30 report, or the management command.

    TODAY ONLY, and never before the cutoff. An Absent row is a day's pay, so
    no date is taken: marking a past day would mark Absent everybody whose
    attendance was simply never entered. Returns (day, skip, people), where
    `skip` says why nobody was marked (Sunday, nobody logged in at all).
    """
    now = timezone.localtime(now) if now else timezone.localtime()
    day = now.date()
    if now.time() < CUTOFF:
        raise TooEarly(
            f"It is {now:%H:%M}. Nobody is marked Absent before {CUTOFF:%H:%M} -- "
            "they still have time to log in."
        )
    skip = reason_to_skip(day)
    if skip:
        return day, skip, []
    return day, None, mark_absent(day, dry_run=dry_run)
