"""Payslip arithmetic, and the casual leave that has to survive a manual edit.

Two promises are protected here.

The first is that earned casual leave is actually paid. It was worked out once,
at generation, from LOP read off the Attendance table — and a month with no
'Absent' row generates with LOP 0, so no leave applied. HR then types the real
absence into the payslip by hand, which is how leave actually reaches payroll
here, and the stored zero was carried forward. The employee lost a day's pay
they had earned.

The second is that the day counts on the slip are HR's, not ours. Total Days,
No of Lop Days and No of Days stay exactly as entered: three days absent reads
as three days absent. The leave is paid as its own line instead, so the slip
shows both the absence and the leave that covered part of it.
"""
import datetime
from decimal import Decimal

from django.test import TestCase
from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee

from .models import Payslip
from .views import (
    CASUAL_LEAVE_ELIGIBILITY_MONTHS,
    _months_of_service,
    casual_leave_available,
    compute_payslip_fields,
)

# A 30,000 salary over a 30-day cycle makes one day exactly 1,000 — so a day
# gained or lost by casual leave is legible in the assertions.
SALARY = Decimal("30000")
PERIOD = 30
DAY = Decimal("1000.00")


def make_employee(name, doj=None, **extra):
    return Employee.objects.create(
        employee_name=name,
        email=f"{name.replace(' ', '.').lower()}@example.com",
        role="Engineer",
        department="Service",
        branch="Chennai",
        salary=SALARY,
        date_of_joining=doj,
        **extra,
    )


class MonthsOfServiceTests(TestCase):
    def test_the_month_turns_on_the_day_of_the_month_they_joined(self):
        joined = datetime.date(2025, 1, 15)
        self.assertEqual(_months_of_service(joined, datetime.date(2025, 7, 14)), 5)
        self.assertEqual(_months_of_service(joined, datetime.date(2025, 7, 15)), 6)

    def test_no_joining_date_is_no_service(self):
        # The feature is opt-in per employee: nobody is given leave off a date
        # nobody has entered.
        self.assertEqual(_months_of_service(None, datetime.date(2026, 8, 24)), 0)

    def test_service_is_never_negative(self):
        self.assertEqual(
            _months_of_service(datetime.date(2026, 8, 1), datetime.date(2026, 1, 1)), 0
        )


class CasualLeaveAccrualTests(TestCase):
    def test_one_day_accrues_for_each_month_after_the_qualifying_period(self):
        # Joined Oct 2025, so six months' service completes on 1 Apr 2026:
        # April through August is five qualifying months in 2026.
        employee = make_employee("Long Server", datetime.date(2025, 10, 1))
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(5))

    def test_nothing_accrues_before_the_qualifying_period(self):
        employee = make_employee("New Joiner", datetime.date(2026, 6, 1))
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(0))

    def test_an_employee_with_no_joining_date_earns_nothing(self):
        employee = make_employee("No DOJ", None)
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(0))

    def test_leave_already_taken_this_year_is_deducted_from_the_balance(self):
        employee = make_employee("Spender", datetime.date(2025, 10, 1))
        Payslip.objects.create(
            employee=employee, month=5, year=2026, casual_leave_used=Decimal(2)
        )
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(3))

    def test_this_months_own_usage_is_not_counted_against_it(self):
        """Otherwise saving the same slip twice would spend the balance twice."""
        employee = make_employee("Resaver", datetime.date(2025, 10, 1))
        Payslip.objects.create(
            employee=employee, month=8, year=2026, casual_leave_used=Decimal(1)
        )
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(5))


class ComputePayslipFieldsTests(TestCase):
    def setUp(self):
        self.employee = make_employee("Calc", datetime.date(2025, 1, 1))

    def _fields(self, **over):
        args = {"total_days": PERIOD, "lop_days": 0}
        args.update(over)
        return compute_payslip_fields(self.employee, **args)

    def test_casual_leave_leaves_the_day_counts_alone(self):
        """The whole point of paying it as a line: HR's figures stand."""
        without = self._fields(lop_days=3)
        with_leave = self._fields(lop_days=3, casual_leave_days=1)

        for fields in (without, with_leave):
            self.assertEqual(fields["total_days"], PERIOD)
            self.assertEqual(fields["lop_days"], Decimal(3))
            self.assertEqual(fields["paid_days"], Decimal(27))
        # Earnings are pro-rated on those same 27 days either way.
        self.assertEqual(with_leave["gross_earnings"], without["gross_earnings"])

    def test_casual_leave_is_paid_as_its_own_line(self):
        fields = self._fields(lop_days=3, casual_leave_days=1)
        self.assertEqual(fields["casual_leave_used"], Decimal(1))
        self.assertEqual(fields["casual_leave_pay"], DAY)

    def test_the_leave_reaches_the_employee_in_the_net(self):
        without = self._fields(lop_days=3)
        with_leave = self._fields(lop_days=3, casual_leave_days=1)
        self.assertEqual(with_leave["net_salary"] - without["net_salary"], DAY)

    def test_no_leave_means_no_line_and_no_change(self):
        fields = self._fields(lop_days=3)
        self.assertEqual(fields["casual_leave_used"], Decimal(0))
        self.assertEqual(fields["casual_leave_pay"], Decimal("0.00"))

    def test_a_leave_day_is_worth_a_full_day_even_in_a_month_with_absence(self):
        """Paid off gross and total days, never off the pro-rated earnings —
        otherwise absence would quietly cheapen the leave that covers it."""
        light = self._fields(lop_days=1, casual_leave_days=1)
        heavy = self._fields(lop_days=10, casual_leave_days=1)
        self.assertEqual(light["casual_leave_pay"], DAY)
        self.assertEqual(heavy["casual_leave_pay"], DAY)

    def test_leave_cannot_exceed_the_absence_it_covers(self):
        fields = self._fields(lop_days=1, casual_leave_days=5)
        self.assertEqual(fields["casual_leave_used"], Decimal(1))
        self.assertEqual(fields["casual_leave_pay"], DAY)

    def test_leave_and_off_days_together_cannot_exceed_the_absence(self):
        fields = self._fields(lop_days=2, off_days=2, casual_leave_days=2)
        self.assertEqual(fields["casual_leave_used"], Decimal(0))
        self.assertEqual(fields["casual_leave_pay"], Decimal("0.00"))
        # Off days still do offset LOP — that behaviour is untouched.
        self.assertEqual(fields["paid_days"], Decimal(PERIOD))

    def test_off_days_still_offset_lop_exactly_as_before(self):
        fields = self._fields(lop_days=3, off_days=1)
        self.assertEqual(fields["paid_days"], Decimal(28))
        self.assertEqual(fields["casual_leave_pay"], Decimal("0.00"))

    def test_nothing_is_paid_for_absence_with_no_leave_to_cover_it(self):
        self.assertEqual(self._fields(lop_days=3)["paid_days"], Decimal(27))


class RecalculateKeepsEarnedLeaveTests(APITestCase):
    """The reported bug, at the endpoint the payslip screen's Save button hits."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr", password="x", role="hr")
        self.client.force_authenticate(self.hr)

    def _slip(self, employee, month=8, year=2026, **over):
        # A slip as bulk generation would leave it for a month whose attendance
        # showed no absence: no LOP, and so no casual leave applied.
        fields = compute_payslip_fields(employee, PERIOD, 0)
        fields.update(over)
        return Payslip.objects.create(employee=employee, month=month, year=year, **fields)

    def _save_lop(self, slip, lop_days, **extra):
        payload = {"total_days": PERIOD, "lop_days": lop_days}
        payload.update(extra)
        return self.client.post(f"/api/payslips/{slip.id}/recalculate/", payload, format="json")

    def test_typing_the_leave_in_by_hand_pays_the_earned_casual_leave(self):
        # Joined Feb 2026, so six months complete on 1 Aug 2026: August is the
        # only qualifying month of the year and exactly one day is earned.
        employee = make_employee("Eligible", datetime.date(2026, 2, 1))
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(1))

        slip = self._slip(employee)
        self.assertEqual(slip.casual_leave_used, Decimal(0))  # nothing to apply at generation

        # The same three days of absence with no leave to cover them. EPF moves
        # with the days worked, so the comparison has to be against this rather
        # than against a full month minus three days.
        uncovered = compute_payslip_fields(employee, PERIOD, 3)

        response = self._save_lop(slip, 3)
        self.assertEqual(response.status_code, 200, response.data)

        slip.refresh_from_db()
        # HR's figures stand, untouched.
        self.assertEqual(slip.total_days, PERIOD)
        self.assertEqual(slip.lop_days, Decimal(3))
        self.assertEqual(slip.paid_days, Decimal(27))
        # And the earned day is paid, as its own line.
        self.assertEqual(slip.casual_leave_used, Decimal(1))
        self.assertEqual(slip.casual_leave_pay, DAY)
        # Three days absent, one of them covered: exactly one day better off
        # than the same absence with no leave behind it.
        self.assertEqual(slip.net_salary, uncovered["net_salary"] + DAY)

    def test_a_saved_up_balance_covers_more_than_one_day(self):
        """Leave accrues monthly and accumulates, so someone who has not taken
        any can have several absent days covered at once."""
        employee = make_employee("Saver", datetime.date(2025, 10, 1))
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(5))

        slip = self._slip(employee)
        self._save_lop(slip, 3)

        slip.refresh_from_db()
        self.assertEqual(slip.lop_days, Decimal(3))
        self.assertEqual(slip.paid_days, Decimal(27))
        self.assertEqual(slip.casual_leave_used, Decimal(3))
        self.assertEqual(slip.casual_leave_pay, DAY * 3)

    def test_an_employee_short_of_the_qualifying_period_gets_nothing(self):
        employee = make_employee("Too New", datetime.date(2026, 6, 1))
        slip = self._slip(employee)
        self._save_lop(slip, 3)

        slip.refresh_from_db()
        self.assertEqual(slip.casual_leave_used, Decimal(0))
        self.assertEqual(slip.casual_leave_pay, Decimal("0.00"))
        self.assertEqual(slip.paid_days, Decimal(27))

    def test_an_employee_with_no_joining_date_is_left_exactly_as_before(self):
        employee = make_employee("No DOJ", None)
        slip = self._slip(employee)
        self._save_lop(slip, 3)

        slip.refresh_from_db()
        self.assertEqual(slip.casual_leave_used, Decimal(0))
        self.assertEqual(slip.casual_leave_pay, Decimal("0.00"))
        self.assertEqual(slip.paid_days, Decimal(27))

    def test_leave_is_capped_by_what_is_absent_not_by_what_is_earned(self):
        # Five days earned, one day absent: only that one day is covered.
        employee = make_employee("Rich Balance", datetime.date(2025, 10, 1))
        slip = self._slip(employee)
        self._save_lop(slip, 1)

        slip.refresh_from_db()
        self.assertEqual(slip.casual_leave_used, Decimal(1))
        self.assertEqual(slip.casual_leave_pay, DAY)
        self.assertEqual(slip.paid_days, Decimal(29))

    def test_saving_the_same_slip_twice_does_not_spend_the_balance_twice(self):
        employee = make_employee("Resaver", datetime.date(2026, 2, 1))  # one day earned
        slip = self._slip(employee)
        self._save_lop(slip, 3)
        self._save_lop(slip, 3)

        slip.refresh_from_db()
        self.assertEqual(slip.casual_leave_used, Decimal(1))
        self.assertEqual(slip.casual_leave_pay, DAY)

    def test_a_balance_already_spent_this_year_is_not_handed_out_again(self):
        employee = make_employee("Spent Up", datetime.date(2025, 10, 1))
        # Five days earned by August, all five already taken in earlier months.
        for month in (3, 4, 5, 6, 7):
            Payslip.objects.create(
                employee=employee, month=month, year=2026, casual_leave_used=Decimal(1)
            )
        slip = self._slip(employee)
        self._save_lop(slip, 3)

        slip.refresh_from_db()
        self.assertEqual(slip.casual_leave_used, Decimal(0))
        self.assertEqual(slip.casual_leave_pay, Decimal("0.00"))

    def test_an_explicit_figure_from_the_operator_still_wins(self):
        employee = make_employee("Overridden", datetime.date(2025, 10, 1))
        slip = self._slip(employee)
        self._save_lop(slip, 3, casual_leave_used=0)

        slip.refresh_from_db()
        self.assertEqual(slip.casual_leave_used, Decimal(0))
        self.assertEqual(slip.casual_leave_pay, Decimal("0.00"))

    def test_an_absolute_paid_days_figure_is_taken_as_final(self):
        # The operator has stated the worked figure outright; leave must not
        # then add to it behind their back.
        employee = make_employee("Absolute", datetime.date(2025, 10, 1))
        slip = self._slip(employee)
        response = self.client.post(
            f"/api/payslips/{slip.id}/recalculate/",
            {"total_days": PERIOD, "paid_days": 27},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)

        slip.refresh_from_db()
        self.assertEqual(slip.casual_leave_used, Decimal(0))
        self.assertEqual(slip.casual_leave_pay, Decimal("0.00"))
        self.assertEqual(slip.paid_days, Decimal(27))

    def test_a_paid_slip_is_still_refused(self):
        employee = make_employee("Already Paid", datetime.date(2025, 10, 1))
        slip = self._slip(employee, status="Paid")
        self.assertEqual(self._save_lop(slip, 3).status_code, 400)


class RevertMatchesGenerationTests(APITestCase):
    def setUp(self):
        self.hr = User.objects.create_user(username="hr2", password="x", role="hr")
        self.client.force_authenticate(self.hr)

    def test_undoing_edits_does_not_strip_earned_casual_leave(self):
        """Revert promises what generation would produce, so it must include the
        casual leave generation applies. It was passing none."""
        employee = make_employee("Reverter", datetime.date(2025, 10, 1))
        from attendance.models import Attendance

        # One absence inside the 25-Jul..24-Aug cycle, the way generation reads it.
        Attendance.objects.create(
            employee=employee,
            employee_name=employee.employee_name,
            role=employee.role,
            department=employee.department,
            salary=employee.salary,
            intime=datetime.datetime(2026, 8, 10, 9, 0, tzinfo=datetime.timezone.utc),
            status="Absent",
        )
        slip = Payslip.objects.create(
            employee=employee,
            month=8,
            year=2026,
            **compute_payslip_fields(employee, 31, 5),  # a manual edit to undo
        )

        response = self.client.post(f"/api/payslips/{slip.id}/revert/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)

        slip.refresh_from_db()
        self.assertEqual(slip.lop_days, Decimal(1))
        self.assertEqual(slip.paid_days, Decimal(30))
        self.assertEqual(slip.casual_leave_used, Decimal(1))
        # A 31-day cycle, so a day is 30000/31.
        self.assertEqual(slip.casual_leave_pay, Decimal("967.74"))

    def test_reverting_an_employee_with_no_joining_date_changes_nothing_for_them(self):
        employee = make_employee("Plain", None)
        slip = Payslip.objects.create(
            employee=employee, month=8, year=2026, **compute_payslip_fields(employee, 31, 5)
        )
        self.client.post(f"/api/payslips/{slip.id}/revert/", {}, format="json")

        slip.refresh_from_db()
        self.assertEqual(slip.casual_leave_used, Decimal(0))
        self.assertEqual(slip.casual_leave_pay, Decimal("0.00"))
        self.assertEqual(slip.lop_days, Decimal(0))  # no absence recorded


class EligibilityConstantTests(TestCase):
    def test_the_qualifying_period_is_six_months(self):
        # Stated in the policy the office works to; a change here changes pay.
        self.assertEqual(CASUAL_LEAVE_ELIGIBILITY_MONTHS, 6)
