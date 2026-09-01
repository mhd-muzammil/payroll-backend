"""Punching in and out of a call, and where it happened.

An engineer in gloves outside a customer's premises was being asked to drive a
four-step workflow — Accept, Start Travel, Reached, Start Work — when the office
only ever needed two facts from it: that they got there, and that they finished.

What the two buttons add over the four they replace is the POSITION. "Reached
2:40pm" is a claim; "reached 2:40pm, here" can be checked against the customer's
address.
"""
import datetime
from decimal import Decimal

from django.utils import timezone
from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee

from .models import Case


class CasePunchTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="punchy", password="x", role="employee")
        self.engineer = Employee.objects.create(
            user=self.user,
            employee_name="Punchy",
            email="punchy@example.com",
            role="Field Engineer",
            department="Service",
            branch="Chennai",
            salary=Decimal("20000"),
        )
        self.client.force_authenticate(self.user)

    def _case(self, status="assigned", **extra):
        return Case.objects.create(
            title="Service call (WO-1)",
            assigned_to=self.engineer,
            status=status,
            in_current_plan=True,
            **extra,
        )

    def _punch(self, case, which, **body):
        return self.client.post(f"/api/cases/{case.id}/{which}/", body, format="json")

    # -- what the buttons are for --------------------------------------------

    def test_punching_in_records_where_the_engineer_was(self):
        case = self._case()
        response = self._punch(case, "punch_in", latitude=12.9716, longitude=77.5946, accuracy=8)
        self.assertEqual(response.status_code, 200, response.data)

        case.refresh_from_db()
        self.assertEqual(case.punch_in_lat, 12.9716)
        self.assertEqual(case.punch_in_lon, 77.5946)
        self.assertEqual(case.punch_in_accuracy, 8)
        self.assertIsNotNone(case.reached_at)

    def test_punching_out_records_where_they_finished(self):
        case = self._case(status="working", reached_at=timezone.now())
        response = self._punch(case, "punch_out", latitude=12.9720, longitude=77.5950, accuracy=6)
        self.assertEqual(response.status_code, 200, response.data)

        case.refresh_from_db()
        self.assertEqual(case.punch_out_lat, 12.9720)
        self.assertEqual(case.punch_out_lon, 77.5950)
        self.assertIsNotNone(case.completed_at)

    def test_the_two_punches_leave_the_statuses_the_rest_of_the_system_reads(self):
        """OpenCall and the case list both key off status. Two buttons instead of
        four must not cost them the answer."""
        case = self._case()
        self._punch(case, "punch_in", latitude=12.9716, longitude=77.5946)
        case.refresh_from_db()
        self.assertEqual(case.status, "working")

        self._punch(case, "punch_out", latitude=12.9716, longitude=77.5946)
        case.refresh_from_db()
        self.assertEqual(case.status, "completed")

    # -- a punch must never be refused for want of GPS -----------------------

    def test_a_punch_with_no_fix_still_records_the_time(self):
        """An engineer standing at a customer with no signal must still be able
        to say they are there. A punch that failed would leave them stuck."""
        case = self._case()
        response = self._punch(case, "punch_in")
        self.assertEqual(response.status_code, 200, response.data)

        case.refresh_from_db()
        self.assertEqual(case.status, "working")
        self.assertIsNotNone(case.reached_at)
        self.assertIsNone(case.punch_in_lat)

    def test_a_half_a_position_is_no_position(self):
        """A latitude with no longitude is not somewhere. Storing it would put
        the engineer on the equator."""
        case = self._case()
        self._punch(case, "punch_in", latitude=12.9716)
        case.refresh_from_db()
        self.assertIsNone(case.punch_in_lat)
        self.assertIsNone(case.punch_in_lon)
        self.assertEqual(case.status, "working")

    def test_nonsense_coordinates_are_dropped_not_stored(self):
        case = self._case()
        self._punch(case, "punch_in", latitude="here", longitude="there")
        case.refresh_from_db()
        self.assertIsNone(case.punch_in_lat)
        self.assertEqual(case.status, "working")

    # -- the shorter road reaches the same places ----------------------------

    def test_punching_in_works_from_any_stage_the_old_buttons_could_reach(self):
        for status in ("assigned", "accepted", "on_the_way", "reached"):
            with self.subTest(status=status):
                case = self._case(status=status)
                response = self._punch(case, "punch_in", latitude=12.9, longitude=77.5)
                self.assertEqual(response.status_code, 200, f"{status}: {response.data}")
                case.refresh_from_db()
                self.assertEqual(case.status, "working")

    def test_a_finished_call_cannot_be_punched_again(self):
        case = self._case(status="completed", completed_at=timezone.now())
        self.assertNotEqual(self._punch(case, "punch_in", latitude=12.9, longitude=77.5).status_code, 200)
        self.assertNotEqual(self._punch(case, "punch_out", latitude=12.9, longitude=77.5).status_code, 200)

    def test_an_engineer_cannot_punch_somebody_elses_call(self):
        other_user = User.objects.create_user(username="other", password="x", role="employee")
        other = Employee.objects.create(
            user=other_user, employee_name="Other", email="other@example.com",
            role="Field Engineer", department="Service", branch="Chennai", salary=Decimal("1"),
        )
        theirs = Case.objects.create(
            title="Not mine", assigned_to=other, status="assigned", in_current_plan=True
        )
        self.assertNotEqual(self._punch(theirs, "punch_in", latitude=12.9, longitude=77.5).status_code, 200)

    def test_the_old_actions_still_work(self):
        """They are not removed, only unbuttoned — anything still calling them,
        including a phone running yesterday's JavaScript, keeps working."""
        case = self._case()
        self.assertEqual(self.client.post(f"/api/cases/{case.id}/accept/").status_code, 200)
        case.refresh_from_db()
        self.assertEqual(case.status, "accepted")
