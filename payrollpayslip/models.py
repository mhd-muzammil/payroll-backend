from django.db import models
from employees.models import Employee

class Payslip(models.Model):
    STATUS_CHOICES = (
        ('Pending', 'Pending'),
        ('Generated', 'Generated'),
        ('Paid', 'Paid'),
    )
    
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='payslips')
    month = models.IntegerField()
    year = models.IntegerField()
    
    # Days Tracking (2 decimal places so partial days like 5.05 are preserved)
    total_days = models.IntegerField(default=30)
    lop_days = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    off_days = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    # Absent days paid via the employee's earned casual-leave balance this period.
    # Unlike off_days these do NOT offset LOP: the day counts stay exactly what
    # HR entered, and the leave is paid as its own line instead.
    casual_leave_used = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    # What those days are worth, at one day of gross per day taken. Added to the
    # net after deductions, and shown as its own row on the slip.
    casual_leave_pay = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    # Days worked beyond the normal cycle — a Sunday call-out, a festival shift.
    # Entered by HR, since nothing in attendance records that a day was extra
    # rather than ordinary. Purely additive: it has nothing to do with leave or
    # absence, so LOP, off days and the day counts are all untouched by it.
    special_work_days = models.DecimalField(max_digits=5, decimal_places=2, default=0.0)
    # What those days are worth, at one day of gross each, added to the net.
    special_work_pay = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    paid_days = models.DecimalField(max_digits=5, decimal_places=2, default=30.0)
    
    # Gross Salary Components (Defined in Structure)
    gross_basic = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_hra = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_conveyance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_child_edu = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_personal_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_incentive = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_other_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Gross Earnings Components (Actually Pro-rated based on worked days)
    earned_basic = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    earned_hra = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    earned_conveyance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    earned_child_edu = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    earned_personal_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    earned_incentive = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    earned_other_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Deductions Components
    deduction_epf = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deduction_esi = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deduction_prof_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deduction_lwf = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deduction_staff_advance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deduction_tds = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deduction_other = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deduction_insurance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    gross_deductions = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Benefits Group (CTC Components)
    employer_epf = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    employer_esi = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    employer_insurance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    petrol_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Net Take Home Salary
    net_salary = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Generated')
    created_at = models.DateTimeField(auto_now_add=True)
    
    class Meta:
        unique_together = ('employee', 'month', 'year')
        ordering = ['-year', '-month', '-created_at']

    def __str__(self):
        return f"Payslip - {self.employee.employee_name} ({self.month}/{self.year})"


class BranchFinancial(models.Model):
    BRANCH_CHOICES = (
        ('Chennai', 'Chennai'),
        ('Vellore', 'Vellore'),
        ('Salem', 'Salem'),
        ('Kanchipuram', 'Kanchipuram'),
        ('Hosur', 'Hosur')
    )
    branch = models.CharField(max_length=100, choices=BRANCH_CHOICES)
    month = models.IntegerField()
    year = models.IntegerField()
    revenue = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    other_expenses = models.DecimalField(max_digits=15, decimal_places=2, default=0)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('branch', 'month', 'year')
        ordering = ['-year', '-month', 'branch']

    def __str__(self):
        return f"{self.branch} Financials - {self.month}/{self.year}"