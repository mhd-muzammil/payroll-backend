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
    status = models.CharField(max_length=20, default='Completed')
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.employee_name} ({self.status})"


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
