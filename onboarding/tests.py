import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import User
from .models import Onboarding


class ProtectedOnboardingDocumentTests(APITestCase):
    def setUp(self):
        self.media_root = tempfile.mkdtemp()
        self.settings_override = override_settings(
            MEDIA_ROOT=self.media_root,
            MAX_UPLOAD_SIZE_MB=1,
            SECURE_SSL_REDIRECT=False,
        )
        self.settings_override.enable()
        self.user = User.objects.create_user(username="admin", password="test-password", role="admin")
        self.onboarding = Onboarding.objects.create(
            employee_name="Test Employee",
            department="Engineering",
            designation="Developer",
            work_location="Chennai",
            date_of_joining="2026-01-01",
            mobile_number="9999999999",
            email_id="employee@example.com",
            doc_aadhaar=SimpleUploadedFile(
                "aadhaar.pdf",
                b"%PDF-1.4\nprotected test document",
                content_type="application/pdf",
            ),
        )

    def tearDown(self):
        self.settings_override.disable()
        shutil.rmtree(self.media_root, ignore_errors=True)

    def test_serializer_returns_protected_document_url(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(reverse("onboarding-detail", args=[self.onboarding.pk]))

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertNotIn("/media/", response.data["doc_aadhaar"])
        self.assertEqual(
            response.data["doc_aadhaar"],
            reverse(
                "onboarding-document",
                kwargs={"pk": self.onboarding.pk, "field_name": "doc_aadhaar"},
            ),
        )

    def test_document_requires_authentication(self):
        response = self.client.get(
            reverse(
                "onboarding-document",
                kwargs={"pk": self.onboarding.pk, "field_name": "doc_aadhaar"},
            )
        )

        self.assertEqual(response.status_code, status.HTTP_401_UNAUTHORIZED)

    def test_authenticated_document_download_is_private(self):
        self.client.force_authenticate(self.user)

        response = self.client.get(
            reverse(
                "onboarding-document",
                kwargs={"pk": self.onboarding.pk, "field_name": "doc_aadhaar"},
            )
        )

        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertEqual(response["Cache-Control"], "private, no-store")
        self.assertIn("attachment", response["Content-Disposition"])
        self.assertEqual(response["X-Content-Type-Options"], "nosniff")

    def test_spoofed_document_is_rejected(self):
        self.client.force_authenticate(self.user)
        payload = {
            "employee_name": "Unsafe Upload",
            "department": "Engineering",
            "designation": "Developer",
            "work_location": "Chennai",
            "date_of_joining": "2026-01-01",
            "mobile_number": "8888888888",
            "email_id": "unsafe@example.com",
            "doc_aadhaar": SimpleUploadedFile(
                "aadhaar.pdf",
                b"this is not a pdf",
                content_type="application/pdf",
            ),
        }

        response = self.client.post(reverse("onboarding-list"), payload, format="multipart")

        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("doc_aadhaar", response.data)
