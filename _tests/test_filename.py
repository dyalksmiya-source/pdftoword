"""Download file names: server rules, Content-Disposition, and the same
result as filename.js on the site.

Run from website/:   python -m unittest discover -s _tests -v
"""
import json
import subprocess
import sys
import unittest
from pathlib import Path
from urllib.parse import quote

SITE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SITE))
sys.path.insert(0, str(SITE / "_tests"))

SCRIBD = ("www.scribd.com-document-738101487-D9-82-D8-B5-D8-B5-2-D9-84-D9-84-D8-B3-D9-86-D8-A9-D8-A7-D9-84-"
          "D8-A3-D9-88-D9-84-D9-89-D8-A7-D8-A8-D8-AA-D8-AF-D8-A7-D8-A6-D9-8A.pdf")
CASES = [
    # (uploaded name, expected .docx name)
    (SCRIBD, "www.scribd.com-document-738101487-قصص-2-للسنةالأولىابتدائي.docx"),
    ("%D8%AA%D9%82%D8%B1%D9%8A%D8%B1.pdf", "تقرير.docx"),
    ("تقرير المشروع النهائي.pdf", "تقرير المشروع النهائي.docx"),
    ("Résumé final (v2).PDF", "Résumé final (v2).docx"),
    ("???.pdf", "converted.docx"),
    ("---.pdf", "converted.docx"),
    ("%%%.pdf", "converted.docx"),
    ("  .pdf", "converted.docx"),
    (".pdf", "converted.docx"),
    ('a<b>c:d"e|f?g*h.pdf', "a-b-c-d-e-f-g-h.docx"),
    ("file-AB-CD.pdf", "file-AB-CD.docx"),          # hex-looking, not UTF-8: unchanged
    ("%E2%28%A1.pdf", "%E2%28%A1.docx"),              # invalid UTF-8: left as is
    ("x" * 200 + ".pdf", "x" * 80 + ".docx"),
    ("ع" * 120 + ".pdf", "ع" * 80 + ".docx"),
]


class ServerName(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401
        cls.server = sys.modules["server"]

    def test_cases(self):
        for name, expected in CASES:
            self.assertEqual(self.server.docx_download_name(name), expected, name)

    def test_never_empty_and_short(self):
        for name, _ in CASES:
            out = self.server.docx_download_name(name)
            self.assertTrue(out.endswith(".docx"))
            self.assertGreater(len(out), len(".docx"))
            self.assertLessEqual(len(out) - len(".docx"), 80)

    def test_never_a_path(self):
        self.assertEqual(self.server.docx_download_name("..\\..\\evil/x.pdf"), "x.docx")
        self.assertEqual(self.server.docx_download_name("/etc/passwd.pdf"), "passwd.docx")

    def test_decode_only_valid_utf8(self):
        d = self.server.decode_file_name
        self.assertEqual(d("D9-82-D8-B5"), "قص")
        self.assertEqual(d("AB-CD"), "AB-CD")
        self.assertEqual(d("10-20-30"), "10-20-30")       # ASCII bytes: not decoded
        self.assertEqual(d("%41%42"), "AB")
        self.assertEqual(d("%0A%0D"), "%0A%0D")           # control characters: kept encoded


class ContentDisposition(unittest.TestCase):
    def run_named(self, filename):
        import test_billing

        class Run(test_billing.ClaimCompat, unittest.TestCase):
            server = sys.modules["server"]

            def runTest(self):
                pass

        r = Run()
        code, _, _ = r.run_convert(r.free(), {"allowed": True, "reason": "first_conversion", "previous": None},
                                   filename=filename)
        self.assertEqual(code, 200)
        return r.last_headers["Content-Disposition"]

    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401

    def test_ascii_name(self):
        cd = self.run_named("Report 2024.pdf")
        self.assertIn('filename="Report 2024.docx"', cd)

    def test_arabic_name_uses_rfc5987(self):
        cd = self.run_named("تقرير.pdf")
        self.assertIn("filename*=UTF-8''" + quote("تقرير.docx"), cd)

    def test_encoded_scribd_name_is_decoded(self):
        cd = self.run_named(SCRIBD)
        self.assertIn("filename*=UTF-8''" + quote("www.scribd.com-document-738101487-قصص-2-للسنةالأولىابتدائي.docx"), cd)

    def test_symbols_only(self):
        cd = self.run_named("???.pdf")
        self.assertIn("converted.docx", cd)


class SameAsSite(unittest.TestCase):
    def test_filename_js_gives_the_same_names(self):
        names = [n for n, _ in CASES]
        js = ("const F=require(process.argv[1]);const names=JSON.parse(process.argv[2]);"
              "process.stdout.write(JSON.stringify(names.map(n=>F.docxName(n))))")
        out = subprocess.run(["node", "-e", js, str(SITE / "filename.js"), json.dumps(names)],
                             capture_output=True, text=True, encoding="utf-8")
        self.assertEqual(out.returncode, 0, out.stderr)
        self.assertEqual(json.loads(out.stdout), [e for _, e in CASES])


if __name__ == "__main__":
    unittest.main()
