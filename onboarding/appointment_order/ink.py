"""The ink of a signature photographed on paper, as a picture with nothing else in it.

The employee signs on paper and takes a photo of it with the signing page's
camera; what goes on the appointment order is the ink alone, on a transparent
ground, so it sits on the letter the way a pen would have. The photo itself is
kept as it was taken, for the record.

Pillow only -- no numpy in production. The steps:

1. The paper's own brightness everywhere, as a camera sees it in uneven light:
   ink removed by a max filter on a small copy, then smoothed and scaled back.
2. How many times darker than that each pixel is -- a ratio, taken as the
   difference of logarithms, so ink in a shadow counts as much as ink in the
   light. Paper comes out near nothing however the light falls across it; ink
   comes out strong, and a faint ruled line far weaker than a pen.
3. Otsu's threshold on that splits ink from paper, with a floor so a blank
   sheet's grain is never mistaken for writing.
4. The ink keeps soft edges (a ramp around the threshold), stray specks go, and
   the picture is cropped to the ink with a little margin.

Ink that runs right across the box -- lines on ruled paper, a shadow's edge, the
paper's own edge against the table -- is refused rather than kept: a signature
fitted inside the box never reaches both of its sides.
"""
import io
import math

from PIL import Image, ImageChops, ImageFilter, ImageStat

# The longest side the photo is worked at: plenty for a signature 120 points wide
# on the letter, and quick.
WORK_SIDE = 1600
# The ink picture is kept at most this big; the letter shows it far smaller.
OUT_SIZE = (900, 340)
# Darker than its paper by less than this is grain, never ink: about 11% darker
# (on the scale below, four times the difference of the logarithms).
MIN_CONTRAST = 22
INK = (20, 33, 115)  # the blue of the date written on page 2
# Each brightness as a logarithm, 0-255. The difference of two of these is a
# ratio, which plain 8-bit Pillow can take on any version (no ImageMath).
_LOG = [round(255 * math.log1p(value) / math.log(256)) for value in range(256)]


class NoSignature(ValueError):
    """The photo has no usable signature in it; the message says what to change."""


def _otsu(histogram):
    """The threshold that best splits a histogram in two (Otsu, 1979)."""
    total = sum(histogram)
    weighted_all = sum(value * count for value, count in enumerate(histogram))
    below = weighted_below = 0
    best_threshold, best_spread = 0, -1.0
    for threshold, count in enumerate(histogram):
        below += count
        if not below:
            continue
        above = total - below
        if not above:
            break
        weighted_below += threshold * count
        mean_below = weighted_below / below
        mean_above = (weighted_all - weighted_below) / above
        spread = below * above * (mean_below - mean_above) ** 2
        if spread > best_spread:
            best_spread, best_threshold = spread, threshold
    return best_threshold


def extract_ink(image):
    """The signature in this photo as an RGBA PNG (bytes), cropped to the ink.

    Raises NoSignature when the photo is dark, has no ink in it, is mostly ink,
    has something running right across the box, or the signature is too small.
    The picture is read as it is: the page's camera writes no EXIF.
    """
    grey = image.convert("L")
    grey.thumbnail((WORK_SIDE, WORK_SIDE))
    width, height = grey.size
    # Dark, not "low contrast": a faint pen on evenly lit paper is low contrast
    # too, and the ink checks below tell a blank sheet apart. Below this the
    # camera's grain is as strong as any pen.
    if ImageStat.Stat(grey).mean[0] < 50:
        raise NoSignature("The signature photo came out dark. Take it again in good light.")

    small = grey.resize((max(1, width // 4), max(1, height // 4)), Image.BILINEAR)
    paper = (
        small.filter(ImageFilter.MaxFilter(7))
        .filter(ImageFilter.GaussianBlur(6))
        .resize((width, height), Image.BILINEAR)
    )
    darker = ImageChops.subtract(paper.point(_LOG), grey.point(_LOG)).point(lambda v: min(255, 4 * v))

    threshold = max(_otsu(darker.histogram()), MIN_CONTRAST)
    low, high = threshold * 0.6, threshold * 1.4
    alpha = darker.point(
        lambda v: 0 if v <= low else 255 if v >= high else int((v - low) * 255 / (high - low))
    )
    strokes = alpha.point(lambda v: 255 if v >= 128 else 0).filter(ImageFilter.MedianFilter(3))

    inked = strokes.histogram()[255]
    share = inked / (width * height)
    box = strokes.getbbox()
    if not box or share < 0.002:
        raise NoSignature(
            "No signature could be seen in the photo. Sign on plain white paper with a dark pen, "
            "fill the box with it, and take the photo again."
        )
    if share > 0.25:
        raise NoSignature(
            "The photo has too much dark in it to find the signature. Use plain white paper with "
            "nothing else in the box, and take it again."
        )
    def reaches(area):
        strip = strokes.crop(area)
        return strip.histogram()[255] > strip.width * strip.height * 0.005

    side, top = max(3, width * 15 // 1000), max(3, height * 15 // 1000)
    if (reaches((0, 0, side, height)) and reaches((width - side, 0, width, height))) or (
        reaches((0, 0, width, top)) and reaches((0, height - top, width, height))
    ):
        raise NoSignature(
            "Something runs right across the box: lines on the paper, a shadow, or the paper's edge. "
            "Use plain white paper with no lines, in even light, and fit only your signature inside the box, "
            "with a little space round it."
        )
    if box[2] - box[0] < width * 0.08 or box[3] - box[1] < height * 0.03:
        raise NoSignature(
            "The signature is too small in the photo. Hold the paper closer so the signature fills the box."
        )

    margin = int(max(box[2] - box[0], box[3] - box[1]) * 0.04) + 2
    box = (
        max(0, box[0] - margin),
        max(0, box[1] - margin),
        min(width, box[2] + margin),
        min(height, box[3] + margin),
    )
    # Soft edges, but only next to real strokes: grain far from the writing stays out.
    near_strokes = strokes.crop(box).filter(ImageFilter.MaxFilter(3))
    ink = Image.new("RGBA", (box[2] - box[0], box[3] - box[1]), INK + (0,))
    ink.putalpha(ImageChops.multiply(alpha.crop(box), near_strokes))
    ink.thumbnail(OUT_SIZE)
    out = io.BytesIO()
    ink.save(out, format="PNG")
    return out.getvalue()
