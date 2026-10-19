"""The offer letter as a PDF: the company's own six-page Word letter, with only
the reference, the name, the role, the joining date and the pay filled in.

It is not typeset from scratch. HR's letter is the format, so:

  template.pdf  is the sample letter with every piece of text deleted -- the
                letterhead, logo, the box over the old phone number, the title
                underline, the Annexure table and the seal, exactly as Word
                drew them. Deleted, not covered: nobody's name or salary is
                hidden underneath.

  layout.json   is where Word put every word of the letter, read from the
                sample. Each fixed word is drawn at its own place, so every line
                keeps Word's spacing and justification; the words that carried
                the sample's details are named fields instead.

Calibri is drawn in Carlito, which has Calibri's exact widths; Verdana and
Tahoma in DejaVu Sans and DejaVu Sans Condensed, scaled to their widths. All
three are free to ship (licences beside them in fonts/). The server image has
no fonts of its own, so these are the only ones this can rely on.
"""
import io
import json
import re
import threading
from functools import lru_cache
from pathlib import Path

HERE = Path(__file__).resolve().parent
TEMPLATE = HERE / "template.pdf"
LAYOUT = HERE / "layout.json"
FONT_FILES = {
    "carlito": "Carlito-Regular.ttf",
    "carlito-bold": "Carlito-Bold.ttf",
    "dejavu": "DejaVuSans.ttf",
    "dejavu-bold": "DejaVuSans-Bold.ttf",
    "dejavu-condensed": "DejaVuSansCondensed.ttf",
    "dejavu-condensed-bold": "DejaVuSansCondensed-Bold.ttf",
}
# Registered under our own names, so no other PDF in the app can collide with them.
FONT_PREFIX = "OfferLetter-"
PAGES = 6
# The line "...as a <designation> the following terms and conditions:" may not
# run past the right margin the rest of page 1 keeps.
RIGHT_MARGIN = 531.0

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
                pdfmetrics.registerFont(TTFont(FONT_PREFIX + key, str(HERE / "fonts" / file)))
            _fonts_registered = True


@lru_cache(maxsize=1)
def _layout():
    with open(LAYOUT, encoding="utf-8") as fh:
        return json.load(fh)


def indian(number):
    """24208 -> "24,208", 290496 -> "2,90,496": lakhs, as the Annexure writes them."""
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


def annexure(letter):
    """Annexure-I as numbers: what the monthly amounts add up to, per month and
    per year. Per year is twelve months of every line."""
    earnings = (
        letter.basic + letter.hra + letter.conveyance + letter.cea + letter.personal_allowance
    )
    benefits = letter.insurance + letter.petrol_allowance
    return {
        "gross": earnings,
        "net": earnings - letter.professional_tax,
        "benefits": benefits,
        "ctc": earnings + benefits,
    }


def field_texts(letter):
    """Every field the layout names, written the way the sample writes it: the
    plain lines without separators, the three totals in lakhs, the professional
    tax with one decimal."""
    sums = annexure(letter)
    texts = {
        "ref_no": letter.ref_no,
        "name": letter.name,
        "designation": letter.designation,
        "joining_date": letter.joining_date.strftime("%d.%m.%Y"),
        "package": indian(sums["ctc"]),
    }
    plain = {
        "basic": letter.basic,
        "hra": letter.hra,
        "conveyance": letter.conveyance,
        "cea": letter.cea,
        "personal": letter.personal_allowance,
        "insurance": letter.insurance,
        "petrol": letter.petrol_allowance,
        "total_c": sums["benefits"],
    }
    for key, value in plain.items():
        texts[f"{key}_m"] = str(value)
        texts[f"{key}_y"] = str(value * 12)
    for key in ("gross", "net", "ctc"):
        texts[f"{key}_m"] = indian(sums[key])
        texts[f"{key}_y"] = indian(sums[key] * 12)
    for key in ("pt", "total_b"):
        texts[f"{key}_m"] = f"{letter.professional_tax:.1f}"
        texts[f"{key}_y"] = str(letter.professional_tax * 12)
    return texts


def offer_letter_filename(letter):
    name = re.sub(r"[^A-Za-z0-9]+", "-", letter.name or "offer-letter").strip("-")
    return f"Offer-Letter-{name or 'candidate'}.pdf"


def render_offer_letter_pdf(letter):
    """The letter as PDF bytes. Raises on any failure; the caller says so."""
    from pypdf import PdfReader, PdfWriter
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfgen import canvas

    _register_fonts()
    layout = _layout()
    width, height = layout["page"]
    scales = layout["scale"]
    texts = field_texts(letter)

    buffer = io.BytesIO()
    page_canvas = canvas.Canvas(buffer, pagesize=(width, height))

    def text_width(text, font, size, scale):
        return scale * pdfmetrics.stringWidth(text, FONT_PREFIX + font, size)

    def draw(text, x, y, font, size, scale):
        line = page_canvas.beginText()
        line.setTextOrigin(x, height - y)
        line.setFont(FONT_PREFIX + font, size)
        line.setHorizScale(scale * 100)
        line.textOut(text)
        page_canvas.drawText(line)

    # How far each field's end moved from the sample's, for the words after it.
    shift = {}
    for page in range(1, PAGES + 1):
        for item in layout["items"]:
            if item["p"] != page:
                continue
            font, size = item["f"], item["s"]
            if "field" in item:
                text = texts[item["field"]] + item.get("suffix", "")
                scale = scales[font]
                if item["field"] == "designation":
                    # Room for the role and the rest of its line before the margin.
                    tail = 150.5  # "the following terms and conditions:", as drawn
                    room = RIGHT_MARGIN - tail - item["x"]
                    natural = text_width(text, font, size, scale)
                    if natural > room:
                        scale *= room / natural
                w = text_width(text, font, size, scale)
                x = item["right"] - w if item.get("align") == "right" else item["x"]
                draw(text, x, item["y"], font, size, scale)
                if "end" in item:
                    shift[item["id"]] = (x + w) - item["end"]
            else:
                x = item["x"] + shift.get(item.get("follows"), 0.0)
                draw(item["t"], x, item["y"], font, size, item.get("k", scales[font]))
        page_canvas.showPage()
    page_canvas.save()

    writer = PdfWriter()
    template = PdfReader(str(TEMPLATE))
    words = PdfReader(io.BytesIO(buffer.getvalue()))
    for back, front in zip(template.pages, words.pages):
        back.merge_page(front)
        writer.add_page(back)
    writer.add_metadata({
        "/Title": f"Offer Letter - {letter.name}",
        "/Author": "Renderways Technology Private Limited",
    })
    out = io.BytesIO()
    writer.write(out)
    return out.getvalue()
