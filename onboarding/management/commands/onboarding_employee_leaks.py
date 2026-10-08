"""Which employees the onboarding signal reached when it should not have.

READ ONLY. It changes nothing, and has no switch that would.

For a while every onboarding record was matched against existing employees
and written over whoever it found -- or turned into a new Employee row and a
login when it found nobody. That is right for an employee the office typed in.
It was also happening to:

  * freelancers and vendors, who are not staff and must not have an Employee
    row or a login; and
  * forms that came in through the public link and were still Pending Review,
    typed by somebody nobody here had met, without logging in.

The signal is now gated on both. This lists what it already did, so a person
can look before anything is removed. Two very different findings:

  CREATED  -- an Employee row that looks like the signal made it for this
              record: first seen on or after the day the form was saved, with
              no attendance and no payslips. Almost certainly junk to remove,
              together with its login.

  EXISTING -- a real employee who was there before the form. The form may have
              been WRITTEN OVER them: name, phone, department, branch, status.
              These are the ones to check by hand first.

    python manage.py onboarding_employee_leaks
"""

from django.core.management.base import BaseCommand
from django.db.models import Q

from employees.models import Employee
from onboarding.models import Onboarding


def _candidates(record):
    """The employees the signal would have matched, in the order it tries."""
    found = []
    seen = set()

    def take(queryset, how):
        for employee in queryset:
            if employee.pk not in seen:
                seen.add(employee.pk)
                found.append((employee, how))

    email = (record.email_id or "").strip()
    code = (record.employee_id or "").strip()
    phone = (record.mobile_number or "").strip()
    name = (record.employee_name or "").strip()
    if email:
        take(Employee.objects.filter(email__iexact=email), "email")
    if code:
        take(Employee.objects.filter(emp_code=code), "employee code")
    if phone:
        take(Employee.objects.filter(phone=phone), "phone")
    if name:
        take(Employee.objects.filter(employee_name__iexact=name), "name")
    return found


class Command(BaseCommand):
    help = "READ ONLY: list employees the onboarding signal reached for freelancers, vendors and unreviewed link forms."

    def handle(self, *args, **options):
        records = Onboarding.objects.filter(
            ~Q(category="Employee") | Q(status="Pending Review")
        ).order_by("created_at")

        if not records.exists():
            self.stdout.write("No freelancer, vendor or unreviewed link form on file. Nothing to check.")
            return

        created_rows = 0
        existing_rows = 0
        for record in records:
            label = record.employee_name or "(no name given)"
            self.stdout.write(
                f"\n#{record.pk} {label} -- {record.category}, {record.source}, "
                f"{record.status}, saved {record.created_at:%Y-%m-%d %H:%M}"
            )
            matches = _candidates(record)
            if not matches:
                self.stdout.write("    no employee matches -- nothing was touched")
                continue

            for employee, how in matches:
                attendance = employee.attendances.count()
                payslips = employee.payslips.count()
                user = getattr(employee, "user", None)
                login = (
                    f"login {user.username!r} ({'active' if user.is_active else 'disabled'})"
                    if user else "no login"
                )
                first_seen = employee.joining_date
                made_by_signal = (
                    first_seen is not None
                    and first_seen >= record.created_at.date()
                    and attendance == 0
                    and payslips == 0
                )
                if made_by_signal:
                    created_rows += 1
                    verdict = "CREATED  "
                else:
                    existing_rows += 1
                    verdict = "EXISTING "
                self.stdout.write(
                    f"    {verdict} employee #{employee.pk} {employee.employee_name!r} "
                    f"(matched by {how}) -- status {employee.status}, first seen {first_seen}, "
                    f"{attendance} attendance, {payslips} payslips, {login}"
                )

        self.stdout.write(
            f"\n{records.count()} record(s) checked: {created_rows} employee row(s) look CREATED "
            f"by the signal, {existing_rows} EXISTING employee(s) it may have written over."
        )
        if existing_rows:
            self.stdout.write(
                "Check every EXISTING one by hand -- compare its name, phone, branch and status "
                "with what it should be -- before removing anything."
            )
