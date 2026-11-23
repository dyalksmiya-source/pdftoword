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

    def test_words_drop_direction_marks_and_empty_entries(self):
        data = {"text": ["Hello‏", "", "  ", "‎world"], "conf": ["91", "-1", "-1", "88"],
                "left": [10, 0, 0, 80], "top": [5, 0, 0, 5], "width": [60, 0, 0, 70], "height": [20, 0, 0, 20],
                "block_num": [1, 1, 1, 1], "par_num": [1, 1, 1, 1], "line_num": [1, 1, 1, 1]}
        words = C._words(data)
        self.assertEqual([w["text"] for w in words], ["Hello", "world"])
        self.assertEqual((words[1]["x0"], words[1]["x1"]), (80, 150))

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
        result = C.ocr_page(doc[0])
        self.assertEqual(result.langs, "ara")
        text = result.text()
        self.assertIn("الجمعية", text)
        self.assertFalse(C.LATIN_RE.search(text), text)

    @needs_ocr
    def test_latin_scan_is_ocrd_without_arabic(self):
        doc = fitz.open()
        _image_of_text(doc, "The quick brown fox jumps\nover the lazy dog again")
        result = C.ocr_page(doc[0])
        doc.close()
        self.assertEqual(result.langs, "eng+fra")
        self.assertFalse(C.AR_RE.search(result.text()), result.text())
        self.assertIn("quick brown fox", result.text())


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
        # /api/convert keeps its own cap (30); the site shows the job caps (jobs.py, Pro 50 / Free 20).
        import jobs
        self.assertEqual(C.MAX_OCR_PAGES, 30)
        self.assertEqual(str(jobs.JOB_MAX_OCR_PAGES_PRO), cfg["scanned-pages-limit-pro"])
        self.assertEqual(str(jobs.JOB_MAX_OCR_PAGES_FREE), cfg["scanned-pages-limit-free"])
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
        self.assertIn("scannedPages:n.scanned_pages,maxScanned:n.max_scanned_pages", js)   # pwApiError (jobs)
        self.assertIn("scannedPages:s.scanned_pages,maxScanned:s.max_scanned_pages", js)   # failed job status
        self.assertIn("Divisez le PDF ou convertissez les {max} premières pages.", js)
        self.assertIn("قسّم الملف أو حوّل أول {max} صفحة.", js)


# ---------------------------------------------------------------- 8. scans: preprocessing, structure, tables
import cv2  # noqa: E402
import numpy as np  # noqa: E402


def _text_image(lines, size=(1700, 2200), scale=1.2, top=200, pitch=70, left=150):
    """White image with black text lines (cv2 Hershey font)."""
    img = np.full((size[1], size[0]), 255, np.uint8)
    for k, line in enumerate(lines):
        cv2.putText(img, line, (left, top + k * pitch), cv2.FONT_HERSHEY_SIMPLEX, scale, 0, 3, cv2.LINE_AA)
    return img


def _grid(img, x0, y0, col_w, row_h, cols, rows, skip=None):
    """Ruled table; skip=(r, c): no rule between cell (r, c) and (r, c + 1) (a merged cell)."""
    for r in range(rows + 1):
        cv2.line(img, (x0, y0 + r * row_h), (x0 + cols * col_w, y0 + r * row_h), 0, 3)
    for c in range(cols + 1):
        if skip and c == skip[1] + 1:
            cv2.line(img, (x0 + c * col_w, y0), (x0 + c * col_w, y0 + skip[0] * row_h), 0, 3)
            cv2.line(img, (x0 + c * col_w, y0 + (skip[0] + 1) * row_h), (x0 + c * col_w, y0 + rows * row_h), 0, 3)
        else:
            cv2.line(img, (x0 + c * col_w, y0), (x0 + c * col_w, y0 + rows * row_h), 0, 3)
    return img


def _line(text, x0, y0, x1, y1, key=(1, 1, 1)):
    words = text.split()
    step = (x1 - x0) / len(words)
    ws = [{"text": w, "conf": 90.0, "x0": x0 + k * step, "x1": x0 + (k + 1) * step - 8, "y0": y0, "y1": y1,
           "key": key} for k, w in enumerate(words)]
    return C._group_lines(ws)[0]


class ScanPreprocessing(unittest.TestCase):
    def test_skew_is_measured_and_corrected(self):
        img = _text_image(["Le club de lecture du quartier a reuni"] * 12)
        for angle in (3.0, -4.5):
            m = cv2.getRotationMatrix2D((850, 1100), angle, 1.0)
            rotated = cv2.warpAffine(img, m, (1700, 2200), borderValue=255)
            measured = C.skew_angle(255 - rotated)
            self.assertAlmostEqual(measured, -angle, delta=0.4)
            straight = C._rotate(rotated, measured)
            self.assertAlmostEqual(C.skew_angle(255 - straight), 0.0, delta=0.3)

    def test_dark_border_cut_and_lighting_flattened(self):
        paper = _text_image(["Uneven light on yellow paper"] * 10).astype(np.float32)
        h, w = paper.shape
        shade = np.linspace(0.55, 1.0, w, dtype=np.float32)[None, :]          # dark on the left
        photo = np.full((h + 300, w + 300), 70, np.float32)                     # desk
        photo[150:150 + h, 150:150 + w] = paper * 0.9 * shade
        gray, binary, horiz, vert, info = C.preprocess(photo.astype(np.uint8))
        self.assertTrue(info.get("border_cut"))
        self.assertLess(gray.shape[1], w + 300 - 200)                          # desk cut off
        # paper is white everywhere (left and right), ink is black
        self.assertGreater(np.percentile(gray[:, :200], 60), 230)
        self.assertGreater(np.percentile(gray[:, -200:], 60), 230)
        self.assertLess(binary.min(), 50)
        self.assertGreater((binary == 255).mean(), 0.85)

    def test_resolution_follows_the_scan(self):
        for img_dpi, expected in ((100, 200), (220, 220), (600, 300)):
            doc = fitz.open()
            page = doc.new_page(width=595, height=842)
            px = int(595 / 72 * img_dpi)
            pix = fitz.Pixmap(fitz.csGRAY, fitz.IRect(0, 0, px, int(842 / 72 * img_dpi)), 0)
            pix.set_rect(pix.irect, (255,))
            page.insert_image(page.rect, pixmap=pix)
            self.assertAlmostEqual(C.ocr_dpi(page), expected, delta=2)
            doc.close()

    def test_clear_grid_is_found_and_a_merged_cell_is_not_trusted(self):
        img = _grid(np.full((1500, 1400), 255, np.uint8), 100, 200, 300, 80, 4, 5)
        _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
        tables = C.find_tables(*C._rules(binary))
        self.assertEqual(len(tables), 1)
        self.assertEqual((len(tables[0]["rows"]), len(tables[0]["cols"])), (6, 5))
        merged = _grid(np.full((1500, 1400), 255, np.uint8), 100, 200, 300, 80, 4, 5, skip=(2, 1))
        _, binary = cv2.threshold(merged, 127, 255, cv2.THRESH_BINARY)
        self.assertEqual(C.find_tables(*C._rules(binary)), [])

    def test_rules_do_not_take_letters(self):
        img = _text_image(["Illustration: a long title line with tall letters"], scale=3.0)
        _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
        horiz, vert = C._rules(binary)
        self.assertEqual(int(horiz.sum()) + int(vert.sum()), 0)

    def test_layout_line_not_covered_by_words_is_found(self):
        img = _text_image(["first line of text here", "second line of text here", "third line of text"])
        _, binary = cv2.threshold(img, 127, 255, cv2.THRESH_BINARY)
        boxes = sorted(C._layout_lines(binary, 30), key=lambda b: b[1])
        self.assertEqual(len(boxes), 3)
        words = [{"x0": b[0], "x1": b[2], "y0": b[1], "y1": b[3]} for b in (boxes[0], boxes[2])]
        self.assertEqual([C._covered(b, words) for b in boxes], [True, False, True])


class ScanStructure(unittest.TestCase):
    def test_lines_become_paragraphs_and_headings(self):
        lines = [_line("Annual report", 100, 100, 700, 160, (1, 1, 1)),
                 _line("The first paragraph starts here and goes on", 100, 220, 1500, 250, (2, 1, 1)),
                 _line("to the full width of the column again and", 100, 260, 1500, 290, (2, 1, 2)),
                 _line("ends on a short line.", 100, 300, 600, 330, (2, 1, 3)),
                 _line("A second paragraph follows the short line", 100, 340, 1500, 370, (2, 2, 1)),
                 _line("and Tesseract cut its last line off.", 100, 380, 1490, 410, (3, 1, 1))]
        paras = C.build_paragraphs(lines, 30)
        self.assertEqual([p["level"] for p in paras], [1, 0, 0])
        self.assertEqual(paras[1]["text"], "The first paragraph starts here and goes on to the full width of "
                                           "the column again and ends on a short line.")
        self.assertTrue(paras[2]["text"].endswith("cut its last line off."))

    def test_column_change_starts_a_new_paragraph(self):
        lines = [_line("left column text line number one", 100, 200, 700, 230, (1, 1, 1)),
                 _line("left column text line number two", 100, 240, 700, 270, (1, 1, 2)),
                 _line("right column text line number one", 800, 200, 1400, 230, (2, 1, 1)),
                 _line("right column text line number two", 800, 240, 1400, 270, (2, 1, 2))]
        paras = C.build_paragraphs(lines, 30)
        self.assertEqual(len(paras), 2)
        self.assertTrue(paras[1]["text"].startswith("right column"))

    def test_hyphen_at_line_end(self):
        self.assertEqual(C._join("rez-de-", "chaussée"), "rez-de-chaussée")
        self.assertEqual(C._join("Total -", "12"), "Total - 12")

    def test_number_cells(self):
        for text, ok in (("1200", True), ("15,5 %", True), ("10 h - 12 h", False), ("Rabat", False)):
            self.assertEqual(bool(C.NUMBER_CELL_RE.match(text)), ok, text)

    def test_ocr_items_are_written_with_styles_and_tables(self):
        result = C.OcrResult([("heading", 1, "Title"), ("para", "Body text."),
                              ("table", [["A", "B"], ["1", "2"]]), ("heading", 2, "Part two"), ("para", "More.")],
                             "eng+fra", {})
        doc = Document()
        C._write_ocr(doc, fitz.Rect(0, 0, 595, 842), result)
        paras = [(p.style.name, p.text) for p in doc.paragraphs]
        self.assertEqual(paras, [("Heading 1", "Title"), ("Normal", "Body text."), ("Heading 2", "Part two"),
                                 ("Normal", "More.")])
        self.assertEqual([[c.text for c in row.cells] for row in doc.tables[0].rows], [["A", "B"], ["1", "2"]])
        self.assertEqual(result.text(), "Title\nBody text.\nA B\n1 2\nPart two\nMore.")


@needs_ocr
class ScanPages(Tmp):
    """Whole OCR pages: phone-photo look, big heading, ruled table."""

    def scan(self, build, angle=0.0, dark=False):
        src = fitz.open()
        page = src.new_page(width=595, height=842)
        build(page)
        pix = page.get_pixmap(dpi=200, colorspace=fitz.csGRAY)
        img = np.frombuffer(pix.samples, np.uint8).reshape(pix.height, pix.stride)[:, :pix.width].copy()
        src.close()
        if dark:                                                        # yellowish, uneven, on a desk
            shade = np.linspace(0.6, 0.95, img.shape[1], dtype=np.float32)[None, :]
            img = (img.astype(np.float32) * shade).astype(np.uint8)
            img = cv2.copyMakeBorder(img, 120, 120, 120, 120, cv2.BORDER_CONSTANT, value=60)
        if angle:
            h, w = img.shape
            img = cv2.warpAffine(img, cv2.getRotationMatrix2D((w / 2, h / 2), angle, 1.0), (w, h),
                                 borderValue=60 if dark else 255)
        ok, jpg = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, 60])
        doc = fitz.open()
        doc.new_page(width=595, height=842).insert_image(fitz.Rect(0, 0, 595, 842), stream=jpg.tobytes())
        path = self.dir / f"scan{len(list(self.dir.iterdir()))}.pdf"
        doc.save(path)
        doc.close()
        return path

    @staticmethod
    def body(page, y=200, n=8):
        text = ("The committee met on Monday and approved the reading programme for the next school year. "
                "Volunteers will visit the village schools every month with new books.")
        page.insert_textbox(fitz.Rect(72, y, 523, y + 22 * n), text * 2, fontsize=12, fontname="helv")

    def test_skewed_photo_on_a_desk_is_read(self):
        pdf = self.scan(lambda p: (p.insert_text((72, 120), "Reading programme", fontsize=26, fontname="hebo"),
                                   self.body(p)), angle=4.0, dark=True)
        report, out = self.convert(pdf)
        info = report["pages"][0]["ocr"]
        self.assertTrue(info["border_cut"])
        self.assertAlmostEqual(abs(info["skew"]), 4.0, delta=0.6)
        self.assertIn("seconds", report["pages"][0])
        doc = Document(out)
        styles = [(p.style.name, p.text) for p in doc.paragraphs if p.text.strip()]
        self.assertEqual(styles[0], ("Heading 1", "Reading programme"))
        body = [t for s, t in styles if s == "Normal"]
        self.assertEqual(len(body), 1, body)                             # one paragraph, not one per line
        self.assertIn("approved the reading programme for the next school year", body[0])
        empty = [p for p in doc.paragraphs if not p.text.strip()]
        self.assertLessEqual(len(empty), 1)                              # at most the section paragraph

    def test_very_large_heading_is_not_lost(self):
        pdf = self.scan(lambda p: (p.insert_text((60, 160), "BIG 2025", fontsize=110, fontname="hebo"),
                                   self.body(p, y=260)))
        report, out = self.convert(pdf)
        text = self.docx_text(out)
        self.assertIn("BIG 2025", text.replace("\n", " "))
        self.assertIn("committee met on Monday", text)

    def test_ruled_table_becomes_a_word_table(self):
        rows = [["City", "Volunteers", "Books"], ["Rabat", "85", "1200"], ["Fes", "70", "950"], ["Tangier", "55", "700"]]

        def build(page):
            page.insert_text((72, 110), "Books by city", fontsize=22, fontname="hebo")
            for r, row in enumerate(rows):
                for c, cell in enumerate(row):
                    rect = fitz.Rect(72 + c * 150, 150 + r * 34, 222 + c * 150, 184 + r * 34)
                    page.draw_rect(rect, color=(0, 0, 0), width=1.2)
                    page.insert_text((rect.x0 + 10, rect.y0 + 23), cell, fontsize=13, fontname="helv")
            self.body(page, y=340, n=4)

        report, out = self.convert(self.scan(build, angle=-2.0))
        self.assertEqual(report["pages"][0]["ocr"]["tables"], 1)
        doc = Document(out)
        self.assertEqual(len(doc.tables), 1)
        got = [[c.text.strip() for c in row.cells] for row in doc.tables[0].rows]
        self.assertEqual(got, rows)
        text = self.docx_text(out)
        self.assertNotIn("Tangier", text)                                # table text not repeated outside
        self.assertIn("committee met on Monday", text)


class UnreadablePages(Tmp):
    """OCR confidence below 50: the page image + one italic note in the document's language, no garbage."""

    GARBAGE = "Sacomful ptopte brass thot cam chomgud"

    def run_with(self, build, conf=30.0):
        pdf = self.pdf(build)
        fake = C.OcrResult([("para", self.GARBAGE)], "eng+fra", {"conf": conf})
        with mock.patch.object(C, "ocr_page", return_value=fake):
            report, out = self.convert(pdf)
        return report, out

    def note_paragraphs(self, out):
        return [p for p in Document(out).paragraphs if p.text in C.UNREADABLE_NOTE.values()]

    def test_low_confidence_page_is_kept_as_image_with_a_note(self):
        report, out = self.run_with(lambda d: (_text_page(d, "The committee met on Monday and the\n"
                                                             "volunteers will visit the schools."),
                                               _image_of_text(d, "handwriting")))
        self.assertEqual([p["outcome"] for p in report["pages"]], ["text", "unreadable"])
        text = self.docx_text(out)
        self.assertNotIn("Sacomful", text)                                 # no garbage text
        notes = self.note_paragraphs(out)
        self.assertEqual([n.text for n in notes], [C.UNREADABLE_NOTE["en"]])
        self.assertTrue(all(r.italic for r in notes[0].runs))
        with zipfile.ZipFile(out) as z:
            self.assertEqual(len([n for n in z.namelist() if n.startswith("word/media/")]), 1)
        # picture and note on the same page: picture height fits the page minus the note
        doc = Document(out)
        section = doc.sections[-1]
        pic = doc.inline_shapes[0]
        self.assertLessEqual(pic.height + C.Pt(48),
                             section.page_height - section.top_margin - section.bottom_margin + 1)

    def test_note_follows_the_document_language(self):
        _, out = self.run_with(lambda d: (_text_page(d, "La commission est réunie pour le programme\n"
                                                        "et les inscriptions des élèves dans la ville."),
                                          _image_of_text(d, "x")))
        self.assertEqual([n.text for n in self.note_paragraphs(out)], [C.UNREADABLE_NOTE["fr"]])
        self.assertEqual(C._document_language("تقدم الجمعية في هذا التقرير حصيلة أنشطتها"), "ar")
        self.assertEqual(C._document_language("The report of the year and the plan"), "en")
        self.assertEqual(C._document_language(""), "en")

    def test_note_follows_the_page_script_when_it_is_clear(self):
        english_ui = "Search ratings views pages Upload Sign in Download free for the days " * 5
        arabic = "تقدم الجمعية في هذا التقرير"
        self.assertEqual(C._note_language("ara", english_ui), "ar")             # Arabic page, English site text
        self.assertEqual(C._note_language("eng+fra", arabic + " le programme et les élèves de la ville"), "fr")
        self.assertEqual(C._note_language("eng+fra", arabic), "en")             # Latin page, nothing French
        self.assertEqual(C._note_language("eng+fra+ara", arabic * 3 + " a b"), "ar")   # mixed: the document's
        self.assertEqual(C._note_language("eng+fra+ara", english_ui), "en")

    def test_arabic_note_is_right_to_left(self):
        doc = Document()
        note = doc.add_paragraph()
        note.add_run(C.UNREADABLE_NOTE["ar"]).italic = True
        C.fix_arabic(doc)
        self.assertIsNotNone(note._p.pPr.find(W + "bidi"))

    def test_document_of_only_unreadable_pages_is_still_returned(self):
        report, out = self.run_with(lambda d: (_image_of_text(d, "a"), _image_of_text(d, "b")))
        self.assertEqual(report["counts"], {"unreadable": 2})
        self.assertEqual(len(self.note_paragraphs(out)), 2)

    def test_confidence_at_the_threshold_keeps_the_text(self):
        report, out = self.run_with(lambda d: (_image_of_text(d, "x"),), conf=50.0)
        self.assertEqual(report["pages"][0]["outcome"], "ocr")
        self.assertIn(self.GARBAGE, self.docx_text(out))

    def test_log_has_no_content(self):
        with self.assertLogs("pdf2wordly.conversion", "INFO") as logs:
            self.run_with(lambda d: (_image_of_text(d, "x"),))
        self.assertIn("kept as an image with a note", " ".join(logs.output))
        self.assertNotIn("Sacomful", " ".join(logs.output))


# ---------------------------------------------------------------- 7. time budget
class TimeBudget(Tmp):
    """Past the deadline: ConversionTimeoutError, no DOCX, no .part file."""

    def text_pdf(self, n=3):
        return self.pdf(lambda d: [_text_page(d, f"Page {k} has enough plain words here.") for k in range(n)])

    def leftovers(self):
        return sorted(p.name for p in self.dir.iterdir() if p.suffix != ".pdf")

    def test_deadline_helpers(self):
        self.assertEqual(C.make_deadline(budget=10, start=100.0), 110.0)
        C.check_deadline(None)                                   # no deadline: never stops
        C.check_deadline(C.make_deadline(budget=60))
        with self.assertRaises(C.ConversionTimeoutError):
            C.check_deadline(C.make_deadline(budget=-1))

    def test_expired_deadline_stops_before_anything_is_written(self):
        pdf = self.text_pdf()
        with mock.patch.object(pytesseract, "image_to_data") as ocr, self.assertRaises(C.ConversionTimeoutError):
            C.convert_pdf_to_docx(str(pdf), str(self.dir / "o.docx"), deadline=C.make_deadline(budget=-1))
        ocr.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_plan_stops_between_pages(self):
        pdf = self.text_pdf()
        with self.assertRaises(C.ConversionTimeoutError):
            C.plan_pdf(str(pdf), deadline=C.make_deadline(budget=-1))
        self.assertEqual(len(C.plan_pdf(str(pdf), deadline=C.make_deadline(budget=60))), 3)

    def test_stops_inside_the_pdf2docx_analysis(self):
        """parse_document's page loop sees the deadline (pages are not parsed one by one after it)."""
        from pdf2docx.page.Page import Page
        pdf = self.text_pdf()
        plan = C.plan_pdf(str(pdf))
        ticks = iter([0.0, 0.0])                                  # two page checks, then the time is up
        with mock.patch.object(C.time, "monotonic", side_effect=lambda: next(ticks, 1000.0)), \
                mock.patch.object(Page, "parse") as parse, self.assertRaises(C.ConversionTimeoutError):
            C.convert_pdf_to_docx(str(pdf), str(self.dir / "o.docx"), plan=plan, deadline=100.0)
        parse.assert_not_called()
        self.assertEqual(self.leftovers(), [])

    def test_stops_before_saving_and_leaves_no_docx(self):
        pdf = self.text_pdf()
        clock = [0.0]
        real_fix = C.fix_arabic

        def slow_fix(document):
            real_fix(document)
            clock[0] = 1000.0                                     # the time runs out during the last step

        with mock.patch.object(C.time, "monotonic", side_effect=lambda: clock[0]), \
                mock.patch.object(C, "fix_arabic", slow_fix), self.assertRaises(C.ConversionTimeoutError):
            C.convert_pdf_to_docx(str(pdf), str(self.dir / "o.docx"), deadline=100.0)
        self.assertEqual(self.leftovers(), [])

    def test_failed_save_leaves_no_partial_file(self):
        from docx.document import Document as Doc
        pdf = self.text_pdf(1)

        def half_save(self_doc, path):
            Path(path).write_bytes(b"PK half")
            raise OSError("disk full")

        with mock.patch.object(Doc, "save", half_save), self.assertRaises(OSError):
            C.convert_pdf_to_docx(str(pdf), str(self.dir / "o.docx"))
        self.assertEqual(self.leftovers(), [])

    def test_in_time_conversion_writes_only_the_docx(self):
        pdf = self.text_pdf()
        report = C.convert_pdf_to_docx(str(pdf), str(self.dir / "o.docx"), deadline=C.make_deadline(budget=120))
        self.assertEqual(report["counts"], {"text": 3})
        self.assertEqual(self.leftovers(), ["o.docx"])
        self.assertIn("Page 2 has enough plain words", self.docx_text(self.dir / "o.docx"))

    def test_default_budget_is_below_the_gunicorn_timeout_and_env_overrides_it(self):
        import subprocess
        docker = (SITE / "Dockerfile").read_text(encoding="utf-8")
        timeout = int(re.search(r"--timeout (\d+)", docker).group(1))
        self.assertEqual(C.CONVERSION_TIME_BUDGET, 240)
        # room after the budget for one slow page (OCR / image-heavy pdf2docx), saving and the quota release
        self.assertGreaterEqual(timeout - C.CONVERSION_TIME_BUDGET, 45)
        env = dict(os.environ, CONVERSION_TIME_BUDGET="90")
        out = subprocess.run([sys.executable, "-c", "import conversion; print(conversion.CONVERSION_TIME_BUDGET)"],
                             cwd=str(SITE), env=env, capture_output=True, text=True)
        self.assertEqual(out.stdout.strip().splitlines()[-1], "90.0", out.stderr)


class TimeBudgetApi(unittest.TestCase):
    """413 CONVERSION_TIMEOUT, clear message, quota released, temp files deleted."""

    def runner(self):
        return EmptyDocumentApi.runner(self)

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="pw-budget-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_timeout(self, pro=False, during_plan=False):
        import contextlib
        import json
        r = self.runner()
        srv = sys.modules["server"]
        err = C.ConversionTimeoutError("x")
        with contextlib.ExitStack() as stack:
            stack.enter_context(mock.patch.object(srv, "TEMP_DIR", self.tmp))
            if during_plan:
                stack.enter_context(mock.patch.object(srv.conversion, "plan_pdf", side_effect=err))
            if pro:
                code, body, calls = r.run_pro({"allowed": True, "claim_id": 5}, convert_error=err)
            else:
                code, body, calls = r.run_convert(r.free(), {"allowed": True, "reason": "first_conversion",
                                                             "previous": None}, convert_error=err)
        return code, json.loads(body), calls

    def test_free_user_keeps_the_quota_and_no_file_is_left(self):
        code, data, calls = self.run_timeout()
        self.assertEqual(code, 413)
        self.assertEqual(data["code"], "CONVERSION_TIMEOUT")
        self.assertIn("took too long to convert", data["error"])
        self.assertIn("split the PDF", data["error"])
        self.assertIn("not counted toward your limit", data["error"])
        self.assertEqual(calls[-1], ("release_free_conversion", {"p_user_id": mock.ANY, "p_previous": None}))
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_pro_user_keeps_the_fair_use_slot(self):
        code, data, calls = self.run_timeout(pro=True)
        self.assertEqual((code, data["code"]), (413, "CONVERSION_TIMEOUT"))
        self.assertEqual(calls[-1][0], "release_pro_conversion")
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_timeout_while_planning(self):
        code, data, calls = self.run_timeout(during_plan=True)
        self.assertEqual((code, data["code"]), (413, "CONVERSION_TIMEOUT"))
        self.assertEqual(calls[-1][0], "release_free_conversion")
        self.assertEqual(list(self.tmp.iterdir()), [])

    def test_the_clock_starts_with_the_request(self):
        """The server's deadline reaches the real planning step: an expired budget stops it."""
        import json
        r = self.runner()
        srv = sys.modules["server"]
        with mock.patch.object(srv, "TEMP_DIR", self.tmp), \
                mock.patch.object(srv.conversion, "CONVERSION_TIME_BUDGET", -1):
            code, body, calls = r.run_convert(r.free(), {"allowed": True, "reason": "first_conversion",
                                                         "previous": None},
                                              convert_error=AssertionError("conversion must not start"))
        self.assertEqual((code, json.loads(body)["code"]), (413, "CONVERSION_TIMEOUT"))
        self.assertEqual(calls[-1][0], "release_free_conversion")

    def test_log_has_no_user_or_file_details(self):
        srv = sys.modules.get("server") or __import__("server")
        with self.assertLogs(srv.logger, "WARNING") as logs:
            self.run_timeout()
        line = " ".join(logs.output)
        import test_billing
        self.assertIn("time budget of 240 s used up", line)
        self.assertNotIn("doc.pdf", line)
        self.assertNotIn(test_billing.USER, line)

    def test_site_message_in_three_languages(self):
        js = (SITE / "script.js").read_text(encoding="utf-8")
        self.assertIn('case"CONVERSION_TIMEOUT":return pwT("This file took too long to convert. Try a shorter file, '
                      'or split the PDF into smaller parts.")+" "+pwT("This file was not counted toward your limit.");', js)
        self.assertIn("La conversion de ce fichier a pris trop de temps.", js)
        self.assertIn("استغرق تحويل هذا الملف وقتًا طويلًا.", js)


if __name__ == "__main__":
    unittest.main()
