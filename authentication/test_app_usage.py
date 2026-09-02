"""Who has started using the phone app.

Nobody can be told who DOWNLOADED the APK: it is passed around as a file and
never touches this server. Who has signed in FROM it can be told, and it is the
better question anyway -- somebody who installed it and never opened it needs
chasing exactly as much as somebody who never installed it.

Two halves. The login view stamps the app-only timestamps when the request
carries the app's header, and the report reads them back employee by employee.
The report is employee-centric rather than user-centric on purpose: the Users
table only holds login accounts, and an employee with no account is precisely
somebody who CANNOT use the app -- the most important row on the page, and the
one a list of users would leave out.
"""
from decimal import Decimal

from rest_framework.test import APITestCase

from authentication.models import User
from employees.models import Employee

APP_HEADER = {"HTTP_X_PAYROLL_CLIENT": "app"}


class AppUsageTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="boss", password="pw", role="superadmin", is_superuser=True
        )

        # Signs in from the app.
        self.app_user = User.objects.create_user(
            username="tharik", password="pw", role="employee"
        )
        self.app_employee = self._employee(self.app_user, "Tharik", "Chennai")

        # Has a login, has never used it.
        self.idle_user = User.objects.create_user(
            username="perumal", password="pw", role="employee"
        )
        self.idle_employee = self._employee(self.idle_user, "Perumal", "Salem")

        # No login account at all: cannot use the app however hard they try.
        self.orphan = Employee.objects.create(
            employee_name="Agalia",
            email="agalia@example.com",
            role="Field Engineer",
            department="Service",
            branch="Vellore",
            salary=Decimal("20000"),
        )

    def _employee(self, user, name, branch):
        return Employee.objects.create(
            user=user,
            employee_name=name,
            email=f"{name.lower()}@example.com",
            role="Field Engineer",
            department="Service",
            branch=branch,
            salary=Decimal("20000"),
        )

    def _login(self, username, from_app):
        return self.client.post(
            "/api/auth/login/",
            {"username": username, "password": "pw"},
            format="json",
            **(APP_HEADER if from_app else {}),
        )

    def _report(self, as_user=None):
        self.client.force_authenticate(as_user or self.admin)
        response = self.client.get("/api/employees/app_usage/")
        self.client.force_authenticate(None)
        return response

    def _row(self, report, name):
        return next(r for r in report.data["rows"] if r["employee_name"] == name)

    # ------------------------------------------------------- the login stamps

    def test_signing_in_from_the_app_is_recorded(self):
        self.assertEqual(self._login("tharik", from_app=True).status_code, 200)

        self.app_user.refresh_from_db()
        self.assertIsNotNone(self.app_user.first_app_login_at)
        self.assertIsNotNone(self.app_user.last_app_login_at)
        # SIMPLE_JWT's UPDATE_LAST_LOGIN, which was off before this and is why
        # not one of 77 accounts had ever recorded a login.
        self.assertIsNotNone(self.app_user.last_login)

    def test_signing_in_from_a_browser_is_not_an_app_login(self):
        self.assertEqual(self._login("tharik", from_app=False).status_code, 200)

        self.app_user.refresh_from_db()
        self.assertIsNone(self.app_user.last_app_login_at)
        self.assertIsNotNone(self.app_user.last_login, "but they did sign in")

    def test_the_first_app_login_is_kept_and_the_last_moves(self):
        self._login("tharik", from_app=True)
        self.app_user.refresh_from_db()
        first = self.app_user.first_app_login_at

        self._login("tharik", from_app=True)
        self.app_user.refresh_from_db()

        self.assertEqual(self.app_user.first_app_login_at, first)
        self.assertGreaterEqual(self.app_user.last_app_login_at, first)

    def test_a_failed_password_is_not_a_login(self):
        response = self.client.post(
            "/api/auth/login/",
            {"username": "tharik", "password": "wrong"},
            format="json",
            **APP_HEADER,
        )
        self.assertEqual(response.status_code, 401)

        self.app_user.refresh_from_db()
        self.assertIsNone(self.app_user.last_app_login_at)

    # ------------------------------------------------------------ the report

    def test_the_report_separates_the_three_states(self):
        self._login("tharik", from_app=True)
        self._login("perumal", from_app=False)

        report = self._report()
        self.assertEqual(report.status_code, 200, report.data)

        self.assertTrue(self._row(report, "Tharik")["uses_app"])
        self.assertFalse(self._row(report, "Perumal")["uses_app"])
        self.assertTrue(self._row(report, "Perumal")["has_login"])
        self.assertFalse(self._row(report, "Agalia")["has_login"])

    def test_somebody_with_no_login_account_is_still_listed(self):
        """The row a list of Users would leave out, and the one that matters."""
        report = self._report()
        names = [r["employee_name"] for r in report.data["rows"]]
        self.assertIn("Agalia", names)
        self.assertEqual(report.data["no_login_account"], 1)

    def test_the_counts_add_up(self):
        self._login("tharik", from_app=True)

        report = self._report()
        self.assertEqual(report.data["total"], 3)
        self.assertEqual(report.data["using_app"], 1)
        self.assertEqual(report.data["not_using_app"], 2)
        self.assertEqual(
            report.data["using_app"] + report.data["not_using_app"],
            report.data["total"],
        )

    def test_browser_only_is_counted_separately(self):
        """A different job from setting somebody up: they have the credentials."""
        self._login("perumal", from_app=False)

        report = self._report()
        self.assertEqual(report.data["browser_only"], 1)

    def test_nobody_has_used_it_before_anybody_signs_in(self):
        report = self._report()
        self.assertEqual(report.data["using_app"], 0)
        self.assertEqual(report.data["not_using_app"], 3)

    def test_a_relieved_employee_is_not_on_the_chase_list(self):
        self.idle_employee.status = "relieved"
        self.idle_employee.save(update_fields=["status"])

        report = self._report()
        names = [r["employee_name"] for r in report.data["rows"]]
        self.assertNotIn("Perumal", names)
        self.assertEqual(report.data["total"], 2)

    # ------------------------------------------------------------- who may read

    def test_an_engineer_cannot_read_it(self):
        """It says when each person last used their phone."""
        response = self._report(as_user=self.app_user)
        self.assertEqual(response.status_code, 403)

    def test_it_needs_a_login(self):
        response = self.client.get("/api/employees/app_usage/")
        self.assertIn(response.status_code, (401, 403))
