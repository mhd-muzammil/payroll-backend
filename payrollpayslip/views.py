from django.db.models import Q, Sum
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from rest_framework.exceptions import PermissionDenied
from django.utils import timezone
from decimal import Decimal, ROUND_HALF_UP
import calendar
import datetime

# Casual leave: employees earn 1 CL per month once they have completed 6 months
# of service (probation), capped per calendar year.
CASUAL_LEAVE_ANNUAL_CAP = 12
CASUAL_LEAVE_ELIGIBILITY_MONTHS = 6


def _months_of_service(doj, as_of):
    """Whole completed months of service from date-of-joining `doj` to `as_of`."""
    if not doj:
        return 0
    months = (as_of.year - doj.year) * 12 + (as_of.month - doj.month)
    if as_of.day < doj.day:
        months -= 1
    return max(0, months)


def casual_leave_available(emp, year, month):
    """CL balance available to offset LOP for this employee's payslip period.

    Entitlement = 1 CL for each month of the calendar `year` (up to `month`) in
    which the employee had completed >= 6 months of service, capped at the annual
    cap. Balance = entitlement minus CL already used earlier in the same year.
    Returns 0 for employees with no real date_of_joining (feature is opt-in per
    employee, so existing employees are unaffected until HR sets their DOJ).
    """
    doj = emp.date_of_joining
    if not doj:
        return Decimal(0)
    earned = 0
    for m in range(1, month + 1):
        last_day = calendar.monthrange(year, m)[1]
        as_of = datetime.date(year, m, last_day)
        if _months_of_service(doj, as_of) >= CASUAL_LEAVE_ELIGIBILITY_MONTHS:
            earned += 1
    earned = min(earned, CASUAL_LEAVE_ANNUAL_CAP)
    used_before = Payslip.objects.filter(
        employee=emp, year=year, month__lt=month
    ).aggregate(s=Sum("casual_leave_used"))["s"] or Decimal(0)
    avail = Decimal(earned) - Decimal(used_before)
    return avail if avail > 0 else Decimal(0)

from .models import Payslip, BranchFinancial
from .serializer import PayslipSerializer, BranchFinancialSerializer
from employees.models import Employee, Performance
from attendance.models import Attendance
from authentication.models import get_allowed_branches

def _q(val):
    """Round a Decimal value to two decimal places (bankers-safe half-up)."""
    return Decimal(val).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)


def compute_payslip_fields(emp, total_days, lop_days, other_deduction_override=None, off_days=0, casual_leave_days=0):
    """Compute every earnings/deduction/net field for one employee given the
    period length, LOP (loss-of-pay) days and paid off-days.

    This is the single source of truth for payslip math. Both bulk generation
    (which derives lop_days from attendance) and manual recalculation (which
    takes operator-supplied values) call this, so a manually edited slip is
    always computed with exactly the same rules as an auto-generated one.

    `off_days` are paid non-working days (weekly offs / holidays). They offset
    LOP so they are NOT deducted: effective unpaid days = max(0, lop - off).
    `other_deduction_override`, when not None, replaces the "Other Deduction"
    line with an operator-supplied amount (the rest of the math is unchanged).

    Returns the `defaults` dict used by update_or_create (minus `status`).
    """
    total_days = Decimal(total_days)
    lop_days = Decimal(lop_days)
    off_days = Decimal(off_days)
    casual_leave_days = Decimal(casual_leave_days)
    if lop_days < 0:
        lop_days = Decimal(0)
    if lop_days > total_days:
        lop_days = total_days
    if off_days < 0:
        off_days = Decimal(0)
    if off_days > lop_days:
        off_days = lop_days
    # Casual leave (paid) offsets the LOP that remains after off-days, capped so
    # off + casual can never exceed the LOP being offset.
    if casual_leave_days < 0:
        casual_leave_days = Decimal(0)
    if casual_leave_days > (lop_days - off_days):
        casual_leave_days = lop_days - off_days
    if casual_leave_days < 0:
        casual_leave_days = Decimal(0)

    # Off days and casual leave are paid, so they cancel out an equal number of
    # LOP days.
    effective_lop = lop_days - off_days - casual_leave_days
    if effective_lop < 0:
        effective_lop = Decimal(0)

    paid_days = total_days - effective_lop
    if paid_days < 0:
        paid_days = Decimal(0)

    # Pro-rata ratio
    multiplier = paid_days / total_days if total_days > 0 else Decimal(0)

    q = _q

    # 3. Gross Structure (Base Fixed Monthly Salary)
    if emp.basic > Decimal('0.00'):
        gross_basic = emp.basic
        gross_hra = emp.hra
        gross_conveyance = emp.conveyance
        gross_child_edu = emp.child_edu
        gross_personal = emp.personal_allowance
        gross_incentive = emp.incentive
        gross_other_earnings = emp.other_earnings
        gross_salary = emp.salary
    else:
        # Legacy/Fallback calculation
        gross_salary = emp.salary
        gross_basic = q(gross_salary * Decimal('0.50'))  # 50% Basic
        gross_hra = q(gross_salary * Decimal('0.25'))    # 25% HRA
        gross_conveyance = Decimal('1600.00')
        gross_child_edu = Decimal('200.00')

        if (gross_basic + gross_hra + gross_conveyance + gross_child_edu) > gross_salary:
            gross_conveyance = Decimal(0)
            gross_child_edu = Decimal(0)

        gross_personal = gross_salary - gross_basic - gross_hra - gross_conveyance - gross_child_edu
        if gross_personal < 0:
            gross_personal = Decimal(0)
        gross_incentive = emp.incentive
        gross_other_earnings = emp.other_earnings
        gross_salary = gross_basic + gross_hra + gross_conveyance + gross_child_edu + gross_personal + gross_incentive + gross_other_earnings

    # 4. Earned Components (pro-rated by LOP)
    if paid_days == 0:
        earned_basic = Decimal('0.00')
        earned_hra = Decimal('0.00')
        earned_conveyance = Decimal('0.00')
        earned_child_edu = Decimal('0.00')
        earned_personal = Decimal('0.00')
        earned_incentive = Decimal('0.00')
        earned_other_earnings = Decimal('0.00')
        gross_earnings = Decimal('0.00')
    else:
        # Compute the LOP deduction in a single step (no intermediate rounding
        # of the per-day rate) so the earned figure is accurate to the paisa.
        lop_deduction = q(gross_salary * effective_lop / total_days)
        if lop_deduction > gross_salary:
            lop_deduction = gross_salary
        gross_earnings = gross_salary - lop_deduction

        earned_basic = q(gross_basic * multiplier)
        earned_hra = q(gross_hra * multiplier)
        earned_conveyance = q(gross_conveyance * multiplier)
        earned_child_edu = q(gross_child_edu * multiplier)
        earned_incentive = q(gross_incentive * multiplier)
        earned_other_earnings = q(gross_other_earnings * multiplier)

        earned_personal = gross_earnings - (earned_basic + earned_hra + earned_conveyance + earned_child_edu + earned_incentive + earned_other_earnings)
        if earned_personal < Decimal('0.00'):
            earned_personal = Decimal('0.00')

    # 5. Deductions
    if emp.basic > Decimal('0.00'):
        deduction_epf = q(emp.epf * multiplier)
        deduction_esi = q(emp.esi * multiplier)
        deduction_prof_tax = emp.prof_tax
        deduction_lwf = emp.lwf
        deduction_staff_advance = emp.staff_advance
        deduction_tds = emp.tds
        deduction_other = emp.other_deduction
        deduction_insurance = emp.deduction_insurance

        employer_epf = q(emp.employer_epf * multiplier)
        employer_esi = q(emp.employer_esi * multiplier)
        employer_insurance = emp.employer_insurance
        petrol_allowance = q(emp.petrol_allowance * multiplier)
    else:
        deduction_epf = q(earned_basic * Decimal('0.12'))
        deduction_prof_tax = Decimal('208.30') if gross_salary > 12000 else Decimal('0.00')
        deduction_esi = Decimal('0.00')
        deduction_lwf = Decimal('0.00')
        deduction_staff_advance = Decimal('0.00')
        deduction_tds = Decimal('0.00')
        deduction_other = Decimal('0.00')
        deduction_insurance = Decimal('0.00')

        employer_epf = q(earned_basic * Decimal('0.13'))
        employer_esi = Decimal('0.00')
        employer_insurance = Decimal('0.00')
        petrol_allowance = Decimal('0.00')

    # Optional manual override of the "Other Deduction" line.
    if other_deduction_override is not None:
        deduction_other = q(other_deduction_override)

    gross_deductions = deduction_epf + deduction_esi + deduction_prof_tax + deduction_lwf + deduction_staff_advance + deduction_tds + deduction_other + deduction_insurance

    # 6. Net Take Home (kept accurate to the paisa; no whole-rupee rounding)
    net_rounded = q(gross_earnings - gross_deductions)

    return {
        'total_days': int(total_days),
        'lop_days': lop_days,
        'off_days': off_days,
        'casual_leave_used': casual_leave_days,
        'paid_days': paid_days,

        'gross_basic': gross_basic,
        'gross_hra': gross_hra,
        'gross_conveyance': gross_conveyance,
        'gross_child_edu': gross_child_edu,
        'gross_personal_allowance': gross_personal,
        'gross_incentive': gross_incentive,
        'gross_other_earnings': gross_other_earnings,
        'gross_salary': gross_salary,

        'earned_basic': earned_basic,
        'earned_hra': earned_hra,
        'earned_conveyance': earned_conveyance,
        'earned_child_edu': earned_child_edu,
        'earned_personal_allowance': earned_personal,
        'earned_incentive': earned_incentive,
        'earned_other_earnings': earned_other_earnings,
        'gross_earnings': gross_earnings,

        'deduction_epf': deduction_epf,
        'deduction_esi': deduction_esi,
        'deduction_prof_tax': deduction_prof_tax,
        'deduction_lwf': deduction_lwf,
        'deduction_staff_advance': deduction_staff_advance,
        'deduction_tds': deduction_tds,
        'deduction_other': deduction_other,
        'deduction_insurance': deduction_insurance,
        'gross_deductions': gross_deductions,

        'employer_epf': employer_epf,
        'employer_esi': employer_esi,
        'employer_insurance': employer_insurance,
        'petrol_allowance': petrol_allowance,

        'net_salary': net_rounded,
    }


class PayslipViewSet(viewsets.ModelViewSet):
    queryset = Payslip.objects.all()
    serializer_class = PayslipSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Payslip.objects.all().order_by("-year", "-month", "-id")
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            queryset = queryset.filter(employee__user=user)
        else:
            branches = get_allowed_branches(user, "payslips")
            if "All" not in branches:
                queryset = queryset.filter(employee__branch__in=branches)
        
        month = self.request.query_params.get('month')
        year = self.request.query_params.get('year')
        if month:
            queryset = queryset.filter(month=month)
        if year:
            queryset = queryset.filter(year=year)

        return queryset

    def _report_scope(self, request):
        """Scope enterprise reports. Returns:
          - False  -> caller is an employee, deny (403).
          - None   -> all branches allowed.
          - [list] -> restrict to these branches.
        """
        user = request.user
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            return False
        allowed = get_allowed_branches(user, "payslips")
        return None if "All" in allowed else allowed

    @action(detail=False, methods=['post'])
    def generate_all(self, request):
        """
        Generates/calculates detailed payslips based on pro-rated Indian salary structures.
        Supports bulk generation or single employee generation when employee_id is specified.
        """
        now = timezone.now()
        month = request.data.get('month', now.month)
        year = request.data.get('year', now.year)
        employee_id = request.data.get('employee_id')

        try:
            month = int(month)
            year = int(year)
        except ValueError:
            return Response({"error": "Invalid month or year format."}, status=status.HTTP_400_BAD_REQUEST)

        # 1. Get previous month/year to calculate days from previous month's 25th to current month's 24th
        if month == 1:
            prev_month = 12
            prev_year = year - 1
        else:
            prev_month = month - 1
            prev_year = year

        import datetime
        from django.utils.timezone import make_aware

        # Start date: 25th of previous month
        # End date: 24th of current month
        start_date = datetime.date(prev_year, prev_month, 25)
        end_date = datetime.date(year, month, 24)

        # Total days in this calculation period (inclusive) is equal to total days in previous month
        total_days = (end_date - start_date).days + 1

        # Timezone-aware datetimes for range queries
        start_datetime = make_aware(datetime.datetime.combine(start_date, datetime.time.min))
        end_datetime = make_aware(datetime.datetime.combine(end_date, datetime.time.max))
        
        if employee_id:
            try:
                active_employees = Employee.objects.filter(id=int(employee_id))
                if not active_employees.exists():
                    return Response({"error": "Employee not found."}, status=status.HTTP_404_NOT_FOUND)
            except ValueError:
                return Response({"error": "Invalid employee ID format."}, status=status.HTTP_400_BAD_REQUEST)
        else:
            active_employees = Employee.objects.filter(status='active')
            
        branches = get_allowed_branches(request.user, "payroll")
        if "All" not in branches:
            active_employees = active_employees.filter(branch__in=branches)
            if employee_id and not active_employees.exists():
                return Response({"error": "You do not have permission to run payroll for this employee's branch."}, status=status.HTTP_403_FORBIDDEN)
                
        created_count = 0
        updated_count = 0

        for emp in active_employees:
            # 2. Calculate LOP days based on Attendance Table within the range
            lop_days = Decimal(Attendance.objects.filter(
                employee=emp,
                intime__range=(start_datetime, end_datetime),
                status='Absent'
            ).count())

            # 2b. Offset LOP with the employee's earned casual-leave balance
            # (6-month rule, from their real date_of_joining). CL days become
            # paid, so they are not deducted. Zero for employees without a DOJ.
            cl_available = casual_leave_available(emp, year, month)
            cl_apply = min(cl_available, lop_days)

            # 3-6. All earnings/deductions/net math lives in one shared helper.
            defaults = compute_payslip_fields(emp, total_days, lop_days, casual_leave_days=cl_apply)
            defaults['status'] = 'Generated'

            # Save / Update Record
            payslip, created = Payslip.objects.update_or_create(
                employee=emp,
                month=month,
                year=year,
                defaults=defaults,
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        emp_name = active_employees.first().employee_name if (employee_id and active_employees.exists()) else None
        msg_detail = f"Processed slip for {emp_name} for {month}/{year}." if emp_name else f"Processed slips for {month}/{year} with pro-rated structural logic."

        return Response({
            "message": msg_detail,
            "created": created_count,
            "updated": updated_count
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def recalculate(self, request, pk=None):
        """Recompute a single payslip from operator-supplied overrides.

        Accepts any of: total_days, lop_days, off_days, paid_days,
        other_deduction. The same pro-rata math as generate_all is applied via
        compute_payslip_fields, so a manually edited slip is computed with
        exactly the same rules as an auto-generated one. Only this one payslip
        is touched.

        Days resolution:
          - total_days defaults to the slip's current value.
          - off_days (paid weekly-offs/holidays) offset LOP; default to current.
          - If paid_days is given, it is treated as the absolute worked+paid
            figure: effective unpaid = total - paid_days (off reset to 0), so an
            operator can enter a fractional figure like 25.05.
          - Else lop_days is used (default current), reduced by off_days.
        """
        payslip = self.get_object()  # respects branch/role scoping in get_queryset

        # Block editing a slip that has already been paid out.
        if payslip.status == 'Paid':
            return Response(
                {"error": "This payslip is already marked Paid and cannot be recalculated."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        def to_decimal(value, field_name):
            try:
                return Decimal(str(value))
            except (ValueError, ArithmeticError):
                raise ValueError(f"{field_name} must be a number.")

        try:
            # total_days
            if request.data.get('total_days') is not None:
                total_days = to_decimal(request.data.get('total_days'), 'total_days')
            else:
                total_days = Decimal(payslip.total_days or 30)

            if total_days <= 0:
                return Response({"error": "total_days must be greater than 0."},
                                status=status.HTTP_400_BAD_REQUEST)

            # Off days (paid weekly-offs/holidays) default to the slip's current value.
            if request.data.get('off_days') is not None:
                off_days = to_decimal(request.data.get('off_days'), 'off_days')
            else:
                off_days = Decimal(payslip.off_days or 0)

            # Days can be set by paid_days (absolute worked days) or lop_days.
            if request.data.get('paid_days') is not None:
                paid_days = to_decimal(request.data.get('paid_days'), 'paid_days')
                # Absolute worked figure: express it purely as LOP, no off offset.
                lop_days = total_days - paid_days
                off_days = Decimal(0)
            elif request.data.get('lop_days') is not None:
                lop_days = to_decimal(request.data.get('lop_days'), 'lop_days')
            else:
                lop_days = Decimal(payslip.lop_days or 0)

            # Casual leave: when an absolute paid_days is supplied that figure is
            # final, so CL must not further reduce LOP. Otherwise preserve the
            # slip's current CL (auto-applied at generation) unless overridden.
            if request.data.get('paid_days') is not None:
                cl_days = Decimal(0)
            elif request.data.get('casual_leave_used') is not None:
                cl_days = to_decimal(request.data.get('casual_leave_used'), 'casual_leave_used')
            else:
                cl_days = Decimal(payslip.casual_leave_used or 0)

            # Optional other-deduction override.
            other_override = None
            if request.data.get('other_deduction') is not None:
                other_override = to_decimal(request.data.get('other_deduction'), 'other_deduction')
                if other_override < 0:
                    return Response({"error": "other_deduction cannot be negative."},
                                    status=status.HTTP_400_BAD_REQUEST)
        except ValueError as exc:
            return Response({"error": str(exc)}, status=status.HTTP_400_BAD_REQUEST)

        if lop_days < 0 or lop_days > total_days:
            return Response(
                {"error": f"Worked/LOP days are out of range for a {total_days}-day cycle."},
                status=status.HTTP_400_BAD_REQUEST,
            )
        if off_days < 0 or off_days > total_days:
            return Response(
                {"error": f"Off days are out of range for a {total_days}-day cycle."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        defaults = compute_payslip_fields(
            payslip.employee, total_days, lop_days,
            other_deduction_override=other_override,
            off_days=off_days,
            casual_leave_days=cl_days,
        )
        for field, value in defaults.items():
            setattr(payslip, field, value)
        payslip.save()

        serializer = self.get_serializer(payslip)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def revert(self, request, pk=None):
        """Undo manual edits: recompute this payslip straight from attendance.

        Re-derives LOP days from the Attendance table for the slip's own
        cycle (25th of previous month to 24th of the slip's month) and applies
        the standard structural math — exactly what bulk generation would
        produce. This discards any manual total_days / lop_days / other_deduction
        overrides for this one slip.
        """
        payslip = self.get_object()

        if payslip.status == 'Paid':
            return Response(
                {"error": "This payslip is already marked Paid and cannot be reverted."},
                status=status.HTTP_400_BAD_REQUEST,
            )

        import datetime
        from django.utils.timezone import make_aware

        month, year = payslip.month, payslip.year
        if month == 1:
            prev_month, prev_year = 12, year - 1
        else:
            prev_month, prev_year = month - 1, year

        start_date = datetime.date(prev_year, prev_month, 25)
        end_date = datetime.date(year, month, 24)
        total_days = (end_date - start_date).days + 1

        start_datetime = make_aware(datetime.datetime.combine(start_date, datetime.time.min))
        end_datetime = make_aware(datetime.datetime.combine(end_date, datetime.time.max))

        lop_days = Decimal(Attendance.objects.filter(
            employee=payslip.employee,
            intime__range=(start_datetime, end_datetime),
            status='Absent'
        ).count())

        defaults = compute_payslip_fields(payslip.employee, total_days, lop_days)
        defaults['status'] = 'Generated'
        for field, value in defaults.items():
            setattr(payslip, field, value)
        payslip.save()

        serializer = self.get_serializer(payslip)
        return Response(serializer.data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def cycles_summary(self, request):
        """Aggregates monthly payslip records to provide enterprise-wide Payroll status cycles."""
        scope = self._report_scope(request)
        if scope is False:
            return Response({"error": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)

        branch = request.query_params.get('branch')
        qs = Payslip.objects.all()
        if scope is not None:
            qs = qs.filter(employee__branch__in=scope)
        if branch:
            qs = qs.filter(employee__branch=branch)

        from django.db.models import Count, Sum
        summary = qs.values('year', 'month').annotate(
            employees_count=Count('id'),
            total_gross=Sum('gross_earnings'),
            total_net=Sum('net_salary')
        ).order_by('-year', '-month')

        data = []
        month_names = ["", "Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"]
        for item in summary:
            m = item['month']
            m_name = month_names[m] if 1 <= m <= 12 else str(m)
            data.append({
                "period": f"{m_name} {item['year']}",
                "employees": item['employees_count'],
                "gross": f"₹{int(item['total_gross']):,}" if item['total_gross'] else "₹0",
                "net": f"₹{int(item['total_net']):,}" if item['total_net'] else "₹0",
                "status": "Completed"
            })
        return Response(data, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def company_stats(self, request):
        """Calculates detailed monthly payroll metrics per selected month, year, and branch."""
        from django.db.models import Sum, Count
        from django.utils import timezone

        now = timezone.now()
        month = request.query_params.get('month', now.month)
        year = request.query_params.get('year', now.year)
        branch = request.query_params.get('branch')

        try:
            month = int(month)
            year = int(year)
        except (ValueError, TypeError):
            month = now.month
            year = now.year

        scope = self._report_scope(request)
        if scope is False:
            return Response({"error": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)

        # Active employees in scope
        emp_qs = Employee.objects.filter(status='active')
        if scope is not None:
            emp_qs = emp_qs.filter(branch__in=scope)
        if branch:
            emp_qs = emp_qs.filter(branch__iexact=branch)

        total_active_employees = emp_qs.count()
        total_base_salary = emp_qs.aggregate(s=Sum('salary'))['s'] or Decimal('0.00')

        # Payslips in scope for selected month and year
        slip_qs = Payslip.objects.filter(month=month, year=year)
        if scope is not None:
            slip_qs = slip_qs.filter(employee__branch__in=scope)
        if branch:
            slip_qs = slip_qs.filter(employee__branch__iexact=branch)

        slips_count = slip_qs.count()
        total_generated_gross = slip_qs.aggregate(s=Sum('gross_earnings'))['s'] or Decimal('0.00')
        total_generated_net = slip_qs.aggregate(s=Sum('net_salary'))['s'] or Decimal('0.00')
        total_deductions = slip_qs.aggregate(s=Sum('gross_deductions'))['s'] or Decimal('0.00')

        paid_slips = slip_qs.filter(status='Paid')
        paid_count = paid_slips.count()
        paid_amount = paid_slips.aggregate(s=Sum('net_salary'))['s'] or Decimal('0.00')

        unpaid_slips = slip_qs.exclude(status='Paid')
        unpaid_count = unpaid_slips.count()
        unpaid_amount = unpaid_slips.aggregate(s=Sum('net_salary'))['s'] or Decimal('0.00')

        # Cumulative YTD net for the selected year
        ytd_slips = Payslip.objects.filter(year=year)
        if branch:
            ytd_slips = ytd_slips.filter(employee__branch__iexact=branch)
        ytd_total_net = ytd_slips.aggregate(s=Sum('net_salary'))['s'] or Decimal('0.00')

        def fmt(val):
            fval = float(val)
            if fval >= 100000:
                return f"₹{(fval/100000.0):.2f}L"
            if fval >= 1000:
                return f"₹{(fval/1000.0):.1f}K"
            return f"₹{int(fval):,}"

        return Response({
            "month": month,
            "year": year,
            "totalActiveEmployees": total_active_employees,
            "totalBaseSalaryRaw": float(total_base_salary),
            "totalBaseSalaryFmt": f"₹{int(total_base_salary):,}",
            "generatedGrossRaw": float(total_generated_gross),
            "generatedGrossFmt": f"₹{int(total_generated_gross):,}",
            "generatedNetRaw": float(total_generated_net),
            "generatedNetFmt": f"₹{int(total_generated_net):,}",
            "totalDeductionsRaw": float(total_deductions),
            "totalDeductionsFmt": f"₹{int(total_deductions):,}",
            "slipsCount": slips_count,
            "notGeneratedCount": max(0, total_active_employees - slips_count),
            "paidCount": paid_count,
            "paidAmountRaw": float(paid_amount),
            "paidAmountFmt": f"₹{int(paid_amount):,}",
            "unpaidCount": unpaid_count,
            "unpaidAmountRaw": float(unpaid_amount),
            "unpaidAmountFmt": f"₹{int(unpaid_amount):,}",
            "ytdTotalRaw": float(ytd_total_net),
            "ytdTotalFmt": fmt(ytd_total_net),
            "thisMonth": fmt(total_generated_net),
            "ytdTotal": fmt(ytd_total_net),
            "pending": f"₹{int(unpaid_amount):,}",
            "processed": f"{round((slips_count / total_active_employees)*100)}%" if total_active_employees > 0 else "0%"
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def dashboard_summary(self, request):
        """Aggregates complete enterprise metrics to power the Corporate Dashboard view."""
        from django.db.models import Sum, Avg, Count
        from django.utils import timezone
        import calendar
        from employees.models import Employee
        from onboarding.models import Onboarding
        
        scope = self._report_scope(request)
        if scope is False:
            return Response({"error": "Not permitted."}, status=status.HTTP_403_FORBIDDEN)

        branch = request.query_params.get('branch')

        active_emps = Employee.objects.filter(status='active')
        if scope is not None:
            active_emps = active_emps.filter(branch__in=scope)
        if branch:
            active_emps = active_emps.filter(branch__iexact=branch)

        total_employees = active_emps.count()

        # Avg Salary of active employees
        avg_salary_raw = active_emps.aggregate(a=Avg('salary'))['a'] or 0

        # Overall Lifetime Payroll distributed
        payslips = Payslip.objects.all()
        if scope is not None:
            payslips = payslips.filter(employee__branch__in=scope)
        if branch:
            payslips = payslips.filter(employee__branch__iexact=branch)
        total_payroll_raw = payslips.aggregate(s=Sum('net_salary'))['s'] or 0

        # Pending onboarding processes
        pending_onboards = Onboarding.objects.exclude(status='Completed')
        if scope is not None:
            pending_onboards = pending_onboards.filter(work_location__in=scope)
        if branch:
            pending_onboards = pending_onboards.filter(work_location__icontains=branch)
        pending_approvals = pending_onboards.count()
        
        # Recent Payslips (last 5)
        recent_slips = payslips.select_related('employee').order_by('-id')[:5]
        recent_txns = []
        for s in recent_slips:
            recent_txns.append({
                "name": s.employee.employee_name,
                "dept": getattr(s.employee, 'department', 'Staff') or 'Staff',
                "amount": f"₹{int(s.net_salary):,}",
                "status": "Paid"
            })
            
        def fmt_dashboard(val):
            fval = float(val)
            if fval >= 100000:
                return f"₹{(fval/100000.0):.2f}L"
            if fval >= 1000:
                return f"₹{(fval/1000.0):.1f}K"
            return f"₹{int(fval):,}"
            
        now = timezone.now()

        # Last 8 calendar months, driven by generated payslip records.
        trend_months = []
        cursor_year = now.year
        cursor_month = now.month
        for _ in range(8):
            trend_months.append((cursor_year, cursor_month))
            cursor_month -= 1
            if cursor_month == 0:
                cursor_month = 12
                cursor_year -= 1
        trend_months.reverse()

        payroll_trend = []
        if Payslip.objects.exists():
            for year, month in trend_months:
                month_slips = Payslip.objects.filter(year=year, month=month)
                if branch:
                    month_slips = month_slips.filter(employee__branch__iexact=branch)
                totals = month_slips.aggregate(
                    payroll=Sum('net_salary'),
                    incentive=Sum('earned_incentive'),
                    other=Sum('earned_other_earnings'),
                )
                payroll_trend.append({
                    "m": calendar.month_abbr[month],
                    "period": f"{calendar.month_abbr[month]} {year}",
                    "payroll": float(totals["payroll"] or 0),
                    "bonus": float((totals["incentive"] or 0) + (totals["other"] or 0)),
                })

        department_rows = active_emps.values('department').annotate(
            count=Count('id')
        ).order_by('-count', 'department')
        department_split = []
        for row in department_rows:
            count = row['count']
            department_split.append({
                "name": row['department'] or "Unassigned",
                "count": count,
                "value": round((count / total_employees) * 100, 1) if total_employees else 0,
            })

        total_disburse_raw = active_emps.aggregate(s=Sum('salary'))['s'] or 0

        # Determine upcoming cycle ending on the 24th
        if now.day >= 25:
            # Current date is on or after 25th, upcoming payout cycle ends on 24th of next month
            if now.month == 12:
                cycle_year = now.year + 1
                cycle_month = 1
            else:
                cycle_year = now.year
                cycle_month = now.month + 1
        else:
            # Current date is before 25th, upcoming payout cycle ends on 24th of current month
            cycle_year = now.year
            cycle_month = now.month

        cycle_date = timezone.datetime(cycle_year, cycle_month, 24)
        days_remaining = (cycle_date.date() - now.date()).days
        if days_remaining < 0:
            days_remaining = 0
            
        return Response({
            "totalPayroll": fmt_dashboard(total_payroll_raw),
            "totalEmployees": f"{total_employees:,}",
            "avgSalary": fmt_dashboard(avg_salary_raw),
            "pendingApprovals": f"{pending_approvals}",
            
            "recentTransactions": recent_txns,
            "payrollTrend": payroll_trend,
            "departmentSplit": department_split,
            
            "upcoming": {
                "totalToDisburse": f"₹{int(total_disburse_raw):,}",
                "date": cycle_date.strftime("%b %d, %Y"),
                "daysLeft": f"In {days_remaining} days" if days_remaining > 0 else "Today",
                "employees": f"{total_employees:,}",
                "avgPayout": fmt_dashboard(avg_salary_raw)
            }
        }, status=status.HTTP_200_OK)

    @action(detail=True, methods=['post'])
    def email_payslip(self, request, pk=None):
        """Sends the payslip details as a formatted HTML email to the employee."""
        from django.core.mail import EmailMultiAlternatives
        from django.utils.html import strip_tags
        
        payslip = self.get_object()
        emp = payslip.employee
        
        if not emp.email:
            return Response({"error": "Employee does not have a registered email address."}, status=status.HTTP_400_BAD_REQUEST)
        
        month_name = calendar.month_name[payslip.month]
        period_str = f"{month_name} {payslip.year}"
        
        subject = f"Salary Payslip for {period_str} - Renderways Technology"
        
        html_content = f"""
        <html>
        <body style="font-family: Arial, sans-serif; color: #333333; line-height: 1.6; margin: 0; padding: 20px; background-color: #f3f4f6;">
            <div style="max-width: 600px; margin: 0 auto; background-color: #ffffff; padding: 30px; border-radius: 12px; box-shadow: 0 4px 6px rgba(0,0,0,0.05); border-top: 6px solid #4f46e5;">
                <div style="text-align: center; margin-bottom: 25px;">
                    <h2 style="margin: 0; color: #4f46e5;">RENDERWAYS TECHNOLOGY</h2>
                    <p style="margin: 5px 0 0 0; color: #6b7280; font-size: 14px; font-weight: bold; letter-spacing: 0.5px;">SALARY PAYSLIP ADVICE</p>
                </div>
                
                <p>Dear <strong>{emp.employee_name}</strong>,</p>
                <p>Your payslip for the month of <strong>{period_str}</strong> has been generated. Please find the details of your earnings and deductions below:</p>
                
                <table style="width: 100%; border-collapse: collapse; margin: 20px 0; border: 1px solid #e5e7eb;">
                    <tr style="background-color: #f9fafb; font-weight: bold; border-bottom: 2px solid #e5e7eb;">
                        <th style="padding: 10px; text-align: left; font-size: 13px;">Salary Detail Component</th>
                        <th style="padding: 10px; text-align: right; font-size: 13px;">Amount (₹)</th>
                    </tr>
                    <tr>
                        <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">Basic Salary</td>
                        <td style="padding: 10px; text-align: right; border-bottom: 1px solid #e5e7eb; font-family: monospace;">{float(payslip.earned_basic):,.2f}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">House Rent Allowance (HRA)</td>
                        <td style="padding: 10px; text-align: right; border-bottom: 1px solid #e5e7eb; font-family: monospace;">{float(payslip.earned_hra):,.2f}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">Incentives & Performance Bonus</td>
                        <td style="padding: 10px; text-align: right; border-bottom: 1px solid #e5e7eb; font-family: monospace;">{float(payslip.earned_incentive):,.2f}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px; border-bottom: 1px solid #e5e7eb;">Conveyance / Other Allowances</td>
                        <td style="padding: 10px; text-align: right; border-bottom: 1px solid #e5e7eb; font-family: monospace;">{float(payslip.earned_conveyance + payslip.earned_child_edu + payslip.earned_personal + payslip.earned_other_earnings):,.2f}</td>
                    </tr>
                    <tr style="background-color: #f9fafb; font-weight: bold;">
                        <td style="padding: 10px; border-bottom: 2px solid #e5e7eb; color: #4f46e5;">Gross Earnings (A)</td>
                        <td style="padding: 10px; text-align: right; border-bottom: 2px solid #e5e7eb; font-family: monospace; color: #4f46e5;">{float(payslip.gross_earnings):,.2f}</td>
                    </tr>
                    <tr>
                        <td style="padding: 10px; border-bottom: 1px solid #e5e7eb; color: #dc2626;">Total Deductions (EPF, ESI, TDS) (B)</td>
                        <td style="padding: 10px; text-align: right; border-bottom: 1px solid #e5e7eb; font-family: monospace; color: #dc2626;">{float(payslip.total_deductions):,.2f}</td>
                    </tr>
                    <tr style="background-color: #f0fdf4; font-weight: bold; border-top: 2px solid #bbf7d0;">
                        <td style="padding: 12px; font-size: 15px; color: #166534;">NET TAKE HOME SALARY</td>
                        <td style="padding: 12px; text-align: right; font-size: 16px; font-family: monospace; color: #166534;">₹{float(payslip.net_salary):,.2f}</td>
                    </tr>
                </table>
                
                <div style="background-color: #fef3c7; border-left: 4px solid #f59e0b; padding: 12px; border-radius: 4px; margin-top: 20px; font-size: 12px; color: #78350f;">
                    <strong>Note:</strong> This is an automatically generated salary slip email notification advice. If you have any queries regarding your payout or deductions, please contact the HR department.
                </div>
                
                <div style="margin-top: 30px; border-top: 1px solid #e5e7eb; padding-top: 15px; text-align: center; font-size: 12px; color: #9ca3af;">
                    &copy; {payslip.year} Renderways Technology Pvt Ltd. All rights reserved.
                </div>
            </div>
        </body>
        </html>
        """
        
        text_content = strip_tags(html_content)
        
        try:
            from django.conf import settings
            from django.core.mail import get_connection
            
            # Use console backend if SMTP credentials are not filled out in .env
            if not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD:
                connection = get_connection(backend='django.core.mail.backends.console.EmailBackend')
                from_email = 'payroll@renderways.com'
            else:
                connection = get_connection(
                    backend='django.core.mail.backends.smtp.EmailBackend',
                    host=settings.EMAIL_HOST,
                    port=settings.EMAIL_PORT,
                    username=settings.EMAIL_HOST_USER,
                    password=settings.EMAIL_HOST_PASSWORD,
                    use_tls=settings.EMAIL_USE_TLS,
                    use_ssl=settings.EMAIL_USE_SSL,
                )
                from_email = settings.EMAIL_HOST_USER
                
            email = EmailMultiAlternatives(
                subject=subject,
                body=text_content,
                from_email=from_email,
                to=[emp.email],
                connection=connection
            )
            email.attach_alternative(html_content, "text/html")
            email.send()
            
            msg = f"Payslip successfully emailed to {emp.email}"
            if not settings.EMAIL_HOST_USER or not settings.EMAIL_HOST_PASSWORD:
                msg += " (Printed to backend console - SMTP credentials not set in .env)"
                
            return Response({"success": msg}, status=status.HTTP_200_OK)
        except Exception as err:
            return Response({"error": f"Failed to send email: {str(err)}"}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)

    @action(detail=False, methods=['get'])
    def pl_summary(self, request):
        """Calculates branch-wise revenue, payroll costs, operational costs and P&L results."""
        now = timezone.now()
        month = request.query_params.get('month', now.month)
        year = request.query_params.get('year', now.year)

        try:
            month = int(month)
            year = int(year)
        except ValueError:
            return Response({"error": "Invalid month or year."}, status=status.HTTP_400_BAD_REQUEST)

        branches = ["Chennai", "Vellore", "Salem", "Kanchipuram", "Hosur"]
        allowed_branches = get_allowed_branches(request.user, "reports")
        if "All" not in allowed_branches:
            branches = [b for b in branches if b in allowed_branches]
        data = []

        from django.db.models import Sum
        
        for br in branches:
            # 1. Fetch financials
            fin = BranchFinancial.objects.filter(branch=br, month=month, year=year).first()
            revenue = fin.revenue if fin else Decimal('0.00')
            other_expenses = fin.other_expenses if fin else Decimal('0.00')

            # 2. Calculate payroll cost
            branch_slips = Payslip.objects.filter(employee__branch=br, month=month, year=year)
            sums = branch_slips.aggregate(
                net=Sum('net_salary'),
                epf=Sum('employer_epf'),
                esi=Sum('employer_esi'),
                ins=Sum('employer_insurance'),
                petrol=Sum('petrol_allowance')
            )
            net_sum = sums['net'] or Decimal('0.00')
            epf_sum = sums['epf'] or Decimal('0.00')
            esi_sum = sums['esi'] or Decimal('0.00')
            ins_sum = sums['ins'] or Decimal('0.00')
            petrol_sum = sums['petrol'] or Decimal('0.00')

            payroll_cost = net_sum + epf_sum + esi_sum + ins_sum + petrol_sum
            total_expenses = payroll_cost + other_expenses
            net_profit_loss = revenue - total_expenses
            
            margin = float((net_profit_loss / revenue) * 100) if revenue > Decimal('0.00') else 0.0

            data.append({
                "branch": br,
                "revenue": float(revenue),
                "other_expenses": float(other_expenses),
                "payroll_cost": float(payroll_cost),
                "total_expenses": float(total_expenses),
                "net_profit_loss": float(net_profit_loss),
                "margin": round(margin, 2),
                "id": fin.id if fin else None
            })

        return Response(data, status=status.HTTP_200_OK)


class BranchFinancialViewSet(viewsets.ModelViewSet):
    queryset = BranchFinancial.objects.all()
    serializer_class = BranchFinancialSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        queryset = super().get_queryset()
        user = self.request.user
        branches = get_allowed_branches(user, "payroll")
        if "All" not in branches:
            queryset = queryset.filter(branch__in=branches)
        month = self.request.query_params.get("month")
        year = self.request.query_params.get("year")
        if month:
            queryset = queryset.filter(month=month)
        if year:
            queryset = queryset.filter(year=year)
        return queryset

    def _check_branch_allowed(self, serializer):
        """Block writing financials for a branch the user does not control."""
        user = self.request.user
        allowed = get_allowed_branches(user, "payroll")
        if "All" in allowed:
            return
        target_branch = serializer.validated_data.get("branch")
        if target_branch and target_branch not in allowed:
            raise PermissionDenied(
                f"You are not allowed to manage financials for branch '{target_branch}'."
            )

    def perform_create(self, serializer):
        self._check_branch_allowed(serializer)
        serializer.save()

    def perform_update(self, serializer):
        self._check_branch_allowed(serializer)
        serializer.save()
