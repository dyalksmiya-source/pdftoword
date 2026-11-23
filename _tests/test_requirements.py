"""
requirements.txt pins EVERY package (direct and indirect) to an exact version, so a Docker
rebuild (e.g. after any change to requirements.txt) can never install other versions.
Run from website/:  python -m unittest _tests.test_requirements
"""
import re
import unittest
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent
PIN_RE = re.compile(r"^([A-Za-z0-9][A-Za-z0-9._-]*)==(\d+(?:\.\d+)*)$")


def pins():
    out = {}
    for line in (SITE / "requirements.txt").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        m = PIN_RE.match(line)
        if not m:
            raise AssertionError(f"not an exact pin: {line!r}")
        name = re.sub(r"[-_.]+", "-", m.group(1)).lower()
        if name in out:
            raise AssertionError(f"listed twice: {name}")
        out[name] = m.group(2)
    return out


class RequirementsTests(unittest.TestCase):
    def test_every_line_is_an_exact_pin(self):
        self.assertGreaterEqual(len(pins()), 50)        # the indirect packages are listed too

    def test_direct_dependencies_are_there(self):
        p = pins()
        for name in ("flask", "flask-cors", "pdf2docx", "opencv-python-headless", "numpy", "gunicorn",
                     "pymupdf", "pytesseract", "pillow", "python-docx", "supabase", "requests",
                     "cryptography", "werkzeug", "lxml", "fonttools", "pyjwt", "supabase-auth"):
            self.assertIn(name, p)

    def test_pdf_stack_of_the_working_image(self):
        p = pins()
        self.assertEqual(p["pymupdf"], "1.24.14")
        self.assertEqual(p["pdf2docx"], "0.5.8")
        self.assertEqual(p["pillow"], "10.4.0")


if __name__ == "__main__":
    unittest.main()
