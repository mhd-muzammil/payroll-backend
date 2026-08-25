"""Tests for the hiring-sheet importer.

The headings here are copied from the four real sheets HR gave us — a college
placement list, a Facebook lead export, a WorkIndia download and an old resume
tracker — because the whole point of the importer is that it does not care which
of them it is given.
"""

import csv
import io

from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import TestCase
from django.urls import reverse
from rest_framework.test import APITestCase

from authentication.models import User

from .candidate_import import (
    build_candidates,
    detect_header,
    map_action,
    normalise_phone,
    parse_experience,
    parse_money,
)
from .models import Candidate


def csv_bytes(rows):
    buffer = io.StringIO()
    csv.writer(buffer, lineterminator="\n").writerows(rows)
    return buffer.getvalue().encode("utf-8")


class PhoneTests(TestCase):
    def test_strips_the_country_code_the_lead_export_adds(self):
        self.assertEqual(normalise_phone("+918939563897"), ("8939563897", []))
        self.assertEqual(normalise_phone("918939563897"), ("8939563897", []))

    def test_strips_a_leading_zero(self):
        self.assertEqual(normalise_phone("08939563897"), ("8939563897", []))

    def test_one_person_with_two_numbers_keeps_both(self):
        # "9884769336 / 8838079358" is one row in the resume tracker. It must not
        # become two candidates, and must not become one 20-digit string.
        first, extra = normalise_phone("9884769336 / 8838079358")
        self.assertEqual(first, "9884769336")
        self.assertEqual(extra, ["8838079358"])

    def test_reads_a_number_typed_with_a_space(self):
        self.assertEqual(normalise_phone("98847 69336"), ("9884769336", []))

    def test_refuses_a_number_that_is_not_a_mobile(self):
        # Real junk from the paid leads: 11 digits beginning with 1.
        self.assertEqual(normalise_phone("19148485678"), ("", []))
        self.assertEqual(normalise_phone("12345"), ("", []))
        self.assertEqual(normalise_phone(""), ("", []))
        self.assertEqual(normalise_phone(None), ("", []))

    def test_a_landline_is_not_accepted_as_a_mobile(self):
        self.assertEqual(normalise_phone("04428234567"), ("", []))


class ExperienceAndMoneyTests(TestCase):
    def test_a_plain_number_of_years(self):
        self.assertEqual(parse_experience("3"), (3.0, ""))

    def test_fresher_is_zero_and_says_nothing_extra(self):
        self.assertEqual(parse_experience("Fresher"), (0.0, ""))

    def test_words_where_a_number_was_expected_keep_their_wording(self):
        # "Experienced" must never be filed silently as no experience.
        years, note = parse_experience("Experienced")
        self.assertEqual(years, 0.0)
        self.assertEqual(note, "Experienced")

        years, note = parse_experience("Fresher in telecalling")
        self.assertEqual(years, 0.0)
        self.assertEqual(note, "Fresher in telecalling")

    def test_a_salary_written_in_thousands_is_flagged_not_multiplied(self):
        # The resume tracker writes 27 for 27,000. Guessing is worse than saying.
        self.assertEqual(parse_money("27"), (27.0, True))
        self.assertEqual(parse_money("20000"), (20000.0, False))

    def test_placeholders_are_not_money(self):
        self.assertEqual(parse_money("-"), (0.0, False))
        self.assertEqual(parse_money(""), (0.0, False))
        self.assertEqual(parse_money("Rs. 16,000"), (16000.0, False))


class ActionTests(TestCase):
    def test_an_unambiguous_status_maps(self):
        self.assertEqual(map_action("NOT INTRESTED")[0], "Decline")
        self.assertEqual(map_action("NOT TAKEN THE CALL")[0], "RNR")
        self.assertEqual(map_action("Rejected")[0], "Rejected")

    def test_a_status_that_says_nothing_about_the_stage_is_kept_as_a_note(self):
        # "NEW" and "MONDAY" are not hiring stages; inventing one would put a
        # candidate in a step nobody has actually worked them to.
        action, note = map_action("NEW")
        self.assertEqual(action, "In Progress")
        self.assertEqual(note, "NEW")


class HeaderDetectionTests(TestCase):
    def test_finds_headings_that_are_not_on_the_first_row(self):
        rows = [
            ["Lead Summary", "", ""],
            ["", "", ""],
            ["Full Name", "Phone (10-digit)", "City"],
            ["Aswin", "8939563897", "Coimbatore"],
        ]
        index, mapping, _ = detect_header(rows)
        self.assertEqual(index, 2)
        self.assertEqual(mapping[0], "name")
        self.assertEqual(mapping[1], "phone_number")

    def test_the_first_column_to_claim_a_field_keeps_it(self):
        # WorkIndia has City AND Location; both look like an address. Whichever
        # comes first wins, so the result is not decided by column-order luck,
        # and the loser still reaches remarks.
        rows = [
            ["Full Name", "Mobile No.", "City", "Location"],
            ["A", "9000000000", "chennai", "Arumbakkam"],
        ]
        _, mapping, unknown = detect_header(rows)
        self.assertEqual(mapping[2], "present_address")
        self.assertNotIn(3, mapping)
        self.assertIn("Location", unknown)


class BuildCandidatesTests(TestCase):
    def test_a_college_placement_list(self):
        data = csv_bytes([
            ["S.NO", "NAME", "PH", "DEPT", "YEAR", "Location", "Respond"],
            ["5", "gautham A", "8056979461", "Cyber Security", "4th", "Tiruppattur", "YES"],
        ])
        report = build_candidates(data, "PRINCE CLG.csv", "Prince College")
        self.assertEqual(len(report.candidates), 1)
        candidate = report.candidates[0]
        self.assertEqual(candidate["name"], "gautham A")
        self.assertEqual(candidate["phone_number"], "8056979461")
        self.assertEqual(candidate["qualification"], "Cyber Security")
        self.assertEqual(candidate["present_address"], "Tiruppattur")
        self.assertEqual(candidate["source"], "Prince College")
        # The year is not a model field, so it is kept where it can still be read.
        self.assertIn("YEAR: 4th", candidate["remarks"])
        # A row number is noise, and is not.
        self.assertNotIn("S.NO", candidate["remarks"])

    def test_a_lead_export_keeps_the_email_the_portal_now_has_room_for(self):
        data = csv_bytes([
            ["Sl No", "Full Name", "Phone (10-digit)", "Email", "City", "Qualification", "Status"],
            ["1", "Aswin", "8939563897", "ashanthi503@gmail.com", "Coimbatore", "Degree", "NEW"],
        ])
        report = build_candidates(data, "leads.csv", "FB Leads")
        candidate = report.candidates[0]
        self.assertEqual(candidate["email"], "ashanthi503@gmail.com")
        self.assertEqual(candidate["action"], "In Progress")
        self.assertIn("Status in sheet: NEW", candidate["remarks"])

    def test_a_row_with_no_usable_number_is_reported_not_dropped_quietly(self):
        data = csv_bytes([
            ["Full Name", "Phone (10-digit)"],
            ["Elavarasan", "19148485678"],
            ["Good One", "9876543210"],
        ])
        report = build_candidates(data, "leads.csv", "FB Leads")
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(len(report.rejected), 1)
        self.assertEqual(report.rejected[0]["name"], "Elavarasan")
        self.assertIn("mobile", report.rejected[0]["reason"])

    def test_a_row_with_no_name_is_reported(self):
        data = csv_bytes([["NAME", "PH"], ["", "9876543210"]])
        report = build_candidates(data, "x.csv", "X")
        self.assertEqual(report.candidates, [])
        self.assertEqual(report.rejected[0]["reason"], "no name")

    def test_blank_spacer_rows_are_simply_ignored(self):
        data = csv_bytes([["NAME", "PH"], ["", ""], ["A", "9876543210"], ["", ""]])
        report = build_candidates(data, "x.csv", "X")
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(report.rejected, [])

    def test_the_same_person_twice_in_one_file_is_imported_once(self):
        data = csv_bytes([
            ["NAME", "PH"],
            ["ASIRVATHAM", "9962546288"],
            ["ASIRVATHAM", "9962546288"],
        ])
        report = build_candidates(data, "x.csv", "X")
        self.assertEqual(len(report.candidates), 1)
        self.assertEqual(len(report.duplicates_in_file), 1)

    def test_nothing_in_the_sheet_is_thrown_away(self):
        data = csv_bytes([
            ["NAME", "PH", "Gender", "Resume Link"],
            ["A", "9876543210", "Male", "https://example.com/cv.pdf"],
        ])
        report = build_candidates(data, "x.csv", "X")
        remarks = report.candidates[0]["remarks"]
        self.assertIn("Gender: Male", remarks)
        self.assertIn("https://example.com/cv.pdf", remarks)
        self.assertIn("Gender", report.as_dict()["unmapped_columns"])

    def test_a_sheet_that_is_not_a_candidate_list_is_skipped_with_a_reason(self):
        data = csv_bytes([["Row Labels", "Count of full_name"], ["chennai", "280"]])
        report = build_candidates(data, "pivot.csv", "X")
        self.assertEqual(report.candidates, [])
        self.assertEqual(len(report.sheets_skipped), 1)
        self.assertIn("no name/phone", report.sheets_skipped[0]["reason"])

    def test_an_invalid_email_does_not_block_the_candidate(self):
        data = csv_bytes([["NAME", "PH", "Email"], ["A", "9876543210", "not-an-email"]])
        report = build_candidates(data, "x.csv", "X")
        candidate = report.candidates[0]
        self.assertIsNone(candidate["email"])
        self.assertIn("not-an-email", candidate["remarks"])

    def test_an_over_long_cell_is_trimmed_to_what_the_column_holds(self):
        data = csv_bytes([["NAME", "PH"], ["N" * 400, "9876543210"]])
        report = build_candidates(data, "x.csv", "X")
        self.assertEqual(len(report.candidates[0]["name"]), 255)


class ImportEndpointTests(APITestCase):
    ROWS = [["NAME", "PH", "Email"], ["A One", "9876543210", "a@example.com"]]

    def setUp(self):
        self.user = User.objects.create_user(
            username="hr", password="test-password", role="hr"
        )
        self.client.force_authenticate(self.user)
        self.url = reverse("candidate-import-file")

    def _upload(self, rows, **extra):
        payload = {"file": SimpleUploadedFile("sheet.csv", csv_bytes(rows), "text/csv")}
        payload.update(extra)
        return self.client.post(self.url, payload, format="multipart")

    def test_a_preview_writes_nothing(self):
        response = self._upload(self.ROWS)
        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.data["committed"])
        self.assertEqual(response.data["new"], 1)
        self.assertEqual(Candidate.objects.count(), 0)

    def test_committing_creates_the_candidates(self):
        response = self._upload(self.ROWS, commit="true")
        self.assertEqual(response.status_code, 201)
        self.assertEqual(response.data["created"], 1)
        candidate = Candidate.objects.get()
        self.assertEqual(candidate.name, "A One")
        self.assertEqual(candidate.phone_number, "9876543210")
        self.assertEqual(candidate.email, "a@example.com")
        # Defaults to the file name, which is how HR labels these already.
        self.assertEqual(candidate.source, "sheet")

    def test_importing_the_same_sheet_twice_does_not_duplicate_anyone(self):
        self._upload(self.ROWS, commit="true")
        response = self._upload(self.ROWS, commit="true")
        self.assertEqual(response.data["created"], 0)
        self.assertEqual(response.data["already_in_portal"], 1)
        self.assertEqual(Candidate.objects.count(), 1)

    def test_the_source_can_be_named(self):
        self._upload(self.ROWS, commit="true", source="Prince College")
        self.assertEqual(Candidate.objects.get().source, "Prince College")

    def test_a_file_of_the_wrong_kind_is_refused(self):
        response = self.client.post(
            self.url,
            {"file": SimpleUploadedFile("cv.pdf", b"%PDF-1.4", "application/pdf")},
            format="multipart",
        )
        self.assertEqual(response.status_code, 400)

    def test_no_file_is_refused(self):
        response = self.client.post(self.url, {}, format="multipart")
        self.assertEqual(response.status_code, 400)

    def test_a_non_hr_login_cannot_import(self):
        self.client.force_authenticate(
            User.objects.create_user(username="eng", password="x", role="employee")
        )
        self.assertIn(self._upload(self.ROWS).status_code, (401, 403))
