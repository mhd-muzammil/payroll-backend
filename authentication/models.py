from django.db import models
from django.contrib.auth.models import AbstractUser

# Create your models here.
class User(AbstractUser):
    ROLE_CHOICES = (
        ('superadmin', 'SuperAdmin'),
        ('admin', 'Admin'),
        ('employee', 'Employee'),
    )

    role = models.CharField(max_length=20, choices=ROLE_CHOICES, default='employee')

    phone_number = models.CharField(max_length=20, unique=True, null=True, blank=True)
    plain_password = models.CharField(max_length=100, null=True, blank=True)

    def __str__(self):
        return self.username