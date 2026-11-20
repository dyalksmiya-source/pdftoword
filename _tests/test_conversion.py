"""Conversion fix pack 1: per-page routing, no lost page, Arabic direction,
OCR languages, EMPTY_DOCUMENT.

Run from website/:   python -m unittest discover -s _tests -v
OCR tests need Tesseract (eng, fra, ara); they are skipped when it is missing.
"""
import os
import re
import shutil
import sys
import tempfile
import unittest
import zipfile
from pathlib import Path
from unittest import mock

SITE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SITE))
sys.path.insert(0, str(SITE / "_tests"))

import fitz  # noqa: E402
import pytesseract  # noqa: E402
from docx import Document  # noqa: E402
from docx.oxml import OxmlElement  # noqa: E402
from docx.oxml.ns import qn  # noqa: E402

import conversion as C  # noqa: E402

W = C.W


def _setup_tesseract():
    local = Path(r"C:\Program Files\Tesseract-OCR\tesseract.exe")
    if local.exists():
        pytesseract.pytesseract.tesseract_cmd = str(local)
    tessdata = SITE.parent / "quality-output" / "tessdata"     # benchmark copy (eng/fra/ara)
    if tessdata.exists() and not os.environ.get("TESSDATA_PREFIX"):
        os.environ["TESSDATA_PREFIX"] = str(tessdata)
    try:
        langs = set(pytesseract.get_languages(config=""))
        return {"eng", "fra", "ara"} <= langs
    except Exception:
        return False


HAS_OCR = _setup_tesseract()
needs_ocr = unittest.skipUnless(HAS_OCR, "Tesseract with eng+fra+ara not installed")


# ---------------------------------------------------------------- PDF builders
def _text_page(doc, text, fontsize=22):
    page = doc.new_page(width=595, height=842)
    y = 100
    for line in text.split("\n"):
        page.insert_text((72, y), line, fontsize=fontsize)
        y += fontsize * 1.6
    return page


def _image_of_text(doc, text):
    """A scanned page: the text page rendered to an image."""
    tmp = fitz.open()
    _text_page(tmp, text, fontsize=26)
    pix = tmp[0].get_pixmap(dpi=150)
    tmp.close()
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, pixmap=pix)
    return page


def _photo(doc):
    """An image without any text."""
    pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 400, 560), 0)
    pix.set_rect(pix.irect, (180, 190, 200))
    page = doc.new_page(width=595, height=842)
    page.insert_image(page.rect, pixmap=pix)
    return page


class Tmp(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="pw-conv-"))

    def tearDown(self):
        shutil.rmtree(self.dir, ignore_errors=True)

    def pdf(self, build):
        doc = fitz.open()
        build(doc)
        path = self.dir / f"in{len(list(self.dir.iterdir()))}.pdf"
        doc.save(path)
        doc.close()
        return path

    def convert(self, pdf):
        out = self.dir / (pdf.stem + ".docx")
        report = C.convert_pdf_to_docx(str(pdf), str(out))
        return report, out

    @staticmethod
    def docx_text(path):
        return "\n".join(p.text for p in Document(path).paragraphs)

    @staticmethod
    def docx_xml(path):
        with zipfile.ZipFile(path) as z:
            return z.read("word/document.xml").decode("utf-8")


# ---------------------------------------------------------------- 1. routing
class Classification(Tmp):
    def classify(self, build):
        doc = fitz.open()
        build(doc)
        route, info = C.classify_page(doc[0])
        doc.close()
        return route, info

    def test_text_page(self):
        self.assertEqual(self.classify(lambda d: _text_page(d, "A normal text page with enough words."))[0], "text")

    def test_blank_page(self):
        self.assertEqual(self.classify(lambda d: d.new_page())[0], "blank")

    def test_scanned_page(self):
        self.assertEqual(self.classify(lambda d: _image_of_text(d, "Scanned words"))[0], "ocr")

    def test_invisible_ocr_layer_counts_as_scanned(self):
        def build(d):
            page = _image_of_text(d, "Scanned words")
            page.insert_text((72, 100), "hidden text layer from an old OCR " * 3, render_mode=3)
        route, info = self.classify(build)
        self.assertEqual(route, "ocr")
        self.assertEqual(info["chars"], 0)

    def test_big_image_with_little_text_is_ocr(self):
        def build(d):
            page = _image_of_text(d, "Content in the image")
            page.insert_text((72, 40), "Header line 1 of 9")
        self.assertEqual(self.classify(build)[0], "ocr")

    def test_text_page_with_a_figure_stays_text(self):
        def build(d):
            page = _text_page(d, "\n".join(["A long line of real text in this report."] * 14), fontsize=12)
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 100, 100), 0)
            page.insert_image(fitz.Rect(72, 500, 520, 800), pixmap=pix)
        self.assertEqual(self.classify(build)[0], "text")

    def test_small_images_without_text_go_to_pdf2docx(self):
        def build(d):
            page = d.new_page()
            pix = fitz.Pixmap(fitz.csRGB, fitz.IRect(0, 0, 50, 50), 0)
            page.insert_image(fitz.Rect(50, 50, 150, 150), pixmap=pix)      # a logo
        self.assertEqual(self.classify(build)[0], "text")

    def test_vector_only_page_is_ocr(self):
        def build(d):
            page = d.new_page()
            page.draw_rect(fitz.Rect(50, 50, 200, 200), color=(0, 0, 0))
        self.assertEqual(self.classify(build)[0], "ocr")


@needs_ocr
class PerPageRouting(Tmp):
    def test_text_scan_text_in_page_order(self):
        pdf = self.pdf(lambda d: (_text_page(d, "First page alpha words here."),
                                  _image_of_text(d, "Second page beta"),
                                  _text_page(d, "Third page gamma words here.")))
        report, out = self.convert(pdf)
        self.assertEqual([p["outcome"] for p in report["pages"]], ["text", "ocr", "text"])
        self.assertEqual(report["pages"][1]["ocr_languages"], "eng+fra")
        text = self.docx_text(out).lower()
        positions = [text.find(w) for w in ("alpha", "beta", "gamma")]
        self.assertTrue(all(p >= 0 for p in positions), text)
        self.assertEqual(positions, sorted(positions))
        self.assertEqual(self.docx_xml(out).count("<w:sectPr"), 3)      # one section per page


# ---------------------------------------------------------------- 2. never lose a page
@needs_ocr
class NeverLoseAPage(Tmp):
    def three_pages(self):
        return self.pdf(lambda d: (_text_page(d, "First page alpha words here."),
                                   _text_page(d, "Second page beta words here."),
                                   _text_page(d, "Third page gamma words here.")))

    def test_pdf2docx_crash_after_partial_write_is_rolled_back_and_ocrd(self):
        from pdf2docx.page.Page import Page
        original = Page.make_docx

        def flaky(page, doc):
            original(page, doc)
            if page.id == 1:
                raise IndexError("list index out of range")

        with mock.patch.object(Page, "make_docx", flaky), self.assertLogs("pdf2wordly.conversion", "WARNING") as logs:
            report, out = self.convert(self.three_pages())
        self.assertEqual([p["outcome"] for p in report["pages"]], ["text", "ocr_fallback", "text"])
        self.assertEqual(report["pages"][1]["fallback_reason"], "IndexError")
        text = self.docx_text(out).lower()
        self.assertEqual(text.count("beta"), 1)                          # partial write removed
        self.assertEqual(self.docx_xml(out).count("<w:sectPr"), 3)
        self.assertNotIn("beta", " ".join(logs.output).lower())          # no content in logs

    def test_page_where_pdf2docx_loses_the_text_falls_back_to_ocr(self):
        from pdf2docx.page.Page import Page
        original = Page.make_docx

        def loses_text(page, doc):
            if page.id == 1:
                doc.add_paragraph("")                                    # "success" with nothing
                return
            original(page, doc)

        with mock.patch.object(Page, "make_docx", loses_text):
            report, out = self.convert(self.three_pages())
        self.assertEqual(report["pages"][1]["outcome"], "ocr_fallback")
        self.assertTrue(report["pages"][1]["fallback_reason"].startswith("low_text_recall"))
        self.assertIn("beta", self.docx_text(out).lower())

    def test_parse_error_falls_back_to_ocr(self):
        from pdf2docx.page.Page import Page
        original = Page.parse

        def bad_parse(page, **kw):
            if page.id == 2:
                raise ValueError("bad page")
            return original(page, **kw)

        with mock.patch.object(Page, "parse", bad_parse):
            report, out = self.convert(self.three_pages())
        self.assertEqual(report["pages"][2]["outcome"], "ocr_fallback")
        self.assertIn("gamma", self.docx_text(out).lower())

    def test_photo_without_text_is_kept_as_a_picture(self):
        pdf = self.pdf(lambda d: (_text_page(d, "First page alpha words here."), _photo(d)))
        report, out = self.convert(pdf)
        self.assertEqual([p["outcome"] for p in report["pages"]], ["text", "image"])
        with zipfile.ZipFile(out) as z:
            self.assertTrue(any(n.startswith("word/media/") for n in z.namelist()))

    def test_page_without_any_text_gets_a_placeholder(self):
        def outlines(d):
            page = d.new_page(width=595, height=842)
            page.draw_rect(fitz.Rect(100, 100, 300, 300), color=(0, 0, 0))   # vector only, no text
        pdf = self.pdf(lambda d: (_text_page(d, "First page alpha words here."), outlines(d)))
        with self.assertLogs("pdf2wordly.conversion", "WARNING") as logs:
            report, out = self.convert(pdf)
        self.assertEqual([p["outcome"] for p in report["pages"]], ["text", "placeholder"])
        self.assertIn("[Page 2 could not be converted]", self.docx_text(out))
        self.assertIn("Page 2 could not be converted", " ".join(logs.output))
        self.assertNotIn("alpha", " ".join(logs.output).lower())

    def test_blank_page_inside_a_document_is_kept(self):
        pdf = self.pdf(lambda d: (_text_page(d, "First page alpha words here."), d.new_page(),
                                  _text_page(d, "Third page gamma words here.")))
        report, out = self.convert(pdf)
        self.assertEqual([p["outcome"] for p in report["pages"]], ["text", "blank", "text"])
        self.assertEqual(self.docx_xml(out).count("<w:sectPr"), 3)


# ---------------------------------------------------------------- 5. empty documents
class EmptyDocument(Tmp):
    def test_blank_pdf(self):
        pdf = self.pdf(lambda d: (d.new_page(), d.new_page()))
        with self.assertRaises(C.EmptyDocumentError):
            self.convert(pdf)
        self.assertFalse((self.dir / (pdf.stem + ".docx")).exists())

    @needs_ocr
    def test_images_without_text(self):
        pdf = self.pdf(lambda d: (_photo(d), _photo(d)))
        with self.assertRaises(C.EmptyDocumentError):
            self.convert(pdf)


class EmptyDocumentApi(unittest.TestCase):
    """EMPTY_DOCUMENT: 422, clear message, quota released (Free and Pro)."""

    def runner(self):
        import server  # noqa: F401
        import test_billing

        class Run(test_billing.ClaimCompat, unittest.TestCase):
            def runTest(self):
                pass

        Run.server = sys.modules["server"]
        Run.run_pro = test_billing.ClaimCompatNewServerTests.run_pro
        return Run()

    def test_free_user_keeps_the_quota(self):
        r = self.runner()
        code, body, calls = r.run_convert(r.free(), {"allowed": True, "reason": "first_conversion", "previous": None},
                                          convert_error=C.EmptyDocumentError("x"))
        self.assertEqual(code, 422)
        import json
        data = json.loads(body)
        self.assertEqual(data["code"], "EMPTY_DOCUMENT")
        self.assertIn("couldn't find any text", data["error"])
        self.assertEqual(calls[-1], ("release_free_conversion", {"p_user_id": mock.ANY, "p_previous": None}))

    def test_pro_user_keeps_the_fair_use_slot(self):
        r = self.runner()
        code, body, calls = r.run_pro({"allowed": True, "claim_id": 7}, convert_error=C.EmptyDocumentError("x"))
        self.assertEqual(code, 422)
        self.assertEqual(calls[-1][0], "release_pro_conversion")

    def test_site_shows_the_message_in_three_languages(self):
        js = (SITE / "script.js").read_text(encoding="utf-8")
        self.assertIn('case"EMPTY_DOCUMENT":return pwT("We couldn\'t find any text in this PDF.', js)
        self.assertIn("Nous n'avons trouvé aucun texte dans ce PDF.", js)
        self.assertIn("لم نعثر على أي نص في ملف PDF هذا.", js)


# ---------------------------------------------------------------- 4. OCR languages
class OcrLanguages(unittest.TestCase):
    def test_choose(self):
        ch = C.choose_languages
        self.assertEqual(ch(500, 3), "ara")
        self.assertEqual(ch(4, 800), "eng+fra")
        self.assertEqual(ch(300, 300), "eng+fra+ara")
        self.assertEqual(ch(2, 3), "eng+fra+ara")                    # too little evidence
        self.assertEqual(ch(500, 0, "eng+fra"), "eng+fra")           # Arabic not configured
        self.assertEqual(ch(500, 0, "ara+eng"), "ara")

    def test_lines_from_data_drop_noise_words(self):
        data = {"text": ["Hello", "world", "لا", "", "Next", "line"],
                "block_num": [1, 1, 1, 1, 1, 2], "par_num": [1, 1, 1, 1, 1, 1], "line_num": [1, 1, 1, 1, 2, 1]}
        lines = C._lines_from_data(data, lambda w: not C.AR_RE.search(w))
        self.assertEqual(lines, ["Hello world", "Next", "", "line"])

    @needs_ocr
    @unittest.skipUnless(Path(r"C:\Windows\Fonts\arial.ttf").exists(), "Arial (Arabic glyphs) not available")
    def test_arabic_scan_is_ocrd_with_arabic_only(self):
        src = fitz.open()
        page = src.new_page(width=595, height=842)
        page.insert_htmlbox(fitz.Rect(60, 60, 535, 600),
                            '<p dir="rtl" style="font-size:15pt">تقدم الجمعية في هذا التقرير حصيلة أنشطتها '
                            'خلال السنة الماضية وتعرض النتائج التي حققتها في مجالات التعليم والصحة</p>',
                            css="@font-face {font-family: ar; src: url(arial.ttf);} * {font-family: ar;}",
                            archive=fitz.Archive(r"C:\Windows\Fonts"))
        pix = page.get_pixmap(dpi=150)
        doc = fitz.open()
        doc.new_page(width=595, height=842).insert_image(fitz.Rect(0, 0, 595, 842), pixmap=pix)
        lines, langs = C.ocr_page(doc[0])
        self.assertEqual(langs, "ara")
        text = " ".join(lines)
        self.assertIn("الجمعية", text)
        self.assertFalse(C.LATIN_RE.search(text), text)

    @needs_ocr
    def test_latin_scan_is_ocrd_without_arabic(self):
        doc = fitz.open()
        _image_of_text(doc, "The quick brown fox jumps\nover the lazy dog again")
        lines, langs = C.ocr_page(doc[0])
        doc.close()
        self.assertEqual(langs, "eng+fra")
        self.assertFalse(any(C.AR_RE.search(line) for line in lines))


# ---------------------------------------------------------------- 3. Arabic direction
def _para(doc, text, jc=None, ind=None, size=None, font=None, bold=False):
    p = doc.add_paragraph()
    r = p.add_run(text)
    ppr = p._p.get_or_add_pPr()
    if ind:
        el = OxmlElement("w:ind")
        for k, v in ind.items():
            el.set(qn("w:" + k), str(v))
        ppr.append(el)
    if jc:
        el = OxmlElement("w:jc")
        el.set(qn("w:val"), jc)
        ppr.append(el)
    rpr = r._r.get_or_add_rPr()
    if font:
        f = OxmlElement("w:rFonts")
        f.set(qn("w:ascii"), font)
        f.set(qn("w:hAnsi"), font)
        rpr.append(f)
    if bold:
        rpr.append(OxmlElement("w:b"))
    if size:
        sz = OxmlElement("w:sz")
        sz.set(qn("w:val"), str(size))
        rpr.append(sz)
    return p


def _children(el):
    return [c.tag[len(W):] for c in el if c.tag.startswith(W)]


class ArabicDirection(unittest.TestCase):
    def assert_schema_order(self, el, order):
        names = _children(el)
        idx = [order.index(n) for n in names]
        self.assertEqual(idx, sorted(idx), names)

    def test_arabic_paragraph(self):
        d = Document()
        p = _para(d, "تقرير النشاط السنوي للجمعية", jc="right", ind={"left": 400, "right": 120, "firstLine": 30},
                  size=26, font="Tahoma", bold=True)
        counts = C.fix_arabic(d)
        self.assertEqual(counts["paragraphs"], 1)
        ppr = p._p.pPr
        self.assertIsNotNone(ppr.find(W + "bidi"))
        self.assertIsNone(ppr.find(W + "jc"))                      # start = right in an RTL paragraph
        ind = ppr.find(W + "ind")
        self.assertEqual(ind.get(qn("w:left")), "120")              # old right (physical) -> start
        self.assertIsNone(ind.get(qn("w:right")))
        self.assertIsNone(ind.get(qn("w:firstLine")))
        rpr = p.runs[0]._r.rPr
        self.assertIsNotNone(rpr.find(W + "rtl"))
        self.assertEqual(rpr.find(W + "rFonts").get(qn("w:cs")), "Tahoma")
        self.assertEqual(rpr.find(W + "szCs").get(qn("w:val")), "26")
        self.assertIsNotNone(rpr.find(W + "bCs"))
        self.assert_schema_order(ppr, C.PPR_ORDER)
        self.assert_schema_order(rpr, C.RPR_ORDER)

    def test_centered_and_justified_alignment_kept(self):
        d = Document()
        a = _para(d, "عنوان في الوسط", jc="center")
        b = _para(d, "فقرة مضبوطة من الجانبين", jc="both")
        C.fix_arabic(d)
        self.assertEqual(a._p.pPr.find(W + "jc").get(qn("w:val")), "center")
        self.assertEqual(b._p.pPr.find(W + "jc").get(qn("w:val")), "both")

    def test_unknown_font_gets_an_arabic_cs_font(self):
        d = Document()
        p = _para(d, "نص عربي", font="SomeLatinOnlyFont")
        C.fix_arabic(d)
        self.assertEqual(p.runs[0]._r.rPr.find(W + "rFonts").get(qn("w:cs")), "Arial")

    def test_latin_paragraph_untouched(self):
        d = Document()
        p = _para(d, "Compte rendu de la réunion (version 2)", jc="left", ind={"left": 300}, size=24)
        before = p._p.xml
        C.fix_arabic(d)
        self.assertEqual(p._p.xml, before)

    def test_mixed_run_keeps_latin_ltr(self):
        d = Document()
        p = _para(d, "يمكن التسجيل عبر منصة Google Forms قبل نهاية الشهر", size=24)
        C.fix_arabic(d)
        runs = [(r.text, r._r.rPr.find(W + "rtl") is not None) for r in p.runs]
        self.assertEqual("".join(t for t, _ in runs), "يمكن التسجيل عبر منصة Google Forms قبل نهاية الشهر")
        self.assertIn(("Google Forms", False), runs)
        self.assertTrue(all(rtl for t, rtl in runs if C.AR_RE.search(t)))
        for r in p.runs:
            self.assertEqual(r._r.rPr.find(W + "sz").get(qn("w:val")), "24")   # formatting copied

    def test_glued_arabic_and_latin_words_are_separated(self):
        d = Document()
        p = _para(d, "يمكن التسجيل عبر الموقعwww.example.ma أو بالهاتف")
        C.fix_arabic(d)
        self.assertEqual("".join(r.text for r in p.runs), "يمكن التسجيل عبر الموقع www.example.ma أو بالهاتف")

    def test_one_letter_prefix_not_separated(self):
        d = Document()
        p = _para(d, "استعمل وGoogle للبحث في الشبكة")
        C.fix_arabic(d)
        self.assertIn("وGoogle", "".join(r.text for r in p.runs))

    def test_presentation_forms_normalized(self):
        d = Document()
        p = _para(d, "ﺗﻘﺭﻳﺭ ﺍﻟﻧﺷﺎﻁ ﻻ")
        C.fix_arabic(d)
        self.assertEqual(p.text, "تقرير النشاط لا")

    def test_visual_order_period_moved_to_the_end(self):
        d = Document()
        a = _para(d, " .المرتفقين")
        b = _para(d, "، وتمت مناقشة البرنامج")
        C.fix_arabic(d)
        self.assertEqual(a.text.strip(), "المرتفقين.")
        self.assertEqual(b.text, "، وتمت مناقشة البرنامج")          # normal mid-line piece: unchanged

    def test_arabic_table_becomes_rtl_with_the_same_look(self):
        d = Document()
        t = d.add_table(rows=2, cols=3)
        rows = [["الكتب", "الورشات", "المدينة"], ["1200", "40", "الرباط"]]   # visual left-to-right (pdf2docx)
        for r, row in enumerate(rows):
            for c, text in enumerate(row):
                t.cell(r, c).text = text
        widths = []
        for k, col in enumerate(t._tbl.tblGrid.findall(W + "gridCol")):
            col.set(qn("w:w"), str(1000 + k))
            widths.append(str(1000 + k))
        first = t.cell(0, 0)._tc.get_or_add_tcPr()
        borders = OxmlElement("w:tcBorders")
        left = OxmlElement("w:left")
        left.set(qn("w:val"), "double")
        borders.append(left)
        first.append(borders)
        counts = C.fix_arabic(d)
        self.assertEqual(counts["tables"], 1)
        tblpr = t._tbl.tblPr
        self.assertIsNotNone(tblpr.find(W + "bidiVisual"))
        self.assert_schema_order(tblpr, C.TBLPR_ORDER)
        self.assertEqual([c.text for c in t.rows[0].cells], ["المدينة", "الورشات", "الكتب"])   # logical order
        self.assertEqual([c.get(qn("w:w")) for c in t._tbl.tblGrid.findall(W + "gridCol")], widths[::-1])
        moved = t.rows[0].cells[2]._tc.tcPr.find(W + "tcBorders")
        self.assertIsNotNone(moved.find(W + "right"))               # physical left == logical end
        self.assertIsNone(moved.find(W + "left"))
        self.assertEqual(C.fix_arabic(d)["tables"], 0)              # idempotent

    def test_latin_table_untouched(self):
        d = Document()
        t = d.add_table(rows=1, cols=2)
        t.cell(0, 0).text, t.cell(0, 1).text = "Name", "Total"
        C.fix_arabic(d)
        self.assertIsNone(t._tbl.tblPr.find(W + "bidiVisual"))
        self.assertEqual([c.text for c in t.rows[0].cells], ["Name", "Total"])

    def test_mainly_arabic(self):
        self.assertTrue(C.mainly_arabic("ملاحظة: Les"))
        self.assertFalse(C.mainly_arabic("ملاحظة: Les documents en français"))
        self.assertFalse(C.mainly_arabic("1200"))



# ---------------------------------------------------------------- scanned page cap
class ScannedPageCap(Tmp):
    def scans(self, n, text_pages=0):
        def build(d):
            for k in range(text_pages):
                _text_page(d, f"Text page {k} with enough words.")
            for k in range(n):
                _image_of_text(d, f"Scan {k}")
        return self.pdf(build)

    def test_plan_counts_scanned_pages(self):
        plan = C.plan_pdf(str(self.scans(3, text_pages=2)))
        self.assertEqual(len(plan), 5)
        self.assertEqual(C.count_ocr_pages(plan), 3)

    def test_conversion_refuses_more_scanned_pages_than_the_cap_without_ocr(self):
        pdf = self.scans(3)
        with mock.patch.object(pytesseract, "image_to_data") as ocr, self.assertRaises(C.ScannedPageLimitError) as cm:
            C.convert_pdf_to_docx(str(pdf), str(self.dir / "o.docx"), max_ocr_pages=2)
        self.assertEqual((cm.exception.scanned_pages, cm.exception.limit), (3, 2))
        ocr.assert_not_called()
        self.assertFalse((self.dir / "o.docx").exists())

    def test_fallback_ocr_shares_the_budget(self):
        from pdf2docx.page.Page import Page
        pdf = self.pdf(lambda d: (_text_page(d, "First page alpha words here."),
                                  _text_page(d, "Second page beta words here.")))

        def fails(page, doc):
            raise IndexError("x")

        with mock.patch.object(Page, "make_docx", fails), mock.patch.object(pytesseract, "image_to_data") as ocr,                 self.assertLogs("pdf2wordly.conversion", "WARNING") as logs:
            with self.assertRaises(C.EmptyDocumentError):        # budget 0: both pages placeholders
                C.convert_pdf_to_docx(str(pdf), str(self.dir / "o.docx"), max_ocr_pages=0)
        ocr.assert_not_called()
        self.assertIn("OCR page budget used up", " ".join(logs.output))

    @needs_ocr
    def test_fallback_beyond_the_budget_gets_a_placeholder(self):
        from pdf2docx.page.Page import Page
        original = Page.make_docx
        pdf = self.pdf(lambda d: (_text_page(d, "First page alpha words here."),
                                  _text_page(d, "Second page beta words here."),
                                  _text_page(d, "Third page gamma words here.")))

        def fails(page, doc):
            if page.id in (1, 2):
                raise IndexError("x")
            original(page, doc)

        with mock.patch.object(Page, "make_docx", fails):
            report = C.convert_pdf_to_docx(str(pdf), str(self.dir / "o.docx"), max_ocr_pages=1)
        self.assertEqual([p["outcome"] for p in report["pages"]], ["text", "ocr_fallback", "placeholder"])
        self.assertIn("[Page 3 could not be converted]", self.docx_text(self.dir / "o.docx"))

    def test_default_cap_is_the_site_config_number_and_env_overrides_it(self):
        import json
        import subprocess
        cfg = json.loads((SITE / "_tools" / "site-config.json").read_text(encoding="utf-8"))
        self.assertEqual(str(C.MAX_OCR_PAGES), cfg["scanned-pages-limit"])
        env = dict(os.environ, MAX_OCR_PAGES="7")
        out = subprocess.run([sys.executable, "-c", "import conversion; print(conversion.MAX_OCR_PAGES)"],
                             cwd=str(SITE), env=env, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "7", out.stderr)   # PyMuPDF may print a notice first


class ScannedPageCapApi(unittest.TestCase):
    """413 SCANNED_PAGE_LIMIT before converting; numbers in the answer; quota released."""

    def runner(self):
        return EmptyDocumentApi.runner(self)

    def run_with_plan(self, plan, pro=False):
        import json
        r = self.runner()
        srv = sys.modules["server"]
        with mock.patch.object(srv.conversion, "plan_pdf", return_value=plan),                 mock.patch.object(srv.conversion, "MAX_OCR_PAGES", 30):
            if pro:
                code, body, calls = r.run_pro({"allowed": True, "claim_id": 9})
            else:
                code, body, calls = r.run_convert(r.free(), {"allowed": True, "reason": "first_conversion",
                                                             "previous": None})
        return code, (json.loads(body) if code != 200 else None), calls

    def test_pro_scan_over_the_cap_is_refused_and_the_slot_released(self):
        code, data, calls = self.run_with_plan([("ocr", {})] * 31, pro=True)
        self.assertEqual(code, 413)
        self.assertEqual(data["code"], "SCANNED_PAGE_LIMIT")
        self.assertEqual((data["scanned_pages"], data["max_scanned_pages"]), (31, 30))
        self.assertIn("Split the PDF or convert the first 30 pages", data["error"])
        self.assertEqual(calls[-1][0], "release_pro_conversion")

    def test_free_claim_released(self):
        code, data, calls = self.run_with_plan([("ocr", {})] * 31)
        self.assertEqual(code, 413)
        self.assertEqual(calls[-1][0], "release_free_conversion")

    def test_conversion_not_started(self):
        r = self.runner()
        srv = sys.modules["server"]
        with mock.patch.object(srv.conversion, "plan_pdf", return_value=[("ocr", {})] * 31):
            # a started conversion would raise here and answer 500
            code, _, _ = r.run_pro({"allowed": True, "claim_id": 9},
                                   convert_error=AssertionError("conversion must not start"))
        self.assertEqual(code, 413)

    def test_exactly_the_cap_and_text_pages_are_accepted(self):
        code, _, _ = self.run_with_plan([("text", {})] * 170 + [("ocr", {})] * 30, pro=True)
        self.assertEqual(code, 200)

    def test_site_message_in_three_languages(self):
        js = (SITE / "script.js").read_text(encoding="utf-8")
        self.assertIn('case"SCANNED_PAGE_LIMIT":return pwScannedMsg(x);', js)
        self.assertIn("S=e.scanned_pages,X=e.max_scanned_pages", js)
        self.assertIn("Divisez le PDF ou convertissez les {max} premières pages.", js)
        self.assertIn("قسّم الملف أو حوّل أول {max} صفحة.", js)


if __name__ == "__main__":
    unittest.main()
