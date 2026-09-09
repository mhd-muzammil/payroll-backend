"""A row the office marks has to belong to the person it is about.

Mark Attendance and Add Record post a name. `employee` was read-only on the
serializer, so the link was never written: the row was saved belonging to
nobody, came back with no employee id, no email and a branch of "Chennai"
whoever the person was -- and the Attendance page, which keys a person by that
id, listed the same employee twice. One card for the days somebody punched,
another for the day the office marked. The same hole put a Hosur absence in
Chennai's count.

These tests pin the link: by the id the page sends, by the name when it does
not, never between two people with one name, and never letting an employee claim
somebody else's row.
"""

from io import StringIO

from django.core.management import call_command
from django.utils import timezone
from rest_framework.test import APIClient, APITestCase

from attendance.models import Attendance
from authentication.models import User
from employees.models import Employee


def _day():
    return timezone.localdate().isoformat()


class OfficeMarkedRowBelongsToSomebodyTests(APITestCase):
    def setUp(self):
        self.admin = User.objects.create_user(
            username="office-link", password="x", role="superadmin", is_superuser=True
        )
        self.hosur = Employee.objects.create(
            employee_name="Vijayananth M", role="Service engineer",
            department="General", branch="Hosur", salary=27208,
            email="mvijayananth@example.com",
        )
        self.client = APIClient()
        self.client.force_authenticate(self.admin)

    def _mark(self, **extra):
        payload = {
            "employee_name": "Vijayananth M",
            "role": "Service engineer",
            "department": "General",
            "salary": "27208.00",
            "status": "Absent",
            "intime": f"{_day()}T00:00:00",
        }
        payload.update(extra)
        return self.client.post("/api/attendance/", payload, format="json")

    def test_the_id_the_page_sends_is_used(self):
        response = self._mark(employee=self.hosur.id)
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Attendance.objects.get().employee_id, self.hosur.id)

    def test_without_an_id_the_name_finds_them(self):
        """Add Record has only a typed name -- that still has to land on them."""
        response = self._mark()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Attendance.objects.get().employee_id, self.hosur.id)

    def test_the_row_comes_back_with_their_branch_and_email_not_a_default(self):
        """The split card, and the miscounted region, in one assertion.

        An unlinked row reads back as employee_id null, email null and branch
        "Chennai" -- which is what made a second card for the same person and
        added their absence to Chennai's count.
        """
        self._mark(employee=self.hosur.id)
        listed = self.client.get(f"/api/attendance/?start_date={_day()}&end_date={_day()}").json()
        rows = listed if isinstance(listed, list) else listed.get("results", [])
        row = next(r for r in rows if r["employee_name"] == "Vijayananth M")
        self.assertEqual(row["employee_id"], self.hosur.id)
        self.assertEqual(row["branch"], "Hosur")
        self.assertEqual(row["email"], "mvijayananth@example.com")

    def test_two_people_with_one_name_are_left_alone(self):
        """A guess here would move an absence onto a colleague's record."""
        Employee.objects.create(
            employee_name="Vijayananth M", role="Service engineer",
            department="General", branch="Chennai", salary=25000,
        )
        response = self._mark()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(Attendance.objects.get().employee_id)

    def test_a_relieved_namesake_does_not_make_it_ambiguous(self):
        Employee.objects.create(
            employee_name="Vijayananth M", role="Service engineer",
            department="General", branch="Chennai", salary=25000, status="relieved",
        )
        response = self._mark()
        self.assertEqual(response.status_code, 201, response.content)
        self.assertEqual(Attendance.objects.get().employee_id, self.hosur.id)

    def test_a_name_nobody_has_is_still_saved(self):
        """Marking attendance must not be blocked by an unknown name."""
        response = self._mark(employee_name="Somebody Not On The List")
        self.assertEqual(response.status_code, 201, response.content)
        self.assertIsNone(Attendance.objects.get().employee_id)

    def test_an_employee_cannot_post_a_row_at_all_let_alone_somebody_elses(self):
        """Making `employee` writable must not open a door for an engineer.

        The office needs to say who a row is about; an employee does not get
        to write raw rows at all -- back-dating and flipping Absent to Present
        is exactly what the view refuses. This pins that the new field did not
        change that answer.
        """
        colleague = Employee.objects.create(
            employee_name="Karthik R", role="Service engineer", department="General",
            branch="Chennai", salary=26000, email="karthik@example.com",
        )
        user = User.objects.create_user(username="Karthik R", password="x", role="employee")
        colleague.user = user
        colleague.save(update_fields=["user"])

        client = APIClient()
        client.force_authenticate(user)
        response = client.post(
            "/api/attendance/",
            {
                "employee": self.hosur.id,
                "employee_name": "Vijayananth M",
                "role": "Service engineer",
                "department": "General",
                "salary": "27208.00",
                "status": "Present",
                "intime": f"{_day()}T09:15:00",
            },
            format="json",
        )
        self.assertEqual(response.status_code, 403, response.content)
        self.assertEqual(Attendance.objects.count(), 0)

    def test_editing_an_unlinked_row_gives_it_back_its_owner(self):
        orphan = Attendance.objects.create(
            employee=None, employee_name="Vijayananth M", role="Service engineer",
            department="General", salary=27208, status="Absent",
            intime=timezone.now().replace(hour=0, minute=0, second=0, microsecond=0),
        )
        response = self.client.patch(
            f"/api/attendance/{orphan.id}/", {"status": "Leave"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        orphan.refresh_from_db()
        self.assertEqual(orphan.employee_id, self.hosur.id)

    def test_editing_a_linked_row_never_moves_it_to_another_person(self):
        """A corrected name must not carry the day onto somebody else."""
        other = Employee.objects.create(
            employee_name="Karthik R", role="Service engineer", department="General",
            branch="Chennai", salary=26000,
        )
        row = Attendance.objects.create(
            employee=self.hosur, employee_name="Vijayananth M", role="Service engineer",
            department="General", salary=27208, status="Present",
            intime=timezone.now(),
        )
        response = self.client.patch(
            f"/api/attendance/{row.id}/", {"employee_name": "Karthik R"}, format="json"
        )
        self.assertEqual(response.status_code, 200, response.content)
        row.refresh_from_db()
        self.assertEqual(row.employee_id, self.hosur.id)
        self.assertNotEqual(row.employee_id, other.id)


class LinkAttendanceRowsCommandTests(APITestCase):
    """The rows already saved with nobody attached."""

    def setUp(self):
        self.hosur = Employee.objects.create(
            employee_name="Vijayananth M", role="Service engineer",
            department="General", branch="Hosur", salary=27208,
        )
        midnight = timezone.now().replace(hour=0, minute=0, second=0, microsecond=0)
        self.orphan = Attendance.objects.create(
            employee=None, employee_name="Vijayananth M", role="Service engineer",
            department="General", salary=27208, status="Absent", intime=midnight,
        )
        self.namesakes = Attendance.objects.create(
            employee=None, employee_name="Two Of These", role="Staff",
            department="General", salary=1, status="Absent", intime=midnight,
        )
        for branch in ("Chennai", "Salem"):
            Employee.objects.create(
                employee_name="Two Of These", role="Staff", department="General",
                branch=branch, salary=1,
            )

    def _run(self, *args):
        out = StringIO()
        call_command("link_attendance_rows", *args, stdout=out)
        return out.getvalue()

    def test_a_dry_run_writes_nothing(self):
        output = self._run()
        self.assertIn("Dry run", output)
        self.orphan.refresh_from_db()
        self.assertIsNone(self.orphan.employee_id)

    def test_applying_links_the_unambiguous_rows_only(self):
        output = self._run("--apply")
        self.orphan.refresh_from_db()
        self.namesakes.refresh_from_db()
        self.assertEqual(self.orphan.employee_id, self.hosur.id)
        self.assertIsNone(
            self.namesakes.employee_id,
            "two employees share that name -- the row must be left alone",
        )
        self.assertIn("Linked 1 row", output)

    def test_it_can_be_pointed_at_one_name(self):
        self._run("--name", "Two Of These", "--apply")
        self.orphan.refresh_from_db()
        self.assertIsNone(self.orphan.employee_id, "a different name must not be touched")
