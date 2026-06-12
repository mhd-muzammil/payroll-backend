from django.db import models
from employees.models import Employee

# Create your models here.
class Attendance(models.Model):
    STATUS_CHOICES = (
        ('Present', 'Present'),
        ('Absent', 'Absent'),
        ('Leave', 'Leave'),
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
