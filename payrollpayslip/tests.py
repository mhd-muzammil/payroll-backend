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
    CASUAL_LEAVE_DAYS_PER_MONTH,
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
    def test_a_qualified_month_is_worth_exactly_one_day(self):
        employee = make_employee("Qualified", datetime.date(2026, 2, 1))
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(1))

    def test_it_is_still_one_day_after_years_of_service(self):
        """It does not accumulate. Someone here since 2020 gets the same single
        day as someone who qualified last month."""
        employee = make_employee("Long Server", datetime.date(2020, 1, 1))
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(1))

    def test_nothing_accrues_before_the_qualifying_period(self):
        employee = make_employee("New Joiner", datetime.date(2026, 6, 1))
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(0))

    def test_the_month_service_completes_in_is_the_first_that_counts(self):
        # Joined 5 Jan 2026: six months are up on 5 July, so July qualifies and
        # June does not.
        employee = make_employee("Boundary", datetime.date(2026, 1, 5))
        self.assertEqual(casual_leave_available(employee, 2026, 6), Decimal(0))
        self.assertEqual(casual_leave_available(employee, 2026, 7), Decimal(1))

    def test_an_employee_with_no_joining_date_earns_nothing(self):
        employee = make_employee("No DOJ", None)
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(0))

    def test_leave_taken_in_another_month_does_not_touch_this_one(self):
        """Each month stands alone: nothing is carried forward and nothing is
        owed back."""
        employee = make_employee("Spender", datetime.date(2025, 10, 1))
        for month in (3, 4, 5, 6, 7):
            Payslip.objects.create(
                employee=employee, month=month, year=2026, casual_leave_used=Decimal(1)
            )
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(1))

    def test_this_months_own_usage_does_not_reduce_it_either(self):
        """Otherwise saving the same slip twice would spend the day twice."""
        employee = make_employee("Resaver", datetime.date(2025, 10, 1))
        Payslip.objects.create(
            employee=employee, month=8, year=2026, casual_leave_used=Decimal(1)
        )
        self.assertEqual(casual_leave_available(employee, 2026, 8), Decimal(1))


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

    def test_only_one_day_is_ever_covered_however_long_they_have_been_here(self):
        """The leave does not accumulate: three days absent still costs two,
        for a long-serving employee exactly as for a newly qualified one."""
        employee = make_employee("Saver", datetime.date(2020, 1, 1))
        slip = self._slip(employee)
        self._save_lop(slip, 3)

        slip.refresh_from_db()
        self.assertEqual(slip.lop_days, Decimal(3))
        self.assertEqual(slip.paid_days, Decimal(27))
        self.assertEqual(slip.casual_leave_used, Decimal(1))
        self.assertEqual(slip.casual_leave_pay, DAY)

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

    def test_last_months_leave_does_not_use_up_this_months(self):
        employee = make_employee("Regular", datetime.date(2025, 10, 1))
        for month in (5, 6, 7):
            Payslip.objects.create(
                employee=employee, month=month, year=2026, casual_leave_used=Decimal(1)
            )
        slip = self._slip(employee)
        self._save_lop(slip, 3)

        slip.refresh_from_db()
        self.assertEqual(slip.casual_leave_used, Decimal(1))
        self.assertEqual(slip.casual_leave_pay, DAY)

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


class PolicyConstantTests(TestCase):
    """The office's policy, in the two numbers that decide what people are paid.
    A change to either changes payroll, so it should be a deliberate edit here
    and not a side effect of something else."""

    def test_the_qualifying_period_is_six_months(self):
        self.assertEqual(CASUAL_LEAVE_ELIGIBILITY_MONTHS, 6)

    def test_a_month_is_worth_one_day(self):
        self.assertEqual(CASUAL_LEAVE_DAYS_PER_MONTH, Decimal(1))


class SpecialWorkTests(TestCase):
    """Days worked beyond the cycle, entered by HR and paid on top.

    Shaped like casual leave — its own days figure, its own paid line — but
    ruled differently: nothing about it is earned, capped by absence, or tied
    to leave. Two days entered is two days' pay, added.
    """

    def setUp(self):
        self.employee = make_employee("Extra", datetime.date(2025, 1, 1))

    def _fields(self, **over):
        args = {"total_days": PERIOD, "lop_days": 0}
        args.update(over)
        return compute_payslip_fields(self.employee, **args)

    def test_two_days_entered_pays_two_days(self):
        fields = self._fields(special_work_days=2)
        self.assertEqual(fields["special_work_days"], Decimal(2))
        self.assertEqual(fields["special_work_pay"], DAY * 2)

    def test_it_reaches_the_employee_in_the_net(self):
        without = self._fields()
        with_extra = self._fields(special_work_days=2)
        self.assertEqual(with_extra["net_salary"] - without["net_salary"], DAY * 2)

    def test_it_leaves_the_day_counts_and_the_earnings_alone(self):
        without = self._fields(lop_days=3)
        with_extra = self._fields(lop_days=3, special_work_days=2)
        for fields in (without, with_extra):
            self.assertEqual(fields["total_days"], PERIOD)
            self.assertEqual(fields["lop_days"], Decimal(3))
            self.assertEqual(fields["paid_days"], Decimal(27))
        self.assertEqual(with_extra["gross_earnings"], without["gross_earnings"])

    def test_none_entered_means_no_line_and_no_change(self):
        fields = self._fields(lop_days=3)
        self.assertEqual(fields["special_work_days"], Decimal(0))
        self.assertEqual(fields["special_work_pay"], Decimal("0.00"))

    def test_absence_does_not_cap_it_the_way_it_caps_leave(self):
        """A Sunday call-out is worth a day whether or not they were also away
        that week. Capping this by absence would swallow work that was done."""
        fields = self._fields(lop_days=0, special_work_days=3)
        self.assertEqual(fields["special_work_days"], Decimal(3))
        self.assertEqual(fields["special_work_pay"], DAY * 3)

    def test_a_day_worked_is_a_full_day_even_in_a_month_with_absence(self):
        light = self._fields(lop_days=1, special_work_days=1)
        heavy = self._fields(lop_days=20, special_work_days=1)
        self.assertEqual(light["special_work_pay"], DAY)
        self.assertEqual(heavy["special_work_pay"], DAY)

    def test_it_cannot_exceed_the_cycle(self):
        """A slipped keystroke should not pay out a year."""
        fields = self._fields(special_work_days=500)
        self.assertEqual(fields["special_work_days"], Decimal(PERIOD))

    def test_a_negative_figure_is_nothing(self):
        fields = self._fields(special_work_days=-3)
        self.assertEqual(fields["special_work_days"], Decimal(0))
        self.assertEqual(fields["special_work_pay"], Decimal("0.00"))

    def test_leave_and_special_work_are_independent_and_both_paid(self):
        fields = self._fields(lop_days=3, casual_leave_days=1, special_work_days=2)
        self.assertEqual(fields["lop_days"], Decimal(3))
        self.assertEqual(fields["paid_days"], Decimal(27))
        self.assertEqual(fields["casual_leave_pay"], DAY)
        self.assertEqual(fields["special_work_pay"], DAY * 2)
        plain = self._fields(lop_days=3)
        self.assertEqual(fields["net_salary"] - plain["net_salary"], DAY * 3)


class SpecialWorkEndpointTests(APITestCase):
    def setUp(self):
        self.hr = User.objects.create_user(username="hr3", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        self.employee = make_employee("Extra API", datetime.date(2025, 1, 1))
        self.slip = Payslip.objects.create(
            employee=self.employee, month=8, year=2026,
            **compute_payslip_fields(self.employee, PERIOD, 0),
        )

    def _post(self, **payload):
        body = {"total_days": PERIOD}
        body.update(payload)
        return self.client.post(f"/api/payslips/{self.slip.id}/recalculate/", body, format="json")

    def test_hr_enters_two_days_and_two_days_are_paid(self):
        before = self.slip.net_salary
        response = self._post(lop_days=0, special_work_days=2)
        self.assertEqual(response.status_code, 200, response.data)
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.special_work_days, Decimal(2))
        self.assertEqual(self.slip.special_work_pay, DAY * 2)
        # Two days better off, and nothing else moved.
        self.assertEqual(self.slip.net_salary - before, DAY * 2)
        self.assertEqual(self.slip.lop_days, Decimal(0))
        self.assertEqual(self.slip.paid_days, Decimal(PERIOD))

    def test_editing_another_box_does_not_wipe_it(self):
        """The reason it is read separately: saving LOP must not default the
        special work away and take back pay that was already granted."""
        self._post(lop_days=0, special_work_days=2)
        self._post(lop_days=3)  # HR now types the month's absence in

        self.slip.refresh_from_db()
        self.assertEqual(self.slip.lop_days, Decimal(3))
        self.assertEqual(self.slip.special_work_days, Decimal(2))
        self.assertEqual(self.slip.special_work_pay, DAY * 2)

    def test_hr_can_take_it_back_by_entering_zero(self):
        self._post(lop_days=0, special_work_days=2)
        self._post(lop_days=0, special_work_days=0)
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.special_work_days, Decimal(0))
        self.assertEqual(self.slip.special_work_pay, Decimal("0.00"))

    def test_out_of_range_is_refused_rather_than_clamped_at_the_endpoint(self):
        response = self._post(lop_days=0, special_work_days=PERIOD + 1)
        self.assertEqual(response.status_code, 400)
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.special_work_days, Decimal(0))

    def test_undoing_edits_takes_the_special_work_back_too(self):
        """Undo Edits promises the slip as generation left it, and generation
        never puts special work on one. Keeping it made the button look
        broken — the figure HR had just entered was still sitting there."""
        self._post(lop_days=0, special_work_days=2)
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.special_work_days, Decimal(2))

        response = self.client.post(f"/api/payslips/{self.slip.id}/revert/", {}, format="json")
        self.assertEqual(response.status_code, 200, response.data)
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.special_work_days, Decimal(0))
        self.assertEqual(self.slip.special_work_pay, Decimal("0.00"))


class StartingOverClearsSpecialWorkTests(APITestCase):
    """Undo Edits and Regenerate both mean start over, so both come back with
    the special work at zero. One of them keeping a figure the other clears is
    what made Undo look broken in the first place."""

    def setUp(self):
        self.hr = User.objects.create_user(username="hr4", password="x", role="hr")
        self.client.force_authenticate(self.hr)
        self.employee = make_employee("Starter", datetime.date(2025, 1, 1))
        self.slip = Payslip.objects.create(
            employee=self.employee, month=8, year=2026,
            **compute_payslip_fields(self.employee, PERIOD, 0, special_work_days=3),
        )

    def test_undo_edits_clears_it(self):
        self.assertEqual(self.slip.special_work_days, Decimal(3))
        self.client.post(f"/api/payslips/{self.slip.id}/revert/", {}, format="json")
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.special_work_days, Decimal(0))
        self.assertEqual(self.slip.special_work_pay, Decimal("0.00"))

    def test_regenerating_clears_it_the_same_way(self):
        self.assertEqual(self.slip.special_work_days, Decimal(3))
        response = self.client.post(
            "/api/payslips/generate_all/",
            {"month": 8, "year": 2026, "employee_id": self.employee.id},
            format="json",
        )
        self.assertIn(response.status_code, (200, 201), response.data)
        self.slip.refresh_from_db()
        self.assertEqual(self.slip.special_work_days, Decimal(0))
        self.assertEqual(self.slip.special_work_pay, Decimal("0.00"))
