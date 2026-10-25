import re
from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from django.urls import reverse
from rest_framework import serializers

from .models import AppointmentOrder, Onboarding, Candidate, OfferLetter, employee_already_holding


DOCUMENT_FIELDS = (
    "agreement",
    "cancelled_cheque",
    "doc_aadhaar",
    "doc_pan",
    "doc_bank_proof",
    "doc_passport_photo",
    "doc_education_cert",
    "doc_resume",
    "doc_driving_license",
)

ALLOWED_CONTENT_TYPES = {
    ".pdf": {"application/pdf", "application/octet-stream"},
    ".jpg": {"image/jpeg", "image/pjpeg", "application/octet-stream"},
    ".jpeg": {"image/jpeg", "image/pjpeg", "application/octet-stream"},
    ".png": {"image/png", "application/octet-stream"},
    ".webp": {"image/webp", "application/octet-stream"},
    ".doc": {"application/msword", "application/octet-stream"},
    ".docx": {
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/zip",
        "application/octet-stream",
    },
}


def _has_valid_signature(uploaded_file, extension):
    uploaded_file.seek(0)
    header = uploaded_file.read(16)
    uploaded_file.seek(0)

    signatures = {
        ".pdf": header.startswith(b"%PDF-"),
        ".jpg": header.startswith(b"\xff\xd8\xff"),
        ".jpeg": header.startswith(b"\xff\xd8\xff"),
        ".png": header.startswith(b"\x89PNG\r\n\x1a\n"),
        ".webp": header.startswith(b"RIFF") and header[8:12] == b"WEBP",
        ".doc": header.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"),
    }
    if extension in signatures:
        return signatures[extension]

    if extension == ".docx":
        try:
            with ZipFile(uploaded_file) as archive:
                names = set(archive.namelist())
                return "[Content_Types].xml" in names and any(name.startswith("word/") for name in names)
        except (BadZipFile, OSError):
            return False
        finally:
            uploaded_file.seek(0)

    return False


def validate_employee_document(uploaded_file):
    extension = Path(uploaded_file.name).suffix.lower()
    if extension not in ALLOWED_CONTENT_TYPES:
        raise serializers.ValidationError(
            "Unsupported file type. Allowed types: PDF, JPG, JPEG, PNG, WEBP, DOC, DOCX."
        )

    max_bytes = settings.MAX_UPLOAD_SIZE_MB * 1024 * 1024
    if uploaded_file.size > max_bytes:
        raise serializers.ValidationError(
            f"File exceeds the {settings.MAX_UPLOAD_SIZE_MB} MB upload limit."
        )

    content_type = (getattr(uploaded_file, "content_type", "") or "").lower().split(";", 1)[0]
    if content_type and content_type not in ALLOWED_CONTENT_TYPES[extension]:
        raise serializers.ValidationError("The file content type does not match its extension.")

    if not _has_valid_signature(uploaded_file, extension):
        raise serializers.ValidationError("The file content does not match its extension.")

    return uploaded_file


class ProtectedDocumentField(serializers.FileField):
    def to_representation(self, value):
        if not value:
            return None
        return reverse(
            "onboarding-document",
            kwargs={"pk": value.instance.pk, "field_name": self.field_name},
        )


class OnboardingSerializer(serializers.ModelSerializer):
    agreement = ProtectedDocumentField(
        required=False, allow_null=True, validators=[validate_employee_document]
    )
    cancelled_cheque = ProtectedDocumentField(
        required=False, allow_null=True, validators=[validate_employee_document]
    )
    doc_aadhaar = ProtectedDocumentField(
        required=False, allow_null=True, validators=[validate_employee_document]
    )
    doc_pan = ProtectedDocumentField(
        required=False, allow_null=True, validators=[validate_employee_document]
    )
    doc_bank_proof = ProtectedDocumentField(
        required=False, allow_null=True, validators=[validate_employee_document]
    )
    doc_passport_photo = ProtectedDocumentField(
        required=False, allow_null=True, validators=[validate_employee_document]
    )
    doc_education_cert = ProtectedDocumentField(
        required=False, allow_null=True, validators=[validate_employee_document]
    )
    doc_resume = ProtectedDocumentField(
        required=False, allow_null=True, validators=[validate_employee_document]
    )
    doc_driving_license = ProtectedDocumentField(
        required=False, allow_null=True, validators=[validate_employee_document]
    )

    class Meta:
        model = Onboarding
        fields = '__all__'
        # NOTHING ON THE FORM IS COMPULSORY.
        #
        # The office's instruction: whatever details somebody has, they fill
        # in, and it saves. A new joiner opening the link on their phone often
        # does not have their bank details or an emergency contact to hand, and
        # a form that refuses to send until they do is a form that never
        # arrives. These four are NOT NULL columns, so a blank arrives as an
        # empty string -- which they hold without complaint.
        extra_kwargs = {
            "employee_name": {"required": False, "allow_blank": True},
            "mobile_number": {"required": False, "allow_blank": True},
            "email_id": {"required": False, "allow_blank": True},
            "work_location": {"required": False, "allow_blank": True},
        }

    def validate(self, attrs):
        """The one thing still refused: a contract that ends before it starts.

        It does not make any field compulsory -- it only speaks when BOTH dates
        are filled in and they contradict each other, which is a typo and not
        a missing detail.
        """
        # A PATCH carries only what changed, so anything absent is read off the
        # record as it stands.
        def value(name):
            if name in attrs:
                return attrs[name]
            return getattr(self.instance, name, None)

        start, end = value("contract_start"), value("contract_end")
        if start and end and end < start:
            raise serializers.ValidationError(
                {"contract_end": "The contract cannot end before it starts."}
            )

        # ACCEPTING A LINK FORM INTO SOMEBODY WHO IS ALREADY HERE.
        #
        # Accepting runs the employee sync, which writes the form over whoever
        # it matches -- and a stranger chose every identity field on it. If the
        # email, phone or code already belongs to an employee, that employee
        # would be renamed, re-numbered and handed a new login by one click.
        # The office used to be told to check first; that is not a control. It
        # is refused here, naming who it would have hit, and the decision is
        # made with that in front of somebody.
        accepting = (
            self.instance is not None
            and self.instance.status == "Pending Review"
            and attrs.get("status", "Pending Review") != "Pending Review"
        )
        if accepting and value("category") in (None, "", "Employee"):
            holder = employee_already_holding(
                email=value("email_id"),
                phone=value("mobile_number"),
                emp_code=value("employee_id"),
                location=value("work_location"),
            )
            if holder:
                raise serializers.ValidationError({
                    "status": (
                        f"This form's email, phone or employee code already belongs to "
                        f"{holder.employee_name or 'an existing employee'} (employee #{holder.pk}). "
                        "Accepting it would write the form over their record and their login. "
                        "If it is the same person, update them from the Employees page and delete "
                        "this form; if not, correct the form first."
                    )
                })
        return attrs


class CandidateSerializer(serializers.ModelSerializer):
    salary_slip = serializers.FileField(required=False, allow_null=True, validators=[validate_employee_document])
    offer_letter = serializers.FileField(required=False, allow_null=True, validators=[validate_employee_document])
    bank_statement = serializers.FileField(required=False, allow_null=True, validators=[validate_employee_document])
    resume = serializers.FileField(required=False, allow_null=True, validators=[validate_employee_document])

    class Meta:
        model = Candidate
        fields = '__all__'


# The longest each line of the letter can take before it runs into the next
# word or off the page. Names longer than this are very rare; a role this long
# would push "the following terms and conditions:" past the margin.
OFFER_LETTER_MAX = {"name": 60, "designation": 40}
# No single monthly line on a field engineer's letter comes near this. A typo
# with an extra zero should be refused, not printed and sent.
OFFER_LETTER_MAX_AMOUNT = 10_00_000
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


class OfferLetterSerializer(serializers.ModelSerializer):
    """An offer letter, with Annexure-I's sums worked out from its monthly lines.
    gross, net and ctc are read-only: they are never typed, so they cannot
    disagree with the lines they add up."""

    gross = serializers.SerializerMethodField()
    net = serializers.SerializerMethodField()
    ctc = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()
    response_by = serializers.SerializerMethodField()
    reply = serializers.SerializerMethodField()

    class Meta:
        model = OfferLetter
        fields = [
            "id", "ref_no", "name", "designation", "joining_date",
            "basic", "hra", "conveyance", "cea", "personal_allowance",
            "professional_tax", "insurance", "petrol_allowance",
            "gross", "net", "ctc", "created_by_name", "created_at", "updated_at",
            "mailed_at", "mailed_to", "response", "response_at", "response_source", "response_by",
            "replied_at", "reply",
        ]
        # The answer and the mailing are the server's to record: HR changes the
        # answer through the response action, never by editing the letter.
        read_only_fields = [
            "id", "created_at", "updated_at", "mailed_at", "mailed_to",
            "response", "response_at", "response_source", "replied_at",
        ]

    def get_response_by(self, obj):
        if obj.response_source == "LINK":
            return "Candidate"
        user = obj.response_user
        return (user.get_full_name() or user.username) if user else ""

    def get_reply(self, obj):
        mail = obj.reply_mail
        if mail is None:
            return None
        return {"id": mail.id, "from_email": mail.from_email, "received_at": mail.received_at, "preview": mail.preview}

    def _sums(self, obj):
        from .offer_letter import annexure

        return annexure(obj)

    def get_gross(self, obj):
        return self._sums(obj)["gross"]

    def get_net(self, obj):
        return self._sums(obj)["net"]

    def get_ctc(self, obj):
        return self._sums(obj)["ctc"]

    def get_created_by_name(self, obj):
        user = obj.created_by
        return (user.get_full_name() or user.username) if user else ""

    def _text(self, value, field):
        value = _CONTROL.sub("", value or "").strip()
        value = re.sub(r"\s+", " ", value)
        if not value:
            raise serializers.ValidationError("This cannot be empty.")
        if len(value) > OFFER_LETTER_MAX[field]:
            raise serializers.ValidationError(
                f"At most {OFFER_LETTER_MAX[field]} characters fit on the letter."
            )
        return value

    def validate_name(self, value):
        return self._text(value, "name")

    def validate_designation(self, value):
        return self._text(value, "designation")

    def validate_ref_no(self, value):
        value = (value or "").strip()
        if not re.fullmatch(r"[A-Za-z0-9/._-]{3,40}", value):
            raise serializers.ValidationError("Use letters, digits and / - . _ only, like RTPL/NJ/2026/141.")
        return value

    def validate(self, attrs):
        for field in (
            "basic", "hra", "conveyance", "cea", "personal_allowance",
            "professional_tax", "insurance", "petrol_allowance",
        ):
            if field in attrs and attrs[field] > OFFER_LETTER_MAX_AMOUNT:
                raise serializers.ValidationError({field: "That is more than any monthly line should be."})
        basic = attrs.get("basic", getattr(self.instance, "basic", None))
        if basic is not None and basic <= 0:
            raise serializers.ValidationError({"basic": "Basic must be more than zero."})
        return attrs


APPOINTMENT_ORDER_MAX = {"name": 60, "address": 140, "designation": 40}
APPOINTMENT_ORDER_AMOUNTS = ("basic", "hra", "conveyance", "cea", "personal_allowance")


class AppointmentOrderSerializer(serializers.ModelSerializer):
    """An appointment order. The signing state is the server's alone: nothing
    here can mark an order sent or signed."""

    status = serializers.ReadOnlyField()
    gross = serializers.SerializerMethodField()
    net = serializers.SerializerMethodField()
    ctc = serializers.SerializerMethodField()
    offer_ref = serializers.SerializerMethodField()
    created_by_name = serializers.SerializerMethodField()

    class Meta:
        model = AppointmentOrder
        fields = [
            "id", "offer_letter", "offer_ref", "salutation", "name", "address", "designation",
            "order_date", "joining_date",
            "basic", "hra", "conveyance", "cea", "personal_allowance", "professional_tax",
            "email", "status", "sent_at", "viewed_at", "signed_at",
            "gross", "net", "ctc", "created_by_name", "created_at", "updated_at",
        ]
        read_only_fields = ["id", "sent_at", "viewed_at", "signed_at", "created_at", "updated_at"]

    def _sums(self, obj):
        from .appointment_order import annexure

        return annexure(obj)

    def get_gross(self, obj):
        return self._sums(obj)["gross"]

    def get_net(self, obj):
        return self._sums(obj)["net"]

    def get_ctc(self, obj):
        return self._sums(obj)["ctc"]

    def get_offer_ref(self, obj):
        return obj.offer_letter.ref_no if obj.offer_letter_id else ""

    def get_created_by_name(self, obj):
        user = obj.created_by
        return (user.get_full_name() or user.username) if user else ""

    def _text(self, value, field):
        value = _CONTROL.sub("", value or "").strip()
        value = re.sub(r"\s+", " ", value)
        if not value:
            raise serializers.ValidationError("This cannot be empty.")
        if len(value) > APPOINTMENT_ORDER_MAX[field]:
            raise serializers.ValidationError(
                f"At most {APPOINTMENT_ORDER_MAX[field]} characters fit on the letter."
            )
        # The letter is set in Times, which has the Western letters and no others.
        try:
            value.encode("cp1252")
        except UnicodeEncodeError:
            raise serializers.ValidationError("Type this in English letters.")
        return value

    def validate_name(self, value):
        return self._text(value, "name")

    def validate_address(self, value):
        return self._text(value, "address")

    def validate_designation(self, value):
        return self._text(value, "designation")

    def validate(self, attrs):
        for field in APPOINTMENT_ORDER_AMOUNTS:
            if field in attrs and attrs[field] > OFFER_LETTER_MAX_AMOUNT:
                raise serializers.ValidationError({field: "That is more than any monthly line should be."})
        if attrs.get("professional_tax", 0) > OFFER_LETTER_MAX_AMOUNT:
            raise serializers.ValidationError({"professional_tax": "That is more than a year's professional tax."})

        def current(field):
            return attrs.get(field, getattr(self.instance, field, None))

        if (current("basic") or 0) <= 0:
            raise serializers.ValidationError({"basic": "Basic must be more than zero."})
        gross = sum(current(field) or 0 for field in APPOINTMENT_ORDER_AMOUNTS)
        if (current("professional_tax") or 0) >= gross * 12:
            raise serializers.ValidationError(
                {"professional_tax": "Professional tax is for the year, and must be less than a year's pay."}
            )
        return attrs
