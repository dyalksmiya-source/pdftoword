"""PDF -> DOCX conversion (used by server.py).

1. Page-by-page routing: every page is classified from its visible text layer
   and image coverage as "text" (pdf2docx), "ocr" (Tesseract) or "blank".
2. One DOCX in the original page order: pdf2docx pages and OCR pages are
   written into the same python-docx Document. A pdf2docx page that fails or
   loses most of its text is rolled back and redone with OCR; a page that
   still yields nothing gets a placeholder line. No page is ever dropped.
3. Scanned pages (section 2): image preprocessing (sheet cut out of the
   border, lighting flattened, contrast, deskew, denoise, binarised copy,
   table rules removed, about 300 dpi), the page's languages from a quick
   low-resolution pass (Latin page: Arabic-letter noise dropped; Arabic page:
   "ara" only; mixed: all), the grey image read too when Tesseract is unsure
   (better result kept), a line-loss guard for text lines no word covers,
   then real paragraphs, Heading 1/2 from the text size, and Word tables
   for clear ruled grids. A page read with a mean confidence below
   OCR_UNREADABLE_CONF (handwriting, poor photo) is kept as its image with a
   short italic note in the document's language, never as garbage text.
4. Arabic: paragraphs that are mainly Arabic get w:bidi, start (right)
   alignment and RTL runs (w:rtl, complex-script font and size); Latin words
   inside them stay in LTR runs. Mainly-Arabic tables become RTL tables
   (w:bidiVisual). Arabic presentation forms are normalized to letters.
5. A PDF with nothing readable raises EmptyDocumentError.
6. At most MAX_OCR_PAGES pages are OCR'd per file (each costs 2-5 s): the
   server refuses bigger scans before converting (ScannedPageLimitError), and
   OCR fallbacks share the same budget (beyond it: placeholder line).
7. Time budget: the server gives every request CONVERSION_TIME_BUDGET seconds
   (default 240, below gunicorn's 300 s timeout, which would kill the worker
   before the quota is released). The deadline is checked between pages
   (planning, pdf2docx analysis and parsing, writing, OCR) and before saving;
   past it, ConversionTimeoutError. The DOCX is written to a ".part" file and
   renamed only when complete, so a stopped conversion leaves no DOCX.

Logs never contain document text.
"""
import copy
import logging
import os
import re
import time
import unicodedata

import cv2
import fitz  # PyMuPDF
import numpy as np
import pytesseract
from docx import Document
from docx.enum.section import WD_SECTION
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from docx.shared import Pt
from PIL import Image

logger = logging.getLogger("pdf2wordly.conversion")

MIN_CHARS_PER_PAGE = 20          # visible characters below which a page has "no text layer"
IMAGE_PAGE_SHARE = 0.4           # one image covering >= 40% of the page ...
IMAGE_PAGE_MAX_CHARS = 400       # ... with little text: the content is in the image -> OCR
SMALL_IMAGES_SHARE = 0.3         # no text and only images smaller than this: decoration -> pdf2docx
MIN_TEXT_RECALL = 0.5            # pdf2docx must keep >= 50% of the page's words, else OCR
OCR_MIN_CONF = 60                # confident words used to detect the script
KEPT_IMAGE_DPI = 150             # page with no text found: kept as a picture
OCR_LANGUAGES = os.environ.get("OCR_LANGUAGES", "eng+fra+ara")
PLACEHOLDER = "[Page {n} could not be converted]"
# OCR page read with a mean confidence below this (handwriting, poor photo):
# the page image is kept, with a short note, instead of unreliable text.
OCR_UNREADABLE_CONF = 50
UNREADABLE_NOTE = {
    "en": "This page could not be read reliably (handwriting or low quality), so it is kept as an image.",
    "fr": "Cette page n'a pas pu être lue de façon fiable (écriture manuscrite ou faible qualité), "
          "elle est donc conservée comme image.",
    "ar": "تعذّرت قراءة هذه الصفحة بشكل موثوق (خط يدوي أو جودة منخفضة)، لذلك احتُفظ بها كصورة.",
}
FRENCH_WORDS = frozenset("le la les des et est une un du pour dans que qui sur avec pas par au aux ce cette sont".split())
ENGLISH_WORDS = frozenset("the and of to is in for that with on are this be as by it an was from".split())
# /api/convert only (kept until its removal). Jobs use jobs.JOB_MAX_OCR_PAGES_PRO / _FREE, the numbers the
# site shows ("scanned-pages-limit-pro" / "-free" in _tools/site-config.json, checked by tests).
MAX_OCR_PAGES = int(os.environ.get("MAX_OCR_PAGES", "30"))
# Seconds per request, below gunicorn's --timeout 300 (Dockerfile; checked by tests).
CONVERSION_TIME_BUDGET = float(os.environ.get("CONVERSION_TIME_BUDGET", "240"))
OCR_MARGIN_PT = 56

AR_RE = re.compile(r"[؀-ۿݐ-ݿࢠ-ࣿﭐ-﷿ﹰ-﻿]")
LATIN_RE = re.compile(r"[A-Za-zÀ-ɏ]")
PRESENTATION_RE = re.compile(r"[ﭐ-﷿ﹰ-﻿]+")
WORD_RE = re.compile(r"\w+", re.UNICODE)
# Latin words (with digits, URL / e-mail characters) inside an Arabic line
LATIN_CHUNK_RE = re.compile(
    r"[A-Za-zÀ-ɏ0-9@._:/\-]*[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ0-9@._:/\-]*"
    r"(?:[  ]+[A-Za-zÀ-ɏ0-9@._:/\-]*[A-Za-zÀ-ɏ][A-Za-zÀ-ɏ0-9@._:/\-]*)*")
LEADING_PUNCT_RE = re.compile(r"^(\s*)([.,:;!?،؛؟]+)(\s*)(?=\S)")
ARABIC_FONTS = {"arial", "tahoma", "times new roman", "traditional arabic", "simplified arabic",
                "arabic typesetting", "sakkal majalla", "segoe ui", "courier new", "microsoft sans serif",
                "amiri", "dubai", "noto naskh arabic", "noto sans arabic", "noto kufi arabic", "cairo"}
DEFAULT_ARABIC_FONT = "Arial"

W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
RUNS_XPATH = "./w:r | ./w:hyperlink/w:r | ./w:ins/w:r | ./w:smartTag/w:r"  # a paragraph's own runs


class EmptyDocumentError(Exception):
    """Nothing readable in the PDF (no text layer, no OCR text, no images)."""


class ScannedPageLimitError(Exception):
    """More pages need OCR than MAX_OCR_PAGES."""

    def __init__(self, scanned_pages, limit):
        super().__init__(f"{scanned_pages} scanned pages, limit {limit}")
        self.scanned_pages = scanned_pages
        self.limit = limit


class ConversionTimeoutError(Exception):
    """The conversion used up its time budget (CONVERSION_TIME_BUDGET)."""


def make_deadline(budget=None, start=None):
    """time.monotonic() value after which a conversion stops."""
    return (time.monotonic() if start is None else start) + (
        CONVERSION_TIME_BUDGET if budget is None else budget)


def check_deadline(deadline):
    if deadline is not None and time.monotonic() >= deadline:
        raise ConversionTimeoutError("time budget used up")


class _DeadlineDoc:
    """The fitz.Document given to pdf2docx's document analysis: the deadline
    is checked every time it opens or iterates a page."""

    def __init__(self, doc, deadline):
        self._doc = doc
        self._deadline = deadline

    def __getitem__(self, index):
        check_deadline(self._deadline)
        return self._doc[index]

    def __iter__(self):
        for page in self._doc:
            check_deadline(self._deadline)
            yield page

    def __len__(self):
        return len(self._doc)

    def __getattr__(self, name):
        return getattr(self._doc, name)


def plan_pdf(pdf_path, deadline=None):
    """Route of every page: [(route, info), ...] (classify_page)."""
    doc = fitz.open(pdf_path)
    try:
        plan = []
        for page in doc:
            check_deadline(deadline)
            plan.append(classify_page(page))
        return plan
    finally:
        doc.close()


def count_ocr_pages(plan):
    return sum(1 for route, _ in plan if route == "ocr")


# ---------------------------------------------------------------------------
# 1. Page classification
# ---------------------------------------------------------------------------

def _visible_chars(page):
    """Non-space characters that are actually drawn (invisible OCR layers excluded)."""
    try:
        n = 0
        for span in page.get_texttrace():
            if span.get("type") == 3 or span.get("opacity", 1) == 0:
                continue
            n += sum(1 for c in span.get("chars", ()) if not chr(c[0]).isspace())
        return n
    except Exception:
        return len("".join(page.get_text().split()))


def classify_page(page):
    """Return (route, info): route is "text", "ocr" or "blank"."""
    area = abs(page.rect) or 1.0
    shares = []
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & page.rect
        if not r.is_empty:
            shares.append(abs(r) / area)
    largest = max(shares, default=0.0)
    chars = _visible_chars(page)
    info = {"chars": chars, "largest_image": round(largest, 2), "images": len(shares)}
    if chars < MIN_CHARS_PER_PAGE:
        if largest >= SMALL_IMAGES_SHARE:
            return "ocr", info           # scan or photo
        if shares:
            return "text", info          # small images only (logos, icons): pdf2docx keeps them
        if page.get_drawings():
            return "ocr", info           # text drawn as vector outlines
        return "blank", info
    if largest >= IMAGE_PAGE_SHARE and chars < IMAGE_PAGE_MAX_CHARS:
        return "ocr", info               # content is in a big image, little real text
    return "text", info


# ---------------------------------------------------------------------------
# 2. OCR
#
# One scanned page:
#   a. render in grey at about 300 dpi (never far above the scan's own
#      resolution: 200-300 dpi);
#   b. preprocess: the sheet is cut out of a darker desk/scanner border,
#      lighting and paper colour are flattened (division by the background),
#      contrast stretched, skew measured and corrected, light denoise; a
#      binarised copy is made, and table rules are removed from both;
#   c. a quick low-resolution pass with every configured language gives the
#      page's script, then the main pass reads the binarised image with the
#      page's languages; if Tesseract is not confident, the grey image is read
#      too and the better result is kept (confident characters);
#   d. line-loss guard: text lines found in the image layout that no OCR word
#      covers (lines skipped by Tesseract, very large headings) are cut out,
#      scaled to a readable size and read again in one extra call;
#   e. structure: real paragraphs from Tesseract's blocks and line geometry,
#      headings from the text height (Word "Heading 1" / "Heading 2"), ruled
#      tables rebuilt as Word tables when the grid is clear.
# ---------------------------------------------------------------------------

OCR_DPI = 300                   # target resolution of the OCR image
OCR_MIN_DPI = 200               # never below (small scans are enlarged up to this)
OCR_LETTER_PX = 22              # letters about this high read well and fast (Tesseract)
OCR_SAMPLE_LINES = 6            # lines read first to choose languages and image variant
OCR_GOOD_CONF = 80              # sample at least this confident: grey image not tried
OCR_MAX_RECOVER = 40            # line-loss guard: regions read again per page
HEADING1_RATIO = 1.45           # text size of a line / body text size (see _line_sizes)
HEADING2_RATIO = 1.2
HEADING_MAX_WORDS = 14
INVISIBLE_RE = re.compile("[‎‏‪-‮⁦-⁩﻿]")   # direction marks, BOM
NUMBER_CELL_RE = re.compile(r"^[\d\s.,:/%+\-–٠-٩]+$")


class OcrResult:
    """items: ("heading", level, text) | ("para", text) | ("table", [[cell, ...], ...])."""

    def __init__(self, items, langs, info):
        self.items = items
        self.langs = langs
        self.info = info

    def text(self):
        out = []
        for item in self.items:
            if item[0] == "table":
                out += [" ".join(row) for row in item[1]]
            else:
                out.append(item[-1])
        return "\n".join(out)

    def has_text(self):
        return bool(self.text().strip())


def _configured(languages):
    return [lang for lang in languages.split("+") if lang]


def choose_languages(arabic_chars, latin_chars, languages=OCR_LANGUAGES):
    """Tesseract languages for a page from the script counts of a quick pass."""
    langs = _configured(languages)
    latin = [lang for lang in langs if lang != "ara"]
    has_ara = "ara" in langs
    total = arabic_chars + latin_chars
    if total < 10 or not has_ara or not latin:
        return "+".join(langs)                       # too little evidence: everything configured
    share = arabic_chars / total
    if share >= 0.9:
        return "ara"
    if share <= 0.1:
        return "+".join(latin)
    return "+".join(latin + ["ara"])                 # really mixed


def _script_counts(data):
    ar = la = 0
    for text, conf in zip(data["text"], data["conf"]):
        try:
            if not text.strip() or float(conf) < OCR_MIN_CONF:
                continue
        except (TypeError, ValueError):
            continue
        ar += len(AR_RE.findall(text))
        la += len(LATIN_RE.findall(text))
    return ar, la


# ---- a. resolution and rendering

def ocr_dpi(page):
    """About 300 dpi, but not much above the page's own scan resolution."""
    best, dpi = 0.0, OCR_DPI
    for info in page.get_image_info():
        r = fitz.Rect(info["bbox"]) & page.rect
        if r.is_empty or not info.get("width"):
            continue
        if abs(r) > best:
            best = abs(r)
            dpi = info["width"] / (r.width / 72.0)
    return int(min(OCR_DPI, max(OCR_MIN_DPI, dpi)))


def _render_gray(page, dpi):
    pix = page.get_pixmap(dpi=dpi, colorspace=fitz.csGRAY, alpha=False)
    img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width]
    return img.copy()


# ---- b. preprocessing

def _sheet(gray):
    """The bright sheet on a darker border: (mask, bounding box) or None."""
    h, w = gray.shape
    s = 600.0 / max(h, w)
    small = cv2.resize(gray, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    blur = cv2.GaussianBlur(small, (0, 0), 3)
    _, bw = cv2.threshold(blur, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    bw = cv2.morphologyEx(bw, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(bw)
    if n < 2:
        return None
    k = 1 + int(np.argmax(stats[1:, cv2.CC_STAT_AREA]))
    share = stats[k, cv2.CC_STAT_AREA] / float(small.shape[0] * small.shape[1])
    if not 0.35 <= share <= 0.97:                    # no clear border: nothing to cut
        return None
    contours, _ = cv2.findContours((labels == k).astype(np.uint8) * 255, cv2.RETR_EXTERNAL,
                                   cv2.CHAIN_APPROX_SIMPLE)
    hull = (cv2.convexHull(max(contours, key=cv2.contourArea)) / s).astype(np.int32)
    mask = np.zeros_like(gray)
    cv2.fillPoly(mask, [hull], 255)
    mask = cv2.erode(mask, np.ones((9, 9), np.uint8))   # drop the sheet edge itself
    return mask, cv2.boundingRect(hull)


def _flatten(gray):
    """Divide by the background (lighting, paper colour) -> white paper, dark ink."""
    h, w = gray.shape
    f = 8
    small = cv2.resize(gray, (max(1, w // f), max(1, h // f)), interpolation=cv2.INTER_AREA)
    bg = cv2.medianBlur(cv2.dilate(small, np.ones((7, 7), np.uint8)), 9)
    bg = cv2.resize(bg, (w, h), interpolation=cv2.INTER_LINEAR)
    return cv2.divide(gray, np.maximum(bg, 1), scale=255)


def _stretch(img):
    lo = float(np.percentile(img, 1))
    if lo >= 200:                                    # (almost) no ink
        return img
    return np.clip((img.astype(np.float32) - lo) * (255.0 / (250.0 - lo)), 0, 255).astype(np.uint8)


def skew_angle(binary_inv):
    """Text skew in degrees (projection profile, -7..7) of an ink-white image."""
    h, w = binary_inv.shape
    s = 1000.0 / max(h, w)
    small = cv2.resize(binary_inv, (max(1, int(w * s)), max(1, int(h * s))), interpolation=cv2.INTER_AREA)
    ys, xs = np.nonzero(small > 127)
    if len(xs) < 200:
        return 0.0
    if len(xs) > 60000:
        pick = np.random.default_rng(0).choice(len(xs), 60000, replace=False)
        xs, ys = xs[pick], ys[pick]
    xs = xs.astype(np.float32)
    ys = ys.astype(np.float32)

    def sharpness(a):
        t = np.deg2rad(a)
        yy = ys * np.cos(t) - xs * np.sin(t)
        hist = np.bincount((yy - yy.min()).astype(np.int32)).astype(np.float64)
        return float(np.sum(hist * hist))

    best = max(np.arange(-7.0, 7.01, 0.5), key=sharpness)
    best = max(np.arange(best - 0.5, best + 0.51, 0.1), key=sharpness)
    return round(float(best), 1)


def _rotate(img, angle):
    h, w = img.shape
    m = cv2.getRotationMatrix2D((w / 2.0, h / 2.0), angle, 1.0)
    return cv2.warpAffine(img, m, (w, h), flags=cv2.INTER_CUBIC, borderMode=cv2.BORDER_CONSTANT,
                          borderValue=255)


def _thin(mask, axis, max_thickness):
    """Keep only the parts of a rule mask that are thin across the rule (a letter
    stem caught by the opening is thick)."""
    n, labels, stats, _ = cv2.connectedComponentsWithStats(mask)
    if n < 2:
        return mask
    across = stats[:, cv2.CC_STAT_HEIGHT] if axis == 0 else stats[:, cv2.CC_STAT_WIDTH]
    keep = across <= max_thickness
    keep[0] = False
    return np.where(keep[labels], 255, 0).astype(np.uint8)


def _rules(binary, letter_px=0):
    """Long thin horizontal / vertical rules (ink-white masks). Long enough that
    no letter, Arabic word stroke or big heading stem matches (at least 5
    letter heights, and a vertical one 1/20 of the page)."""
    inv = 255 - binary
    h, w = binary.shape
    lp = letter_px or 20
    horiz = cv2.morphologyEx(inv, cv2.MORPH_OPEN, np.ones((1, max(40, w // 6, 8 * lp)), np.uint8))
    vert = cv2.morphologyEx(inv, cv2.MORPH_OPEN, np.ones((max(40, h // 20, 5 * lp), 1), np.uint8))
    thickness = max(6, int(0.35 * lp))
    return _thin(horiz, 0, thickness), _thin(vert, 1, thickness)


def _without_edge_ink(binary_inv):
    """Ink-white image without the large pieces touching the image edge (desk,
    scanner lid, shadow of the sheet edge). Used only to measure the skew: the
    image itself is not changed (a title near the edge may touch such a piece)."""
    h, w = binary_inv.shape
    n, labels, stats, _ = cv2.connectedComponentsWithStats(binary_inv)
    drop = np.zeros(n, bool)
    for k in range(1, n):
        x, y, bw, bh = stats[k, :4]
        if (x == 0 or y == 0 or x + bw >= w or y + bh >= h) and (bw > 0.25 * w or bh > 0.25 * h):
            drop[k] = True
    if not drop.any():
        return binary_inv
    return np.where(drop[labels], 0, binary_inv).astype(np.uint8)


def letter_height(binary):
    """Typical letter height in pixels (connected ink components of letter shape), 0 if unknown."""
    n, _, stats, _ = cv2.connectedComponentsWithStats(255 - binary)
    if n < 30:
        return 0
    h = stats[1:, cv2.CC_STAT_HEIGHT]
    w = stats[1:, cv2.CC_STAT_WIDTH]
    ok = (h >= 6) & (h <= binary.shape[0] * 0.1) & (w <= 2.5 * h) & (stats[1:, cv2.CC_STAT_AREA] >= 12)
    h = np.sort(h[ok])
    if len(h) < 30:
        return 0
    h = h[h >= 0.5 * h[int(len(h) * 0.75)]]            # without dots, commas, specks
    return int(h[len(h) // 2])


def preprocess(gray, dpi=OCR_DPI):
    """-> (grey, binary, horizontal rules, vertical rules, info): white paper, black ink,
    sheet cut out, deskewed, scaled so letters are about OCR_LETTER_PX high (not below
    OCR_MIN_DPI). Rules are returned separately (removed by the caller)."""
    info = {}
    sheet = _sheet(gray)
    flat = _flatten(gray)
    if sheet is not None:
        mask, (x, y, bw, bh) = sheet
        flat[mask == 0] = 255
        flat = flat[y:y + bh, x:x + bw]
        info["border_cut"] = True
    flat = _stretch(flat)
    flat = cv2.medianBlur(flat, 3)                     # salt-and-pepper / JPEG noise
    _, binary = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    angle = skew_angle(_without_edge_ink(255 - binary))
    if abs(angle) >= 0.2:
        flat = _rotate(flat, angle)
        _, binary = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    info["skew"] = angle
    # resolution per page: big print does not need 300 dpi (Tesseract time grows with pixels)
    letters = letter_height(binary)
    scale = min(1.0, max(OCR_MIN_DPI / float(dpi), OCR_LETTER_PX / float(letters))) if letters else 1.0
    if scale < 0.95:
        flat = cv2.resize(flat, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
        _, binary = cv2.threshold(flat, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    else:
        scale = 1.0
    info["dpi"] = int(round(dpi * scale))
    info["letter_px"] = int(round(letters * scale))
    horiz, vert = _rules(binary, info["letter_px"])
    return flat, binary, horiz, vert, info


# ---- ruled tables

def _positions(profile, min_share, length):
    """Centres of runs where profile >= min_share * length."""
    on = profile >= min_share * length
    out, start = [], None
    for i, v in enumerate(list(on) + [False]):
        if v and start is None:
            start = i
        elif not v and start is not None:
            out.append((start + i - 1) // 2)
            start = None
    return out


def find_tables(horiz, vert):
    """Clear ruled grids: [{"bbox": (x0, y0, x1, y1), "rows": [y...], "cols": [x...]}].
    Only full grids (every rule crosses the whole table, >= 2 rows and 2 columns)."""
    grid = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((5, 5), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(grid)
    h, w = horiz.shape
    tables = []
    for k in range(1, n):
        x, y, bw, bh = stats[k, :4]
        if bw < w * 0.25 or bh < 40:
            continue
        hs = (horiz[y:y + bh, x:x + bw] > 0).sum(axis=1)
        vs = (vert[y:y + bh, x:x + bw] > 0).sum(axis=0)
        rows = _positions(hs, 0.85, bw)
        cols = _positions(vs, 0.85, bh)
        partial_rows = _positions(hs, 0.2, bw)
        partial_cols = _positions(vs, 0.2, bh)
        if len(rows) < 3 or len(cols) < 3:
            continue
        if len(partial_rows) != len(rows) or len(partial_cols) != len(cols):
            continue                                 # merged cells or broken rules: not reliable
        if min(np.diff(rows)) < 12 or min(np.diff(cols)) < 12:
            continue
        tables.append({"bbox": (x, y, x + bw, y + bh), "rows": [y + r for r in rows],
                       "cols": [x + c for c in cols]})
    return tables


# ---- c. Tesseract passes

def _data(img, langs, config=""):
    return pytesseract.image_to_data(Image.fromarray(img), lang=langs, config=config,
                                     output_type=pytesseract.Output.DICT)


def _words(data, dx=0, dy=0, scale=1.0):
    """Words of image_to_data: text, conf, box, Tesseract (block, par, line)."""
    out = []
    for k, text in enumerate(data["text"]):
        text = INVISIBLE_RE.sub("", text or "").strip()
        if not text:
            continue
        try:
            conf = float(data["conf"][k])
        except (TypeError, ValueError):
            conf = -1.0
        x0 = dx + data["left"][k] / scale
        y0 = dy + data["top"][k] / scale
        out.append({"text": text, "conf": conf, "x0": x0, "y0": y0,
                    "x1": x0 + data["width"][k] / scale, "y1": y0 + data["height"][k] / scale,
                    "key": (data["block_num"][k], data["par_num"][k], data["line_num"][k])})
    return out


def _mean_conf(words):
    confs = [w["conf"] for w in words if w["conf"] >= 0]
    return sum(confs) / len(confs) if confs else 0.0


def _confident_chars(words):
    return sum(len(w["text"]) * w["conf"] / 100.0 for w in words if w["conf"] >= 50)


# ---- d. line-loss guard

def _layout_lines(binary, body_h):
    """Text-line boxes from the image itself (ink merged along the line)."""
    inv = 255 - binary
    h, w = binary.shape
    kx = max(9, int(w * 0.012))
    ky = max(1, int(body_h * 0.25))
    merged = cv2.dilate(inv, np.ones((ky, kx), np.uint8))
    n, labels, stats, _ = cv2.connectedComponentsWithStats(merged)
    boxes = []
    for k in range(1, n):
        x, y, bw, bh = stats[k, :4]
        if bh < 0.6 * body_h or bh > min(15 * body_h, 0.3 * h) or bw < 1.2 * body_h:
            continue
        if bh > 2 * body_h and bw < 2.5 * bh:      # tall and not line-shaped: a picture, not a heading
            continue
        ink = inv[y:y + bh, x:x + bw]
        density = float((ink > 0).mean())
        if not 0.04 <= density <= 0.7:              # specks, photos, filled areas
            continue
        boxes.append((x, y, x + bw, y + bh))
    return boxes


def _covered(box, words):
    x0, y0, x1, y1 = box
    covered = 0.0
    for wd in words:
        v = min(y1, wd["y1"]) - max(y0, wd["y0"])
        if v <= 0.4 * min(y1 - y0, wd["y1"] - wd["y0"]):
            continue
        covered += max(0.0, min(x1, wd["x1"]) - max(x0, wd["x0"]))
    return covered >= 0.4 * (x1 - x0)


def _is_clear_text(words):
    """A recovered line is kept only if it reads as text, not as marks in a picture:
    confident, mostly letters, and at least one real word."""
    text = "".join(w["text"] for w in words)
    letters = len(LATIN_RE.findall(text)) + len(AR_RE.findall(text))
    real_word = any(w["conf"] >= 70 and len(LATIN_RE.findall(w["text"])) + len(AR_RE.findall(w["text"])) >= 3
                    for w in words)
    return bool(text) and _mean_conf(words) >= 70 and letters >= 0.6 * len(text) and real_word


def _read_regions(img, boxes, langs, target_h):
    """Read several regions in ONE Tesseract call: each is cut out, scaled so its
    text is about target_h pixels high, and stacked with white gaps.
    Returns {index: [words in page coordinates]}."""
    pieces, ys, width = [], [], 0
    gap = int(target_h * 1.2)
    y = gap
    for (x0, y0, x1, y1) in boxes:
        pad = int((y1 - y0) * 0.25) + 2
        crop = img[max(0, y0 - pad):y1 + pad, max(0, x0 - pad):x1 + pad]
        scale = min(1.0, target_h * 1.5 / float(max(1, y1 - y0)))
        if scale < 1.0:
            crop = cv2.resize(crop, (max(1, int(crop.shape[1] * scale)), max(1, int(crop.shape[0] * scale))),
                              interpolation=cv2.INTER_AREA)
        pieces.append((crop, y, max(0, x0 - pad), max(0, y0 - pad), scale))
        ys.append((y, y + crop.shape[0]))
        y += crop.shape[0] + gap
        width = max(width, crop.shape[1])
    if not pieces:
        return {}
    canvas = np.full((y, width + 2 * gap), 255, np.uint8)
    for crop, top, *_ in pieces:
        canvas[top:top + crop.shape[0], gap:gap + crop.shape[1]] = crop
    data = _data(canvas, langs, "--psm 6")
    found = {}
    for wd in _words(data):
        cy = (wd["y0"] + wd["y1"]) / 2
        for i, (top, bottom) in enumerate(ys):
            if top <= cy <= bottom:
                _, _, ox, oy, scale = pieces[i]
                found.setdefault(i, []).append(dict(
                    wd, x0=ox + (wd["x0"] - gap) / scale, x1=ox + (wd["x1"] - gap) / scale,
                    y0=oy + (wd["y0"] - top) / scale, y1=oy + (wd["y1"] - top) / scale))
                break
    return found


# ---- e. structure

def _group_lines(words):
    lines = []
    for wd in words:
        if lines and lines[-1]["key"] == wd["key"]:
            lines[-1]["words"].append(wd)
        else:
            lines.append({"key": wd["key"], "words": [wd]})
    for line in lines:
        ws = line["words"]
        line["x0"] = min(w["x0"] for w in ws)
        line["x1"] = max(w["x1"] for w in ws)
        line["y0"] = min(w["y0"] for w in ws)
        line["y1"] = max(w["y1"] for w in ws)
        heights = sorted(w["y1"] - w["y0"] for w in ws)
        line["h"] = heights[len(heights) // 2]
        line["text"] = " ".join(w["text"] for w in ws)
    return lines


def _median(values, default=1.0):
    values = sorted(values)
    return values[len(values) // 2] if values else default


def _body_height(lines):
    return _median([line["h"] for line in lines for _ in line["words"]], 0)


def _line_sizes(lines):
    """Text size of each line relative to the body text: geometric mean of the
    line height ratio and the average character width ratio. A bigger or bold
    font is both taller and wider; either measure alone is noisy (accents,
    descenders, Arabic dots, short words)."""
    def char_w(line):
        return sum(w["x1"] - w["x0"] for w in line["words"]) / max(1, sum(len(w["text"]) for w in line["words"]))
    ref = [line for line in lines if len(line["words"]) >= 4]
    if len(ref) < 3:
        ref = lines
    body_cw = _median([char_w(line) for line in ref]) or 1.0
    body_lh = _median([line["y1"] - line["y0"] for line in ref]) or 1.0
    for line in lines:
        line["size"] = ((char_w(line) / body_cw) * ((line["y1"] - line["y0"]) / body_lh)) ** 0.5


def _join(a, b):
    if a.endswith("-") and len(a) > 1 and a[-2].isalpha():
        return a + b
    return a + " " + b


def build_paragraphs(lines, body_h):
    """Lines (in Tesseract reading order) -> paragraphs
    [{"level": 0 (body) / 1 / 2 (heading), "text", "y0"}]."""
    if not lines:
        return []
    if any("size" not in line for line in lines):
        _line_sizes(lines)
    for line in lines:
        ratio = line["size"]
        few = len(line["words"]) <= HEADING_MAX_WORDS
        line["level"] = 1 if few and ratio >= HEADING1_RATIO else 2 if few and ratio >= HEADING2_RATIO else 0
    block_width = {}
    for line in lines:
        b = line["key"][0]
        block_width[b] = max(block_width.get(b, 0), line["x1"] - line["x0"])
    pitches = sorted(b["y0"] - a["y0"] for a, b in zip(lines, lines[1:])
                     if a["key"][0] == b["key"][0] and not a["level"] and not b["level"] and b["y0"] > a["y0"])
    pitch = pitches[len(pitches) // 2] if pitches else 1.6 * body_h
    items, cur, prev = [], None, None
    for line in lines:
        # Tesseract's own block/paragraph numbers sometimes cut off the last line
        # of a paragraph: the next line straight below, in the same column, continues it.
        gap = line["y0"] - prev["y0"] if prev is not None else 0
        below = prev is not None and 0 < gap <= 1.8 * pitch and \
            min(line["x1"], prev["x1"]) - max(line["x0"], prev["x0"]) >= \
            0.5 * min(line["x1"] - line["x0"], prev["x1"] - prev["x0"])
        if prev is None or line["level"] != prev["level"] or not below:
            new = True
        elif line["key"][:2] == prev["key"][:2]:
            new = gap > 1.4 * pitch                    # same Tesseract paragraph: only a real gap splits
        else:
            new = gap > 1.2 * pitch                    # Tesseract's split, confirmed by extra space
        if not new and not line["level"]:
            # the previous line was short: it ended its paragraph
            width = max(block_width[prev["key"][0]], block_width[line["key"][0]])
            new = (prev["x1"] - prev["x0"]) < 0.7 * width
        if new:
            if cur:
                items.append(cur)
            cur = {"level": line["level"], "text": line["text"], "y0": line["y0"]}
        else:
            cur["text"] = _join(cur["text"], line["text"])
        prev = line
    items.append(cur)
    return items


def _table_rows(table, words):
    """Words whose centre falls in a cell -> rows of cell texts (visual order, left to right)."""
    rows, cols = table["rows"], table["cols"]
    cells = [[[] for _ in range(len(cols) - 1)] for _ in range(len(rows) - 1)]
    for wd in words:
        cx, cy = (wd["x0"] + wd["x1"]) / 2, (wd["y0"] + wd["y1"]) / 2
        r = next((i for i in range(len(rows) - 1) if rows[i] <= cy < rows[i + 1]), None)
        c = next((i for i in range(len(cols) - 1) if cols[i] <= cx < cols[i + 1]), None)
        if r is not None and c is not None:
            cells[r][c].append(wd)
    return cells


def _in_box(wd, box):
    cx, cy = (wd["x0"] + wd["x1"]) / 2, (wd["y0"] + wd["y1"]) / 2
    return box[0] <= cx <= box[2] and box[1] <= cy <= box[3]


def _cell_text(words):
    return " ".join(line["text"] for line in _group_lines(sorted(words, key=lambda w: (w["key"], 0))))


def _reread_number_cells(img, table, rows, languages):
    """The Arabic model misreads Western digits (1200 -> 100): table cells that
    hold only a number are read again with the Latin languages, in one call."""
    latin = "+".join(lang for lang in _configured(languages) if lang != "ara")
    targets = [(r, c) for r, row in enumerate(rows) for c, text in enumerate(row)
               if text and NUMBER_CELL_RE.match(text) and any(ch.isdigit() for ch in text)]
    if not latin or not targets:
        return
    ys, xs = table["rows"], table["cols"]
    boxes = [(xs[c] + 4, ys[r] + 4, xs[c + 1] - 4, ys[r + 1] - 4) for r, c in targets]
    found = _read_regions(img, boxes, latin, 32)
    for i, (r, c) in enumerate(targets):
        text = " ".join(w["text"] for w in found.get(i, []) if w["conf"] >= 50)
        if text and NUMBER_CELL_RE.match(text):
            rows[r][c] = text


def _sample_lines(binary, letter_px):
    """About OCR_SAMPLE_LINES text-line boxes spread evenly over the page."""
    body_h = max(10, int(1.4 * (letter_px or 20)))
    lines = sorted(_layout_lines(binary, body_h), key=lambda b: (b[1], b[0]))
    if len(lines) > OCR_SAMPLE_LINES:
        step = len(lines) / float(OCR_SAMPLE_LINES)
        lines = [lines[int(k * step)] for k in range(OCR_SAMPLE_LINES)]
    return lines


def _choose_variant_and_languages(gray, binary, languages, letter_px, info):
    """Decide on a small sample of the page's lines (one cheap call each), so the
    whole page is read only once: binary or grey image (the grey one only when
    Tesseract is unsure of the binary one; better confident characters win),
    and the page's languages (every configured language reads the sample)."""
    lines = _sample_lines(binary, letter_px)
    detect = len(_configured(languages)) > 1 and "ara" in _configured(languages)
    if not lines:
        return languages, "binary", False
    sample = [w for found in _read_regions(binary, lines, languages, OCR_LETTER_PX).values() for w in found]
    variant = "binary"
    if _mean_conf(sample) < OCR_GOOD_CONF:
        grey = [w for found in _read_regions(gray, lines, languages, OCR_LETTER_PX).values() for w in found]
        if _confident_chars(grey) > _confident_chars(sample):
            sample, variant = grey, "grey"
    info["sample_conf"] = round(_mean_conf(sample), 1)
    langs = languages
    if detect:
        data = {"text": [w["text"] for w in sample], "conf": [w["conf"] for w in sample]}
        langs = choose_languages(*_script_counts(data), languages)
    return langs, variant, True


def ocr_page(page, languages=OCR_LANGUAGES):
    """Read one page -> OcrResult (paragraphs, headings, tables; languages used; details)."""
    dpi = ocr_dpi(page)
    gray, binary, horiz, vert, info = preprocess(_render_gray(page, dpi), dpi)
    rules = cv2.dilate(cv2.bitwise_or(horiz, vert), np.ones((3, 3), np.uint8))
    tables = find_tables(horiz, vert)
    gray[rules > 0] = 255
    binary[rules > 0] = 255

    langs, variant, sampled = _choose_variant_and_languages(gray, binary, languages, info["letter_px"], info)
    words = _words(_data(gray if variant == "grey" else binary, langs))
    if not sampled and _mean_conf(words) < OCR_GOOD_CONF:      # no sample lines: decide on the page
        grey_words = _words(_data(gray, langs))
        if _confident_chars(grey_words) > _confident_chars(words):
            words, variant = grey_words, "grey"
    info["variant"] = variant
    info["conf"] = round(_mean_conf(words), 1)

    keep = (lambda word: True) if "ara" in langs.split("+") or langs == languages \
        else (lambda word: not AR_RE.search(word))   # Latin page: Arabic letters are noise
    sure = [w for w in words if w["conf"] >= 70]          # garbage read in pictures has big boxes
    body_h = _body_height(_group_lines(sure)) or int(1.4 * info["letter_px"]) or max(12, dpi // 12)

    # line-loss guard (+ empty table cells): read what no word covers
    regions = [b for b in _layout_lines(binary, body_h) if not _covered(b, words)]
    regions = sorted(regions, key=lambda b: (b[1], b[0]))[:OCR_MAX_RECOVER]
    recovered = []
    if regions:
        source = gray if info["variant"] == "grey" else binary
        for i, found in sorted(_read_regions(source, regions, langs, max(24, int(body_h))).items()):
            good = [w for w in found if w["conf"] >= 30]
            if _is_clear_text(good):
                for w in good:
                    w["key"] = (-1 - i, 1, 1)           # its own line; placed by position below
                recovered.append(good)
    info["recovered_lines"] = len(recovered)
    words = [w for w in words if keep(w["text"])]
    lines = _group_lines(words)
    for rec in recovered:
        line = _group_lines([w for w in rec if keep(w["text"])])
        if not line:
            continue
        line = line[0]
        at = next((k for k, other in enumerate(lines) if other["y0"] > line["y0"]
                   and min(other["x1"], line["x1"]) > max(other["x0"], line["x0"])), len(lines))
        if 0 < at < len(lines) and lines[at - 1]["key"][0] == lines[at]["key"][0] >= 0 \
                and line["h"] < 1.3 * body_h:
            line["key"] = lines[at - 1]["key"]          # a line skipped inside a paragraph
        lines.insert(at, line)

    _line_sizes(lines)                  # text sizes measured with the table cells still in
    # tables: words inside a clear grid go to its cells
    placed = []
    for table in tables:
        inside = [w for line in lines for w in line["words"] if _in_box(w, table["bbox"])]
        cells = _table_rows(table, inside)
        filled = sum(1 for row in cells for c in row if c)
        if filled < 0.5 * sum(len(row) for row in cells):
            continue                                 # not reliable: keep the text as paragraphs
        rows = [[_cell_text(c) for c in row] for row in cells]
        _reread_number_cells(binary, table, rows, languages)
        lines = [line for line in lines if not _in_box(line, table["bbox"])]
        placed.append((table["bbox"], rows))
    info["tables"] = len(placed)

    paragraphs = build_paragraphs(lines, body_h)
    items = [("heading", p["level"], p["text"]) if p["level"] else ("para", p["text"]) for p in paragraphs]
    for bbox, rows in sorted(placed, key=lambda t: -t[0][1]):     # bottom table first: indexes stay valid
        at = next((k for k, p in enumerate(paragraphs) if p["y0"] > bbox[1]), len(paragraphs))
        items.insert(at, ("table", rows))
    return OcrResult(items, langs, info)


# ---------------------------------------------------------------------------
# 3. Assembly
# ---------------------------------------------------------------------------

def _start_page(document, rect):
    """New page section (same rule as pdf2docx: the first page uses the default section)."""
    if document.paragraphs or len(document.element.body) > 1:
        section = document.add_section(WD_SECTION.NEW_PAGE)
    else:
        section = document.sections[0]
    section.page_width = Pt(rect.width)
    section.page_height = Pt(rect.height)
    for side in ("left_margin", "right_margin", "top_margin", "bottom_margin"):
        setattr(section, side, Pt(min(OCR_MARGIN_PT, rect.width / 8, rect.height / 8)))
    return section


def _write_lines(document, rect, lines):
    _start_page(document, rect)
    for line in lines or [""]:
        document.add_paragraph(line)


def _write_ocr(document, rect, result):
    """OCR result -> paragraphs (Normal / Heading 1 / Heading 2) and tables; no empty paragraphs."""
    _start_page(document, rect)
    for item in result.items:
        if item[0] == "table":
            rows = item[1]
            table = document.add_table(rows=len(rows), cols=len(rows[0]))
            table.style = "Table Grid"
            for r, row in enumerate(rows):
                for c, text in enumerate(row):
                    table.cell(r, c).text = text
        elif item[0] == "heading":
            document.add_paragraph(item[2], style=f"Heading {item[1]}")
        else:
            document.add_paragraph(item[1]).paragraph_format.space_after = Pt(6)


def _write_picture(document, page, reserve=0):
    """The page as a picture, fitted to the page (minus `reserve` EMU of height for a note)."""
    import io
    _start_page(document, page.rect)
    section = document.sections[-1]
    width = section.page_width - section.left_margin - section.right_margin
    height = section.page_height - section.top_margin - section.bottom_margin - reserve
    scale = min(width / page.rect.width, height / page.rect.height)
    png = page.get_pixmap(dpi=KEPT_IMAGE_DPI, alpha=False).tobytes("png")
    # 0.5% smaller: pixel rounding must not push the picture (or the note) to a new page
    document.add_picture(io.BytesIO(png), width=int(page.rect.width * scale * 0.995))


def _write_unreadable(document, page):
    """Page image + one short italic note; the note text is set at the end, in the
    page's language (_note_language). Returns the note paragraph."""
    _write_picture(document, page, reserve=Pt(48))
    note = document.add_paragraph()
    note.add_run(UNREADABLE_NOTE["en"]).italic = True
    return note


def _note_language(page_langs, text, languages=OCR_LANGUAGES):
    """Language of the note for an unreadable page: the page's own script when the
    sample made it clear (Arabic -> "ar"; Latin -> French or English from the
    document's words), else the document's language."""
    if page_langs == "ara":
        return "ar"
    latin = "+".join(lang for lang in _configured(languages) if lang != "ara")
    return _document_language(text, latin_only=page_langs == latin)


def _document_language(text, latin_only=False):
    """"ar", "fr" or "en" from the document's own text (Arabic letters, then
    common French vs English words); English when there is nothing to go on."""
    ar, la = len(AR_RE.findall(text)), len(LATIN_RE.findall(text))
    if ar and ar >= la and not latin_only:
        return "ar"
    words = [w.lower() for w in WORD_RE.findall(text)]
    fr = sum(1 for w in words if w in FRENCH_WORDS)
    en = sum(1 for w in words if w in ENGLISH_WORDS)
    return "fr" if fr > en else "en"


def _norm_words(text):
    text = unicodedata.normalize("NFKC", text).lower()
    return WORD_RE.findall(text)


def _recall(source_words, produced_words):
    if not source_words:
        return 1.0
    produced = {}
    for w in produced_words:
        produced[w] = produced.get(w, 0) + 1
    kept = 0
    for w in source_words:
        if produced.get(w, 0) > 0:
            produced[w] -= 1
            kept += 1
    return kept / len(source_words)


def _text_of(elements):
    return " ".join(t.text or "" for el in elements for t in el.iter(W + "t"))


NO_SPACE_BEFORE = tuple(".,;:!?)]}%،؛؟»")


def _text_blocks(blocks):
    """Text blocks of a pdf2docx layout, including those inside table cells."""
    for block in blocks:
        if block.is_text_block:
            yield block
        elif block.is_table_block:
            for row in block:
                for cell in row:
                    inner = getattr(cell, "blocks", None)
                    if inner:
                        yield from _text_blocks(inner)


def _line_space_patch():
    """pdf2docx joins soft-wrapped lines with nothing in between; lines marked
    by _prepare_lines get a separating space (same formatting as the line)."""
    from pdf2docx.text.Line import Line
    if getattr(Line.make_docx, "_pw_patched", False):
        return
    original = Line.make_docx

    def make_docx(self, p):
        original(self, p)
        if getattr(self, "_pw_space_after", False):
            runs = p.runs
            if runs and runs[-1].text and not runs[-1].text.endswith("\n"):
                runs[-1].text = runs[-1].text + " "
            else:
                p.add_run(" ")

    make_docx._pw_patched = True
    Line.make_docx = make_docx


def _prepare_lines(conv_page):
    """Before writing a parsed page: Arabic rows in right-to-left piece order,
    and a space where two lines join without one."""
    for section in conv_page.sections:
        for column in section:
            for block in _text_blocks(column.blocks):
                rows = block.lines.group_by_physical_rows()
                if mainly_arabic(block.text):
                    ordered = []
                    for row in rows:
                        brk = max((line.line_break for line in row), default=0)
                        row = sorted(row, key=lambda line: -line.bbox[2])
                        for line in row:
                            line.line_break = 0
                        row[-1].line_break = brk
                        ordered.extend(row)
                    block.lines.reset(ordered)
                lines = list(block.lines)
                for line, nxt in zip(lines, lines[1:]):
                    a, b = line.text, nxt.text
                    if (not line.line_break and not nxt.tab_stop and a and b and not a[-1].isspace()
                            and not b[0].isspace() and not a.endswith("-") and not b.startswith(NO_SPACE_BEFORE)):
                        line._pw_space_after = True


def _try_pdf2docx_page(document, conv_page, pdf_page):
    """Write one parsed pdf2docx page; roll back and return a reason on failure."""
    body = document.element.body
    before = len(body) - 1                                 # children before the body sectPr
    saved_sectpr = copy.deepcopy(body[-1]) if body[-1].tag == W + "sectPr" else None
    reason = None
    try:
        _line_space_patch()
        _prepare_lines(conv_page)
        conv_page.make_docx(document)
        added = list(body)[before:len(body) - 1]
        recall = _recall(_norm_words(pdf_page.get_text()), _norm_words(_text_of(added)))
        if recall < MIN_TEXT_RECALL:
            reason = f"low_text_recall_{recall:.2f}"
    except Exception as exc:                               # pdf2docx page error
        reason = type(exc).__name__
    if reason:
        for el in list(body)[before:len(body) - 1]:
            body.remove(el)
        if saved_sectpr is not None:
            body.remove(body[-1])
            body.append(saved_sectpr)
    return reason


def convert_pdf_to_docx(pdf_path, output_path, languages=OCR_LANGUAGES, plan=None, max_ocr_pages=None,
                        deadline=None, progress=None):
    """Convert; return a report {"pages": [...], "counts": {...}}.
    Raises EmptyDocumentError, ScannedPageLimitError, ConversionTimeoutError.
    `plan`: from plan_pdf(). `deadline`: from make_deadline() (None: no limit).
    `progress(pages_done, pages_total)`: called before each page and at the end (jobs.py);
    it may raise to stop the conversion. output_path exists only if the conversion finished."""
    max_ocr = MAX_OCR_PAGES if max_ocr_pages is None else max_ocr_pages
    part_path = str(output_path) + ".part"
    doc = fitz.open(pdf_path)
    conv = None
    try:
        if plan is None:
            plan = []
            for page in doc:
                check_deadline(deadline)
                plan.append(classify_page(page))
        planned_ocr = count_ocr_pages(plan)
        if planned_ocr > max_ocr:
            raise ScannedPageLimitError(planned_ocr, max_ocr)
        ocr_budget = max_ocr - planned_ocr                 # left for pdf2docx fallbacks
        if not plan or all(route == "blank" for route, _ in plan):
            raise EmptyDocumentError("no text, images or drawings")

        text_ids = [i for i, (route, _) in enumerate(plan) if route == "text"]
        parse_errors = {}
        if text_ids:
            from pdf2docx import Converter
            conv = Converter(str(pdf_path))
            settings = conv.default_settings
            settings.update({"ignore_page_error": True})
            conv.load_pages(pages=text_ids)
            # = conv.parse_document(), with the deadline checked page by page
            conv.pages.parse(_DeadlineDoc(conv.fitz_doc, deadline), **settings)
            for i in text_ids:
                check_deadline(deadline)
                try:
                    conv.pages[i].parse(**settings)
                except Exception as exc:
                    parse_errors[i] = type(exc).__name__

        document = Document()
        report = []
        notes = []                                         # notes of pages kept as images
        for i, page in enumerate(doc):
            check_deadline(deadline)
            if progress is not None:
                progress(i, len(plan))
            started = time.monotonic()
            route, info = plan[i]
            entry = {"page": i + 1, "route": route, **info}
            if route == "text":
                reason = parse_errors.get(i)
                if reason is None and not conv.pages[i].finalized:
                    reason = "not_parsed"
                if reason is None:
                    reason = _try_pdf2docx_page(document, conv.pages[i], page)
                if reason is None:
                    entry["outcome"] = "text"
                    entry["seconds"] = round(time.monotonic() - started, 2)
                    report.append(entry)
                    continue
                entry["fallback_reason"] = reason
                if ocr_budget <= 0:
                    _write_lines(document, page.rect, [PLACEHOLDER.format(n=i + 1)])
                    entry["outcome"] = "placeholder"
                    logger.warning("Page %d: pdf2docx failed (%s), OCR page budget used up", i + 1, reason)
                    entry["seconds"] = round(time.monotonic() - started, 2)
                    report.append(entry)
                    continue
                ocr_budget -= 1
                logger.warning("Page %d: pdf2docx failed (%s), using OCR", i + 1, reason)
                route = "ocr"
            if route == "ocr":
                result = ocr_page(page, languages)
                entry["ocr_languages"] = result.langs
                entry["ocr"] = result.info
                if result.has_text() and result.info.get("conf", 100) < OCR_UNREADABLE_CONF:
                    notes.append((_write_unreadable(document, page), result.langs))   # no unreliable text
                    entry["outcome"] = "unreadable"
                    logger.info("Page %d: OCR confidence %.0f, kept as an image with a note",
                                i + 1, result.info.get("conf", 0))
                elif result.has_text():
                    _write_ocr(document, page.rect, result)
                    entry["outcome"] = "ocr_fallback" if "fallback_reason" in entry else "ocr"
                elif info["images"]:
                    _write_picture(document, page)          # a photo without text: keep it
                    entry["outcome"] = "image"
                    logger.info("Page %d: no text found, kept as a picture", i + 1)
                else:
                    _write_lines(document, page.rect, [PLACEHOLDER.format(n=i + 1)])
                    entry["outcome"] = "placeholder"
                    logger.warning("Page %d could not be converted (no text found)", i + 1)
            else:  # blank: keep the page so the page count and order stay the same
                _write_lines(document, page.rect, [""])
                entry["outcome"] = "blank"
            entry["seconds"] = round(time.monotonic() - started, 2)
            report.append(entry)

        if not any(e["outcome"] in ("text", "ocr", "ocr_fallback", "unreadable") for e in report):
            raise EmptyDocumentError("no readable page")
        if notes:
            body = document.element.body
            own = set(id(n._p) for n, _ in notes)
            text = " ".join(t.text or "" for p in body.iter(W + "p") if id(p) not in own for t in p.iter(W + "t"))
            for note, page_langs in notes:
                note.runs[0].text = UNREADABLE_NOTE[_note_language(page_langs, text, languages)]

        check_deadline(deadline)
        fix_arabic(document)
        check_deadline(deadline)
        document.save(part_path)
        os.replace(part_path, output_path)        # never a half-written DOCX
        if progress is not None:
            progress(len(plan), len(plan))
    finally:
        if conv is not None:
            conv.close()
        doc.close()
        if os.path.exists(part_path):
            try:
                os.remove(part_path)
            except OSError:
                logger.warning("Could not remove a partial DOCX")

    counts = {}
    for e in report:
        counts[e["outcome"]] = counts.get(e["outcome"], 0) + 1
    return {"pages": report, "counts": counts}


# ---------------------------------------------------------------------------
# 4. Arabic direction (DOCX post-processing)
# ---------------------------------------------------------------------------

PPR_ORDER = ["pStyle", "keepNext", "keepLines", "pageBreakBefore", "framePr", "widowControl", "numPr",
             "suppressLineNumbers", "pBdr", "shd", "tabs", "suppressAutoHyphens", "kinsoku", "wordWrap",
             "overflowPunct", "topLinePunct", "autoSpaceDE", "autoSpaceDN", "bidi", "adjustRightInd",
             "snapToGrid", "spacing", "ind", "contextualSpacing", "mirrorIndents", "suppressOverlap", "jc",
             "textDirection", "textAlignment", "textboxTightWrap", "outlineLvl", "divId", "cnfStyle", "rPr",
             "sectPr", "pPrChange"]
RPR_ORDER = ["rStyle", "rFonts", "b", "bCs", "i", "iCs", "caps", "smallCaps", "strike", "dstrike", "outline",
             "shadow", "emboss", "imprint", "noProof", "snapToGrid", "vanish", "webHidden", "color", "spacing",
             "w", "kern", "position", "sz", "szCs", "highlight", "u", "effect", "bdr", "shd", "fitText",
             "vertAlign", "rtl", "cs", "em", "lang", "eastAsianLayout", "specVanish", "oMath"]
TBLPR_ORDER = ["tblStyle", "tblpPr", "tblOverlap", "bidiVisual", "tblStyleRowBandSize", "tblStyleColBandSize",
               "tblW", "jc", "tblCellSpacing", "tblInd", "tblBorders", "shd", "tblLayout", "tblCellMar", "tblLook",
               "tblCaption", "tblDescription", "tblPrChange"]


def _get_or_add(parent, name, order):
    """Child w:<name> of parent, created at its schema position if missing."""
    child = parent.find(W + name)
    if child is not None:
        return child
    child = OxmlElement("w:" + name)
    later = set(order[order.index(name) + 1:])
    for i, existing in enumerate(parent):
        if existing.tag.startswith(W) and existing.tag[len(W):] in later:
            parent.insert(i, child)
            return child
    parent.append(child)
    return child


def _first_child(parent, name):
    """w:pPr / w:rPr / w:tblPr / w:tcPr: always the first child of its parent."""
    child = parent.find(W + name)
    if child is None:
        child = OxmlElement("w:" + name)
        parent.insert(0, child)
    return child


def mainly_arabic(text):
    ar = len(AR_RE.findall(text))
    return ar >= 2 and ar >= len(LATIN_RE.findall(text))


def _normalize_presentation_forms(body):
    for t in body.iter(W + "t"):
        if t.text and PRESENTATION_RE.search(t.text):
            t.text = PRESENTATION_RE.sub(lambda m: unicodedata.normalize("NFKC", m.group(0)), t.text)


def _swap_attrs(el, a, b):
    va, vb = el.get(qn("w:" + a)), el.get(qn("w:" + b))
    for name, value in ((a, vb), (b, va)):
        if value is None:
            el.attrib.pop(qn("w:" + name), None)
        else:
            el.set(qn("w:" + name), value)


def _rtl_run(r):
    rpr = _first_child(r, "rPr")
    fonts = _get_or_add(rpr, "rFonts", RPR_ORDER)
    if not fonts.get(qn("w:cs")):
        ascii_font = (fonts.get(qn("w:ascii")) or "").strip()
        fonts.set(qn("w:cs"), ascii_font if ascii_font.lower() in ARABIC_FONTS else DEFAULT_ARABIC_FONT)
    fonts.set(qn("w:hint"), "cs")
    for plain, cs in (("b", "bCs"), ("i", "iCs")):
        src = rpr.find(W + plain)
        if src is not None and rpr.find(W + cs) is None:
            el = _get_or_add(rpr, cs, RPR_ORDER)
            if src.get(qn("w:val")) is not None:
                el.set(qn("w:val"), src.get(qn("w:val")))
    sz = rpr.find(W + "sz")
    if sz is not None:
        _get_or_add(rpr, "szCs", RPR_ORDER).set(qn("w:val"), sz.get(qn("w:val")))
    _get_or_add(rpr, "rtl", RPR_ORDER)
    lang = _get_or_add(rpr, "lang", RPR_ORDER)
    if not lang.get(qn("w:bidi")):
        lang.set(qn("w:bidi"), "ar-SA")


def _ltr_run(r):
    rpr = r.find(W + "rPr")
    if rpr is not None:
        rtl = rpr.find(W + "rtl")
        if rtl is not None:
            rpr.remove(rtl)


def _split_run(r):
    """Split a run holding Arabic and Latin text into script runs. Returns the runs."""
    ts = r.findall(W + "t")
    others = [c for c in r if c.tag not in (W + "rPr", W + "t")]
    if len(ts) != 1 or others:
        return [r]
    text = ts[0].text or ""
    if not (AR_RE.search(text) and LATIN_RE.search(text)):
        return [r]
    pieces, pos = [], 0
    for m in LATIN_CHUNK_RE.finditer(text):
        if m.start() > pos:
            pieces.append(text[pos:m.start()])
        pieces.append(m.group(0))
        pos = m.end()
    if pos < len(text):
        pieces.append(text[pos:])
    # Arabic and Latin words glued together ("الموقعwww") come from the PDF text
    # layer: separate them (not one-letter prefixes such as "و").
    for k in range(len(pieces) - 1):
        left, right = pieces[k], pieces[k + 1]
        lw = re.findall(r"\w+$", left)
        rw = re.findall(r"^\w+", right)
        if lw and rw and len(lw[0]) >= 2 and len(rw[0]) >= 2:
            pieces[k] = left + " "
    runs = []
    parent = r.getparent()
    index = parent.index(r)
    for k, piece in enumerate(pieces):
        new = copy.deepcopy(r)
        t = new.find(W + "t")
        t.text = piece
        t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
        parent.insert(index + k, new)
        runs.append(new)
    parent.remove(r)
    return runs


def _move_leading_punctuation(runs):
    """Word-exported PDFs put an Arabic line's final period at its start (".word")."""
    for r in runs:
        t = r.find(W + "t")
        if t is None or not t.text:
            continue
        m = LEADING_PUNCT_RE.match(t.text)
        # only punctuation glued to the next Arabic word (".word"): a visual-order
        # artifact. "، word" (with a space) is a normal mid-line piece.
        if m and not m.group(3) and AR_RE.match(t.text[m.end():m.end() + 1] or ""):
            t.text = m.group(1) + t.text[m.end():].rstrip() + m.group(2) + (" " if t.text.endswith(" ") else "")
            t.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")


def _rtl_paragraph(p):
    ppr = _first_child(p, "pPr")
    _get_or_add(ppr, "bidi", PPR_ORDER)
    jc = ppr.find(W + "jc")
    if jc is not None and jc.get(qn("w:val")) in ("left", "right", "start", "end"):
        ppr.remove(jc)                                    # start alignment = right in RTL
    ind = ppr.find(W + "ind")
    if ind is not None:
        # pdf2docx measures indents from the physical sides as for LTR text. In a
        # bidi paragraph w:left is the START (right) side: the start indent is the
        # old right indent; the ragged end side gets none.
        start = ind.get(qn("w:right")) or ind.get(qn("w:end"))
        for a in ("left", "right", "start", "end", "firstLine", "hanging", "firstLineChars",
                  "hangingChars", "leftChars", "rightChars", "startChars", "endChars"):
            ind.attrib.pop(qn("w:" + a), None)
        if start and start != "0":
            ind.set(qn("w:left"), start)
    runs = p.xpath(RUNS_XPATH)
    _move_leading_punctuation([r for r in runs if AR_RE.search("".join(t.text or "" for t in r.findall(W + "t")))])
    for r in runs:
        for part in _split_run(r):
            text = "".join(t.text or "" for t in part.findall(W + "t"))
            if LATIN_RE.search(text) and not AR_RE.search(text):
                _ltr_run(part)
            else:
                _rtl_run(part)


def _swap_borders(container):
    if container is None:
        return
    left, right = container.find(W + "left"), container.find(W + "right")
    start, end = container.find(W + "start"), container.find(W + "end")
    for a, b in ((left, right), (start, end)):
        if a is not None and b is not None:
            a.tag, b.tag = b.tag, a.tag
        elif a is not None:
            a.tag = W + {"left": "right", "start": "end"}[a.tag[len(W):]]
        elif b is not None:
            b.tag = W + {"right": "left", "end": "start"}[b.tag[len(W):]]


def _rtl_table(tbl):
    """Mainly-Arabic table: RTL table, same look (cells reversed, left/right swapped)."""
    tblpr = _first_child(tbl, "tblPr")
    if tblpr.find(W + "bidiVisual") is not None:
        return False
    _get_or_add(tblpr, "bidiVisual", TBLPR_ORDER)
    _swap_borders(tblpr.find(W + "tblBorders"))
    _swap_borders(tblpr.find(W + "tblCellMar"))
    grid = tbl.find(W + "tblGrid")
    if grid is not None:
        cols = grid.findall(W + "gridCol")
        for c in cols:
            grid.remove(c)
        for c in reversed(cols):
            grid.append(c)
    for tr in tbl.findall(W + "tr"):
        cells = [c for c in tr if c.tag in (W + "tc", W + "sdt", W + "customXml")]
        if not cells:
            continue
        first = tr.index(cells[0])
        for c in cells:
            tr.remove(c)
        for k, c in enumerate(reversed(cells)):
            tr.insert(first + k, c)
        trpr = tr.find(W + "trPr")
        if trpr is not None:
            before, after = trpr.find(W + "gridBefore"), trpr.find(W + "gridAfter")
            if before is not None:
                before.tag = W + "gridAfter"
            if after is not None:
                after.tag = W + "gridBefore"
        for tc in tr.findall(W + "tc"):
            tcpr = tc.find(W + "tcPr")
            if tcpr is not None:
                _swap_borders(tcpr.find(W + "tcBorders"))
                _swap_borders(tcpr.find(W + "tcMar"))
    return True


def fix_arabic(document):
    """Post-process a python-docx Document in place. Returns counts (for tests/benchmark)."""
    body = document.element.body
    _normalize_presentation_forms(body)
    counts = {"paragraphs": 0, "tables": 0}
    for tbl in list(body.iter(W + "tbl")):
        text = " ".join(t.text or "" for t in tbl.iter(W + "t"))
        if mainly_arabic(text) and _rtl_table(tbl):
            counts["tables"] += 1
    for p in list(body.iter(W + "p")):
        text = "".join(t.text or "" for r in p.xpath(RUNS_XPATH) for t in r.findall(W + "t"))
        if mainly_arabic(text):
            _rtl_paragraph(p)
            counts["paragraphs"] += 1
    return counts
