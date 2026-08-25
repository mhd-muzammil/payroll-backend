from django.db import models

class Onboarding(models.Model):
    # 1. Basic Details
    employee_name = models.CharField(max_length=255)
    employee_id = models.CharField(max_length=50, blank=True, null=True)
    department = models.CharField(max_length=100)
    designation = models.CharField(max_length=100)
    work_location = models.CharField(max_length=100)
    date_of_joining = models.DateField()
    mobile_number = models.CharField(max_length=20)
    email_id = models.EmailField()

    # 2. Personal Details
    dob = models.DateField(null=True, blank=True)
    gender = models.CharField(max_length=20, blank=True, null=True)
    blood_group = models.CharField(max_length=20, blank=True, null=True)
    address = models.TextField(blank=True, null=True)
    tshirt_size = models.CharField(max_length=10, blank=True, null=True)

    # 3. Emergency Contact
    emergency_contact_name = models.CharField(max_length=255, blank=True, null=True)
    emergency_relationship = models.CharField(max_length=100, blank=True, null=True)
    emergency_number = models.CharField(max_length=20, blank=True, null=True)

    # 4. Bank Details
    bank_name = models.CharField(max_length=255, blank=True, null=True)
    account_holder_name = models.CharField(max_length=255, blank=True, null=True)
    account_number = models.CharField(max_length=50, blank=True, null=True)
    ifsc_code = models.CharField(max_length=50, blank=True, null=True)
    bank_branch = models.CharField(max_length=100, blank=True, null=True)
    # Placeholder for attachment path
    cancelled_cheque = models.FileField(upload_to='bank_proofs/', blank=True, null=True)

    # 5. ID Card Details
    photo_submitted = models.CharField(max_length=10, blank=True, null=True)
    id_card_blood_group = models.CharField(max_length=20, blank=True, null=True)

    # 6. Documents Submitted (Store as FileField instead of Boolean)
    doc_aadhaar = models.FileField(upload_to='onboarding_docs/aadhaar/', blank=True, null=True)
    doc_pan = models.FileField(upload_to='onboarding_docs/pan/', blank=True, null=True)
    doc_bank_proof = models.FileField(upload_to='onboarding_docs/bank_proof/', blank=True, null=True)
    doc_passport_photo = models.FileField(upload_to='onboarding_docs/passport/', blank=True, null=True)
    doc_education_cert = models.FileField(upload_to='onboarding_docs/education/', blank=True, null=True)
    doc_resume = models.FileField(upload_to='onboarding_docs/resume/', blank=True, null=True)
    doc_driving_license = models.FileField(upload_to='onboarding_docs/driving_license/', blank=True, null=True)

    # 7. Additional Info
    total_experience = models.CharField(max_length=100, blank=True, null=True)
    hp_experience = models.CharField(max_length=100, blank=True, null=True)
    skills = models.CharField(max_length=50, blank=True, null=True)

    # Timestamps and internal tracking
    # How far the onboarding PAPERWORK got. Separate from employment_status
    # below: a person can be fully onboarded and since have left.
    status = models.CharField(max_length=20, default='Completed')

    # Where the person stands with the company TODAY. These three are mutually
    # exclusive and cover everyone, so the summary cards can count each person
    # exactly once instead of showing the same head under several totals.
    EMPLOYMENT_STATUS_CHOICES = (
        ('Active', 'Active'),        # working with us
        ('Inactive', 'Inactive'),    # on our books but not currently working
        ('Relieved', 'Relieved'),    # left the company
    )
    employment_status = models.CharField(
        max_length=20,
        choices=EMPLOYMENT_STATUS_CHOICES,
        default='Active',
        db_index=True,
    )
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.employee_name} ({self.employment_status})"


class Candidate(models.Model):
    SEGMENT_CHOICES = (
        ('Combo', 'Combo'),
        ('PC', 'PC'),
        ('Print', 'Print'),
        ('CCTV', 'CCTV'),
        ('Networking', 'Networking'),
    )
    
    ACTION_CHOICES = (
        ('RNR', 'RNR'),
        ('In Progress', 'In Progress'),
        ('Offer Shared', 'Offer Shared'),
        ('Waiting For Acceptance', 'Waiting For Acceptance'),
        ('Waiting For Joining Date', 'Waiting For Joining Date'),
        ('Salary Discussion', 'Salary Discussion'),
        ('Rejected', 'Rejected'),
        ('Decline', 'Decline'),
    )

    name = models.CharField(max_length=255)
    phone_number = models.CharField(max_length=20)
    # Every lead sheet we import carries an email and the portal had nowhere to
    # put it, so 600-odd contactable candidates would have arrived with only a
    # phone number. Not unique: the same person can be in two lead sheets, and
    # the phone is what we de-duplicate on.
    email = models.EmailField(max_length=255, blank=True, null=True)
    # Where this candidate came from — "FB Leads Aug 2026", "Prince College",
    # "WorkIndia". Without it an import of several hundred paid leads and a
    # college list become one undifferentiated pile that cannot be worked
    # through or reported on separately.
    source = models.CharField(max_length=120, blank=True, default="", db_index=True)
    qualification = models.CharField(max_length=100, blank=True, null=True)
    permanent_address = models.CharField(max_length=255, blank=True, null=True)
    present_address = models.CharField(max_length=255, blank=True, null=True)
    years_of_experience = models.DecimalField(max_digits=4, decimal_places=1, default=0.0)
    segment = models.CharField(max_length=50, choices=SEGMENT_CHOICES, default='Combo')
    previous_company = models.CharField(max_length=255, blank=True, null=True)
    last_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    expecting_salary = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    remarks = models.TextField(blank=True, null=True)
    action = models.CharField(max_length=50, choices=ACTION_CHOICES, default='In Progress')
    
    # Proof Uploads
    salary_slip = models.FileField(upload_to='hiring/salary_slips/', blank=True, null=True)
    offer_letter = models.FileField(upload_to='hiring/offer_letters/', blank=True, null=True)
    bank_statement = models.FileField(upload_to='hiring/bank_statements/', blank=True, null=True)
    resume = models.FileField(upload_to='hiring/resumes/', blank=True, null=True)
    
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    def __str__(self):
        return f"{self.name} - {self.segment} ({self.action})"


import logging

from django.db.models.signals import post_save
from django.dispatch import receiver

logger = logging.getLogger(__name__)

# Everything on Employee that onboarding writes and that CANNOT collide with
# another row. email and phone are deliberately absent: they are unique, so they
# are the only two that can ever fail, and they must not take the rest with them.
_NON_UNIQUE_EMPLOYEE_FIELDS = [
    "employee_name",
    "emp_code",
    "department",
    "role",
    "branch",
    "date_of_joining",
    "status",
]


def _identity_clashes(Employee, emp):
    """Which unique field on this employee is already somebody else's."""
    clashes = []
    for field in ("email", "phone"):
        wanted = getattr(emp, field, None)
        if wanted and Employee.objects.filter(**{field: wanted}).exclude(pk=emp.pk).exists():
            clashes.append(f"{field}={wanted!r}")
    return clashes


def _ensure_user_for_employee(emp):
    """Create + link a login User for an employee that has none, so an onboarded
    person is provisioned everywhere. Prefers an existing user matched by email;
    otherwise generates a unique username + a temp password (stored as
    plain_password so an admin can copy/share it from the Users section). Safe to
    call repeatedly — a no-op once the employee already has a user."""
    import re
    import secrets
    from authentication.models import User

    # Already has a login: keep the User in sync with the Employee (email + name)
    # so onboarding details align across the User and Employee sections too.
    if getattr(emp, "user_id", None):
        existing = getattr(emp, "user", None)
        if existing:
            changed = False
            if emp.email and (existing.email or "").lower() != emp.email.lower():
                existing.email = emp.email
                changed = True
            if emp.employee_name and existing.first_name != emp.employee_name:
                existing.first_name = emp.employee_name
                changed = True
            if changed:
                try:
                    existing.save(update_fields=["email", "first_name"])
                except Exception:
                    pass
        return

    user = None
    if emp.email:
        candidate = User.objects.filter(email__iexact=emp.email).first()
        if candidate:
            # Only reuse this user if it isn't already the login of a DIFFERENT
            # employee (Employee.user is OneToOne — reusing it would raise an
            # IntegrityError that leaves this employee with no login at all).
            linked = getattr(candidate, "employee_profile", None)
            if linked is None or linked.pk == emp.pk:
                user = candidate

    if not user:
        base = (emp.email.split("@")[0] if emp.email else (emp.employee_name or "user")).lower()
        base = re.sub(r"[^a-z0-9_.]", "", base.replace(" ", "")) or "user"
        username = base
        i = 1
        while User.objects.filter(username=username).exists():
            username = f"{base}_{i}"
            i += 1
        first = "User"
        if emp.employee_name and emp.employee_name.strip():
            first = re.sub(r"[^a-zA-Z]", "", emp.employee_name.strip().split()[0]).capitalize() or "User"
        temp_pwd = f"{first}@{secrets.randbelow(9000) + 1000}"
        try:
            user = User.objects.create_user(
                username=username,
                email=emp.email or "",
                first_name=emp.employee_name or "",
                role="employee",
                password=temp_pwd,
                plain_password=temp_pwd,
            )
        except Exception:
            return

    emp.user = user
    try:
        emp.save(update_fields=["user"])
    except Exception:
        pass


def _employee_status_for(employment_status):
    """Onboarding's employment status -> the Employee record's status.

    Employee has no 'relieved' of its own, and someone who has left must not
    stay active: they would keep showing up in attendance, payroll and the
    payslip run. Both Inactive and Relieved therefore land on 'inactive';
    which of the two it was stays readable on the onboarding record.
    """
    return 'active' if employment_status == 'Active' else 'inactive'


@receiver(post_save, sender=Onboarding)
def sync_onboarding_to_employee(sender, instance, created, **kwargs):
    """Automatically connects or creates corresponding Employee record upon Onboarding save."""
    from employees.models import Employee
    from decimal import Decimal
    from django.db import IntegrityError, transaction

    valid_branches = ['Chennai', 'Vellore', 'Salem', 'Kanchipuram', 'Hosur']
    branch_name = 'Chennai'
    if instance.work_location:
        loc = instance.work_location.strip()
        matched = next((b for b in valid_branches if b.lower() == loc.lower()), None)
        if matched:
            branch_name = matched

    # Try to find existing employee by email, emp_code, phone, or name.
    # matched_by_identity is True only for the reliable keys (email/code/phone);
    # a name-only match is weak because names are not unique.
    emp = None
    matched_by_identity = False
    if instance.email_id and instance.email_id.strip():
        emp = Employee.objects.filter(email__iexact=instance.email_id.strip()).first()
        matched_by_identity = bool(emp)
    if not emp and instance.employee_id and instance.employee_id.strip():
        emp = Employee.objects.filter(emp_code=instance.employee_id.strip()).first()
        matched_by_identity = bool(emp)
    if not emp and instance.mobile_number and instance.mobile_number.strip():
        emp = Employee.objects.filter(phone=instance.mobile_number.strip()).first()
        matched_by_identity = bool(emp)
    if not emp and instance.employee_name and instance.employee_name.strip():
        nm = instance.employee_name.strip()
        # Prefer a match WITHIN the onboarding's branch (same name in another
        # branch is a different person); fall back to a plain name match only if
        # there is none in this branch.
        emp = Employee.objects.filter(employee_name__iexact=nm, branch=branch_name).first()
        if not emp:
            emp = Employee.objects.filter(employee_name__iexact=nm).first()

    # A name-only match whose email OR phone CONFLICTS with the existing record
    # is almost certainly a different person who happens to share the name —
    # don't overwrite that person's identity; create a fresh employee instead.
    if emp and not matched_by_identity:
        onb_email = (instance.email_id or "").strip().lower()
        onb_phone = (instance.mobile_number or "").strip()
        if (onb_email and emp.email and emp.email.strip().lower() != onb_email) or (
            onb_phone and emp.phone and emp.phone.strip() != onb_phone
        ):
            emp = None

    if emp:
        # Connect & update existing Employee
        emp.employee_name = instance.employee_name or emp.employee_name
        if instance.employee_id and instance.employee_id.strip():
            emp.emp_code = instance.employee_id.strip()
        if instance.email_id and instance.email_id.strip():
            emp.email = instance.email_id.strip()
        if instance.mobile_number and instance.mobile_number.strip():
            emp.phone = instance.mobile_number.strip()
        if instance.department and instance.department.strip():
            emp.department = instance.department.strip()
        if instance.designation and instance.designation.strip():
            emp.role = instance.designation.strip()
        if branch_name:
            emp.branch = branch_name
        if instance.date_of_joining:
            emp.date_of_joining = instance.date_of_joining
        # Follow the onboarding record. This used to be hardcoded to 'active',
        # which silently revived anyone HR had marked Inactive or Relieved on
        # the next save of their onboarding row.
        emp.status = _employee_status_for(instance.employment_status)
        try:
            # atomic() so a failure rolls back to a savepoint. Without it the
            # broken statement poisons the whole transaction on PostgreSQL and
            # every query after this one fails too.
            with transaction.atomic():
                emp.save()
        except IntegrityError:
            # email and phone are UNIQUE, and an onboarding form can carry one
            # that already belongs to somebody else — usually a duplicate row for
            # the same person. This used to swallow the ENTIRE update, so the
            # hire date went with it: HR filled in a joining date, the onboarding
            # record saved cleanly, and the Employees list showed a blank Joined
            # column with nothing anywhere saying why.
            #
            # A hire date cannot collide with anything. Put the clashing identity
            # back and write everything that is safe to write.
            clashes = _identity_clashes(Employee, emp)
            emp.refresh_from_db(fields=["email", "phone"])
            try:
                with transaction.atomic():
                    emp.save(update_fields=_NON_UNIQUE_EMPLOYEE_FIELDS)
                logger.warning(
                    "onboarding %r: kept the hire date, but could not apply %s — "
                    "already used by another employee row. Merge the duplicates.",
                    instance.employee_name,
                    ", ".join(clashes) or "an identity field",
                )
            except IntegrityError:
                logger.warning(
                    "onboarding %r: the employee row could not be updated at all (%s)",
                    instance.employee_name,
                    ", ".join(clashes) or "unknown conflict",
                )
    else:
        # Create new Employee
        try:
            # See above: atomic() keeps a failed INSERT from poisoning the
            # transaction, so the fallback lookup below can still run.
            with transaction.atomic():
                emp = Employee.objects.create(
                employee_name=instance.employee_name.strip() if instance.employee_name else "New Employee",
                emp_code=instance.employee_id.strip() if instance.employee_id else None,
                email=instance.email_id.strip() if instance.email_id else None,
                phone=instance.mobile_number.strip() if instance.mobile_number else None,
                department=instance.department.strip() if instance.department else 'General',
                role=instance.designation.strip() if instance.designation else 'Staff',
                branch=branch_name,
                salary=Decimal('0.00'),
                status=_employee_status_for(instance.employment_status),
                    date_of_joining=instance.date_of_joining,
                )
        except IntegrityError:
            emp = Employee.objects.filter(employee_name__iexact=instance.employee_name.strip()).first()
            if emp:
                # Reached when the new row's email or phone is already taken. The
                # hire date still belongs on whoever this is.
                emp.status = _employee_status_for(instance.employment_status)
                if instance.date_of_joining:
                    emp.date_of_joining = instance.date_of_joining
                try:
                    with transaction.atomic():
                        emp.save(update_fields=["status", "date_of_joining"])
                except Exception:
                    logger.warning(
                        "onboarding %r: could not create or update an employee row",
                        instance.employee_name,
                    )
            else:
                logger.warning(
                    "onboarding %r: no employee row could be created (%s)",
                    instance.employee_name,
                    ", ".join(_identity_clashes(Employee, Employee(
                        email=(instance.email_id or "").strip() or None,
                        phone=(instance.mobile_number or "").strip() or None,
                    ))) or "unknown conflict",
                )

    # Ensure the employee has a linked login user, so onboarding auto-provisions
    # across all sections: the account shows in Users (with a shareable password),
    # the person can check in for Attendance, and they are picked up by Payroll.
    if emp:
        _ensure_user_for_employee(emp)
