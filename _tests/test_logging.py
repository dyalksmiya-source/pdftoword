"""Logs never contain request URLs or user ids.

supabase-py uses httpx; httpx logs "HTTP Request: GET <url>" at INFO, and the
URL holds user ids (profiles?select=...&id=eq.<uuid>). server.py keeps httpx
and httpcore at WARNING.

Run from website/:   python -m unittest discover -s _tests -v
"""
import logging
import sys
import unittest
from pathlib import Path

SITE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SITE))
sys.path.insert(0, str(SITE / "_tests"))

import httpx  # noqa: E402

USER = "3f1c2a7e-6b1d-4c55-9a8e-2d3b4c5d6e7f"


class Capture(logging.Handler):
    """Collects every record that reaches the root logger (like Railway's stdout)."""

    def __init__(self):
        super().__init__(level=logging.DEBUG)
        self.records = []

    def emit(self, record):
        self.records.append(record)

    def text(self):
        return "\n".join(f"{r.name} {r.levelname} {r.getMessage()}" for r in self.records)


class QuietHttpLoggingTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401  (sets the levels at import, like gunicorn does)
        cls.server = sys.modules["server"]

    def setUp(self):
        self.cap = Capture()
        root = logging.getLogger()
        root.addHandler(self.cap)
        self.addCleanup(root.removeHandler, self.cap)

    def test_levels(self):
        self.assertEqual(self.server.QUIET_LOGGERS, ("httpx", "httpcore"))
        for name in ("httpx", "httpcore"):
            logger = logging.getLogger(name)
            self.assertEqual(logger.level, logging.WARNING, name)
            self.assertFalse(logger.isEnabledFor(logging.INFO), name)
            self.assertTrue(logger.isEnabledFor(logging.WARNING), name)
        # our own logger still logs INFO (event ids, result codes)
        self.assertTrue(logging.getLogger("pdf2wordly").isEnabledFor(logging.INFO))

    def test_a_real_httpx_request_url_is_not_logged(self):
        url = f"https://yjxfxdbrjsiuevnkiiak.supabase.co/rest/v1/profiles?select=id%2Cplan&id=eq.{USER}&limit=1"
        transport = httpx.MockTransport(lambda request: httpx.Response(200, json=[{"id": USER}]))
        with httpx.Client(transport=transport) as client:
            self.assertEqual(client.get(url).status_code, 200)
        text = self.cap.text()
        self.assertNotIn(USER, text)
        self.assertNotIn("supabase.co", text)
        self.assertNotIn("HTTP Request", text)

    def test_warnings_from_httpx_still_show(self):
        logging.getLogger("httpx").warning("connection pool full")
        self.assertIn("connection pool full", self.cap.text())

    def test_conversion_logs_no_user_id(self):
        import test_billing

        class Run(test_billing.ClaimCompat, unittest.TestCase):
            server = self.server

            def runTest(self):
                pass

        r = Run()
        code, _, _ = r.run_convert(r.free(), {"allowed": True, "reason": "first_conversion", "previous": None})
        self.assertEqual(code, 200)
        text = self.cap.text()
        self.assertIn("Converting upload (", text)
        self.assertNotIn(USER, text)


if __name__ == "__main__":
    unittest.main()
