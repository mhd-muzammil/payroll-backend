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


class EmploymentStatusTests(APITestCase):
    """Active / Inactive / Relieved is one dimension covering everyone exactly
    once, and it drives the linked Employee record so a person who has left
    stops appearing in attendance and payroll."""

    def setUp(self):
        self.user = User.objects.create_user(username="hr", password="x", role="admin")
        self.client.force_authenticate(self.user)

    def _onboard(self, name, email, phone, **extra):
        return Onboarding.objects.create(
            employee_name=name,
            department="Service",
            designation="Service engineer",
            work_location="Chennai",
            date_of_joining="2026-01-01",
            mobile_number=phone,
            email_id=email,
            **extra,
        )

    def _employee_for(self, onboarding):
        from employees.models import Employee

        return Employee.objects.filter(email__iexact=onboarding.email_id).first()

    def test_new_onboarding_defaults_to_active(self):
        record = self._onboard("Lavakumar", "lava@example.com", "9000000001")
        self.assertEqual(record.employment_status, "Active")
        self.assertEqual(self._employee_for(record).status, "active")

    def test_the_three_statuses_partition_everyone(self):
        """No person is counted twice: the three buckets sum to the total."""
        a = self._onboard("A One", "a@example.com", "9000000011")
        b = self._onboard("B Two", "b@example.com", "9000000012")
        c = self._onboard("C Three", "c@example.com", "9000000013")
        b.employment_status = "Inactive"
        b.save()
        c.employment_status = "Relieved"
        c.save()

        response = self.client.get(reverse("onboarding-list"))
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        rows = response.data["results"] if isinstance(response.data, dict) else response.data
        counts = {s: 0 for s in ("Active", "Inactive", "Relieved")}
        for row in rows:
            counts[row["employment_status"]] += 1

        self.assertEqual(counts, {"Active": 1, "Inactive": 1, "Relieved": 1})
        self.assertEqual(sum(counts.values()), len(rows), "every record lands in exactly one bucket")
        self.assertEqual(a.employment_status, "Active")

    def test_relieving_someone_deactivates_their_employee_record(self):
        record = self._onboard("Gone Away", "gone@example.com", "9000000002")
        self.assertEqual(self._employee_for(record).status, "active")

        response = self.client.patch(
            reverse("onboarding-detail", args=[record.pk]),
            {"employment_status": "Relieved"},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK, response.data)
        self.assertEqual(response.data["employment_status"], "Relieved")
        self.assertEqual(self._employee_for(record).status, "inactive")

    def test_marking_inactive_also_deactivates_the_employee(self):
        record = self._onboard("On Hold", "hold@example.com", "9000000003")
        record.employment_status = "Inactive"
        record.save()
        self.assertEqual(self._employee_for(record).status, "inactive")

    def test_a_later_edit_does_not_revive_someone_who_left(self):
        """The sync used to hardcode status='active', so any subsequent save of
        the onboarding row silently put a relieved person back on the payroll."""
        record = self._onboard("Left Us", "left@example.com", "9000000004")
        record.employment_status = "Relieved"
        record.save()
        self.assertEqual(self._employee_for(record).status, "inactive")

        record.designation = "Senior Service engineer"
        record.save()

        self.assertEqual(self._employee_for(record).status, "inactive")

    def test_bringing_someone_back_reactivates_them(self):
        record = self._onboard("Rejoined", "rejoin@example.com", "9000000005")
        record.employment_status = "Relieved"
        record.save()
        self.assertEqual(self._employee_for(record).status, "inactive")

        record.employment_status = "Active"
        record.save()
        self.assertEqual(self._employee_for(record).status, "active")

    def test_an_unknown_status_is_rejected(self):
        record = self._onboard("Typo Test", "typo@example.com", "9000000006")
        response = self.client.patch(
            reverse("onboarding-detail", args=[record.pk]),
            {"employment_status": "Retired"},
            format="multipart",
        )
        self.assertEqual(response.status_code, status.HTTP_400_BAD_REQUEST)
        self.assertIn("employment_status", response.data)
