from django.db import models

from employees.models import Employee


class EmployeeRequest(models.Model):
    """Something an employee asks the office for: money up front, or a matter
    they want looked at.

    Approving a money request records a DECISION only — it deliberately does
    not touch payroll. The deduction stays HR's to enter on the employee record,
    so an approval can never quietly change what someone gets paid.
    """

    SALARY_ADVANCE = "salary_advance"
    PETROL_ADVANCE = "petrol_advance"
    OTHER_AMOUNT = "other_amount"
    EXPENSE = "expense"
    REPORT = "report"

    TYPE_CHOICES = (
        (SALARY_ADVANCE, "Salary advance"),
        (PETROL_ADVANCE, "Petrol advance"),
        (EXPENSE, "Expense claim"),
        (OTHER_AMOUNT, "Other amount"),
        (REPORT, "Report / message"),
    )
    # An advance is money asked for before it is spent; an expense is money
    # already out of the engineer's own pocket, claimed back. Both carry a
    # figure, so both are validated the same way. A report is raised to be read.
    AMOUNT_TYPES = (SALARY_ADVANCE, PETROL_ADVANCE, EXPENSE, OTHER_AMOUNT)

    STATUS_CHOICES = (
        ("Pending", "Pending"),
        ("Approved", "Approved"),
        ("Rejected", "Rejected"),
    )

    employee = models.ForeignKey(
        Employee,
        on_delete=models.CASCADE,
        related_name="staff_requests",
    )
    request_type = models.CharField(max_length=20, choices=TYPE_CHOICES, db_index=True)
    # Null for a report. Money requests are validated to carry a positive amount.
    amount = models.DecimalField(max_digits=10, decimal_places=2, null=True, blank=True)
    reason = models.TextField()

    status = models.CharField(
        max_length=20, choices=STATUS_CHOICES, default="Pending", db_index=True
    )
    reviewed_by = models.ForeignKey(
        "authentication.User",
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name="reviewed_staff_requests",
    )
    reviewed_at = models.DateTimeField(null=True, blank=True)

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-created_at"]
        indexes = [models.Index(fields=["employee", "status"])]

    @property
    def needs_amount(self):
        return self.request_type in self.AMOUNT_TYPES

    def __str__(self):
        label = dict(self.TYPE_CHOICES).get(self.request_type, self.request_type)
        who = self.employee.employee_name if self.employee_id else "?"
        return f"{who} - {label} ({self.status})"


class RequestMessage(models.Model):
    """One message in the conversation on a request.

    Both sides post here, so a decision can be discussed rather than just
    handed down — the office can ask what the money is for, the employee can
    answer, and the whole exchange stays attached to the request.

    Read state is tracked per SIDE rather than per user: any staff member
    reading it clears it for the office, since whoever picks it up is acting
    for the office.
    """

    request = models.ForeignKey(
        EmployeeRequest,
        on_delete=models.CASCADE,
        related_name="messages",
    )
    sender = models.ForeignKey(
        "authentication.User",
        on_delete=models.SET_NULL,
        null=True,
        related_name="staff_request_messages",
    )
    # True when written by the employee who raised the request, False for staff.
    # Stored rather than derived so the thread still reads correctly after a
    # sender's login is deleted.
    from_employee = models.BooleanField()
    body = models.TextField()
    # Set when the decision itself generated the message, so the UI can style
    # "Approved - here's why" differently from ordinary chat.
    is_decision = models.BooleanField(default=False)

    read_by_employee = models.BooleanField(default=False)
    read_by_staff = models.BooleanField(default=False)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["created_at"]

    def __str__(self):
        side = "employee" if self.from_employee else "office"
        return f"{side}: {self.body[:40]}"
