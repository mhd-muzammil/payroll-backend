from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from attendance.models import Attendance
from authentication.models import User
from employees.models import Employee


class AttendanceMustHaveADayTests(APITestCase):
    """A record has to say which day it is about.

    The office marked somebody absent, left both times empty -- there is no
    punch on a day nobody came in -- and the row was created with no date at
    all. Every list on the page is grouped and filtered by the date, read off
    intime, so the record existed and nobody could see or correct it.
    """

    def setUp(self):
        self.admin = User.objects.create_user(
            username="office-day", password="x", role="superadmin", is_superuser=True
        )
        self.employee = Employee.objects.create(
            employee_name="Mohan R", role="Service engineer", department="Service",
            branch="Chennai", salary=27208,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _post(self, **extra):
        payload = {
            "employee_name": "Mohan R",
            "role": "Service engineer",
            "department": "Service",
            "salary": "27208.00",
            "status": "Absent",
        }
        payload.update(extra)
        return self.client.post("/api/attendance/", payload, format="json")

    def test_a_record_with_no_day_is_refused(self):
        response = self._post(intime=None, outtime=None)
        self.assertEqual(response.status_code, 400)
        self.assertIn("intime", response.json())
        self.assertEqual(Attendance.objects.count(), 0)

    def test_an_absent_day_is_stored_at_midnight_and_can_be_found(self):
        day = timezone.localdate()
        response = self._post(intime=f"{day.isoformat()}T00:00:00", outtime=None)
        self.assertEqual(response.status_code, 201, response.content)

        record = Attendance.objects.get()
        self.assertEqual(timezone.localtime(record.intime).date(), day)
        self.assertIsNone(record.outtime)
        self.assertEqual(record.status, "Absent")

        # The list the page reads, filtered the way the page filters it.
        listed = self.client.get(f"/api/attendance/?start_date={day}&end_date={day}").json()
        rows = listed if isinstance(listed, list) else listed.get("results", [])
        self.assertTrue(
            any(r["status"] == "Absent" for r in rows),
            "the absent day must appear in the day's list",
        )

    def test_editing_a_record_does_not_require_the_day_again(self):
        day = timezone.localdate()
        self._post(intime=f"{day.isoformat()}T00:00:00")
        record = Attendance.objects.get()
        response = self.client.patch(
            f"/api/attendance/{record.id}/", {"status": "Leave"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
