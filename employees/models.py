from django.db import models

# Create your models here.
class Employee(models.Model):
    user = models.OneToOneField('authentication.User', on_delete=models.CASCADE, related_name='employee_profile',    null=True,
    blank=True)
    STATUS_CHOICES = (
        ('active', 'Active'),
        ('inactive', 'Inactive'),
        ('onleave', 'On Leave'),
        # Left the company. Distinct from 'inactive', which is someone still on
        # the books who is not working at the moment: only 'relieved' drops off
        # the working screens and stops the login. Nothing of theirs is deleted,
        # so setting them back to Active in onboarding restores everything.
        ('relieved', 'Relieved'),
    )
    BRANCH_CHOICES = (
        ('Chennai', 'Chennai'),
        ('Vellore', 'Vellore'),
        ('Salem', 'Salem'),
        ('Kanchipuram', 'Kanchipuram'),
        ('Hosur', 'Hosur')
    )
    employee_name = models.CharField(max_length=100)
    branch = models.CharField(max_length=100, choices=BRANCH_CHOICES, default='Chennai')
    # Per-branch code as entered by HR in the attendance sheet (e.g. "1", "2", ...).
    # NOT globally unique — the same code maps to different people in different
    # branches — so it is only meaningful together with `branch`. Nullable/blank so
    # existing rows and non-imported employees are unaffected.
    emp_code = models.CharField(max_length=50, null=True, blank=True)
    email = models.EmailField(unique=True, max_length=100, null=True, blank=True)
    phone = models.CharField(max_length=20, unique=True, null=True, blank=True)
    role = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    salary = models.DecimalField(max_digits=10, decimal_places=2)
    
    # Detailed Earnings Breakdown
    basic = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    hra = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    conveyance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    child_edu = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    personal_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    incentive = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    other_earnings = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    
    # Detailed Deductions Breakdown
    epf = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    esi = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    prof_tax = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    lwf = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    staff_advance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    tds = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    other_deduction = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    deduction_insurance = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    # Benefits Group (CTC Components)
    employer_epf = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    employer_esi = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    employer_insurance = models.DecimalField(max_digits=10, decimal_places=2, default=0)
    petrol_allowance = models.DecimalField(max_digits=10, decimal_places=2, default=0)

    work_lat = models.FloatField(null=True, blank=True, help_text="Allowed work location latitude")
    work_lon = models.FloatField(null=True, blank=True, help_text="Allowed work location longitude")
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='active')
    # `joining_date` is auto-stamped at record creation and is NOT the real hire
    # date. `date_of_joining` is the real hire date (editable, from onboarding or
    # HR) and is what casual-leave accrual (6-month rule) is computed from.
    joining_date = models.DateField(auto_now_add=True)
    date_of_joining = models.DateField(null=True, blank=True, help_text="Real hire date; used for casual leave accrual")

    def __str__(self):
        return self.employee_name


class Task(models.Model):
    STATUS_CHOICES = (
        ('pending', 'Pending'),
        ('in_progress', 'In Progress'),
        ('completed', 'Completed'),
    )
    PRIORITY_CHOICES = (
        ('low', 'Low'),
        ('medium', 'Medium'),
        ('high', 'High'),
    )
    title = models.CharField(max_length=255)
    description = models.TextField()
    assigned_to = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='tasks')
    assigned_by = models.ForeignKey('authentication.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='assigned_tasks')
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='pending')
    priority = models.CharField(max_length=20, choices=PRIORITY_CHOICES, default='medium')
    due_date = models.DateField()
    employee_notes = models.TextField(blank=True, default='')
    checklist = models.JSONField(default=list, blank=True)
    activity_log = models.JSONField(default=list, blank=True)
    attachments = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.title} - {self.assigned_to.employee_name}"


class Performance(models.Model):
    employee = models.ForeignKey(Employee, on_delete=models.CASCADE, related_name='performances')
    reviewer = models.ForeignKey('authentication.User', on_delete=models.SET_NULL, null=True, blank=True, related_name='submitted_reviews')
    review_period = models.CharField(max_length=100)
    work_quality = models.IntegerField(default=5)
    attendance = models.IntegerField(default=5)
    communication = models.IntegerField(default=5)
    dependability = models.IntegerField(default=5)
    # max_digits=5 so a perfect 10-scale average (10.00) fits; max_digits=3
    # previously overflowed (DataError/500) whenever the average reached 10.
    overall_score = models.DecimalField(max_digits=5, decimal_places=2, default=5.0)
    comments = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def save(self, *args, **kwargs):
        total = int(self.work_quality) + int(self.attendance) + int(self.communication) + int(self.dependability)
        self.overall_score = round(total / 4.0, 2)
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.employee.employee_name} - {self.review_period}"


class Asset(models.Model):
    STATUS_CHOICES = (
        ('available', 'Available'),
        ('assigned', 'Assigned'),
        ('repair', 'Under Repair'),
        ('retired', 'Retired'),
    )
    asset_name = models.CharField(max_length=255)
    asset_type = models.CharField(max_length=100)
    serial_number = models.CharField(max_length=255, null=True, blank=True)
    status = models.CharField(max_length=50, choices=STATUS_CHOICES, default='available')
    assigned_to = models.ForeignKey(Employee, on_delete=models.SET_NULL, null=True, blank=True, related_name='assets')
    purchase_date = models.DateField(null=True, blank=True)
    cost = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    notes = models.TextField(blank=True, default='')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.asset_name} ({self.serial_number or 'No Serial'})"