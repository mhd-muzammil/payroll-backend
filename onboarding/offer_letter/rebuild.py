"""Rebuild template.pdf and layout.json from HR's sample offer letter.

Run this only when HR changes their Word letter: export it to PDF with a
sample candidate (the fields below must hold exactly the SAMPLE values, or this
stops), then

    pip install pymupdf     # a tool for this script only; the app never imports it
    python -m onboarding.offer_letter.rebuild "path/to/sample offer letter.pdf"   # from payroll_backend

and check a rendered letter against the sample before committing. It writes:

  template.pdf  the sample with all its text deleted (graphics kept exactly)
  layout.json   every word's place, with the sample's details as named fields

If the new letter moves the fields to other spans, FIELD_SPANS and ANNEXURE
must be updated to match; the checks below say which span disagrees.
"""
import json
import statistics
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent

FONTS = {
    "Calibri": "carlito",
    "Calibri-Bold": "carlito-bold",
    "Verdana": "dejavu",
    "Verdana-Bold": "dejavu-bold",
    "Tahoma": "dejavu-condensed",
    "Tahoma-Bold": "dejavu-condensed-bold",
    "SymbolMT": "dejavu",
}
BULLET = "•"

# span index -> (field id, field, the sample's text there)
FIELD_SPANS = {
    1: ("ref", "ref_no", "RTPL/NJ/2026/140"),
    3: ("name1", "name", "Kumar V"),
    7: ("name2", "name", "Kumar V"),
    11: ("designation", "designation", "Service engineer"),
    217: ("package", "package", "27,415."),
}
# Spans whose words move with the field before them.
FOLLOW_SPANS = {4: "name1", 8: "name2", 12: "designation"}
JOINING_SPAN, JOINING_WORD = 47, "31.08.2026"
ANNEXURE = {
    225: "basic_m", 226: "basic_y", 228: "hra_m", 229: "hra_y",
    231: "conveyance_m", 232: "conveyance_y", 234: "cea_m", 235: "cea_y",
    237: "personal_m", 238: "personal_y", 240: "gross_m", 241: "gross_y",
    244: "pt_m", 245: "pt_y", 247: "total_b_m", 248: "total_b_y",
    250: "net_m", 251: "net_y", 254: "insurance_m", 255: "insurance_y",
    257: "petrol_m", 258: "petrol_y", 260: "total_c_m", 261: "total_c_y",
    263: "ctc_m", 264: "ctc_y",
}
SAMPLE = {
    "basic_m": "12104", "basic_y": "145248", "hra_m": "6052", "hra_y": "72624",
    "conveyance_m": "1600", "conveyance_y": "19200", "cea_m": "200", "cea_y": "2400",
    "personal_m": "4252", "personal_y": "51024", "gross_m": "24,208", "gross_y": "2,90,496",
    "pt_m": "208.0", "pt_y": "2496", "total_b_m": "208.0", "total_b_y": "2496",
    "net_m": "24,000", "net_y": "2,88,000", "insurance_m": "207", "insurance_y": "2484",
    "petrol_m": "3000", "petrol_y": "3000", "total_c_m": "3207", "total_c_y": "5484",
    "ctc_m": "27,415", "ctc_y": "2,95,980",
}


def words_of(doc, fitz):
    """Every word, where Word put it, numbered by the span it sits in."""
    words, span_index = [], -1
    for pno, page in enumerate(doc):
        data = page.get_text("rawdict", flags=fitz.TEXT_PRESERVE_WHITESPACE)
        for block in data["blocks"]:
            for line in block.get("lines", []):
                for span in line["spans"]:
                    if not "".join(ch["c"] for ch in span["chars"]).strip():
                        continue
                    span_index += 1
                    current = None
                    for ch in span["chars"]:
                        if ch["c"].isspace():
                            current = None
                            continue
                        if current is None:
                            current = {
                                "span": span_index, "page": pno + 1,
                                "x": round(ch["origin"][0], 2), "y": round(ch["origin"][1], 2),
                                "font": span["font"], "size": round(span["size"], 2), "text": "",
                            }
                            words.append(current)
                        current["text"] += ch["c"]
                        current["x1"] = round(ch["bbox"][2], 2)
    # Word sometimes types "7 . 0" and closes it up with kerning: one word to us.
    merged = []
    for w in words:
        prev = merged[-1] if merged else None
        if (prev and prev["span"] == w["span"] and prev["page"] == w["page"]
                and abs(prev["y"] - w["y"]) < 0.5 and -4.0 <= w["x"] - prev["x1"] < 1.0):
            prev["text"] += w["text"]
            prev["x1"] = w["x1"]
            continue
        merged.append(dict(w))
    return merged


def build_template(sample, fitz):
    doc = fitz.open(sample)
    for page in doc:
        page.add_redact_annot(page.rect)
        page.apply_redactions(
            images=fitz.PDF_REDACT_IMAGE_NONE,
            graphics=fitz.PDF_REDACT_LINE_ART_NONE,
            text=fitz.PDF_REDACT_TEXT_REMOVE,
        )
    doc.set_metadata({})
    doc.del_xml_metadata()
    doc.save(str(HERE / "template.pdf"), garbage=4, deflate=True, clean=True)
    left = [p.number + 1 for p in fitz.open(str(HERE / "template.pdf")) if p.get_text().strip()]
    if left:
        sys.exit(f"text survived on pages {left}")


def build_layout(sample, fitz):
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont

    from . import FONT_FILES  # noqa: F401 - run as a module, see below

    for key, file in FONT_FILES.items():
        pdfmetrics.registerFont(TTFont(key, str(HERE / "fonts" / file)))

    def width(text, font, size):
        return pdfmetrics.stringWidth(text, font, size)

    words = words_of(fitz.open(sample), fitz)
    for w in words:
        w["f"] = FONTS[w["font"]]
        if w["font"] == "SymbolMT":
            w["text"] = BULLET  # the Symbol font keeps its bullet at a private code

    ratios = {}
    for w in words:
        if len(w["text"]) >= 3 and w["text"] != BULLET:
            ratios.setdefault(w["f"], []).append((w["x1"] - w["x"]) / width(w["text"], w["f"], w["size"]))
    # Bold words sit in headings with plain gaps, where a narrow scale shows as
    # wide spacing: the median. Regular text a little under it, so few words
    # need squeezing to keep their gaps.
    scale = {
        f: round(statistics.median(v) if f.endswith("bold") else statistics.quantiles(v, n=4)[0], 4)
        for f, v in ratios.items()
    }

    by_span = {}
    for w in words:
        by_span.setdefault(w["span"], []).append(w)
    items, seen = [], set()
    for w in words:
        sp = w["span"]
        base = {"p": w["page"], "x": w["x"], "y": w["y"], "f": w["f"], "s": w["size"]}
        if sp in FIELD_SPANS:
            fid, field, expected = FIELD_SPANS[sp]
            got = " ".join(x["text"] for x in by_span[sp])
            if got != expected:
                sys.exit(f"span {sp}: expected {expected!r}, found {got!r}")
            if fid in seen:
                continue
            seen.add(fid)
            item = dict(base, field=field, id=fid, end=by_span[sp][-1]["x1"])
            if field == "package":
                item["suffix"] = "."
            items.append(item)
        elif sp in FOLLOW_SPANS:
            items.append(dict(base, t=w["text"], follows=FOLLOW_SPANS[sp]))
        elif sp == JOINING_SPAN and w["text"] == JOINING_WORD:
            items.append(dict(base, field="joining_date", id="joining_date"))
        elif sp in ANNEXURE:
            key = ANNEXURE[sp]
            if w["text"] != SAMPLE[key]:
                sys.exit(f"span {sp}: expected {SAMPLE[key]!r}, found {w['text']!r}")
            items.append(dict(base, field=key, id=key, align="right", right=w["x1"]))
        else:
            items.append(dict(base, t=w["text"], x1=w["x1"]))

    # A word may not run into the next: where ours comes out wider than the gap
    # Word left (keeping most of that gap), that word alone is squeezed to fit.
    for a, b in zip(items, items[1:]):
        if "t" not in a or "follows" in a or a["p"] != b["p"] or abs(a["y"] - b["y"]) > 0.5 or b["x"] <= a["x"]:
            continue
        natural = scale[a["f"]] * width(a["t"], a["f"], a["s"])
        gap = max(0.8, 0.7 * (b["x"] - a.get("x1", a["x"] + natural)))
        room = b["x"] - gap - a["x"]
        if natural > room:
            a["k"] = round(room / width(a["t"], a["f"], a["s"]), 4)
    for it in items:
        it.pop("x1", None)

    layout = {"page": [595.32, 841.92], "scale": scale, "items": items}
    raw = json.dumps(layout, ensure_ascii=False)
    for value in ("Kumar", "27,415", "24,208", "12104", "RTPL/NJ/2026/140", "31.08.2026", "2,95,980"):
        if value in raw:
            sys.exit(f"layout still holds the sample's {value!r}")
    (HERE / "layout.json").write_text(json.dumps(layout, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")


if __name__ == "__main__":
    # Run as: python -m onboarding.offer_letter.rebuild "<sample.pdf>" (from payroll_backend)
    import fitz  # PyMuPDF

    if len(sys.argv) != 2:
        sys.exit(__doc__)
    build_template(sys.argv[1], fitz)
    build_layout(sys.argv[1], fitz)
    print("template.pdf and layout.json rebuilt; render a letter and compare it with the sample.")
