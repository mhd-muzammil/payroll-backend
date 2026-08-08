"""Tests for the OpenCall -> Payroll case sync.

The scenario that matters in production: OpenCall re-syncs TODAY's "Assigned"
set every few minutes. A service call stays assigned for several days until it
is closed, so the same ticket is re-sent day after day. The engineer must keep
seeing it every one of those days, and their own field progress must survive
the next sync tick.
"""
from datetime import timedelta

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.utils import timezone
from rest_framework.test import APIClient

from employees.models import Employee

from .models import Case, EngineerAlias

User = get_user_model()


class CaseSyncTests(TestCase):
    def setUp(self):
        self.bot = User.objects.create_user(
            username="opencall-bot", password="x", role="admin", is_staff=True
        )
        self.engineer_user = User.objects.create_user(
            username="praveen", password="x", role="employee"
        )
        self.engineer = Employee.objects.create(
            user=self.engineer_user,
            employee_name="Praveen S",
            branch="Chennai",
            salary=0,
        )

        self.bot_client = APIClient()
        self.bot_client.force_authenticate(self.bot)
        self.engineer_client = APIClient()
        self.engineer_client.force_authenticate(self.engineer_user)

    def _sync(self, tickets, **extra):
        """One OpenCall auto-sync tick carrying the current Assigned set."""
        body = {
            "cases": [
                {
                    "external_ref": ticket,
                    "title": f"Service call ({ticket})",
                    "engineer_name": "Praveen",
                    "status": "assigned",
                }
                for ticket in tickets
            ]
        }
        body.update(extra)
        res = self.bot_client.post("/api/cases/bulk_dispatch/", body, format="json")
        self.assertEqual(res.status_code, 200, res.data)
        return res.data

    def _engineer_cases(self):
        res = self.engineer_client.get("/api/cases/")
        self.assertEqual(res.status_code, 200, res.data)
        data = res.data
        return data["results"] if isinstance(data, dict) and "results" in data else data

    def _age_by_days(self, days):
        """Pretend every existing case was dispatched `days` days ago."""
        for case in Case.objects.all():
            case.assigned_at = timezone.now() - timedelta(days=days)
            case.save(update_fields=["assigned_at"])

    # -- the day-2 regression -------------------------------------------------

    def test_engineer_sees_case_still_assigned_from_an_earlier_day(self):
        """A ticket first synced yesterday and STILL in today's Assigned set
        must show in the engineer's list today."""
        self._sync(["TKT-1"])
        self.assertEqual(len(self._engineer_cases()), 1, "day 1 should show the case")

        # Next day: the call is not closed yet, so OpenCall sends it again.
        self._age_by_days(1)
        self._sync(["TKT-1"])

        cases = self._engineer_cases()
        self.assertEqual(len(cases), 1, "day 2 should still show the case")
        self.assertEqual(cases[0]["external_ref"], "TKT-1")

    def test_engineer_field_progress_survives_the_next_sync_tick(self):
        """Auto-sync runs every 5 min; it must not reset an engineer who has
        already started travelling back to 'assigned'."""
        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")

        res = self.engineer_client.post(f"/api/cases/{case.id}/accept/")
        self.assertEqual(res.status_code, 200, res.data)
        res = self.engineer_client.post(f"/api/cases/{case.id}/start_travel/")
        self.assertEqual(res.status_code, 200, res.data)

        self._sync(["TKT-1"])
        case.refresh_from_db()
        self.assertEqual(case.status, "on_the_way")

    def test_completed_case_is_hidden_from_the_active_list(self):
        self._sync(["TKT-1", "TKT-2"])
        case = Case.objects.get(external_ref="TKT-1")
        case.status = "completed"
        case.save(update_fields=["status"])

        refs = {c["external_ref"] for c in self._engineer_cases()}
        self.assertEqual(refs, {"TKT-2"})

    def test_ticket_dropped_from_the_assigned_set_disappears(self):
        """Mirror mode: a ticket no longer in the Assigned set is cancelled
        (not deleted) so the engineer's list matches the productivity view."""
        self._sync(["TKT-1", "TKT-2"])
        self.assertEqual(len(self._engineer_cases()), 2)

        self._sync(["TKT-2"])
        refs = {c["external_ref"] for c in self._engineer_cases()}
        self.assertEqual(refs, {"TKT-2"})
        self.assertEqual(Case.objects.get(external_ref="TKT-1").status, "cancelled")
        self.assertEqual(Case.objects.count(), 2, "cancelled, never deleted")

    def test_a_cancelled_ticket_comes_back_if_it_is_reassigned(self):
        """Mirror cancels on a sync where the ticket is absent; if OpenCall
        assigns it again the engineer must see it again."""
        self._sync(["TKT-1"])
        self._sync(["TKT-2"])
        self.assertEqual(Case.objects.get(external_ref="TKT-1").status, "cancelled")

        self._sync(["TKT-1", "TKT-2"])
        refs = {c["external_ref"] for c in self._engineer_cases()}
        self.assertEqual(refs, {"TKT-1", "TKT-2"})

    def test_unmatched_engineer_is_skipped_not_orphaned(self):
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-9",
                        "title": "Service call (TKT-9)",
                        "engineer_name": "Nobody At All",
                        "status": "assigned",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["skipped"], 1)
        self.assertFalse(Case.objects.filter(external_ref="TKT-9").exists())

    def test_completed_case_is_not_resurrected_by_the_next_sync(self):
        """The engineer finishes the job before OpenCall's row is closed. The
        next tick still says "assigned" — that must not undo the completion."""
        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        for step in ("accept", "start_travel", "reached", "start_work", "complete"):
            res = self.engineer_client.post(f"/api/cases/{case.id}/{step}/")
            self.assertEqual(res.status_code, 200, f"{step}: {res.data}")

        self._sync(["TKT-1"])
        case.refresh_from_db()
        self.assertEqual(case.status, "completed")
        self.assertIsNotNone(case.completed_at)

    def test_mirror_does_not_cancel_a_completed_case(self):
        """Once the call is closed upstream it leaves the Assigned set. The
        engineer's completed record must survive the mirror sweep."""
        self._sync(["TKT-1", "TKT-2"])
        case = Case.objects.get(external_ref="TKT-1")
        case.status = "completed"
        case.completed_at = timezone.now()
        case.save(update_fields=["status", "completed_at"])

        self._sync(["TKT-2"])
        case.refresh_from_db()
        self.assertEqual(case.status, "completed")

    def test_closed_upstream_still_wins_over_field_status(self):
        """OpenCall reporting the call closed is authoritative even if the
        engineer left the case mid-flight."""
        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        self.engineer_client.post(f"/api/cases/{case.id}/accept/")

        self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-1",
                        "title": "Service call (TKT-1)",
                        "engineer_name": "Praveen",
                        "status": "closed",
                    }
                ]
            },
            format="json",
        )
        case.refresh_from_db()
        self.assertEqual(case.status, "completed")
        self.assertEqual(self._engineer_cases(), [])

    def test_reassigned_ticket_moves_to_the_new_engineer_and_restarts(self):
        other_user = User.objects.create_user(username="samim", password="x", role="employee")
        other = Employee.objects.create(
            user=other_user, employee_name="Samim", branch="Chennai", salary=0
        )
        other_client = APIClient()
        other_client.force_authenticate(other_user)

        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        self.engineer_client.post(f"/api/cases/{case.id}/accept/")
        self.engineer_client.post(f"/api/cases/{case.id}/start_travel/")

        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-1",
                        "title": "Service call (TKT-1)",
                        "engineer_name": "Samim",
                        "status": "assigned",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)

        case.refresh_from_db()
        self.assertEqual(case.assigned_to_id, other.id)
        self.assertEqual(case.status, "assigned", "new engineer starts fresh")
        self.assertEqual(self._engineer_cases(), [], "old engineer loses it")
        res = other_client.get("/api/cases/")
        self.assertEqual(len(res.data["results"] if "results" in res.data else res.data), 1)

    def test_sync_is_idempotent(self):
        self._sync(["TKT-1"])
        self._sync(["TKT-1"])
        self._sync(["TKT-1"])
        self.assertEqual(Case.objects.filter(external_ref="TKT-1").count(), 1)

    # -- diagnosing a short or empty sync ------------------------------------

    def test_skipped_item_names_the_unmatched_engineer(self):
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {"external_ref": "TKT-1", "title": "a", "engineer_name": "Praveen", "status": "assigned"},
                    {"external_ref": "TKT-8", "title": "b", "engineer_name": "Lava", "status": "assigned"},
                    {"external_ref": "TKT-9", "title": "c", "engineer_name": "Lava", "status": "assigned"},
                    {"external_ref": "TKT-7", "title": "d", "engineer_name": "VijayaKumar Egmore", "status": "assigned"},
                ]
            },
            format="json",
        )
        self.assertEqual(res.status_code, 200, res.data)
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(res.data["skipped"], 3)
        # Reported once per PERSON, not once per ticket — this is the onboarding list.
        self.assertEqual(res.data["unmatched_engineers"], ["Lava", "VijayaKumar Egmore"])
        skipped = [d for d in res.data["details"] if d["result"] == "skipped"]
        self.assertTrue(all(d["engineer_name"] for d in skipped))

    def test_login_without_an_employee_record_gets_a_clear_error(self):
        """Not an empty list — an engineer handed an unlinked login must be told."""
        orphan = User.objects.create_user(username="orphan", password="x", role="employee")
        client = APIClient()
        client.force_authenticate(orphan)

        res = client.get("/api/cases/")
        self.assertEqual(res.status_code, 409, res.data)
        self.assertIn("not linked", res.data["detail"].lower())

    def test_name_match_never_lands_on_an_employee_with_no_login(self):
        """A stale duplicate row with no login must not swallow the cases —
        the live row with the login gets them."""
        Employee.objects.create(employee_name="Praveen S", branch="Vellore", salary=0)  # no user

        self._sync(["TKT-1"])
        case = Case.objects.get(external_ref="TKT-1")
        self.assertEqual(case.assigned_to_id, self.engineer.id)
        self.assertEqual(len(self._engineer_cases()), 1)

    def test_only_a_loginless_namesake_is_skipped_not_silently_assigned(self):
        Employee.objects.create(employee_name="Ghost Engineer", branch="Chennai", salary=0)

        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-5", "title": "x", "engineer_name": "Ghost Engineer", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["skipped"], 1)
        self.assertEqual(res.data["unmatched_engineers"], ["Ghost Engineer"])
        self.assertFalse(Case.objects.filter(external_ref="TKT-5").exists())

    # -- explicit alias for names the automatic rules refuse -----------------

    def test_alias_resolves_a_name_with_nothing_in_common(self):
        """OpenCall "Lava" is Payroll "LAVAKUMAR" — no prefix, no email, no phone.
        Only an explicit alias can connect them."""
        lava = Employee.objects.create(
            user=User.objects.create_user(username="lavakumar", password="x", role="employee"),
            employee_name="LAVAKUMAR",
            branch="Chennai",
            salary=0,
        )
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-L", "title": "x", "engineer_name": "Lava", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["skipped"], 1, "no alias yet -> skipped, never guessed")
        self.assertEqual(res.data["unmatched_engineers"], ["Lava"])

        EngineerAlias.objects.create(external_name="Lava", employee=lava)
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-L", "title": "x", "engineer_name": "Lava", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(Case.objects.get(external_ref="TKT-L").assigned_to_id, lava.id)

    def test_alias_picks_the_right_person_among_namesakes(self):
        """Four employees called VIJAYAKUMAR; matching must stay refused until an
        alias says which one, and then land on exactly that one."""
        for branch in ("Chennai", "Vellore", "Salem"):
            Employee.objects.create(
                user=User.objects.create_user(username=f"vk-{branch}", password="x", role="employee"),
                employee_name="VIJAYAKUMAR",
                branch=branch,
                salary=0,
            )
        target = Employee.objects.create(
            user=User.objects.create_user(username="vk-kanchi", password="x", role="employee"),
            employee_name="VIJAYAKUMAR",
            branch="Kanchipuram",
            salary=0,
        )

        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-V", "title": "x", "engineer_name": "Vijayakumar", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["skipped"], 1, "ambiguous -> refuse, never pick one")

        EngineerAlias.objects.create(external_name="Vijayakumar", employee=target, note="Kanchipuram")
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-V", "title": "x", "engineer_name": "Vijayakumar", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(Case.objects.get(external_ref="TKT-V").assigned_to_id, target.id)

    def test_alias_lookup_ignores_case_and_padding(self):
        EngineerAlias.objects.create(external_name="  LaVa  ", employee=self.engineer)
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {"cases": [{"external_ref": "TKT-C", "title": "x", "engineer_name": "lava", "status": "assigned"}]},
            format="json",
        )
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(Case.objects.get(external_ref="TKT-C").assigned_to_id, self.engineer.id)

    def test_alias_beats_an_exact_name_match_on_someone_else(self):
        """An alias is the operator's explicit instruction; it must not be
        overridden by a coincidental exact-name row."""
        Employee.objects.create(
            user=User.objects.create_user(username="other-praveen", password="x", role="employee"),
            employee_name="Praveen",
            branch="Vellore",
            salary=0,
        )
        EngineerAlias.objects.create(external_name="Praveen", employee=self.engineer)

        self._sync(["TKT-1"])
        self.assertEqual(Case.objects.get(external_ref="TKT-1").assigned_to_id, self.engineer.id)

    def test_email_match_onto_a_loginless_row_is_reported_as_unreachable(self):
        """email/phone stay authoritative, but a case nobody can open is flagged."""
        Employee.objects.create(
            employee_name="Sivaraj", branch="Vellore", salary=0, email="sivaraj@example.com"
        )
        res = self.bot_client.post(
            "/api/cases/bulk_dispatch/",
            {
                "cases": [
                    {
                        "external_ref": "TKT-6",
                        "title": "y",
                        "engineer_name": "Sivaraj",
                        "engineer_email": "sivaraj@example.com",
                        "status": "assigned",
                    }
                ]
            },
            format="json",
        )
        self.assertEqual(res.data["assigned"], 1)
        self.assertEqual(res.data["unreachable_engineers"], ["Sivaraj"])
