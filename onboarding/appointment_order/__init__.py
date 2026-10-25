"""The appointment order as a PDF, and the copy the employee signs online.

HR has no Word file of this letter, only a printed one that somebody signed
(Tharun R's, 8 June 2026). So it is typeset here, line by line from that print,
on the same letterhead as the offer letter: letterhead.pdf is page 2 of the
offer letter's template -- the logo, the dots, the watermark and the footer,
with no text of its own.

As on the print, the letter is in Times and Annexure-I is a Calibri table
(drawn in Carlito, which has Calibri's widths, like the offer letter's). Only
the date, the name and address, the role, the joining date and the pay change.
Two things on the print are not copied: its Annexure was headed with somebody
else's name, and its yearly CTC (1,76,496) was not twelve times the monthly
16,208. Here the Annexure is headed with the employee's name and every yearly
figure is worked out from the monthly ones.

The words live in letter(), which both the PDF and the signing page read, so
the page an employee reads on their phone says exactly what the PDF says.
"""
import io
import re
import threading
from decimal import ROUND_HALF_UP, Decimal
from pathlib import Path

HERE = Path(__file__).resolve().parent
LETTERHEAD = HERE / "letterhead.pdf"
FONTS = HERE.parent / "offer_letter" / "fonts"
FONT_FILES = {
    "carlito": "Carlito-Regular.ttf",
    "carlito-bold": "Carlito-Bold.ttf",
    "dejavu-bold": "DejaVuSans-Bold.ttf",
}
# Registered under our own names, so no other PDF in the app can collide with them.
FONT_PREFIX = "AppointmentOrder-"

PAGE_W, PAGE_H = 595.32, 841.92
LEFT = 72.0
RIGHT = 523.0
NUMBER_X = 90.0
ITEM_X = 108.0
ROMAN, BOLD = "Times-Roman", "Times-Bold"
SIZE = 12
LEADING = 14.0
SUP_SCALE = 0.62
SUP_RISE = 4.2
# Every line the letter has to fit before "Contd....2" on page 1.
PAGE1_LAST_LINE = 610.0

MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)

COMPANY = "Renderways Technology Private Limited"
AGREEMENT = "I agree to the conditions stated in the Appointment Order."

# Where the employee's signature goes on each page, as on the print: beside
# "Contd....2" on page 1, after "Signature:" on page 2, under the Annexure on
# page 3. (x, top, width, height) in points from the top left of the page.
SIGNATURE_BOXES = {
    0: (440.0, 633.0, 110.0, 38.0),
    1: (410.0, 536.0, 120.0, 36.0),
    2: (396.0, 562.0, 120.0, 38.0),
}
SIGNED_DATE_AT = (382.0, 589.0)  # after "Date:" on page 2
INK = (0.08, 0.13, 0.45)  # the blue of the pen the print was signed with

_fonts_lock = threading.Lock()
_fonts_registered = False


def _register_fonts():
    global _fonts_registered
    if _fonts_registered:
        return
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    with _fonts_lock:
        if not _fonts_registered:
            for key, file in FONT_FILES.items():
                pdfmetrics.registerFont(TTFont(FONT_PREFIX + key, str(FONTS / file)))
            _fonts_registered = True


# ---------------------------------------------------------------- the numbers

def indian(number):
    """16208 -> "16,208", 194496 -> "1,94,496": lakhs, as the Annexure writes them."""
    digits = str(int(number))
    if len(digits) <= 3:
        return digits
    head, tail = digits[:-3], digits[-3:]
    groups = []
    while len(head) > 2:
        groups.insert(0, head[-2:])
        head = head[:-2]
    if head:
        groups.insert(0, head)
    return ",".join(groups + [tail])


def annexure(order):
    """Annexure-I as numbers. Professional tax is a yearly amount (2,500 on the
    print), so a month of it is a twelfth -- 208.3 -- and the monthly net pay
    is rounded to the rupee, as the print's 16,000 is."""
    gross = order.basic + order.hra + order.conveyance + order.cea + order.personal_allowance
    pt_year = order.professional_tax
    pt_month = (Decimal(pt_year) / 12).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    net_month = (Decimal(gross) - Decimal(pt_year) / 12).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    return {
        "gross": gross,
        "gross_year": gross * 12,
        "pt_month": pt_month,
        "pt_year": pt_year,
        "net": int(net_month),
        "net_year": gross * 12 - pt_year,
        "ctc": gross,
        "ctc_year": gross * 12,
    }


def annexure_rows(order):
    """The table, row by row: (label, per month, per annum, style). The plain
    lines have no separators and the three totals are in lakhs, as printed."""
    sums = annexure(order)
    plain = [
        ("Basic", order.basic),
        ("HRA", order.hra),
        ("Conveyance", order.conveyance),
        ("CEA", order.cea),
        ("Personal Allowances", order.personal_allowance),
    ]
    rows = [(label, str(value), str(value * 12), "line") for label, value in plain]
    rows += [
        ("Gross Salary(A)", indian(sums["gross"]), indian(sums["gross_year"]), "total"),
        ("Deduction:", "", "", "heading"),
        ("Professional Tax", f"{sums['pt_month']}", str(sums["pt_year"]), "plain"),
        ("Total(B)", f"{sums['pt_month']}", str(sums["pt_year"]), "bold"),
        ("Net Salary(A-B)", indian(sums["net"]), indian(sums["net_year"]), "total"),
        ("", "", "", "blank"),
        ("CTC(A+C)", indian(sums["ctc"]), indian(sums["ctc_year"]), "total"),
    ]
    return rows


# ------------------------------------------------------------------ the words
# A run is (text, bold, superscript). A block is one paragraph of the letter.

_ORDINAL = re.compile(r"\b(\d+)(st|nd|rd|th)\b", re.IGNORECASE)


def ordinal_suffix(day):
    if 11 <= day % 100 <= 13:
        return "th"
    return {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")


def date_runs(day, bold):
    """08th June 2026, the "th" raised as Word raises it."""
    return [
        (f"{day.day:02d}", bold, False),
        (ordinal_suffix(day.day), bold, True),
        (f" {MONTHS[day.month - 1]} {day.year}", bold, False),
    ]


def text_runs(text, bold=False):
    """Typed text with its ordinals raised: "4th cross street" -> 4, th, cross..."""
    runs, pos = [], 0
    for match in _ORDINAL.finditer(text):
        runs.append((text[pos:match.end(1)], bold, False))
        runs.append((match.group(2), bold, True))
        pos = match.end()
    runs.append((text[pos:], bold, False))
    return [run for run in runs if run[0]]


def _lines(*lines):
    """Fixed wording, broken where the print breaks it."""
    return [text_runs(line) for line in lines]


def letter(order):
    """The letter, page by page. Each block has a kind, its runs, and -- for the
    fixed wording -- the print's own line breaks."""
    sums = annexure(order)
    intro = [
        ("We are pleased to appoint you as ", False, False),
        (order.designation, True, False),
        (" to be based at our ", False, False),
        ("Renderways Technology Pvt Ltd", True, False),
        (" #25 1", False, False),
        ("st", False, True),
        (" floor Gandhi street, mettukuppam, maduravoyal, chennai 600 095, with effect from ", False, False),
        *date_runs(order.joining_date, True),
        (" on the following terms and conditions:-", False, False),
    ]
    pay = [
        ("You will be paid a sum of ", False, False),
        (f"Rs. {indian(sums['net'])}", True, False),
        (" per month as per the breakup furnished at the end of this document annexed as annexure 1"
         " Your CTC is Rs. ", False, False),
        (f"{indian(sums['ctc'])}/-", True, False),
    ]
    page1 = [
        {"kind": "date", "runs": date_runs(order.order_date, True)},
        {"kind": "name", "runs": [(f"{order.salutation} {order.name}", True, False)]},
        {"kind": "address", "runs": [("Address: ", True, False)] + text_runs(order.address, bold=True)},
        {"kind": "title", "runs": [("APPOINTMENT ORDER", True, False)]},
        {"kind": "para", "runs": intro},
        {"kind": "item", "number": "1.", "runs": pay},
        {"kind": "item", "number": "2.", "lines": _lines(
            "You will also be eligible for other allowances and perquisites subject to the Company's",
            "Rules in this regard as may be applicable from time to time to in your grade. The actual",
            "remuneration package applicable to you is given in the enclosed statement.",
        )},
        {"kind": "item", "number": "3.", "lines": _lines(
            "You shall always maintain utmost secrecy of all information relating to the company's",
            "business and all matters connected directly or indirectly thereto.",
        )},
        {"kind": "item", "number": "4.", "lines": _lines(
            "The Company is entitled to depute your services to any other company or",
            "organization in a similar or such other capacity as the company may decide. Such",
            "deputation will be treated as a continuation of your services with the Company.",
        )},
        {"kind": "item", "number": "5.", "lines": _lines(
            "You are expected to devote full attention and time to the Company's work. You shall",
            "not gainfully engage yourself in any outside work or business over and above your",
            "legitimate work in the company, i.e. on duty days, on holidays, and when you are on",
            "leave without written permission from the company.",
        )},
    ]
    page2 = [
        {"kind": "item", "number": "6.", "lines": _lines(
            "You will be on probation for a period of six months. The probation can be extended",
            "for a further period at the discretion of and as may be deemed fit by the Management.",
        )},
        {"kind": "item", "number": "7.", "lines": _lines(
            "You will be considered for confirmation on completion of the probationary period if",
            "your service is found satisfactory.",
        )},
        {"kind": "item", "number": "8.", "lines": _lines(
            "Your services are terminable on three months’ notice or payment of three months’",
            "basic salary in lieu thereof from either side.",
        )},
        {"kind": "item", "number": "9.", "lines": _lines(
            "You will retire from service on your completion of 58 (fifty-eight) years of age.",
        )},
        {"kind": "para", "lines": _lines("We look forward to your successful association with us.")},
        {"kind": "item", "number": "10.", "lines": _lines(
            "As per company policy, employees who are terminated, leave without notice, or do",
            "not report to work within 30 days from the date of joining, their salary will not be",
            "processed",
        )},
        {"kind": "closing", "runs": [("With Best Wishes.", False, False)]},
        {"kind": "closing", "runs": [("for ", False, False), (COMPANY, True, False)]},
        {"kind": "closing", "runs": [("No:", False, False)]},
        {"kind": "agreement", "lines": _lines("I agree to the conditions stated", "in the Appointment Order.")},
    ]
    return [page1, page2]


def block_runs(block):
    """A block's runs as one paragraph, its line breaks dropped."""
    if "runs" in block:
        return block["runs"]
    runs = []
    for i, line in enumerate(block["lines"]):
        if i:
            runs.append((" ", False, False))
        runs.extend(line)
    return runs


def letter_for_page(order):
    """The letter for the signing page: plain data a browser can render without
    trusting any of it as markup."""
    pages = []
    for blocks in letter(order):
        pages.append([
            {
                "kind": block["kind"],
                "number": block.get("number", ""),
                "runs": [{"t": t, "b": b, "s": s} for t, b, s in block_runs(block)],
            }
            for block in blocks
        ])
    return {
        "pages": pages,
        "annexure": {
            "name": order.name,
            "rows": [
                {"label": label, "month": month, "year": year, "style": style}
                for label, month, year, style in annexure_rows(order)
            ],
        },
    }


# ------------------------------------------------------------------ the PDF

def _width(runs, size=SIZE):
    from reportlab.pdfbase.pdfmetrics import stringWidth

    return sum(
        stringWidth(text, BOLD if bold else ROMAN, size * SUP_SCALE if sup else size)
        for text, bold, sup in runs
    )


def _words(runs):
    """Runs cut into words, each word the styled pieces it is made of ("1" and a
    raised "st" make one word)."""
    words, current = [], []
    for text, bold, sup in runs:
        for i, part in enumerate(text.split(" ")):
            if i and current:
                words.append(current)
                current = []
            if part:
                current.append((part, bold, sup))
    if current:
        words.append(current)
    return words


def wrap(runs, width, size=SIZE):
    """Runs broken into lines no wider than width."""
    space = _width([(" ", False, False)], size)
    lines, line, used = [], [], 0.0
    for word in _words(runs):
        word_width = _width(word, size)
        if line and used + space + word_width > width:
            lines.append(line)
            line, used = [], 0.0
        if line:
            line.append((" ", False, False))
            used += space
        line.extend(word)
        used += word_width
    if line:
        lines.append(line)
    return lines


def _draw(canvas, runs, x, y, size=SIZE):
    """One line of runs with its baseline y points from the top of the page."""
    for text, bold, sup in runs:
        font, font_size = (BOLD if bold else ROMAN), (size * SUP_SCALE if sup else size)
        canvas.setFont(font, font_size)
        canvas.drawString(x, PAGE_H - y + (SUP_RISE * size / SIZE if sup else 0), text)
        x += _width([(text, bold, sup)], size)
    return x


def _draw_lines(canvas, lines, x, y, size=SIZE, leading=LEADING):
    for i, line in enumerate(lines):
        _draw(canvas, line, x, y + i * leading, size)
    return y + (len(lines) - 1) * leading


def _item(canvas, block, y):
    canvas.setFont(ROMAN, SIZE)
    canvas.drawString(NUMBER_X, PAGE_H - y, block["number"])
    lines = block.get("lines") or wrap(block["runs"], RIGHT - ITEM_X)
    return _draw_lines(canvas, lines, ITEM_X, y)


def _page_one(canvas, blocks):
    date, name, address, title, intro, *items = blocks
    _draw(canvas, date["runs"], RIGHT - _width(date["runs"], 14), 104, 14)
    _draw(canvas, name["runs"], LEFT, 161, 14)
    y = _draw_lines(canvas, wrap(address["runs"], RIGHT - LEFT), LEFT, 202)
    y += 30
    _draw(canvas, title["runs"], (PAGE_W - _width(title["runs"], 14)) / 2, y, 14)
    y = _draw_lines(canvas, wrap(intro["runs"], RIGHT - LEFT), LEFT, y + 24)
    y += 34
    for i, block in enumerate(items):
        if i:
            y += 30
        y = _item(canvas, block, y)
    if y > PAGE1_LAST_LINE:
        raise ValueError("The name, address or role is too long for page 1.")
    canvas.setFont(ROMAN, SIZE)
    canvas.drawString(425, PAGE_H - 627, "Contd….2")
    canvas.drawString(472, PAGE_H - 681, "Page 2…")


# Baselines on page 2, as on the print.
PAGE2_ITEM_AT = {"6.": 106, "7.": 149, "8.": 206, "9.": 255, "10.": 303}


def _page_two(canvas, blocks):
    *body, wishes, company, number, agreement = blocks
    for block in body:
        if block["kind"] == "item":
            _item(canvas, block, PAGE2_ITEM_AT[block["number"]])
        else:  # "We look forward...", level with the numbers
            _draw_lines(canvas, block["lines"], NUMBER_X, 277)
    _draw(canvas, wishes["runs"], NUMBER_X, 359, 14)
    _draw(canvas, company["runs"], NUMBER_X, 386, 14)
    _draw(canvas, number["runs"], NUMBER_X, 402, 11)
    _draw_lines(canvas, agreement["lines"], 352, 470, 11, 12.6)
    canvas.setFont(ROMAN, 11)
    canvas.drawString(352, PAGE_H - 566, "Signature:")
    canvas.drawString(352, PAGE_H - 589, "Date:")


# Annexure-I: the print's table, its colours those of the offer letter's.
TABLE_X = 80.0
TABLE_TOP = 139.0
COLUMNS = (201.0, 110.0, 125.0)
ROW_H = 32.0
HEADER_FILL = (0.886, 0.937, 0.851)  # #E2EFD9
TOTAL_FILL = (1.0, 0.949, 0.8)  # #FFF2CC
CELL_SIZE = 10


def _page_three(canvas, order):
    from reportlab.pdfbase.pdfmetrics import stringWidth

    _register_fonts()
    regular, bold = FONT_PREFIX + "carlito", FONT_PREFIX + "carlito-bold"
    table_w = sum(COLUMNS)
    title = "ANNEXURE-I"
    canvas.setFont(FONT_PREFIX + "dejavu-bold", 9.5)
    canvas.drawString(
        TABLE_X + (table_w - stringWidth(title, FONT_PREFIX + "dejavu-bold", 9.5)) / 2,
        PAGE_H - 122, title,
    )

    edges = [TABLE_X, TABLE_X + COLUMNS[0], TABLE_X + COLUMNS[0] + COLUMNS[1], TABLE_X + table_w]
    rows = [(order.name, "Per Month", "Per Annum", "header")] + annexure_rows(order)

    def text(value, x, y, font, align="left", cell_w=0.0):
        canvas.setFont(font, CELL_SIZE)
        if align == "right":
            canvas.drawRightString(x, PAGE_H - y, value)
        elif align == "center":
            canvas.drawCentredString(x + cell_w / 2, PAGE_H - y, value)
        else:
            canvas.drawString(x, PAGE_H - y, value)

    for i, (label, month, year, style) in enumerate(rows):
        top = TABLE_TOP + i * ROW_H
        fill = HEADER_FILL if style == "header" else TOTAL_FILL if style == "total" else None
        if fill:
            canvas.setFillColorRGB(*fill)
            canvas.rect(TABLE_X, PAGE_H - top - ROW_H, table_w, ROW_H, stroke=0, fill=1)
            canvas.setFillColorRGB(0, 0, 0)
        font = bold if style in ("header", "total", "heading", "bold") else regular
        middle = top + ROW_H / 2 + CELL_SIZE * 0.35
        if style == "header":
            for j, value in enumerate((label, month, year)):
                text(value, edges[j], middle, font, "center", edges[j + 1] - edges[j])
            continue
        # The plain lines sit as on the print: the name at the top of the cell,
        # the amounts at the bottom.
        label_y = top + 12.5 if style == "line" else middle
        value_y = top + ROW_H - 8 if style == "line" else middle
        text(label, edges[0] + 6, label_y, font)
        text(month, edges[2] - 6, value_y, font, "right")
        text(year, edges[3] - 6, value_y, font, "right")

    canvas.setStrokeColorRGB(0.25, 0.25, 0.25)
    canvas.setLineWidth(0.75)
    bottom = TABLE_TOP + len(rows) * ROW_H
    for i in range(len(rows) + 1):
        y = PAGE_H - (TABLE_TOP + i * ROW_H)
        canvas.line(TABLE_X, y, TABLE_X + table_w, y)
    heading_row = 1 + [r[3] for r in annexure_rows(order)].index("heading")
    for x in edges:
        if x in (edges[0], edges[3]):
            canvas.line(x, PAGE_H - TABLE_TOP, x, PAGE_H - bottom)
            continue
        # "Deduction:" runs across the whole row.
        canvas.line(x, PAGE_H - TABLE_TOP, x, PAGE_H - (TABLE_TOP + heading_row * ROW_H))
        canvas.line(x, PAGE_H - (TABLE_TOP + (heading_row + 1) * ROW_H), x, PAGE_H - bottom)


def _on_letterhead(front_pages_pdf, title):
    """Each page of front_pages_pdf printed over the letterhead."""
    from pypdf import PdfReader, PdfWriter

    letterhead = PdfReader(str(LETTERHEAD)).pages[0]
    writer = PdfWriter()
    for page in PdfReader(io.BytesIO(front_pages_pdf)).pages:
        page.merge_page(letterhead, over=False)
        writer.add_page(page)
    writer.add_metadata({"/Title": title, "/Author": COMPANY})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()


def appointment_order_filename(order, signed=False):
    name = re.sub(r"[^A-Za-z0-9]+", "-", order.name or "employee").strip("-")
    return f"Appointment-Order-{name or 'employee'}{'-signed' if signed else ''}.pdf"


def render_appointment_order_pdf(order):
    """The order as PDF bytes, unsigned. Raises on any failure; the caller says so."""
    from reportlab.pdfgen import canvas as pdf_canvas

    page1, page2 = letter(order)
    buffer = io.BytesIO()
    canvas = pdf_canvas.Canvas(buffer, pagesize=(PAGE_W, PAGE_H), invariant=1)
    _page_one(canvas, page1)
    canvas.showPage()
    _page_two(canvas, page2)
    canvas.showPage()
    _page_three(canvas, order)
    canvas.showPage()
    canvas.save()
    return _on_letterhead(buffer.getvalue(), f"Appointment Order - {order.name}")


# ------------------------------------------------------------ the signed copy

def _when(moment):
    """25-10-2026, 10:42 AM -- India time, 12-hour, as the office reads times."""
    from django.utils import timezone

    if moment is None:
        return "-"
    local = timezone.localtime(moment)
    return local.strftime("%d-%m-%Y, %I:%M %p").replace(", 0", ", ")


def sign_pdf(sent_pdf, signature_png, signed_at, record):
    """The PDF the employee was shown, with their signature where the print
    has it, the date beside "Date:", and a last page saying how it was signed.

    record is the audit trail: a list of (label, value) lines. Pages 1-3 are
    the bytes that were sent, untouched underneath; only ink is laid on top.
    """
    from django.utils import timezone
    from pypdf import PdfReader, PdfWriter
    from reportlab.lib.utils import ImageReader
    from reportlab.pdfgen import canvas as pdf_canvas

    sent = PdfReader(io.BytesIO(sent_pdf))
    image = ImageReader(io.BytesIO(signature_png))

    ink = io.BytesIO()
    canvas = pdf_canvas.Canvas(ink, pagesize=(PAGE_W, PAGE_H), invariant=1)
    for index in range(len(sent.pages)):
        box = SIGNATURE_BOXES.get(index)
        if box:
            x, top, w, h = box
            canvas.drawImage(image, x, PAGE_H - top - h, w, h, mask="auto",
                             preserveAspectRatio=True, anchor="sw")
        if index == 1:
            canvas.setFillColorRGB(*INK)
            canvas.setFont("Helvetica", 12)
            canvas.drawString(SIGNED_DATE_AT[0], PAGE_H - SIGNED_DATE_AT[1],
                              timezone.localtime(signed_at).strftime("%d-%m-%Y"))
            canvas.setFillColorRGB(0, 0, 0)
        canvas.showPage()
    canvas.save()

    # The record, on its own page of letterhead.
    audit = io.BytesIO()
    page = pdf_canvas.Canvas(audit, pagesize=(PAGE_W, PAGE_H), invariant=1)
    _draw(page, [("Electronic Signature Record", True, False)], LEFT, 125, 14)
    _draw_lines(page, _lines(
        "How this Appointment Order was signed. Pages 1 to 3 are the document as it was sent;",
        "the signature and the date on them were added when it was signed.",
    ), LEFT, 145, 10.5)
    value_x, y = 200.0, 186.0
    for label, value in record:
        _draw(page, [(label, True, False)], LEFT, y, 10.5)
        for line in value if isinstance(value, (list, tuple)) else [value]:
            if label.endswith("SHA-256"):
                page.setFont("Courier", 9)
                page.drawString(value_x, PAGE_H - y, line)
                y += 13
                continue
            for wrapped in wrap([(str(line), False, False)], RIGHT - value_x, 10.5):
                _draw(page, wrapped, value_x, y, 10.5)
                y += 14
        y += 6
    _draw(page, [("Signature drawn", True, False)], LEFT, y + 10, 10.5)
    page.drawImage(image, value_x, PAGE_H - (y + 52), 160, 50, mask="auto",
                   preserveAspectRatio=True, anchor="sw")
    page.showPage()
    page.save()
    audit_page = PdfReader(io.BytesIO(_on_letterhead(audit.getvalue(), "Signature record"))).pages[0]

    writer = PdfWriter()
    for sent_page, ink_page in zip(sent.pages, PdfReader(io.BytesIO(ink.getvalue())).pages):
        sent_page.merge_page(ink_page)
        writer.add_page(sent_page)
    writer.add_page(audit_page)
    writer.add_metadata({"/Title": "Appointment Order (signed)", "/Author": COMPANY})
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
