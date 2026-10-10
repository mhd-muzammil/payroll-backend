"""Who has not logged in today -- and, at 10am, marking them Absent.

The office's rule: anybody who has not pressed Login by 10am is Absent for the
day. Leave applied for in the app does not change that; the office corrects a
leave day by hand.

One rule, read in two places that have to agree. The WhatsApp reminders at
9:11 and 9:37 name the people who have not logged in yet, and the 10am job
marks those same people Absent. A reminder that named somebody the job then
skipped, or a job that marked somebody no reminder had named, would be worse
than having neither -- so both ask `not_logged_in`, and nothing else.

Absent is not a label here, it is pay: a payslip's loss-of-pay days are the
count of Absent rows in its cycle. So every rule below is about who must NOT
be marked:

  * Sunday. It is marked Leave for everybody by mark_sunday_leave.
  * A day nobody at all has logged in. That is a holiday, or the app was down,
    and a whole company of Absent rows is the one mistake that must not happen.
  * Anybody who could not have logged in: not 'active' (inactive, relieved,
    on leave in the employee record), not joined yet, no app account, or an
    account with no Login button (admin accounts).
  * Anybody who already has anything on the day -- a Login, or a row the
    office marked -- whatever it says.
"""

from datetime import datetime, time

from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from attendance.models import Attendance
from employees.models import Employee

# Not logged in by then is Absent.
CUTOFF = time(10, 0)

SUNDAY = 6  # datetime.weekday()

# A day with no punch is stored as midnight on that date with no logout -- the
# same as the Add Record form, the Excel import and the Sunday rule write it.
DAY_MARK = time(0, 0)

# The statuses a no-punch day is written with.
NO_PUNCH_STATUSES = ("Absent", "Leave")

# App accounts that have a Login button. The admin accounts do not.
CAN_LOG_IN = ("employee", "hr")


def is_day_mark(row):
    """Whether this row holds only a date -- an Absent or Leave day nobody
    punched -- rather than a Login."""
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
