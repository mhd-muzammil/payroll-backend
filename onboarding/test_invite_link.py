"""The shared onboarding link, and everything it must not let through.

This is the only unauthenticated endpoint in the system that writes a row, and
the rows it writes hold Aadhaar numbers and bank accounts. Most of what is
below is therefore about what the link refuses: a form that tries to file
itself as an employee through a freelancer's link, one that tries to arrive
already approved and working here, a token that has been replaced, and
anybody hoping to read a record back out of it.
"""
from django.test import override_settings
from rest_framework.test import APITestCase

from authentication.models import User

from .models import Onboarding, OnboardingInvite

_PHONE = [9700000000]


def _phone():
    _PHONE[0] += 1
    return str(_PHONE[0])


# The throttle counts in a cache shared across the process; these tests post
# more than a person ever would, so it is lifted for all but its own test.
NO_THROTTLE = {"DEFAULT_THROTTLE_RATES": {"onboarding_link": None}}


@override_settings(REST_FRAMEWORK={**NO_THROTTLE})
class PublicOnboardingLinkTests(APITestCase):
    def setUp(self):
        self.invite = OnboardingInvite.objects.create(category="Freelancer")
        self.url = f"/api/onboard/{self.invite.token}/"

    def _form(self, **extra):
        body = {
            "employee_name": "Walk In",
            "email_id": "walkin%s@example.com" % _PHONE[0],
            "mobile_number": _phone(),
            "work_location": "Chennai",
        }
        body.update(extra)
        return body

    # ------------------------------------------------------------ what it does

    def test_it_says_which_form_to_draw(self):
        response = self.client.get(self.url)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.data, {"category": "Freelancer"})

    def test_somebody_can_fill_their_own_form_without_logging_in(self):
        response = self.client.post(self.url, self._form(), format="multipart")
        self.assertEqual(response.status_code, 201, response.data)
        row = Onboarding.objects.get(employee_name="Walk In")
        self.assertEqual(row.category, "Freelancer")
        self.assertEqual(row.source, "Self")

    def test_what_arrives_is_waiting_to_be_looked_at(self):
        self.client.post(self.url, self._form(), format="multipart")
        row = Onboarding.objects.get(employee_name="Walk In")
        self.assertEqual(row.status, "Pending Review")
        self.assertEqual(
            row.employment_status, "Inactive",
            "nobody becomes staff by filling in a form nobody has read",
        )

    # ------------------------------------------------------- what it refuses

    def test_the_link_decides_the_category_not_the_form(self):
        response = self.client.post(
            self.url, self._form(category="Employee"), format="multipart"
        )
        self.assertEqual(response.status_code, 201, response.data)
        self.assertEqual(
            Onboarding.objects.get(employee_name="Walk In").category, "Freelancer",
            "a freelancer's link cannot file an employee",
        )

    def test_a_form_cannot_approve_itself(self):
        response = self.client.post(
            self.url,
            self._form(employment_status="Active", status="Completed", source="Office"),
            format="multipart",
        )
        self.assertEqual(response.status_code, 201, response.data)
        row = Onboarding.objects.get(employee_name="Walk In")
        self.assertEqual(row.employment_status, "Inactive")
        self.assertEqual(row.status, "Pending Review")
        self.assertEqual(row.source, "Self")

    def test_a_token_that_was_never_real_is_a_flat_404(self):
        self.assertEqual(self.client.get("/api/onboard/not-a-token/").status_code, 404)
        self.assertEqual(
            self.client.post("/api/onboard/not-a-token/", self._form(), format="multipart").status_code,
            404,
        )
        self.assertFalse(Onboarding.objects.exists())

    def test_a_replaced_token_stops_working(self):
        old = self.invite.token
        self.invite.token = "replaced-token-value"
        self.invite.save(update_fields=["token"])
        self.assertEqual(self.client.get(f"/api/onboard/{old}/").status_code, 404)

    def test_the_public_end_reads_nothing_back(self):
        self.client.post(self.url, self._form(), format="multipart")
        response = self.client.post(self.url, self._form(employee_name="Second"), format="multipart")
        self.assertEqual(response.data, {"submitted": True}, "no record comes back out")
        # And the authenticated list is still shut.
        self.assertIn(self.client.get("/api/onboarding/").status_code, (401, 403))

    def test_a_vendor_link_still_needs_the_name_of_the_firm(self):
        vendor_invite = OnboardingInvite.objects.create(category="Vendor")
        response = self.client.post(
            f"/api/onboard/{vendor_invite.token}/", self._form(), format="multipart"
        )
        self.assertEqual(response.status_code, 400)
        self.assertIn("company_name", response.data)


@override_settings(REST_FRAMEWORK={**NO_THROTTLE})
class OnboardingLinkOfficeTests(APITestCase):
    def setUp(self):
        self.hr = User.objects.create_user(username="hr_link", password="x", role="hr")
        self.client.force_authenticate(self.hr)

    def test_the_office_gets_one_link_per_kind(self):
        response = self.client.get("/api/onboarding-links/")
        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            sorted(row["category"] for row in response.data),
            ["Employee", "Freelancer", "Vendor"],
        )
        self.assertTrue(all(row["token"] for row in response.data))

    def test_asking_twice_gives_the_same_link(self):
        first = self.client.get("/api/onboarding-links/").data
        second = self.client.get("/api/onboarding-links/").data
        self.assertEqual(
            {r["category"]: r["token"] for r in first},
            {r["category"]: r["token"] for r in second},
            "a link handed out yesterday has to still work today",
        )

    def test_replacing_a_link_breaks_the_old_one_and_leaves_the_others(self):
        before = {r["category"]: r["token"] for r in self.client.get("/api/onboarding-links/").data}
        response = self.client.post("/api/onboarding-links/Freelancer/rotate/")
        self.assertEqual(response.status_code, 200, response.data)
        after = {r["category"]: r["token"] for r in self.client.get("/api/onboarding-links/").data}
        self.assertNotEqual(before["Freelancer"], after["Freelancer"])
        self.assertEqual(before["Employee"], after["Employee"])
        self.assertEqual(before["Vendor"], after["Vendor"])
        self.assertEqual(self.client.get(f"/api/onboard/{before['Freelancer']}/").status_code, 404)

    def test_a_category_nobody_has_heard_of_is_a_404(self):
        self.assertEqual(self.client.post("/api/onboarding-links/Robot/rotate/").status_code, 404)

    def test_the_links_are_not_public(self):
        self.client.force_authenticate(None)
        self.assertIn(self.client.get("/api/onboarding-links/").status_code, (401, 403))

    def test_an_employee_cannot_see_the_links(self):
        worker = User.objects.create_user(username="worker_link", password="x", role="employee")
        self.client.force_authenticate(worker)
        self.assertEqual(self.client.get("/api/onboarding-links/").status_code, 403)


class PublicOnboardingThrottleTests(APITestCase):
    """The open door is bolted: it writes rows and takes uploads.

    The rate is patched on the throttle class rather than through settings.
    DRF reads DEFAULT_THROTTLE_RATES once, when the module is imported, so an
    override_settings here would be read by nothing -- and a throttle test that
    silently tests nothing is worse than no test at all.
    """

    def test_it_stops_answering_after_the_day_s_allowance(self):
        from unittest.mock import patch

        from django.core.cache import cache
        from rest_framework.throttling import SimpleRateThrottle

        invite = OnboardingInvite.objects.create(category="Employee")
        url = f"/api/onboard/{invite.token}/"
        cache.clear()
        with patch.dict(SimpleRateThrottle.THROTTLE_RATES, {"onboarding_link": "3/day"}):
            codes = [self.client.get(url).status_code for _ in range(4)]
        cache.clear()
        self.assertEqual(codes[:3], [200, 200, 200])
        self.assertEqual(codes[3], 429, "the fourth in a day is refused")

    def test_the_rate_is_actually_configured(self):
        """A scope with no rate against it throttles nothing at all."""
        from rest_framework.settings import api_settings

        self.assertIn("onboarding_link", api_settings.DEFAULT_THROTTLE_RATES)
        self.assertTrue(api_settings.DEFAULT_THROTTLE_RATES["onboarding_link"])
