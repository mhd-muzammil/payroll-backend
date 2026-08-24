import shutil
import tempfile

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase, override_settings
from django.urls import reverse
from rest_framework import status
from rest_framework.test import APITestCase

from authentication.models import User
from .models import Onboarding
from decimal import Decimal
from employees.models import Employee


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


class OnboardingHireDateSurvivesConflictTests(TestCase):
    """The hire date must reach the employee even when their identity cannot.

    Employee.email and Employee.phone are UNIQUE. Onboarding is matched to an
    employee by email first, so a phone number that belongs to a THIRD row is
    only discovered when the update is written — as an IntegrityError. That used
    to be swallowed by a bare `except IntegrityError: pass`, taking
    date_of_joining down with it: HR filled in a joining date, the onboarding
    record saved cleanly, and the Employees list showed a blank Joined column
    with nothing anywhere saying why.

    A hire date cannot collide with anything. It has no business being lost
    because a phone number did.
    """

    def _onboard(self, **kwargs):
        defaults = dict(
            employee_name="Vishnu S",
            department="General",
            designation="Service engineer",
            work_location="Vellore",
            date_of_joining="2026-03-15",
            mobile_number="9943150841",
            email_id="mahavishnu6598@gmail.com",
        )
        defaults.update(kwargs)
        return Onboarding.objects.create(**defaults)

    def _employee(self, name, **kwargs):
        return Employee.objects.create(
            employee_name=name,
            branch=kwargs.pop("branch", "Vellore"),
            role=kwargs.pop("role", "service engineer"),
            department=kwargs.pop("department", "General"),
            salary=kwargs.pop("salary", Decimal("23456.00")),
            **kwargs,
        )

    def test_hire_date_lands_even_when_the_phone_belongs_to_a_third_row(self):
        # Matched on email, so the clash on phone only surfaces on save.
        target = self._employee("Vishnu S", email="mahavishnu6598@gmail.com")
        self._employee("Someone Else", phone="9943150841")

        self._onboard()

        target.refresh_from_db()
        self.assertEqual(str(target.date_of_joining), "2026-03-15")
        # The clashing number stays with whoever already had it.
        self.assertIsNone(target.phone)

    def test_the_rest_of_the_onboarding_details_land_too(self):
        target = self._employee("Vishnu S", email="mahavishnu6598@gmail.com", role="old role")
        self._employee("Someone Else", phone="9943150841")

        self._onboard()

        target.refresh_from_db()
        self.assertEqual(target.role, "Service engineer")
        self.assertEqual(target.department, "General")
        self.assertEqual(str(target.date_of_joining), "2026-03-15")

    def test_a_clean_onboarding_still_writes_everything(self):
        target = self._employee("Vishnu S", role="old role")

        self._onboard()

        target.refresh_from_db()
        self.assertEqual(str(target.date_of_joining), "2026-03-15")
        self.assertEqual(target.email, "mahavishnu6598@gmail.com")
        self.assertEqual(target.phone, "9943150841")
        self.assertEqual(target.role, "Service engineer")

    def test_the_conflict_is_reported_rather_than_swallowed(self):
        self._employee("Vishnu S", email="mahavishnu6598@gmail.com")
        self._employee("Someone Else", phone="9943150841")

        with self.assertLogs("onboarding", level="WARNING") as captured:
            self._onboard()

        joined = " ".join(captured.output)
        self.assertIn("Vishnu S", joined)
        # Names the field that could not be written, so HR knows what to merge.
        self.assertIn("phone", joined)
