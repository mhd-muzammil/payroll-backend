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
    # WHEN the office released this slip to the employee, and null until they
    # do. Generating one used to publish it: the moment payroll ran, every
    # employee could open a slip nobody had checked yet. Now generating is
    # private and sending is a decision somebody makes.
    sent_at = models.DateTimeField(null=True, blank=True, db_index=True)

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
    
    # THIS MONTH'S AMOUNTS, as the office set them on this slip.
    #
    # Keys are the request names in views.MONTHLY_AMOUNTS (incentive,
    # other_earnings, staff_advance, tds, insurance, other_deduction), values
    # decimal strings. Kept so that editing a day count afterwards re-runs the
    # sums WITH them rather than over them. Empty means the slip is exactly
    # what the employee's structure produces. Undo Edits and Regenerate clear it.
    # Nullable on purpose: a NOT NULL column with no database default turns a
    # code rollback into every payslip INSERT failing. Read it as `or {}`.
    manual_overrides = models.JSONField(default=dict, blank=True, null=True)

    # THE SALARY STRUCTURE THIS SLIP WAS PRICED WITH -- the twenty employee
    # figures compute_payslip_fields reads, as they stood when the slip was
    # generated. Every edit re-prices from this, never from the employee, so a
    # raise given in October does not quietly re-price August the next time
    # somebody corrects August's TDS. Only Generate reads the employee and
    # writes this. Empty on slips generated before it existed.
    structure = models.JSONField(default=dict, blank=True, null=True)

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