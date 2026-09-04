"""Only the engineer's own phone app may report where an engineer is.

The office opened one engineer's account in a desktop browser, two hundred and
fifty kilometres away, to see what he sees. The page resumed his duty, started a
watcher and posted the OFFICE LAPTOP'S position as his. His day drew a straight
line from Hosur to the coast and read 519 km for a man who never left Hosur --
and kilometres feed allowances, so that is money, not just a wrong map.

The app puts X-Payroll-Client: app on every request and a browser puts nothing,
so that header is the whole test. The app is also stopped from tracking outside
itself in useLiveTracking; this is the half a page cannot talk its way past.
"""
import datetime

from django.utils import timezone
from django.test import TestCase
from rest_framework.test import APIClient

from authentication.models import User
from cases.models import DutySession, LocationPing
from employees.models import Employee

HOSUR = {"latitude": 12.75767, "longitude": 77.81050, "accuracy": 3}
# Where the office laptop was sitting, on the coast.
CUDDALORE = {"latitude": 11.30725, "longitude": 79.55676, "accuracy": 178}


class AppOnlyPingTests(TestCase):
    def setUp(self):
        self.user = User.objects.create_user(username="kausar", password="x")
        self.engineer = Employee.objects.create(
            employee_name="Kausar Basha", role="Service engineer", department="Service",
            branch="Hosur", salary=30000, email="kausar@test.local", user=self.user,
        )
        DutySession.objects.create(engineer=self.engineer)

        self.phone = APIClient()
        self.phone.force_authenticate(self.user)
        self.phone.credentials(HTTP_X_PAYROLL_CLIENT="app")

        # The same account, signed in on somebody's desktop.
        self.browser = APIClient()
        self.browser.force_authenticate(self.user)

    def _fix(self, client, where, key):
        return client.post(
            "/api/tracking/ping/",
            {**where, "client_key": key, "status": "", "after_gap": False},
            format="json",
        )

    def test_the_phone_is_recorded(self):
        response = self._fix(self.phone, HOSUR, "phone-1")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(LocationPing.objects.filter(engineer=self.engineer).count(), 1)

    def test_a_browser_is_refused(self):
        response = self._fix(self.browser, CUDDALORE, "laptop-1")
        self.assertEqual(response.status_code, 403)
        self.assertTrue(response.json().get("app_only"))
        self.assertEqual(
            LocationPing.objects.filter(engineer=self.engineer).count(),
            0,
            "a browser must leave no trace on an engineer's trail",
        )

    def test_a_browser_batch_is_refused(self):
        response = self.browser.post(
            "/api/tracking/ping/batch/",
            {"pings": [{**CUDDALORE, "client_key": "laptop-b1", "timestamp": timezone.now().isoformat()}]},
            format="json",
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(LocationPing.objects.count(), 0)

    def test_the_office_cannot_add_kilometres_to_a_day(self):
        """The whole point, stated as the number the office reads.

        A morning in Hosur, then a laptop two hundred and fifty kilometres away
        joining in. The distance must be the morning's, not the laptop's.
        """
        base = timezone.now() - datetime.timedelta(hours=2)
        for i in range(6):
            LocationPing.objects.create(
                engineer=self.engineer,
                latitude=12.75767 + i * 0.0045,
                longitude=77.81050,
                accuracy=8,
                timestamp=base + datetime.timedelta(minutes=10 * i),
            )
        from cases.views import _trail_km

        honest = _trail_km(list(LocationPing.objects.order_by("timestamp")))
        self.assertGreater(honest, 2)

        refused = self._fix(self.browser, CUDDALORE, "laptop-2")
        self.assertEqual(refused.status_code, 403)

        after = _trail_km(list(LocationPing.objects.order_by("timestamp")))
        self.assertEqual(after, honest, "the laptop must not move the number at all")

    def test_a_header_that_says_something_else_is_still_a_browser(self):
        odd = APIClient()
        odd.force_authenticate(self.user)
        odd.credentials(HTTP_X_PAYROLL_CLIENT="browser")
        self.assertEqual(self._fix(odd, CUDDALORE, "odd-1").status_code, 403)

    def test_the_header_is_read_loosely_enough_to_survive_casing(self):
        shouty = APIClient()
        shouty.force_authenticate(self.user)
        shouty.credentials(HTTP_X_PAYROLL_CLIENT=" APP ")
        self.assertEqual(self._fix(shouty, HOSUR, "app-caps").status_code, 201)

    def test_reading_the_board_from_a_browser_still_works(self):
        """Refusing a POSITION must not refuse a look.

        The office reads this board in a browser all day; only reporting a
        position is the app's business.
        """
        # A real office account: the board is gated on the app's own role, not
        # on Django's is_staff.
        staff_user = User.objects.create_user(
            username="office", password="x", role="superadmin", is_staff=True
        )
        staff = APIClient()
        staff.force_authenticate(staff_user)
        self.assertEqual(staff.get("/api/tracking/live/").status_code, 200)
        self.assertEqual(staff.get("/api/tracking/roster/").status_code, 200)

    def test_the_engineers_own_duty_state_still_reads_from_a_browser(self):
        """An engineer looking at their own page in a browser can still see it.

        They cannot report a position from there, but nothing about reading is
        the app's business either.
        """
        self.assertEqual(self.browser.get("/api/tracking/duty/").status_code, 200)
