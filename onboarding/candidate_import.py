"""Read a spreadsheet of candidates and turn it into Candidate rows.

Every hiring sheet HR has arrives in a different shape — a college placement
list, a Facebook lead export, a WorkIndia download, an old resume tracker — and
they will keep arriving in new ones. So nothing here is keyed to a fixed layout:
columns are recognised by their heading, and any heading we do not recognise is
carried into remarks rather than dropped. A sheet that changes shape next month
still imports; it just puts more into remarks.

The parsing is deliberately separate from the view so it can be tested against
real headers without HTTP, a database, or a file upload.
"""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

# Which heading means which field. Compared after lowercasing and stripping
# everything that is not a letter or a digit, so "Phone (10-digit)", "PH." and
# "phone_number" all land on the same key.
COLUMN_ALIASES: dict[str, tuple[str, ...]] = {
    "name": (
        "name", "fullname", "candidatename", "employeename", "applicantname",
        "studentname", "person",
    ),
    "phone_number": (
        "ph", "phone", "phoneno", "phonenumber", "phone10digit", "mobile",
        "mobileno", "mobilenumber", "contact", "contactno", "contactnumber",
        "whatsapp", "whatsappno", "rawdigitshelper",
    ),
    "email": ("email", "emailid", "mail", "mailid"),
    "qualification": (
        "qualification", "dept", "department", "course", "degree", "education",
        "whatisyourcurrentqualification", "specialization", "branch",
    ),
    "present_address": (
        "location", "city", "preferredlocation", "presentaddress", "area",
        "whichworklocationdoyouprefer", "joininglocation", "currentaddress",
        "currentaddresslocation",
    ),
    "permanent_address": ("permanentaddress", "nativeplace", "homeaddress"),
    "years_of_experience": (
        "exp", "experience", "yearsofexperience", "levelofexperience",
        "relevantexperience", "totalexperience",
    ),
    "previous_company": (
        "work", "company", "previouscompany", "previouscompanyname",
        "lastworkingcompany", "currentcompany",
    ),
    "last_salary": ("lastsalary", "currentsalary", "salary", "presentsalary"),
    "expecting_salary": (
        "salaryexp", "expectingsalary", "expectedsalary", "salaryexpectation",
        "expectation",
    ),
    "remarks": (
        "remark", "remarks", "reason", "note", "notes", "reviewnotes", "comment",
        "comments", "afterinterview",
    ),
    "action": ("action", "status", "candidatestatus", "currentstatus", "respond"),
}

# Raw status text -> the portal's own action. Only the unambiguous ones: a lead
# marked "NEW" or "MONDAY" says nothing about where the conversation stands, and
# guessing would put candidates in a stage nobody has actually worked them to.
# Everything unmatched stays "In Progress" with the original wording kept in
# remarks, so no judgement is invented and none is lost either.
ACTION_HINTS: tuple[tuple[str, str], ...] = (
    ("not interested", "Decline"),
    ("notintrested", "Decline"),
    ("not intrested", "Decline"),
    ("declin", "Decline"),
    ("reject", "Rejected"),
    ("not taken the call", "RNR"),
    ("rnr", "RNR"),
    ("no response", "RNR"),
    ("not reachable", "RNR"),
    ("switched off", "RNR"),
    ("offer shared", "Offer Shared"),
    ("offer released", "Offer Shared"),
    ("salary discussion", "Salary Discussion"),
    ("waiting for acceptance", "Waiting For Acceptance"),
    ("waiting for joining", "Waiting For Joining Date"),
)

# Bookkeeping columns. They are neither mapped nor worth keeping: a row number
# and a spreadsheet's own validation helpers tell HR nothing, and in remarks they
# push the actual notes out of view.
NOISE_HEADINGS = frozenset({
    "sno", "slno", "srno", "serialno", "sl", "sr", "no", "id",
    "phonecheck", "emailcheck", "duplicateflag", "rawdigitshelper",
})

DEFAULT_ACTION = "In Progress"
DEFAULT_SEGMENT = "Combo"

# Model limits, so a long spreadsheet cell is trimmed here rather than raising a
# DataError halfway through an import.
MAX_LENGTHS = {
    "name": 255,
    "phone_number": 20,
    "email": 255,
    "source": 120,
    "qualification": 100,
    "permanent_address": 255,
    "present_address": 255,
    "previous_company": 255,
}

# A salary this small is almost certainly written in thousands ("27" for 27,000).
# We store what the sheet said and count these instead of multiplying: a guessed
# salary is worse than a flagged one.
SUSPICIOUS_SALARY_BELOW = 1000


def _key(heading) -> str:
    return re.sub(r"[^a-z0-9]", "", str(heading or "").lower())


def _clean(value) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    # openpyxl hands back floats for whole numbers typed as numbers.
    if text.endswith(".0") and text[:-2].isdigit():
        text = text[:-2]
    return "" if text.lower() in {"nan", "none", "-", "--", "n/a", "na"} else text


@dataclass
class RowResult:
    row_number: int
    sheet: str
    candidate: dict | None = None
    reason: str = ""


@dataclass
class ImportReport:
    """What an import did, in the terms HR needs to see before committing it."""

    sheets_read: list[str] = field(default_factory=list)
    sheets_skipped: list[dict] = field(default_factory=list)
    candidates: list[dict] = field(default_factory=list)
    rejected: list[dict] = field(default_factory=list)
    duplicates_in_file: list[dict] = field(default_factory=list)
    unmapped_columns: list[str] = field(default_factory=list)
    suspicious_salaries: int = 0

    def as_dict(self) -> dict:
        return {
            "sheets_read": self.sheets_read,
            "sheets_skipped": self.sheets_skipped,
            "ready": len(self.candidates),
            "rejected": self.rejected,
            "rejected_count": len(self.rejected),
            "duplicates_in_file": self.duplicates_in_file,
            "duplicate_in_file_count": len(self.duplicates_in_file),
            "unmapped_columns": sorted(
                {c for c in self.unmapped_columns if _key(c) not in NOISE_HEADINGS}
            ),
            "suspicious_salaries": self.suspicious_salaries,
        }


def normalise_phone(raw) -> tuple[str, list[str]]:
    """One 10-digit Indian mobile, plus any other numbers found in the cell.

    Sheets carry "+919876543210", "0 9876543210" and "9884769336 / 8838079358" —
    the last being one person with two numbers, which must not become two
    candidates or one 21-digit string.
    """
    text = _clean(raw)
    if not text:
        return "", []

    found: list[str] = []
    for chunk in re.split(r"[\s,/|;&]+|\bor\b", text, flags=re.IGNORECASE):
        digits = re.sub(r"\D", "", chunk)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        elif len(digits) == 11 and digits.startswith("0"):
            digits = digits[1:]
        if len(digits) == 10 and digits[0] in "6789" and digits not in found:
            found.append(digits)

    if not found:
        # A single run of digits with separators inside, e.g. "98847 69336".
        digits = re.sub(r"\D", "", text)
        if len(digits) == 12 and digits.startswith("91"):
            digits = digits[2:]
        if len(digits) == 10 and digits[0] in "6789":
            found = [digits]

    return (found[0], found[1:]) if found else ("", [])


def parse_experience(raw) -> tuple[float, str]:
    """Years as a number, plus the original wording when it was not one.

    "Fresher", "Experienced" and "Fresher in telecalling" all appear where a
    number is expected. Anything we cannot read becomes 0 and keeps its wording
    in remarks, so "Experienced" is never silently filed as no experience.
    """
    text = _clean(raw)
    if not text:
        return 0.0, ""
    match = re.search(r"\d+(?:\.\d+)?", text)
    if match:
        try:
            years = float(match.group(0))
            # 4 digits, 1 decimal on the model.
            return (min(years, 999.9), "" if text == match.group(0) else text)
        except ValueError:
            pass
    if "fresher" in text.lower():
        return 0.0, "" if text.strip().lower() == "fresher" else text
    return 0.0, text


def parse_money(raw) -> tuple[float, bool]:
    """Amount, and whether it looks like it was written in thousands."""
    text = _clean(raw)
    if not text:
        return 0.0, False
    # Match the number as written, commas and all, then drop the separators.
    # Stripping every non-digit first turned "Rs. 16,000" into ".16000" and so
    # into 0.16 — a salary quietly corrupted by a thousands comma, which is how
    # most of these sheets write one.
    match = re.search(r"\d[\d,]*(?:\.\d+)?", text)
    if not match:
        return 0.0, False
    try:
        amount = float(match.group(0).replace(",", ""))
    except ValueError:
        return 0.0, False
    return amount, 0 < amount < SUSPICIOUS_SALARY_BELOW


def map_action(raw) -> tuple[str, str]:
    """The portal's action, plus the original text when it did not map."""
    text = _clean(raw)
    if not text:
        return DEFAULT_ACTION, ""
    lowered = text.lower()
    for needle, action in ACTION_HINTS:
        if needle in lowered:
            return action, ""
    return DEFAULT_ACTION, text


def detect_header(rows: list[list], look_ahead: int = 12) -> tuple[int, dict[int, str], list[str]]:
    """Find the heading row and what each of its columns means.

    Exported sheets often start with a title and some blank rows, so the headings
    are not necessarily row 1. The best row is the one whose cells map onto the
    most known fields; a sheet with no name and no phone is not a candidate list
    at all and its caller skips it.
    """
    best = (-1, {}, [])
    best_score = 0
    for index, row in enumerate(rows[:look_ahead]):
        mapping: dict[int, str] = {}
        unknown: list[str] = []
        claimed: set[str] = set()
        for position, cell in enumerate(row):
            key = _key(cell)
            if not key:
                continue
            for target, aliases in COLUMN_ALIASES.items():
                if key in aliases:
                    # First column to claim a field keeps it. A second column
                    # meaning the same thing is left unmapped so it reaches
                    # remarks instead of overwriting the first.
                    if target not in claimed:
                        claimed.add(target)
                        mapping[position] = target
                    else:
                        unknown.append(str(cell).strip())
                    break
            else:
                unknown.append(str(cell).strip())
        targets = set(mapping.values())
        score = len(targets) + (3 if "name" in targets else 0) + (3 if "phone_number" in targets else 0)
        if score > best_score:
            best_score, best = score, (index, mapping, unknown)
    return best


def read_rows(data: bytes, filename: str) -> list[tuple[str, list[list]]]:
    """(sheet name, rows) for a .xlsx or .csv upload."""
    lower = filename.lower()
    if lower.endswith(".csv") or lower.endswith(".txt"):
        text = data.decode("utf-8-sig", errors="replace")
        sample = text[:4096]
        try:
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        except csv.Error:
            dialect = csv.excel
        return [("CSV", [list(r) for r in csv.reader(io.StringIO(text), dialect)])]

    import openpyxl  # imported here so a CSV upload never needs it

    workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    sheets = []
    try:
        for worksheet in workbook.worksheets:
            sheets.append(
                (worksheet.title, [list(r) for r in worksheet.iter_rows(values_only=True)])
            )
    finally:
        workbook.close()
    return sheets


def build_candidates(data: bytes, filename: str, source: str) -> ImportReport:
    """Parse an upload into candidate dicts, without touching the database.

    EVERY sheet that looks like a candidate list is read, not just the first:
    one workbook here holds the same people on three sheets and another splits
    them across two, while its remaining sheets are pivot tables. Pivots have no
    name or phone heading and are skipped by that fact alone, and the same person
    appearing on two sheets is caught as a duplicate on their phone number.
    """
    report = ImportReport()
    seen_phones: dict[str, dict] = {}

    for sheet_name, rows in read_rows(data, filename):
        if not rows:
            report.sheets_skipped.append({"sheet": sheet_name, "reason": "empty"})
            continue

        header_index, mapping, unknown = detect_header(rows)
        targets = set(mapping.values())
        if header_index < 0 or "name" not in targets or "phone_number" not in targets:
            report.sheets_skipped.append(
                {"sheet": sheet_name, "reason": "no name/phone columns — not a candidate list"}
            )
            continue

        report.sheets_read.append(sheet_name)
        report.unmapped_columns.extend(unknown)
        headings = {
            position: str(rows[header_index][position]).strip()
            for position in range(len(rows[header_index]))
            if position < len(rows[header_index]) and rows[header_index][position] is not None
        }

        for offset, row in enumerate(rows[header_index + 1 :], start=header_index + 2):
            values = {target: row[position] if position < len(row) else None
                      for position, target in mapping.items()}
            name = _clean(values.get("name"))
            phone, extra_phones = normalise_phone(values.get("phone_number"))

            if not name and not phone:
                continue  # blank spacer row
            if not name:
                report.rejected.append(
                    {"sheet": sheet_name, "row": offset, "reason": "no name", "phone": phone}
                )
                continue
            if not phone:
                report.rejected.append(
                    {"sheet": sheet_name, "row": offset, "reason": "no usable 10-digit mobile",
                     "name": name, "raw": _clean(values.get("phone_number"))}
                )
                continue

            years, exp_note = parse_experience(values.get("years_of_experience"))
            last_salary, last_odd = parse_money(values.get("last_salary"))
            expecting, exp_odd = parse_money(values.get("expecting_salary"))
            action, action_note = map_action(values.get("action"))
            if last_odd or exp_odd:
                report.suspicious_salaries += 1

            # Nothing from the sheet is thrown away: whatever had no home in the
            # model is written down here, labelled, so HR can still read it.
            notes: list[str] = []
            if _clean(values.get("remarks")):
                notes.append(_clean(values.get("remarks")))
            if action_note:
                notes.append(f"Status in sheet: {action_note}")
            if exp_note:
                notes.append(f"Experience in sheet: {exp_note}")
            if extra_phones:
                notes.append("Other number(s): " + ", ".join(extra_phones))
            for position, heading in headings.items():
                if position in mapping or _key(heading) in NOISE_HEADINGS:
                    continue
                cell = _clean(row[position] if position < len(row) else None)
                if cell:
                    notes.append(f"{heading}: {cell}")

            candidate = {
                "name": name[: MAX_LENGTHS["name"]],
                "phone_number": phone,
                "email": (_clean(values.get("email")) or None),
                "source": source[: MAX_LENGTHS["source"]],
                "qualification": _clean(values.get("qualification"))[: MAX_LENGTHS["qualification"]] or None,
                "present_address": _clean(values.get("present_address"))[: MAX_LENGTHS["present_address"]] or None,
                "permanent_address": _clean(values.get("permanent_address"))[: MAX_LENGTHS["permanent_address"]] or None,
                "years_of_experience": years,
                "segment": DEFAULT_SEGMENT,
                "previous_company": _clean(values.get("previous_company"))[: MAX_LENGTHS["previous_company"]] or None,
                "last_salary": last_salary,
                "expecting_salary": expecting,
                "remarks": "\n".join(notes),
                "action": action,
            }
            if candidate["email"] and "@" not in candidate["email"]:
                candidate["remarks"] = (
                    candidate["remarks"] + f"\nEmail in sheet: {candidate['email']}"
                ).strip()
                candidate["email"] = None

            if phone in seen_phones:
                report.duplicates_in_file.append(
                    {"sheet": sheet_name, "row": offset, "name": name, "phone": phone,
                     "kept_from": seen_phones[phone]["_sheet"]}
                )
                continue
            candidate["_sheet"] = sheet_name
            seen_phones[phone] = candidate
            report.candidates.append(candidate)

    for candidate in report.candidates:
        candidate.pop("_sheet", None)
    return report
