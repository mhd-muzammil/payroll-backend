from pathlib import Path
from zipfile import BadZipFile, ZipFile

from django.conf import settings
from django.urls import reverse
from rest_framework import serializers

from .models import Onboarding, Candidate


DOCUMENT_FIELDS = (
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


class CandidateSerializer(serializers.ModelSerializer):
    salary_slip = serializers.FileField(required=False, allow_null=True, validators=[validate_employee_document])
    offer_letter = serializers.FileField(required=False, allow_null=True, validators=[validate_employee_document])
    bank_statement = serializers.FileField(required=False, allow_null=True, validators=[validate_employee_document])
    resume = serializers.FileField(required=False, allow_null=True, validators=[validate_employee_document])

    class Meta:
        model = Candidate
        fields = '__all__'
