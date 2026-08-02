from django.db import models
from django.contrib.auth.models import AbstractUser

def default_sections():
    return {
        "dashboard": ["All"],
        "hiring": ["All"],
        "onboarding": ["All"],
        "employees": ["All"],
        "tasks": ["All"],
        "attendance": ["All"],
        "payroll": ["All"],
        "payslips": ["All"],
        "leaves": ["All"],
        "performance": ["All"],
        "assets": ["All"]
    }


class User(AbstractUser):
    ROLE_CHOICES = (
        ('superadmin', 'SuperAdmin'),
        ('admin', 'Admin'),
        ('hr', 'HR'),
        ('employee', 'Employee'),
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='employee')
    phone_number = models.CharField(max_length=20, unique=True, null=True, blank=True)

    # Plaintext copy of the password so admins can view/copy/share credentials
    # with employees from the Users section. Kept in sync whenever the password
    # is set through the app. NOTE: this is a deliberate convenience-over-security
    # choice for this internal HR tool; treat DB access as credential access.
    plain_password = models.CharField(max_length=128, null=True, blank=True)

    allowed_sections = models.JSONField(default=default_sections, blank=True)
    assigned_branch = models.CharField(max_length=50, null=True, blank=True, default=None)

    def __str__(self):
        return self.username


def get_allowed_branches(user, section_name):
    if user.is_superuser or user.role in ["superadmin", "admin"]:
        return ["All"]

    allowed_sec = getattr(user, "allowed_sections", None)
    if not allowed_sec:
        # Fail closed: a non-admin user with no section config gets no access.
        if getattr(user, "assigned_branch", None):
            return [user.assigned_branch]
        return []

    if isinstance(allowed_sec, list):
        if section_name in allowed_sec:
            if getattr(user, "assigned_branch", None):
                return [user.assigned_branch]
            return ["All"]
        return []

    if isinstance(allowed_sec, dict):
        if section_name in allowed_sec:
            branches = allowed_sec[section_name]
            if not branches:
                return []
            return branches
        return []

    return []