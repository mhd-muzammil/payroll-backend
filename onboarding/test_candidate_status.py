"""The Hiring Portal's Status / Action dropdown, as the server takes it.

The dropdown's options are written in two places, the model's ACTION_CHOICES
and the portal's list, and the server refuses anything not in the model's. A
status added to the portal alone would show in the dropdown and fail on save.
Nothing here reads the portal's list, so these tests cannot see the two drift
apart; that stays a check by hand whenever a status is added.
"""

from rest_framework.test import APITestCase

from authentication.models import User

from .models import Candidate


class CandidateStatusTests(APITestCase):
    def setUp(self):
        self.user = User.objects.create_user(
            username="hr", password="test-password", role="hr"
        )
        self.client.force_authenticate(self.user)
        self.candidate = Candidate.objects.create(name="Hari", phone_number="8428531800")

    def _set(self, action):
        return self.client.patch(
            f"/api/candidates/{self.candidate.id}/", {"action": action}, format="json"
        )

    def test_interview_scheduled_is_saved(self):
        response = self._set("Interview Scheduled")
        self.assertEqual(response.status_code, 200, response.data)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.action, "Interview Scheduled")

    def test_every_status_the_server_lists_is_saved(self):
        for action, _ in Candidate.ACTION_CHOICES:
            with self.subTest(action=action):
                self.assertEqual(self._set(action).status_code, 200)

    def test_a_status_the_server_does_not_know_is_refused(self):
        response = self._set("Interview Done")
        self.assertEqual(response.status_code, 400)
        self.candidate.refresh_from_db()
        self.assertEqual(self.candidate.action, "In Progress")
