import secrets

from django.db import models


def new_invite_token():
    """Unguessable, and short enough to paste into a WhatsApp message."""
    return secrets.token_urlsafe(32)


class Onboarding(models.Model):
    # WHO this record is about.
    #
    # An employee, a freelancer we pay by the job, and a vendor we buy work
    # from are three different relationships, and all three were being put
    # through the employee's form. One record type still -- they share who to
    # ring, where they work, the bank details and the documents on file, and
    # the office looks at one list -- with the parts that are not shared in
    # their own fields below.
    CATEGORY_CHOICES = (
        ('Employee', 'Employee'),
        ('Freelancer', 'Freelancer'),
        ('Vendor', 'Vendor'),
    )
    category = models.CharField(
        max_length=20,
        choices=CATEGORY_CHOICES,
        default='Employee',
        db_index=True,
    )

    # 1. Basic Details
    employee_name = models.CharField(max_length=255)
    employee_id = models.CharField(max_length=50, blank=True, null=True)
    # An employee's facts, not everybody's: a vendor has no department, no
    # designation and no date of joining -- they have a contract that starts.
    # The employee form still asks for all three.
    department = models.CharField(max_length=100, blank=True, null=True)
    designation = models.CharField(max_length=100, blank=True, null=True)
    work_location = models.CharField(max_length=100)
    date_of_joining = models.DateField(blank=True, null=True)
    mobile_number = models.CharField(max_length=20)
    email_id = models.EmailField()

    # 1b. A vendor is a firm, not a person. The person's name still goes in
    # employee_name -- it is who you ring -- and the firm goes here.
    company_name = models.CharField(max_length=255, blank=True, null=True)
    gst_number = models.CharField(max_length=30, blank=True, null=True)
    contact_person_role = models.CharField(max_length=100, blank=True, null=True)
    service_type = models.CharField(max_length=255, blank=True, null=True)

    # 1c. What the work costs and how long the arrangement runs. Freelancers
    # and vendors are paid per job or per contract rather than by the month,
    # and an engagement with no end date is an open one, not a missing one.
    RATE_TYPE_CHOICES = (
        ('Per case', 'Per case'),
        ('Per day', 'Per day'),
        ('Per hour', 'Per hour'),
        ('Monthly', 'Monthly'),
        ('Per job quote', 'Per job quote'),
    )
    rate_type = models.CharField(
        max_length=20, choices=RATE_TYPE_CHOICES, blank=True, null=True
    )
    rate_amount = models.DecimalField(
        max_digits=10, decimal_places=2, blank=True, null=True
    )
    contract_start = models.DateField(blank=True, null=True)
    contract_end = models.DateField(blank=True, null=True)
    agreement = models.FileField(
        upload_to='onboarding_docs/agreement/', blank=True, null=True
    )

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

    # WHO FILLED THIS IN. A record typed by the office has been seen by
    # somebody here; one that arrived through a shared link has not, and until
    # it is looked at it must not be mistaken for the other kind.
    SOURCE_CHOICES = (
        ('Office', 'Office'),
        ('Self', 'Self'),
    )
    source = models.CharField(
        max_length=20, choices=SOURCE_CHOICES, default='Office', db_index=True
    )

    # Timestamps and internal tracking
    # How far the onboarding PAPERWORK got. Separate from employment_status
    # below: a person can be fully onboarded and since have left.
    #
    # 'Pending Review' is what arrives through a shared link and what the
    # office clears by hand; anything the office types itself is 'Completed'
    # the moment it is saved, as it always was.
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


def trusted_onboarding_rows():
    """Onboarding rows that are allowed to speak for an employee.

    An employee's, and one the office has accepted. A form still Pending Review
    was typed by somebody nobody here has met, through a link with no login;
    a freelancer's or a vendor's is not about an employee at all. Neither may
    be the source of anybody's bank account.

    Oldest first, which is the order the lookups always used: the genuine row
    that has been on file the longest is the one that answers.
    """
    return (
        Onboarding.objects.filter(category="Employee")
        .exclude(status="Pending Review")
        .order_by("id")
    )


def onboarding_record_for(employee):
    """The onboarding row whose bank and personal details belong to this employee.

    Email first, because it is unique and reliable; then the employee code
    within the employee's branch, because codes repeat across branches. Only
    trusted rows are considered -- see trusted_onboarding_rows. This is the ONE
    place the lookup lives: the Employees API and the payslip PDF used to carry
    a copy each, and both copies read unreviewed link forms, which let a
    stranger's bank account appear on a working engineer's payslip.
    """
    rows = trusted_onboarding_rows()
    if employee.email:
        record = rows.filter(email_id__iexact=employee.email).first()
        if record:
            return record
    if employee.emp_code:
        qs = rows.filter(employee_id=employee.emp_code)
        if employee.branch:
            qs = qs.filter(work_location__iexact=employee.branch)
        return qs.first()
    return None


VALID_BRANCHES = ('Chennai', 'Vellore', 'Salem', 'Kanchipuram', 'Hosur')


def branch_named_in(location):
    """The branch a work location names, or None when it names none."""
    loc = (location or "").strip().lower()
    return next((b for b in VALID_BRANCHES if b.lower() == loc), None)


def employee_already_holding(email=None, phone=None, emp_code=None, location=None):
    """The existing employee whose identity a form would take over, if any.

    Email and phone are unique on Employee, so either one is a definite match.
    An employee code is only unique within a branch, so it is matched within
    the branch the form names -- or not at all when it names none.
    """
    from employees.models import Employee

    email = (email or "").strip()
    phone = (phone or "").strip()
    code = (emp_code or "").strip()
    if email:
        found = Employee.objects.filter(email__iexact=email).first()
        if found:
            return found
    if phone:
        found = Employee.objects.filter(phone=phone).first()
        if found:
            return found
    branch = branch_named_in(location)
    if code and branch:
        return Employee.objects.filter(emp_code=code, branch=branch).first()
    return None


class OnboardingInvite(models.Model):
    """The shareable link for one kind of form.

    One row per category, so the office has one standing link per kind to hand
    out rather than a new one per person -- that is how they said they would
    use it. The token is a column rather than anything derived, so a link that
    has been forwarded further than intended can be replaced without touching
    anything else.
    """
    category = models.CharField(
        max_length=20, choices=Onboarding.CATEGORY_CHOICES, unique=True
    )
    token = models.CharField(max_length=64, unique=True, db_index=True, default=new_invite_token)
    created_at = models.DateTimeField(auto_now_add=True)
    rotated_at = models.DateTimeField(null=True, blank=True)

    def __str__(self):
        return f"{self.category} invite"


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


def _sync_login_access(emp):
    """Close or reopen the login belonging to this employee record.

    Relieving someone has to stop them signing in, and nothing else does it:
    the lists can hide them and their existing token would still work.
    is_active is the one lever simplejwt already honours, so it is the one we
    turn — and we turn it back on when they are not relieved, so an accidental
    relief is undone by setting them Active again rather than by an admin going
    hunting through the user list.
    """
    user = getattr(emp, 'user', None)
    if user is None:
        return
    should_be_active = emp.status != 'relieved'
    if user.is_active != should_be_active:
        user.is_active = should_be_active
        user.save(update_fields=['is_active'])


def _employee_status_for(employment_status):
    """Onboarding's employment status -> the Employee record's status.

    All three map straight across. Relieved used to be flattened into
    'inactive' because Employee had nowhere else to put it, which left no way
    to tell someone who had LEFT from someone temporarily off the roster — and
    only the first should disappear from the working screens.
    """
    if employment_status == 'Active':
        return 'active'
    if employment_status == 'Relieved':
        return 'relieved'
    return 'inactive'


@receiver(post_save, sender=Onboarding)
def sync_onboarding_to_employee(sender, instance, created, **kwargs):
    """Automatically connects or creates corresponding Employee record upon Onboarding save."""
    # NOT EVERY ONBOARDING IS AN EMPLOYEE, AND NOT EVERY ONE HAS BEEN READ.
    #
    # Everything below matches the form against EXISTING employees -- by
    # email, by employee code, by phone, by name -- and writes the form over
    # whoever it finds: their name, phone, department, branch and status, and
    # the login attached to them. That is right for a record the office has
    # typed. It is not right for either of these:
    #
    #   * a freelancer or a vendor. They are not staff: no Employee row, no
    #     place in attendance or payroll, and no login with role "employee";
    #
    #   * a form that arrived through the public link and is still Pending
    #     Review. Somebody nobody here has met typed it, without logging in.
    #     Let this run on it and anybody holding the link could type a working
    #     engineer's email and rename them, re-number their phone and switch
    #     them to Inactive -- before the office has even seen the form.
    #
    # Nothing is provisioned or touched until the office accepts it. Accepting
    # moves it out of Pending Review, and from then on it is an ordinary record
    # and this runs exactly as it always has.
    if (instance.category or "Employee") != "Employee":
        return
    if instance.status == "Pending Review":
        return

    # A NAME IS NOT AN IDENTITY.
    #
    # Nothing on the form is compulsory now, so a record can arrive with a name
    # and nothing else -- or with nothing at all. Without something to tell
    # this person apart, everything below goes wrong in one of two ways: it
    # finds nobody and creates a fresh "New Employee" with a working login on
    # EVERY save, or it finds somebody who shares the name -- a relieved
    # namesake included -- writes the form over them and reopens their login.
    # Nothing is provisioned until the record carries one of the three that
    # can: email, phone or employee code. Adding one later and saving is enough.
    if not any(
        (value or "").strip()
        for value in (instance.email_id, instance.mobile_number, instance.employee_id)
    ):
        return

    from employees.models import Employee
    from decimal import Decimal
    from django.db import IntegrityError, transaction

    # The branch the form actually names, or None. A new employee still lands
    # in Chennai when it names none, as before -- but an EXISTING employee is
    # only moved when the form names somewhere: a blank location used to send
    # whoever it matched to Chennai.
    location_branch = branch_named_in(instance.work_location)
    branch_name = location_branch or 'Chennai'

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
        if location_branch:
            emp.branch = location_branch
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
        # Last, because the login may have only just been created above: a
        # relieved employee must not walk away with a working account, and
        # nothing else in the system closes one.
        _sync_login_access(emp)
