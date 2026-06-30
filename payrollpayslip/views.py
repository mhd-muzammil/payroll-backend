from django.db.models import Q
from rest_framework import viewsets, status
from rest_framework.decorators import action
from rest_framework.response import Response
from rest_framework.permissions import IsAuthenticated
from django.utils import timezone
from decimal import Decimal, ROUND_HALF_UP
import calendar

from .models import Payslip, BranchFinancial
from .serializer import PayslipSerializer, BranchFinancialSerializer
from employees.models import Employee, Performance
from attendance.models import Attendance

class PayslipViewSet(viewsets.ModelViewSet):
    queryset = Payslip.objects.all()
    serializer_class = PayslipSerializer
    permission_classes = [IsAuthenticated]

    def get_queryset(self):
        user = self.request.user
        queryset = Payslip.objects.all().order_by("-year", "-month", "-id")
        role = "superadmin" if user.is_superuser else getattr(user, 'role', 'employee')
        if role == "employee":
            return queryset.filter(employee__user=user)
        return queryset

    @action(detail=False, methods=['post'])
    def generate_all(self, request):
        """
        Bulk generates/calculates detailed payslips based on pro-rated Indian salary structures
        as provided by user guidelines.
        """
        now = timezone.now()
        month = request.data.get('month', now.month)
        year = request.data.get('year', now.year)

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
        
        active_employees = Employee.objects.filter(status='active')
        created_count = 0
        updated_count = 0

        def q(val):
            """Helper to round Decimal value to two decimal places."""
            return Decimal(val).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP)

        for emp in active_employees:
            # 2. Calculate LOP days based on Attendance Table within the range
            lop_days = Decimal(Attendance.objects.filter(
                employee=emp,
                intime__range=(start_datetime, end_datetime),
                status='Absent'
            ).count())

            paid_days = Decimal(total_days) - lop_days
            if paid_days < 0:
                paid_days = Decimal(0)

            # Pro-rata ratio
            multiplier = paid_days / Decimal(total_days)

            # 3. Gross Structure (Base Fixed Monthly Salary)
            # If Employee has been configured with a base structure, use it!
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
                gross_basic = q(gross_salary * Decimal('0.50')) # 50% Basic
                gross_hra = q(gross_salary * Decimal('0.25'))   # 25% HRA
                gross_conveyance = Decimal('1600.00')
                gross_child_edu = Decimal('200.00')
                
                # Handled fallback in case overall salary is low
                if (gross_basic + gross_hra + gross_conveyance + gross_child_edu) > gross_salary:
                    gross_conveyance = Decimal(0)
                    gross_child_edu = Decimal(0)
                
                gross_personal = gross_salary - gross_basic - gross_hra - gross_conveyance - gross_child_edu
                if gross_personal < 0:
                    gross_personal = Decimal(0)
                gross_incentive = Decimal('0.00')
                gross_other_earnings = Decimal('0.00')
                # Re-adjust gross total
                gross_salary = gross_basic + gross_hra + gross_conveyance + gross_child_edu + gross_personal

            # 4. Earned Components (Pro-rated based on paid_days / total_days)
            earned_basic = q(gross_basic * multiplier)
            earned_hra = q(gross_hra * multiplier)
            earned_conveyance = q(gross_conveyance * multiplier)
            earned_child_edu = q(gross_child_edu * multiplier)
            earned_personal = q(gross_personal * multiplier)
            
            # Dynamic incentive policy:
            # 1. Perfect Attendance Bonus: ₹1000 if 0 LOP days
            # 2. Performance Review Bonus: ₹2000 if rating >= 4.5, ₹1000 if rating >= 4.0
            dynamic_incentive = Decimal('0.00')
            if lop_days == 0:
                dynamic_incentive += Decimal('1000.00')
            
            month_name = calendar.month_name[month]
            period_format_1 = f"{month_name} {year}"
            period_format_2 = f"{month}/{year}"
            period_format_3 = f"{month:02d}/{year}"
            
            perf = Performance.objects.filter(
                Q(review_period__iexact=period_format_1) | 
                Q(review_period__iexact=period_format_2) |
                Q(review_period__iexact=period_format_3),
                employee=emp
            ).first()
            
            if perf:
                perf_score = perf.overall_score
                if perf_score >= Decimal('4.50'):
                    dynamic_incentive += Decimal('2000.00')
                elif perf_score >= Decimal('4.00'):
                    dynamic_incentive += Decimal('1000.00')
            
            earned_incentive = q(gross_incentive * multiplier) + dynamic_incentive
            earned_other_earnings = q(gross_other_earnings * multiplier)
            
            # Calculate Earned Gross Sum
            gross_earnings = earned_basic + earned_hra + earned_conveyance + earned_child_edu + earned_personal + earned_incentive + earned_other_earnings

            # 5. Deductions Components
            if emp.basic > Decimal('0.00'):
                # Pro-rate PF and ESI based on attendance, other fixed deductions taken fully
                deduction_epf = q(emp.epf * multiplier)
                deduction_esi = q(emp.esi * multiplier)
                deduction_prof_tax = emp.prof_tax
                deduction_lwf = emp.lwf
                deduction_staff_advance = emp.staff_advance
                deduction_tds = emp.tds
                deduction_other = emp.other_deduction
                deduction_insurance = emp.deduction_insurance
                
                # Pro-rate Employer contributions by attendance as well
                employer_epf = q(emp.employer_epf * multiplier)
                employer_esi = q(emp.employer_esi * multiplier)
                employer_insurance = emp.employer_insurance
                petrol_allowance = q(emp.petrol_allowance * multiplier)
            else:
                # Legacy/Fallback deduction calculation
                deduction_epf = q(earned_basic * Decimal('0.12'))
                deduction_prof_tax = Decimal('208.30') if gross_salary > 12000 else Decimal('0.00')
                deduction_esi = Decimal('0.00')
                deduction_lwf = Decimal('0.00')
                deduction_staff_advance = Decimal('0.00')
                deduction_tds = Decimal('0.00')
                deduction_other = Decimal('0.00')
                deduction_insurance = Decimal('0.00')
                
                # Fallback Employer Benefits
                employer_epf = q(earned_basic * Decimal('0.13')) # Standard 13% fallback
                employer_esi = Decimal('0.00')
                employer_insurance = Decimal('0.00')
                petrol_allowance = Decimal('0.00')
            
            gross_deductions = deduction_epf + deduction_esi + deduction_prof_tax + deduction_lwf + deduction_staff_advance + deduction_tds + deduction_other + deduction_insurance

            # 6. Net Take Home Salary (Rounded to nearest full integer)
            net_salary = gross_earnings - gross_deductions
            net_rounded = Decimal(net_salary).quantize(Decimal('1'), rounding=ROUND_HALF_UP)

            # Save / Update Record
            payslip, created = Payslip.objects.update_or_create(
                employee=emp,
                month=month,
                year=year,
                defaults={
                    'total_days': total_days,
                    'lop_days': lop_days,
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
                    'status': 'Generated'
                }
            )

            if created:
                created_count += 1
            else:
                updated_count += 1

        return Response({
            "message": f"Processed slips for {month}/{year} with pro-rated structural logic.",
            "created": created_count,
            "updated": updated_count
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def cycles_summary(self, request):
        """Aggregates monthly payslip records to provide enterprise-wide Payroll status cycles."""
        branch = request.query_params.get('branch')
        qs = Payslip.objects.all()
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
        """Calculates high-level corporate KPIs based on true computed data."""
        branch = request.query_params.get('branch')
        qs = Payslip.objects.all()
        if branch:
            qs = qs.filter(employee__branch=branch)

        from django.db.models import Sum
        from django.utils import timezone
        now = timezone.now()
        
        this_month_slips = qs.filter(month=now.month, year=now.year)
        total_this_month = this_month_slips.aggregate(s=Sum('net_salary'))['s'] or 0
        
        ytd_slips = qs.filter(year=now.year)
        total_ytd = ytd_slips.aggregate(s=Sum('net_salary'))['s'] or 0
        
        processed_pct = "100%" if total_this_month > 0 else "0%"
        
        def fmt(val):
            fval = float(val)
            if fval >= 100000:
                return f"₹{(fval/100000.0):.2f}L"
            if fval >= 1000:
                return f"₹{(fval/1000.0):.1f}K"
            return f"₹{int(fval):,}"
            
        return Response({
            "thisMonth": fmt(total_this_month),
            "ytdTotal": fmt(total_ytd),
            "pending": "₹0",
            "processed": processed_pct
        }, status=status.HTTP_200_OK)

    @action(detail=False, methods=['get'])
    def dashboard_summary(self, request):
        """Aggregates complete enterprise metrics to power the Corporate Dashboard view."""
        from django.db.models import Sum, Avg, Count
        from django.utils import timezone
        import calendar
        from employees.models import Employee
        from onboarding.models import Onboarding
        
        branch = request.query_params.get('branch')
        
        active_emps = Employee.objects.filter(status='active')
        if branch:
            active_emps = active_emps.filter(branch__iexact=branch)
            
        total_employees = active_emps.count()
        
        # Avg Salary of active employees
        avg_salary_raw = active_emps.aggregate(a=Avg('salary'))['a'] or 0
        
        # Overall Lifetime Payroll distributed
        payslips = Payslip.objects.all()
        if branch:
            payslips = payslips.filter(employee__branch__iexact=branch)
        total_payroll_raw = payslips.aggregate(s=Sum('net_salary'))['s'] or 0
        
        # Pending onboarding processes
        pending_onboards = Onboarding.objects.exclude(status='Completed')
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
        month = self.request.query_params.get("month")
        year = self.request.query_params.get("year")
        if month:
            queryset = queryset.filter(month=month)
        if year:
            queryset = queryset.filter(year=year)
        return queryset
