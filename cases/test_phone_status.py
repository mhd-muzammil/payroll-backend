"""How each phone is set up to record, and the history of it.

The office could not see whether a phone would keep recording: the battery left
to the phone's optimiser stops the app in the background, and Location or
notifications can be refused. The app now reports its settings when it opens
(/api/tracking/phone_status/); App Usage shows each phone as it is now, and its
history of changes (/api/employees/<id>/app_history/).
"""
import datetime
from decimal import Decimal
from unittest import mock

from django.db import connection
from django.test import TestCase
from django.test.utils import CaptureQueriesContext
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from cases.models import PhoneStatus
from employees.models import Employee

REPORT = {
    "app_version": "1.5.0 (7)",
    "android_version": "14",
    "battery_exempt": True,
    "location_permission": "granted",
    "notification_permission": "granted",
    "gps_on": True,
}


def make_engineer(username, name, branch="Chennai"):
    user = User.objects.create_user(username=username, password="x", role="employee")
    engineer = Employee.objects.create(
        user=user,
        employee_name=name,
        email=f"{username}@example.com",
        role="Field Engineer",
        department="Service",
        branch=branch,
        salary=Decimal("20000"),
    )
    return user, engineer


class PhoneStatusReportTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.user, cls.engineer = make_engineer("phoned", "Phoned Prabhu")

    def setUp(self):
        self.phone = APIClient()
        self.phone.force_authenticate(self.user)
        self.phone.credentials(HTTP_X_PAYROLL_CLIENT="app")

    def _post(self, body, client=None):
        return (client or self.phone).post("/api/tracking/phone_status/", body, format="json")

    def test_a_first_report_is_stored(self):
        response = self._post(REPORT)
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(response.data, {"changed": True})
        status = PhoneStatus.objects.get(employee=self.engineer)
        self.assertEqual(status.app_version, "1.5.0 (7)")
        self.assertEqual(status.android_version, "14")
        self.assertIs(status.battery_exempt, True)
        self.assertEqual(status.location_permission, "granted")
        self.assertEqual(status.notification_permission, "granted")
        self.assertIs(status.gps_on, True)
        self.assertEqual(status.first_seen_at, status.last_seen_at)

    def test_the_same_report_again_only_moves_last_seen(self):
        self._post(REPORT)
        first = PhoneStatus.objects.get(employee=self.engineer)
        PhoneStatus.objects.filter(pk=first.pk).update(
            last_seen_at=first.last_seen_at - datetime.timedelta(hours=1)
        )

        response = self._post(REPORT)

        self.assertEqual(response.status_code, 200, response.data)
        self.assertEqual(response.data, {"changed": False})
        self.assertEqual(PhoneStatus.objects.filter(employee=self.engineer).count(), 1)
        again = PhoneStatus.objects.get(employee=self.engineer)
        self.assertEqual(again.first_seen_at, first.first_seen_at)
        self.assertGreater(again.last_seen_at, first.last_seen_at - datetime.timedelta(hours=1))

    def test_a_change_starts_a_new_row_and_keeps_the_old_one(self):
        self._post({**REPORT, "battery_exempt": False})
        self._post(REPORT)

        rows = list(PhoneStatus.objects.filter(employee=self.engineer).order_by("first_seen_at", "id"))
        self.assertEqual([r.battery_exempt for r in rows], [False, True])

    def test_a_browser_cannot_report_a_phone(self):
        """Somebody signed in as the engineer in a browser is not his phone."""
        browser = APIClient()
        browser.force_authenticate(self.user)

        response = self._post(REPORT, client=browser)

        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.data["app_only"])
        self.assertFalse(PhoneStatus.objects.exists())

    def test_a_login_with_no_employee_is_refused(self):
        stranger = User.objects.create_user(username="no-employee", password="x", role="employee")
        client = APIClient()
        client.force_authenticate(stranger)
        client.credentials(HTTP_X_PAYROLL_CLIENT="app")

        response = self._post(REPORT, client=client)

        self.assertEqual(response.status_code, 400)
        self.assertFalse(PhoneStatus.objects.exists())

    def test_odd_values_read_as_not_said_and_cost_nothing_else(self):
        response = self._post({
            "app_version": "x" * 99,
            "android_version": 14,
            "battery_exempt": "yes",
            "location_permission": "always",
            "notification_permission": ["granted"],
            "gps_on": 1,
        })

        self.assertEqual(response.status_code, 201, response.data)
        status = PhoneStatus.objects.get(employee=self.engineer)
        self.assertEqual(status.app_version, "x" * 40)
        self.assertEqual(status.android_version, "")
        self.assertIsNone(status.battery_exempt)
        self.assertEqual(status.location_permission, "")
        self.assertEqual(status.notification_permission, "")
        self.assertIsNone(status.gps_on)

    def test_an_older_phone_that_cannot_say_is_stored_as_not_said(self):
        self._post({"app_version": "1.4.1 (6)", "location_permission": "granted"})
        status = PhoneStatus.objects.get(employee=self.engineer)
        self.assertIsNone(status.battery_exempt)
        self.assertIsNone(status.gps_on)
        self.assertEqual(status.location_permission, "granted")

    def test_an_empty_or_shapeless_report_is_refused_and_keeps_the_last_one(self):
        """A body that says nothing must not replace what the office sees."""
        self._post(REPORT)

        for body in ({}, {"unknown": 1}, [REPORT], "granted"):
            with self.subTest(body=body):
                self.assertEqual(self._post(body).status_code, 400)

        self.assertEqual(PhoneStatus.objects.filter(employee=self.engineer).count(), 1)
        self.assertIs(PhoneStatus.objects.get(employee=self.engineer).battery_exempt, True)

    def test_control_characters_and_lone_surrogates_are_dropped_not_a_500(self):
        """Postgres refuses a NUL in text and a lone surrogate cannot be encoded."""
        # Raw, so the JSON carries the escapes and the server decodes them.
        body = r'{"app_version": "1.5\u0000.0\ud800 (7)", "android_version": "1\u00074"}'

        response = self.phone.post("/api/tracking/phone_status/", body, content_type="application/json")

        self.assertEqual(response.status_code, 201, response.data)
        status = PhoneStatus.objects.get(employee=self.engineer)
        self.assertEqual(status.app_version, "1.5.0 (7)")
        self.assertEqual(status.android_version, "14")

    def test_a_report_touches_nothing_but_its_own_rows(self):
        """Not the duty, not the contact the board reads, not the kilometres."""
        from cases.models import DutySession, LocationPing, PhoneEvent

        session = DutySession.objects.create(engineer=self.engineer)

        response = self._post(REPORT)

        self.assertEqual(response.status_code, 201, response.data)
        session.refresh_from_db()
        self.assertIsNone(session.last_contact_at)
        self.assertIsNone(session.ended_at)
        self.assertEqual(DutySession.objects.count(), 1)
        self.assertFalse(LocationPing.objects.exists())
        self.assertFalse(PhoneEvent.objects.exists())


class AppUsagePhoneTests(TestCase):
    def setUp(self):
        self.office = User.objects.create_user(
            username="office-p", password="x", role="superadmin", is_superuser=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.now = timezone.now()

    def _engineer(self, name, branch="Chennai"):
        user, engineer = make_engineer(name.lower().replace(" ", "-"), name, branch)
        User.objects.filter(pk=user.pk).update(last_app_login_at=self.now)
        return engineer

    def _status(self, engineer, minutes_ago, **values):
        at = self.now - datetime.timedelta(minutes=minutes_ago)
        return PhoneStatus.objects.create(
            employee=engineer, first_seen_at=at, last_seen_at=at, **{**REPORT, **values}
        )

    def _usage(self):
        response = self.client.get("/api/employees/app_usage/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_each_row_carries_the_phone_as_it_is_now(self):
        allowed = self._engineer("Allowed Arun")
        self._status(allowed, 60, battery_exempt=False)
        self._status(allowed, 5, battery_exempt=True)
        self._engineer("Silent Siva")

        rows = {r["employee_name"]: r for r in self._usage()["rows"]}

        self.assertIs(rows["Allowed Arun"]["phone"]["battery_exempt"], True)
        self.assertEqual(rows["Allowed Arun"]["phone"]["location_permission"], "granted")
        self.assertIsNone(rows["Silent Siva"]["phone"])

    def test_two_reports_in_the_same_instant_read_the_later_one(self):
        tied = self._engineer("Tied Tamil")
        self._status(tied, 5, battery_exempt=False)
        self._status(tied, 5, battery_exempt=True)

        rows = {r["employee_name"]: r for r in self._usage()["rows"]}

        self.assertIs(rows["Tied Tamil"]["phone"]["battery_exempt"], True)

    def test_the_page_counts_every_phone_that_reported(self):
        self._status(self._engineer("Allowed Arun"), 5, battery_exempt=True)
        self._status(self._engineer("Blocked Bala"), 5, battery_exempt=False)
        self._status(self._engineer("Old Phone Oviya"), 5, battery_exempt=None)
        self._engineer("Silent Siva")
        # Signed in on the app before its logins were recorded: no app login on
        # file, but the phone reports all the same and is counted.
        _, early = make_engineer("early-ezhil", "Early Ezhil")
        self._status(early, 5, battery_exempt=False)

        usage = self._usage()

        self.assertEqual(usage["phones_reported"], 4)
        self.assertEqual(usage["battery_allowed"], 1)
        self.assertEqual(usage["battery_not_allowed"], 2)
        self.assertEqual(usage["battery_cannot_tell"], 1)
        rows = {r["employee_name"]: r for r in usage["rows"]}
        self.assertFalse(rows["Early Ezhil"]["uses_app"])
        self.assertIs(rows["Early Ezhil"]["phone"]["battery_exempt"], False)

    def test_the_lookup_does_not_grow_with_the_people_or_their_history(self):
        def queries():
            with CaptureQueriesContext(connection) as captured:
                self._usage()
            return len(captured)

        self._status(self._engineer("Allowed Arun"), 5)
        few = queries()
        for i in range(5):
            engineer = self._engineer(f"More Mani {i}")
            for minutes in (50, 40, 30):
                self._status(engineer, minutes, battery_exempt=bool(minutes % 20))

        self.assertEqual(queries(), few)

    def test_the_existing_fields_are_unchanged(self):
        self._engineer("Allowed Arun")
        usage = self._usage()
        for key in ("total", "using_app", "not_using_app", "no_login_account", "browser_only",
                    "current_app_version", "on_current_version", "behind_version"):
            self.assertIn(key, usage)
        row = usage["rows"][0]
        for key in ("employee_id", "employee_name", "branch", "role", "username", "has_login",
                    "last_login", "first_app_login_at", "last_app_login_at", "uses_app",
                    "app_version", "app_version_at"):
            self.assertIn(key, row)


class AppHistoryTests(TestCase):
    def setUp(self):
        self.office = User.objects.create_user(
            username="office-h", password="x", role="superadmin", is_superuser=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.user, self.engineer = make_engineer("histo", "Histo Hari")
        self.now = timezone.now()

    def _status(self, minutes_ago, **values):
        at = self.now - datetime.timedelta(minutes=minutes_ago)
        return PhoneStatus.objects.create(
            employee=self.engineer, first_seen_at=at, last_seen_at=at, **{**REPORT, **values}
        )

    def _history(self, client=None, pk=None):
        return (client or self.client).get(f"/api/employees/{pk or self.engineer.id}/app_history/")

    def test_the_history_is_every_change_newest_first(self):
        self._status(120, app_version="1.4.1 (6)", battery_exempt=None)
        self._status(60, battery_exempt=False)
        self._status(5, battery_exempt=True)

        response = self._history()

        self.assertEqual(response.status_code, 200, response.content)
        body = response.json()
        self.assertEqual(body["employee_name"], "Histo Hari")
        self.assertEqual([r["battery_exempt"] for r in body["rows"]], [True, False, None])
        self.assertEqual(body["rows"][-1]["app_version"], "1.4.1 (6)")
        self.assertEqual(body["total"], 3)
        self.assertFalse(body["truncated"])

    def test_a_long_history_says_it_was_cut(self):
        for minutes in (30, 20, 10):
            self._status(minutes, battery_exempt=bool(minutes % 20))

        with mock.patch("employees.views.HISTORY_LIMIT", 2):
            body = self._history().json()

        self.assertEqual(body["total"], 3)
        self.assertTrue(body["truncated"])
        self.assertEqual([r["battery_exempt"] for r in body["rows"]], [True, False])

    def test_an_engineer_cannot_read_anybody_s_history(self):
        engineer_client = APIClient()
        engineer_client.force_authenticate(self.user)
        self.assertEqual(self._history(client=engineer_client).status_code, 403)

    def test_an_unknown_or_odd_id_is_not_found(self):
        self.assertEqual(self._history(pk=999999).status_code, 404)
        self.assertEqual(self.client.get("/api/employees/abc/app_history/").status_code, 404)

    def test_hr_outside_the_branch_cannot_read_it(self):
        hr = User.objects.create_user(username="hr-salem", password="x", role="hr")
        hr_client = APIClient()
        hr_client.force_authenticate(hr)
        with mock.patch("employees.views.get_allowed_branches", return_value=["Salem"]):
            self.assertEqual(self._history(client=hr_client).status_code, 404)
        with mock.patch("employees.views.get_allowed_branches", return_value=["Chennai"]):
            self.assertEqual(self._history(client=hr_client).status_code, 200)
