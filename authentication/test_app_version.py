"""Which build of the app each phone is running.

There is no store to ask -- the APK is handed around as a file and nothing here
ever sees an install -- so "who has picked up the new version" was a question
the office answered by ringing people. The app names its build on every request
now, and these pin what that is allowed to cost and what it must never do.
"""
from django.test import TestCase, override_settings
from django.utils import timezone
from rest_framework.test import APIClient

from authentication.models import User
from employees.models import Employee


class AppVersionIsRememberedTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="engineer-v", password="x", role="employee"
        )
        self.employee = Employee.objects.create(
            user=self.user, employee_name="Version Tester", role="Service engineer",
            department="Service", branch="Chennai", salary=1,
        )
        self.client = APIClient()
        self.client.force_authenticate(self.user)

    def _get(self, **headers):
        return self.client.get("/api/cases/", **headers)

    def test_the_app_saying_its_build_is_written_down(self):
        self._get(HTTP_X_PAYROLL_APP_VERSION="1.4 (5)")
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_version, "1.4 (5)")
        self.assertIsNotNone(self.user.app_version_at)

    def test_saying_it_again_does_not_move_the_date(self):
        """The date means "on this build since", so a repeat is not a change.

        An engineer's phone calls this server every thirty seconds while they
        are on duty; rewriting the row each time would cost a write a second
        and turn the date into "last seen", which last_app_login_at already is.
        """
        self._get(HTTP_X_PAYROLL_APP_VERSION="1.4 (5)")
        self.user.refresh_from_db()
        first = self.user.app_version_at

        self._get(HTTP_X_PAYROLL_APP_VERSION="1.4 (5)")
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_version_at, first)

    def test_an_update_moves_it(self):
        self._get(HTTP_X_PAYROLL_APP_VERSION="1.4 (5)")
        self.user.refresh_from_db()
        before = self.user.app_version_at

        self._get(HTTP_X_PAYROLL_APP_VERSION="1.5 (6)")
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_version, "1.5 (6)")
        self.assertGreater(self.user.app_version_at, before)

    def test_a_browser_says_nothing_and_nothing_is_recorded(self):
        """The old app is silent too, and that silence is the answer."""
        self._get()
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_version, "")
        self.assertIsNone(self.user.app_version_at)

    def test_a_signed_out_request_records_nothing(self):
        anonymous = APIClient()
        response = anonymous.get("/api/cases/", HTTP_X_PAYROLL_APP_VERSION="1.4 (5)")
        self.assertIn(response.status_code, (401, 403))
        self.user.refresh_from_db()
        self.assertEqual(self.user.app_version, "")

    def test_a_junk_header_cannot_break_a_request_or_the_column(self):
        response = self._get(HTTP_X_PAYROLL_APP_VERSION="x" * 500)
        self.assertEqual(response.status_code, 200)
        self.user.refresh_from_db()
        self.assertEqual(len(self.user.app_version), 40)


@override_settings(CURRENT_APP_VERSION="1.4")
class AppUsageShowsWhoHasUpdatedTests(TestCase):
    """The page that already answers "who is using the app" now answers "on what"."""

    def setUp(self):
        self.office = User.objects.create_user(
            username="office-v", password="x", role="superadmin", is_superuser=True
        )
        self.client = APIClient()
        self.client.force_authenticate(self.office)
        self.now = timezone.now()

    def _engineer(self, name, *, version="", used_app=True):
        user = User.objects.create_user(username=name.lower().replace(" ", "-"), password="x", role="employee")
        if used_app:
            User.objects.filter(pk=user.pk).update(last_app_login_at=self.now)
        if version:
            User.objects.filter(pk=user.pk).update(app_version=version, app_version_at=self.now)
        return Employee.objects.create(
            user=user, employee_name=name, role="Service engineer",
            department="Service", branch="Chennai", salary=1,
        )

    def _usage(self):
        response = self.client.get("/api/employees/app_usage/")
        self.assertEqual(response.status_code, 200, response.content)
        return response.json()

    def test_the_page_counts_who_is_on_the_new_build(self):
        self._engineer("Updated One", version="1.4 (5)")
        self._engineer("Updated Two", version="1.4 (5)")
        self._engineer("Old One")  # uses the app, never reported a version

        usage = self._usage()
        self.assertEqual(usage["current_app_version"], "1.4")
        self.assertEqual(usage["using_app"], 3)
        self.assertEqual(usage["on_current_version"], 2)
        self.assertEqual(usage["behind_version"], 1)

    def test_each_row_carries_its_own_build(self):
        self._engineer("Updated One", version="1.4 (5)")
        rows = {r["employee_name"]: r for r in self._usage()["rows"]}
        self.assertEqual(rows["Updated One"]["app_version"], "1.4 (5)")
        self.assertIsNotNone(rows["Updated One"]["app_version_at"])

    def test_somebody_who_never_used_the_app_is_not_counted_as_behind(self):
        """They are a different problem, and the page already names it."""
        self._engineer("Never Opened It", used_app=False)
        usage = self._usage()
        self.assertEqual(usage["using_app"], 0)
        self.assertEqual(usage["behind_version"], 0)
