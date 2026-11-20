"""PDF -> DOCX conversion (used by server.py).

1. Page-by-page routing: every page is classified from its visible text layer
   and image coverage as "text" (pdf2docx), "ocr" (Tesseract) or "blank".
2. One DOCX in the original page order: pdf2docx pages and OCR pages are
   written into the same python-docx Document. A pdf2docx page that fails or
   loses most of its text is rolled back and redone with OCR; a page that
   still yields nothing gets a placeholder line. No page is ever dropped.
3. OCR languages per page: one pass with all configured languages gives the
   page's script from its confident words. Latin page: Arabic-letter noise
   words are dropped (no second pass). Arabic page: OCR'd again with "ara"
   only (clean Arabic). Mixed page: the first pass is kept.
4. Arabic: paragraphs that are mainly Arabic get w:bidi, start (right)
   alignment and RTL runs (w:rtl, complex-script font and size); Latin words
   inside them stay in LTR runs. Mainly-Arabic tables become RTL tables
   (w:bidiVisual). Arabic presentation forms are normalized to letters.
5. A PDF with nothing readable raises EmptyDocumentError.
6. At most MAX_OCR_PAGES pages are OCR'd per file (each costs 2-5 s): the
   server refuses bigger scans before converting (ScannedPageLimitError), and
   OCR fallbacks share the same budget (beyond it: placeholder line).

Logs never contain document text.
"""
import copy
import logging
import os
import re
import unicodedata

import fitz  # PyMuPDF
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
OCR_ZOOM = 2.0
OCR_MIN_CONF = 60                # confident words used to detect the script
KEPT_IMAGE_DPI = 150             # page with no text found: kept as a picture
OCR_LANGUAGES = os.environ.get("OCR_LANGUAGES", "eng+fra+ara")
PLACEHOLDER = "[Page {n} could not be converted]"
# Same number as "scanned-pages-limit" in _tools/site-config.json (checked by tests).
MAX_OCR_PAGES = int(os.environ.get("MAX_OCR_PAGES", "30"))
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


def plan_pdf(pdf_path):
    """Route of every page: [(route, info), ...] (classify_page)."""
    doc = fitz.open(pdf_path)
    try:
        return [classify_page(page) for page in doc]
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
# ---------------------------------------------------------------------------

def _render(page, zoom):
    pix = page.get_pixmap(matrix=fitz.Matrix(zoom, zoom), alpha=False)
    return Image.frombytes("RGB", (pix.width, pix.height), pix.samples)


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


def _lines_from_data(data, keep_word):
    """Rebuild text lines from image_to_data (blank line between paragraphs)."""
    lines, current, last_line, last_par = [], [], None, None
    for k, text in enumerate(data["text"]):
        if not text or not text.strip() or not keep_word(text):
            continue
        par = (data["block_num"][k], data["par_num"][k])
        line = par + (data["line_num"][k],)
        if line != last_line:
            if current:
                lines.append(" ".join(current))
            if last_par is not None and par != last_par:
                lines.append("")
            current, last_line, last_par = [], line, par
        current.append(text.strip())
    if current:
        lines.append(" ".join(current))
    return lines


def _arabic_pass(img, langs):
    """Arabic-only OCR. Tesseract's automatic layout (psm 3) sometimes drops
    whole lines of an Arabic paragraph; psm 6 (one text block) is kept only if
    it reads clearly more (a multi-column page reads the same words in both
    modes, so it keeps psm 3 and its column order)."""
    auto = pytesseract.image_to_data(img, lang=langs, output_type=pytesseract.Output.DICT)
    block = pytesseract.image_to_data(img, lang=langs, config="--psm 6", output_type=pytesseract.Output.DICT)
    if _script_counts(block)[0] > 1.15 * _script_counts(auto)[0]:
        return block
    return auto


def ocr_page(page, languages=OCR_LANGUAGES):
    """Return (lines, languages_used)."""
    img = _render(page, OCR_ZOOM)
    data = pytesseract.image_to_data(img, lang=languages, output_type=pytesseract.Output.DICT)
    langs = choose_languages(*_script_counts(data), languages)
    keep = lambda word: True  # noqa: E731
    if langs != languages:
        if "ara" in langs.split("+"):
            data = _arabic_pass(img, langs)
        else:
            keep = lambda word: not AR_RE.search(word)  # noqa: E731  Latin page: Arabic is noise
    lines = _lines_from_data(data, keep)
    while lines and not lines[-1].strip():
        lines.pop()
    return lines, langs


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


def _write_picture(document, page):
    import io
    _start_page(document, page.rect)
    section = document.sections[-1]
    width = section.page_width - section.left_margin - section.right_margin
    height = section.page_height - section.top_margin - section.bottom_margin
    scale = min(width / page.rect.width, height / page.rect.height)
    png = page.get_pixmap(dpi=KEPT_IMAGE_DPI, alpha=False).tobytes("png")
    document.add_picture(io.BytesIO(png), width=int(page.rect.width * scale))


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


def convert_pdf_to_docx(pdf_path, output_path, languages=OCR_LANGUAGES, plan=None, max_ocr_pages=None):
    """Convert; return a report {"pages": [...], "counts": {...}}.
    Raises EmptyDocumentError, ScannedPageLimitError. `plan`: from plan_pdf()."""
    max_ocr = MAX_OCR_PAGES if max_ocr_pages is None else max_ocr_pages
    doc = fitz.open(pdf_path)
    conv = None
    try:
        if plan is None:
            plan = [classify_page(page) for page in doc]
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
            conv.load_pages(pages=text_ids).parse_document(**settings)
            for i in text_ids:
                try:
                    conv.pages[i].parse(**settings)
                except Exception as exc:
                    parse_errors[i] = type(exc).__name__

        document = Document()
        report = []
        for i, page in enumerate(doc):
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
                    report.append(entry)
                    continue
                entry["fallback_reason"] = reason
                if ocr_budget <= 0:
                    _write_lines(document, page.rect, [PLACEHOLDER.format(n=i + 1)])
                    entry["outcome"] = "placeholder"
                    logger.warning("Page %d: pdf2docx failed (%s), OCR page budget used up", i + 1, reason)
                    report.append(entry)
                    continue
                ocr_budget -= 1
                logger.warning("Page %d: pdf2docx failed (%s), using OCR", i + 1, reason)
                route = "ocr"
            if route == "ocr":
                lines, langs = ocr_page(page, languages)
                entry["ocr_languages"] = langs
                if any(line.strip() for line in lines):
                    _write_lines(document, page.rect, lines)
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
            report.append(entry)

        if not any(e["outcome"] in ("text", "ocr", "ocr_fallback") for e in report):
            raise EmptyDocumentError("no readable page")

        fix_arabic(document)
        document.save(output_path)
    finally:
        if conv is not None:
            conv.close()
        doc.close()

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
