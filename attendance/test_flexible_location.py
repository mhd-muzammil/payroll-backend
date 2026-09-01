"""Marking attendance from anywhere, for the people who work everywhere.

Attendance check-in measures the distance from a work location stored on the
employee and refuses anything past 50m. That is the right rule for an office
job and the wrong question for a field engineer, who is legitimately somewhere
different every day. In practice they were not being refused for standing too
far away at all -- they were refused earlier, by "Allowed location not set for
this employee", because nobody ever sets an office for someone who has no
office.

`flexible_location` turns the test off for one employee rather than widening
the radius for everybody. These tests hold both halves: that the flag lets the
right people through, and that with it off nothing about the old behaviour has
moved -- same refusals, same wording, same status codes.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee

# The registered office, and a point far enough away that no accuracy fudge
# could excuse it -- roughly 5km north.
OFFICE = (13.0827, 80.2707)
FAR_AWAY = (13.1277, 80.2707)
# Inside the fence: about 22m east of the office.
NEXT_DOOR = (13.0827, 80.2709)


class FlexibleLocationTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="rover", password="x", role="employee"
        )
        self.employee = Employee.objects.create(
            user=self.user,
            employee_name="Rover",
            email="rover@example.com",
            role="Field Engineer",
            department="Service",
            branch="Chennai",
            salary=Decimal("20000"),
            work_lat=OFFICE[0],
            work_lon=OFFICE[1],
        )
        self.client.force_authenticate(self.user)

    def _check_in(self, point):
        return self.client.post(
            "/api/attendance/check_in/",
            {"latitude": point[0], "longitude": point[1]},
            format="json",
        )

    def _check_out(self, point):
        return self.client.post(
            "/api/attendance/check_out/",
            {"latitude": point[0], "longitude": point[1]},
            format="json",
        )

    # ------------------------------------------------ the flag does its job

    def test_far_away_is_refused_when_not_flexible(self):
        response = self._check_in(FAR_AWAY)
        self.assertEqual(response.status_code, 403)
        self.assertIn("too far from the office", response.data["detail"])
        self.assertIn("Must be within 50m.", response.data["detail"])

    def test_far_away_is_accepted_when_flexible(self):
        self.employee.flexible_location = True
        self.employee.save(update_fields=["flexible_location"])

        response = self._check_in(FAR_AWAY)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data["status"], "Present")

    def test_no_work_location_is_refused_when_not_flexible(self):
        """The refusal a field engineer actually hits: no office was ever set."""
        self.employee.work_lat = None
        self.employee.work_lon = None
        self.employee.save(update_fields=["work_lat", "work_lon"])

        response = self._check_in(FAR_AWAY)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["detail"],
            "Allowed location not set for this employee. Please contact HR.",
        )

    def test_no_work_location_is_fine_when_flexible(self):
        self.employee.work_lat = None
        self.employee.work_lon = None
        self.employee.flexible_location = True
        self.employee.save(
            update_fields=["work_lat", "work_lon", "flexible_location"]
        )

        response = self._check_in(FAR_AWAY)
        self.assertEqual(response.status_code, 201, response.data)

    def test_check_out_from_anywhere_when_flexible(self):
        self.employee.flexible_location = True
        self.employee.save(update_fields=["flexible_location"])

        self.assertEqual(self._check_in(FAR_AWAY).status_code, 201)
        # Somewhere else again by the end of the day, which is the point.
        response = self._check_out((12.9716, 77.5946))
        self.assertEqual(response.status_code, 200, response.data)
        self.assertIsNotNone(response.data["outtime"])

    def test_check_out_far_away_still_refused_when_not_flexible(self):
        self.assertEqual(self._check_in(NEXT_DOOR).status_code, 201)

        response = self._check_out(FAR_AWAY)
        self.assertEqual(response.status_code, 403)
        self.assertIn("Must be within 50m to clock out.", response.data["detail"])

    # ------------------------- and nothing about the old behaviour has moved

    def test_default_is_off(self):
        """A field nobody sets must not quietly open the gate for everyone."""
        self.assertFalse(Employee.objects.get(pk=self.employee.pk).flexible_location)

    def test_inside_the_fence_still_works(self):
        response = self._check_in(NEXT_DOOR)
        self.assertEqual(response.status_code, 201, response.data)

    def test_missing_coordinates_still_refused(self):
        response = self.client.post("/api/attendance/check_in/", {}, format="json")
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["detail"], "Latitude and Longitude are required."
        )

    def test_junk_coordinates_still_refused(self):
        response = self.client.post(
            "/api/attendance/check_in/",
            {"latitude": "here", "longitude": "there"},
            format="json",
        )
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.data["detail"], "Invalid coordinates provided.")

    def test_double_check_in_still_refused_when_flexible(self):
        """The flag relaxes location. It must not relax anything else."""
        self.employee.flexible_location = True
        self.employee.save(update_fields=["flexible_location"])

        self.assertEqual(self._check_in(FAR_AWAY).status_code, 201)
        second = self._check_in(FAR_AWAY)
        self.assertEqual(second.status_code, 400)
        self.assertEqual(second.data["detail"], "Already checked in for today.")

    def test_check_out_without_check_in_still_refused_when_flexible(self):
        self.employee.flexible_location = True
        self.employee.save(update_fields=["flexible_location"])

        response = self._check_out(FAR_AWAY)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(
            response.data["detail"],
            "No clock-in found for today. Cannot clock out.",
        )

    # ------------------------------------------------- and HR can set it

    def test_the_flag_round_trips_through_the_employee_api(self):
        admin = User.objects.create_user(
            username="hr-boss", password="x", role="superadmin", is_superuser=True
        )
        self.client.force_authenticate(admin)

        response = self.client.patch(
            f"/api/employees/{self.employee.pk}/",
            {"flexible_location": True},
            format="json",
        )
        self.assertEqual(response.status_code, 200, response.data)
        self.assertTrue(response.data["flexible_location"])
        self.employee.refresh_from_db()
        self.assertTrue(self.employee.flexible_location)
