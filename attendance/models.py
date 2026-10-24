from decimal import Decimal

from django.db import models
from django.db.models import Count
from employees.models import Employee

# What a row of each unpaid status costs, in days of pay: a whole day for
# Absent, half a day for a Half Day -- the office's decisions, 24 Oct 2026.
LOSS_OF_PAY_DAYS = {"Absent": Decimal("1"), "Half Day": Decimal("0.5")}


def loss_of_pay_days(rows):
    """The loss-of-pay days in `rows`, an Attendance queryset: one for each
    Absent row and a half for each Half Day row."""
    counts = rows.filter(status__in=LOSS_OF_PAY_DAYS).values("status").annotate(n=Count("id"))
    return sum((LOSS_OF_PAY_DAYS[c["status"]] * c["n"] for c in counts), Decimal(0))

# Create your models here.
class Attendance(models.Model):
    STATUS_CHOICES = (
        ('Present', 'Present'),
        ('Absent', 'Absent'),
        ('Leave', 'Leave'),
        # A half day the office marks. A payslip counts it as half a day's loss
        # of pay (LOSS_OF_PAY_DAYS), and the register names it apart so it is
        # not read as a day somebody failed to turn up.
        ('Half Day', 'Half Day'),
        ('Late', 'Late'),
        ('Overtime', 'Overtime'),
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="attendances",
        null=True,
        blank=True,
    )
    employee_name = models.CharField(max_length=100)
    role = models.CharField(max_length=100)
    department = models.CharField(max_length=100)
    salary = models.DecimalField(max_digits=10, decimal_places=2)
    intime = models.DateTimeField(null=True, blank=True, db_index=True)
    outtime = models.DateTimeField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=STATUS_CHOICES)

    # The day was closed by the 11.59pm rule, not by the person.
    #
    # People forget to press Logout, and the day then reads 0.0h forever. It is
    # closed for them at 11.59pm so the register has an answer -- but an answer
    # nobody gave has to be marked as such, or the office cannot tell a
    # fifteen-hour day somebody worked from a Logout they never pressed. Shaped
    # after DutySession.auto_closed, which exists for exactly this reason.
    auto_closed_out = models.BooleanField(default=False)

    def __str__(self):
        return f"{self.employee_name} - {self.intime.date() if self.intime else 'No Date'}"


class LeaveRequest(models.Model):
    LEAVE_TYPE_CHOICES = (
        ('Leave', 'Leave'),
        ('Permission', 'Permission'),
    )
    STATUS_CHOICES = (
        ('Pending', 'Pending'),
        ('Approved', 'Approved'),
        ('Rejected', 'Rejected'),
    )
    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="leave_requests"
    )
    leave_type = models.CharField(max_length=20, choices=LEAVE_TYPE_CHOICES, default='Leave')
    reason = models.TextField()
    start_date = models.DateField()
    end_date = models.DateField(null=True, blank=True) # Used primarily for Leaves
    start_time = models.TimeField(null=True, blank=True) # Used for short permissions
    end_time = models.TimeField(null=True, blank=True)   # Used for short permissions
    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='Pending')
    applied_on = models.DateTimeField(auto_now_add=True)
    
    def __str__(self):
        return f"{self.employee.employee_name} - {self.leave_type} ({self.status})"
