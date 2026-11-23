"""
Rewarded ads (ads.py, jobs_api.py; SQL in WORD docs/sql/005_ad_rewards.sql).
Run from website/:  python -m unittest _tests.test_ads   (or discover -s _tests)

Supabase is replaced by fakes with the same rules as SQL 004 + 005: transaction_id and the
token's reward id are unique, one reward = one conversion, and a job's reward comes back once
when its quota is released (trigger), never after a download.
"""
import base64
import inspect
import io
import os
import re
import sys
import time
import unittest
import uuid
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock
from urllib.parse import urlencode

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec

SITE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SITE))
sys.path.insert(0, str(SITE / "_tests"))

import ads  # noqa: E402
import jobs  # noqa: E402
import jobs_worker  # noqa: E402
from test_jobs import FakeProc, FakeStore, TempJobsDir, text_pdf  # noqa: E402

USER = "3f1c2a7e-6b1d-4c55-9a8e-2d3b4c5d6e7f"
OTHER = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
SECRET = "s" * 40
UNIT = "ca-app-pub-1111111111111111/2222222222"


# ---------------------------------------------------------------------------
# Fakes (same rules as SQL 005)
# ---------------------------------------------------------------------------
class TriggerStore(FakeStore):
    """conversion_jobs + the 005 trigger: a released quota gives the job's ad reward back."""
    ads = None

    def release(self, job_id, status, code):
        released = super().release(job_id, status, code)
        if released and self.ads is not None:
            self.ads.on_quota_released(job_id)
        return released


class FakeAdStore:
    def __init__(self, job_store, clock=time.time):
        self.rows = []
        self.job_store = job_store
        self.clock = clock

    def add_reward(self, row):
        if any(r["transaction_id"] == row["transaction_id"] or r["token_nonce"] == row["token_nonce"]
               for r in self.rows):
            return False
        self.rows.append({**row, "id": len(self.rows) + 1, "verified_at": self.clock(), "consumed_at": None,
                          "job_id": None, "returned_at": None, "times_returned": 0})
        return True

    def _give(self, r):
        r.update(consumed_at=None, job_id=None, returned_at=self.clock(), times_returned=r["times_returned"] + 1)

    def _available(self, user):
        now = self.clock()
        return [r for r in self.rows if r["user_id"] == user and r["consumed_at"] is None
                and (r["returned_at"] or r["verified_at"]) > now - 86400]

    def _used(self, user):
        now = self.clock()
        return [r for r in self.rows if r["user_id"] == user and r["consumed_at"] is not None
                and r["consumed_at"] > now - 86400]

    def status(self, user_id, nonce=None):
        return {"available": len(self._available(user_id)), "used_24h": len(self._used(user_id)),
                "arrived": nonce is not None and any(r["user_id"] == user_id and r["token_nonce"] == nonce
                                                     for r in self.rows)}

    def claim(self, user_id, job_id, max_per_day):
        now = self.clock()
        for r in self.rows:      # self-heal: claimed by a job that was never created
            if (r["user_id"] == user_id and r["consumed_at"] is not None and r["consumed_at"] < now - 900
                    and r["consumed_at"] > now - 86400 and r["job_id"] not in self.job_store.rows):
                self._give(r)
        if max_per_day and len(self._used(user_id)) >= max_per_day:
            return {"allowed": False, "reason": "cap"}
        free = sorted(self._available(user_id), key=lambda r: r["verified_at"])
        if not free:
            return {"allowed": False, "reason": "no_reward"}
        free[0].update(consumed_at=now, job_id=job_id)
        return {"allowed": True, "reward_id": free[0]["id"]}

    def give_back(self, job_id):
        if job_id in self.job_store.rows:
            return False
        hit = [r for r in self.rows if r["job_id"] == job_id and r["consumed_at"] is not None]
        for r in hit:
            self._give(r)
        return bool(hit)

    def on_quota_released(self, job_id):
        for r in self.rows:
            if r["job_id"] == job_id and r["consumed_at"] is not None:
                self._give(r)


class GoogleSigner:
    """Signs SSV callbacks like AdMob (ECDSA P-256 / SHA-256, DER, web-safe base64)."""

    def __init__(self, key_id=3335741209):
        self.key = ec.generate_private_key(ec.SECP256R1())
        self.key_id = key_id
        self.fetches = 0

    def keys_doc(self):
        pem = self.key.public_key().public_bytes(serialization.Encoding.PEM,
                                                 serialization.PublicFormat.SubjectPublicKeyInfo)
        self.fetches += 1
        return {"keys": [{"keyId": self.key_id, "pem": pem.decode("ascii"), "base64": "x"}]}

    def query(self, custom_data, txn="txn-1", unit=UNIT, ts_ms=None, key=None, key_id=None):
        params = [("ad_network", "5450213213286189855"), ("ad_unit", unit.rsplit("/", 1)[-1]),
                  ("custom_data", custom_data), ("reward_amount", "1"), ("reward_item", "conversion"),
                  ("timestamp", str(int(time.time() * 1000) if ts_ms is None else ts_ms)),
                  ("transaction_id", txn), ("user_id", "")]
        content = urlencode(params)
        sig = (key or self.key).sign(content.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        b64 = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
        return f"{content}&signature={b64}&key_id={key_id or self.key_id}"


# ---------------------------------------------------------------------------
# custom_data token
# ---------------------------------------------------------------------------
class TokenTests(unittest.TestCase):
    def test_round_trip_and_short(self):
        token, nonce, exp = ads.make_token(USER, SECRET, now=1000)
        self.assertLessEqual(len(token), 64)
        self.assertRegex(token, r"^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$")
        self.assertRegex(nonce, ads.NONCE_RE)
        self.assertEqual(exp, 1000 + ads.TOKEN_TTL)
        self.assertEqual(ads.read_token(token, SECRET, at=1500), {"user_id": USER, "nonce": nonce, "exp": exp})

    def test_tampered_wrong_secret_or_expired_is_refused(self):
        token, _, exp = ads.make_token(USER, SECRET, now=1000)
        body, sig = token.split(".")
        other_body = ads.make_token(OTHER, SECRET, now=1000)[0].split(".")[0]
        self.assertIsNone(ads.read_token(f"{other_body}.{sig}", SECRET, at=1500))   # another user id
        self.assertIsNone(ads.read_token(token, "t" * 40, at=1500))
        self.assertIsNone(ads.read_token(token, SECRET, at=exp + 1))
        for bad in ("", "abc", "a.b.c", token + "x", "." + sig, None, 42):
            self.assertIsNone(ads.read_token(bad, SECRET, at=1500), bad)

    def test_every_token_is_new(self):
        nonces = {ads.make_token(USER, SECRET)[1] for _ in range(500)}
        self.assertEqual(len(nonces), 500)


# ---------------------------------------------------------------------------
# Google's signature and the verifier keys cache
# ---------------------------------------------------------------------------
class SignatureTests(unittest.TestCase):
    def setUp(self):
        self.g = GoogleSigner()

    def test_valid_and_invalid_signatures(self):
        q = self.g.query("tok")
        content, sig, key_id = ads.split_signed_query(q.encode())
        self.assertEqual(key_id, str(self.g.key_id))
        self.assertFalse(content.endswith("&"))
        pub = self.g.key.public_key()
        self.assertTrue(ads.signature_ok(content, sig, pub))
        self.assertFalse(ads.signature_ok(content.replace("txn-1", "txn-2"), sig, pub))
        self.assertFalse(ads.signature_ok(content, sig, ec.generate_private_key(ec.SECP256R1()).public_key()))
        self.assertFalse(ads.signature_ok(content, "!!notbase64", pub))
        for bad in ("", "a=1", "signature=x&key_id=1", "a=1&signature=&key_id=1", "a=1&signature=x&key_id=z"):
            self.assertIsNone(ads.split_signed_query(bad), bad)

    def test_keys_cached_24h_and_unknown_ids_fetch_at_most_every_5_minutes(self):
        now = [1000.0]
        keys = ads.VerifierKeys(fetch=self.g.keys_doc, clock=lambda: now[0])
        kid = str(self.g.key_id)
        self.assertIsNotNone(keys.get(kid))
        self.assertIsNotNone(keys.get(kid))
        self.assertEqual(self.g.fetches, 1)
        self.assertIsNone(keys.get("123"))                     # unknown, keys just fetched: no refetch
        self.assertEqual(self.g.fetches, 1)
        now[0] += ads.KEYS_REFETCH_MIN + 1
        self.assertIsNone(keys.get("124"))                     # unknown: one refetch (Google rotated keys)
        self.assertIsNone(keys.get("125"))                     # throttled
        self.assertEqual(self.g.fetches, 2)
        now[0] += ads.KEYS_TTL
        keys.get(kid)
        self.assertEqual(self.g.fetches, 3)

    def test_fetch_failure_keeps_last_good_keys_else_raises(self):
        now = [1000.0]
        fail = [False]

        def fetch():
            if fail[0]:
                raise OSError("network")
            return self.g.keys_doc()
        keys = ads.VerifierKeys(fetch=fetch, clock=lambda: now[0])
        kid = str(self.g.key_id)
        keys.get(kid)
        fail[0] = True
        now[0] += ads.KEYS_TTL + 1
        self.assertIsNotNone(keys.get(kid))
        with self.assertRaises(OSError):
            ads.VerifierKeys(fetch=fetch).get(kid)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
class AdsApiTests(TempJobsDir):
    @classmethod
    def setUpClass(cls):
        env = {k: "" for k in ("PADDLE_ENV", "PADDLE_API_KEY", "PADDLE_WEBHOOK_SECRET", "PADDLE_PRICE_ID")}
        with mock.patch.dict(os.environ, env):
            import server  # noqa: F401
        cls.server = sys.modules["server"]

    def setUp(self):
        super().setUp()
        self.store = TriggerStore()
        self.ads = FakeAdStore(self.store)
        self.store.ads = self.ads
        self.rpc_calls = []
        self.free_allowed = False                     # the daily free conversion is used
        admin = mock.Mock()

        def rpc(name, args):
            self.rpc_calls.append(name)
            data = {"allowed": True, "previous": None}
            if name == "claim_free_conversion":
                data = {"allowed": self.free_allowed, "previous": "2026-11-01T00:00:00+00:00"}
            elif name == "claim_pro_conversion":
                data = {"allowed": True, "claim_id": 41}
            return mock.Mock(execute=mock.Mock(return_value=mock.Mock(data=data)))
        admin.rpc.side_effect = rpc
        self.profile = {"plan": "free", "last_free_conversion_at": "2026-11-23T10:00:00+00:00"}
        self.users = {}
        self.g = GoogleSigner()
        bp = self.server.app.blueprints["ads"]
        bp.verifier.fetch = self.g.keys_doc
        bp.verifier.keys, bp.verifier.fetched_at, bp.verifier.last_try = {}, None, None
        self.env = mock.patch.dict(os.environ, {"ADS_TOKEN_SECRET": SECRET, "ADS_REWARDED_AD_UNITS": UNIT,
                                                "ADS_MAX_REWARDS_PER_DAY": "0"})
        self.patches = [
            self.env,
            mock.patch.object(self.server, "_supabase_admin", return_value=admin),
            mock.patch.object(self.server, "get_profile", side_effect=lambda a, u: {**self.profile, "id": u}),
            mock.patch.object(self.server, "get_authenticated_user_id", side_effect=self.fake_auth),
            mock.patch.object(jobs, "JobStore", lambda admin: self.store),
            mock.patch.object(ads, "AdStore", lambda admin: self.ads),
        ]
        for p in self.patches:
            p.start()
        jobs.write_runner_beat()
        for name in ("jobs", "ads"):
            for limiter in self.server.app.blueprints[name].limiters.values():
                limiter._hits.clear()
        self.client = self.server.app.test_client()

    def tearDown(self):
        for p in reversed(self.patches):
            p.stop()
        super().tearDown()

    # helpers
    def fake_auth(self):
        user = self.users.get(self.server.request.headers.get("Authorization", ""))
        if user is None:
            return None, (self.server.jsonify({"code": "AUTH_REQUIRED"}), 401)
        return user, None

    def auth(self, user=USER):
        header = f"Bearer tok-{uuid.uuid4().hex}"
        self.users[header] = user
        return {"Authorization": header}

    def token(self, user=USER):
        r = self.client.post("/api/ads/reward-token", headers=self.auth(user))
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()

    def ssv(self, query):
        return self.client.get("/api/ads/ssv?" + query)

    def watch_ad(self, user=USER, txn=None):
        t = self.token(user)
        r = self.ssv(self.g.query(t["custom_data"], txn=txn or uuid.uuid4().hex))
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()["rewarded"])
        return t

    def post_pdf(self, user=USER, pages=1):
        path = self.dir / f"upload-{uuid.uuid4().hex}.pdf"
        text_pdf(path, pages)
        data = {"file": (io.BytesIO(path.read_bytes()), "Report.pdf")}
        path.unlink()
        return self.client.post("/api/jobs", data=data, headers=self.auth(user), content_type="multipart/form-data")

    # reward token
    def test_reward_token_for_free_users_only(self):
        t = self.token()
        self.assertEqual(ads.read_token(t["custom_data"], SECRET)["user_id"], USER)
        self.assertEqual(ads.read_token(t["custom_data"], SECRET)["nonce"], t["reward_id"])
        self.assertGreater(t["expires_in_seconds"], 3500)
        self.profile = {"plan": "pro", "subscription_status": "active"}
        r = self.client.post("/api/ads/reward-token", headers=self.auth())
        self.assertEqual((r.status_code, r.get_json()["code"]), (409, "ADS_NOT_FOR_PRO"))
        self.assertEqual(self.client.post("/api/ads/reward-token").status_code, 401)

    def test_reward_token_needs_the_secret(self):
        with mock.patch.dict(os.environ, {"ADS_TOKEN_SECRET": "short"}):
            r = self.client.post("/api/ads/reward-token", headers=self.auth())
        self.assertEqual((r.status_code, r.get_json()["code"]), (503, "ADS_UNAVAILABLE"))

    # SSV callback
    def test_ssv_stores_one_reward_for_the_token_user(self):
        t = self.token()
        q = self.g.query(t["custom_data"], txn="txn-A")
        r = self.ssv(q)
        self.assertEqual((r.status_code, r.get_json()["rewarded"]), (200, True))
        self.assertEqual(len(self.ads.rows), 1)
        row = self.ads.rows[0]
        self.assertEqual((row["user_id"], row["transaction_id"], row["token_nonce"]), (USER, "txn-A", t["reward_id"]))
        # Google retries / a replay of the same callback: 200, nothing added
        r = self.ssv(q)
        self.assertEqual((r.status_code, r.get_json()["rewarded"]), (200, False))
        # Same token, new transaction (a second ad on one token): nothing added
        r = self.ssv(self.g.query(t["custom_data"], txn="txn-B"))
        self.assertEqual(r.get_json()["rewarded"], False)
        self.assertEqual(len(self.ads.rows), 1)

    def test_ssv_refuses_forged_signatures(self):
        t = self.token()
        forged = self.g.query(t["custom_data"], key=ec.generate_private_key(ec.SECP256R1()))
        self.assertEqual(self.ssv(forged).status_code, 403)
        good = self.g.query(t["custom_data"])
        self.assertEqual(self.ssv(good.replace("reward_amount=1", "reward_amount=9")).status_code, 403)
        self.assertEqual(self.ssv(self.g.query(t["custom_data"], key_id=42)).status_code, 403)
        self.assertEqual(self.ssv("custom_data=" + t["custom_data"]).status_code, 403)
        self.assertEqual(self.ads.rows, [])

    def test_ssv_signed_but_not_rewardable_is_acknowledged_without_a_reward(self):
        t = self.token()
        other_secret = ads.make_token(USER, "x" * 40)[0]
        cases = [self.g.query(t["custom_data"], unit="ca-app-pub-9/999"),         # not our ad unit
                 self.g.query(""),                                                 # AdMob "verify URL" test
                 self.g.query(other_secret),                                       # not our token
                 self.g.query(t["custom_data"], ts_ms=int((time.time() + 7200) * 1000))]  # token expired
        for q in cases:
            r = self.ssv(q)
            self.assertEqual((r.status_code, r.get_json()["rewarded"]), (200, False))
        with mock.patch.dict(os.environ, {"ADS_REWARDED_AD_UNITS": ""}):
            self.assertEqual(self.ssv(self.g.query(t["custom_data"])).get_json()["rewarded"], False)
        self.assertEqual(self.ads.rows, [])

    def test_ssv_temporary_problems_are_503_so_google_retries(self):
        t = self.token()
        bp = self.server.app.blueprints["ads"]
        bp.verifier.fetch = mock.Mock(side_effect=OSError("down"))
        self.assertEqual(self.ssv(self.g.query(t["custom_data"])).status_code, 503)
        bp.verifier.fetch = self.g.keys_doc
        bp.verifier.last_try = None
        with mock.patch.object(self.ads, "add_reward", side_effect=RuntimeError("db")):
            self.assertEqual(self.ssv(self.g.query(t["custom_data"])).status_code, 503)
        self.assertEqual(self.ssv(self.g.query(t["custom_data"])).status_code, 200)

    # status
    def test_status_says_when_the_reward_arrived(self):
        t = self.token()
        url = f"/api/ads/status?reward_id={t['reward_id']}"
        body = self.client.get(url, headers=self.auth()).get_json()
        self.assertEqual((body["free_available"], body["reward_arrived"], body["rewards_available"],
                          body["can_watch_ad"]), (False, False, 0, True))
        self.ssv(self.g.query(t["custom_data"]))
        body = self.client.get(url, headers=self.auth()).get_json()
        self.assertEqual((body["reward_arrived"], body["rewards_available"]), (True, 1))
        # another user can't see it
        self.assertFalse(self.client.get(url, headers=self.auth(OTHER)).get_json()["reward_arrived"])
        self.assertEqual(self.client.get("/api/ads/status?reward_id=zz", headers=self.auth()).status_code, 400)
        self.assertEqual(self.client.get(url).status_code, 401)

    def test_status_for_pro_and_free_available(self):
        self.profile = {"plan": "free", "last_free_conversion_at": None}
        self.assertTrue(self.client.get("/api/ads/status", headers=self.auth()).get_json()["free_available"])
        self.profile = {"plan": "pro", "subscription_status": "active"}
        body = self.client.get("/api/ads/status", headers=self.auth()).get_json()
        self.assertEqual((body["plan"], body["can_watch_ad"], body["free_available"]), ("pro", False, False))

    # POST /api/jobs with a reward
    def test_job_after_the_free_conversion_needs_a_reward(self):
        r = self.post_pdf()
        body = r.get_json()
        self.assertEqual((r.status_code, body["code"], body["ad_allowed"]), (429, "FREE_LIMIT_REACHED", True))
        self.watch_ad()
        r = self.post_pdf()
        self.assertEqual(r.status_code, 202, r.get_json())
        job = r.get_json()["job_id"]
        row = self.store.rows[job]
        self.assertEqual((row["plan"], row["free_claimed"], row["pro_claim_id"]), ("free", False, None))
        self.assertEqual(self.ads.rows[0]["job_id"], job)
        self.assertNotIn("release_free_conversion", self.rpc_calls)

    def test_one_reward_is_one_conversion(self):
        self.watch_ad()
        job = self.post_pdf().get_json()["job_id"]
        self.store.rows[job]["status"] = "done"                       # first job finished
        jobs.write_state(job, status="done")
        r = self.post_pdf()
        self.assertEqual((r.status_code, r.get_json()["code"]), (429, "FREE_LIMIT_REACHED"))

    def test_rewards_are_per_user(self):
        self.watch_ad(user=OTHER)
        self.assertEqual(self.post_pdf(user=USER).status_code, 429)
        self.assertEqual(self.post_pdf(user=OTHER).status_code, 202)

    def test_free_conversion_is_used_first(self):
        self.free_allowed = True
        self.watch_ad()
        self.assertEqual(self.post_pdf().status_code, 202)
        self.assertIsNone(self.ads.rows[0]["consumed_at"])            # reward kept for later

    def test_failed_job_gives_the_reward_back_once(self):
        self.watch_ad()
        job = self.post_pdf().get_json()["job_id"]
        jobs.fail_job(self.store, job, "CONVERSION_FAILED")
        jobs.fail_job(self.store, job, "CONVERSION_FAILED")           # twice: still once
        reward = self.ads.rows[0]
        self.assertEqual((reward["consumed_at"], reward["job_id"], reward["times_returned"]), (None, None, 1))
        self.assertEqual(self.post_pdf().status_code, 202)            # usable again

    def test_cancelled_job_gives_the_reward_back(self):
        self.watch_ad()
        job = self.post_pdf().get_json()["job_id"]
        self.client.delete(f"/api/jobs/{job}", headers=self.auth())
        jobs_worker.Runner(self.store, spawn=lambda j: FakeProc()).tick()
        self.assertEqual(self.store.rows[job]["error_code"], "CANCELLED")
        self.assertIsNone(self.ads.rows[0]["consumed_at"])

    def test_downloaded_result_keeps_the_reward_used(self):
        self.watch_ad()
        job = self.post_pdf().get_json()["job_id"]
        self.store.rows[job]["status"] = "done"
        self.store.mark_downloaded(job, datetime.now(timezone.utc))
        jobs.expire_done_job(self.store, job, downloaded=True)
        jobs.fail_job(self.store, job, "JOB_EXPIRED", status="expired")
        self.assertEqual(self.ads.rows[0]["job_id"], job)
        self.assertIsNotNone(self.ads.rows[0]["consumed_at"])

    def test_refused_pdf_gives_the_reward_back(self):
        self.watch_ad()
        r = self.post_pdf(pages=21)                                   # Free page limit: 20
        self.assertEqual((r.status_code, r.get_json()["code"]), (400, "PAGE_LIMIT_EXCEEDED"))
        self.assertIsNone(self.ads.rows[0]["consumed_at"])
        with mock.patch.object(self.server.conversion, "plan_pdf", return_value=[("ocr", {"images": 1})] * 21):
            r = self.post_pdf()
        self.assertEqual((r.status_code, r.get_json()["max_scanned_pages"]), (413, 20))   # Free scanned cap
        self.assertIsNone(self.ads.rows[0]["consumed_at"])
        with mock.patch.object(self.server.conversion, "plan_pdf", return_value=[("ocr", {"images": 1})] * 20):
            self.assertEqual(self.post_pdf().status_code, 202)

    def test_job_insert_failure_gives_the_reward_back(self):
        self.watch_ad()
        with mock.patch.object(self.store, "insert", side_effect=RuntimeError("db")):
            self.assertEqual(self.post_pdf().status_code, 500)
        self.assertIsNone(self.ads.rows[0]["consumed_at"])

    def test_reward_lost_by_a_crash_heals_after_15_minutes(self):
        self.watch_ad()
        self.ads.claim(USER, "job-never-created", 0)                  # server died before the insert
        self.assertEqual(self.post_pdf().status_code, 429)
        self.ads.rows[0]["consumed_at"] -= 901
        self.assertEqual(self.post_pdf().status_code, 202)

    def test_old_used_reward_never_comes_back_when_its_job_row_is_gone(self):
        self.watch_ad()
        job = self.post_pdf().get_json()["job_id"]
        self.store.rows[job]["status"] = "done"
        jobs.write_state(job, status="done")
        self.ads.rows[0]["consumed_at"] -= 86400 + 1                  # used more than 24 h ago
        del self.store.rows[job]                                      # old conversion_jobs row cleaned
        self.assertEqual(self.post_pdf().status_code, 429)
        self.assertEqual(self.ads.rows[0]["job_id"], job)
        self.assertEqual(self.ads.rows[0]["times_returned"], 0)

    def test_hidden_daily_cap(self):
        with mock.patch.dict(os.environ, {"ADS_MAX_REWARDS_PER_DAY": "1"}):
            self.watch_ad()
            self.watch_ad()                                           # stocked before the cap was hit
            job = self.post_pdf().get_json()["job_id"]
            self.store.rows[job]["status"] = "done"
            jobs.write_state(job, status="done")
            r = self.post_pdf()
            body = r.get_json()
            self.assertEqual((r.status_code, body["code"], body["ad_allowed"]), (429, "FREE_LIMIT_REACHED", False))
            self.assertNotIn("1", re.sub(r"\d+ files per day", "", body["error"]))  # number not shown
            r = self.client.post("/api/ads/reward-token", headers=self.auth())
            self.assertEqual((r.status_code, r.get_json()["code"]), (429, "AD_LIMIT_REACHED"))
            self.assertFalse(self.client.get("/api/ads/status", headers=self.auth()).get_json()["can_watch_ad"])

    def test_pro_never_uses_rewards(self):
        self.watch_ad()
        self.profile = {"plan": "pro", "subscription_status": "active"}
        self.assertEqual(self.post_pdf().status_code, 202)
        self.assertIsNone(self.ads.rows[0]["consumed_at"])

    def test_before_sql_005_the_free_limit_answer_is_unchanged(self):
        with mock.patch.object(self.ads, "claim", side_effect=RuntimeError("function does not exist")):
            r = self.post_pdf()
        self.assertEqual((r.status_code, r.get_json()["code"]), (429, "FREE_LIMIT_REACHED"))

    def test_convert_endpoint_untouched(self):
        src = inspect.getsource(self.server.convert)
        self.assertNotIn("ads", src)
        self.assertNotIn("claim_quota", src)
        rules = {r.rule for r in self.server.app.url_map.iter_rules()}
        self.assertTrue({"/api/convert", "/api/ads/ssv", "/api/ads/status", "/api/ads/reward-token"} <= rules)


# ---------------------------------------------------------------------------
# Files: SQL 005, app-ads.txt, Docker image
# ---------------------------------------------------------------------------
class FileTests(unittest.TestCase):
    def test_app_ads_txt(self):
        text = (SITE / "app-ads.txt").read_text(encoding="utf-8")
        lines = [l.strip() for l in text.splitlines() if l.strip() and not l.startswith("#")]
        self.assertEqual(len(lines), 1)
        self.assertRegex(lines[0], r"^google\.com, pub-\d{16}, DIRECT, f08c47fec0942fa0$")

    def test_ads_module_is_in_the_image(self):
        from test_billing import docker_ignored
        lines = (SITE / ".dockerignore").read_text(encoding="utf-8").splitlines()
        patterns = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
        self.assertFalse(docker_ignored("ads.py", patterns))

    def test_sql_005_is_additive_and_locked_down(self):
        sql_path = SITE.parent / "docs" / "sql" / "005_ad_rewards.sql"   # WORD repo, when next to the site
        if not sql_path.is_file():
            self.skipTest("WORD docs/sql not next to the site")
        sql = sql_path.read_text(encoding="utf-8")
        body = sql.split("-- Checks", 1)[0]
        code = "\n".join(l.split("--", 1)[0] for l in body.splitlines()).lower()
        for banned in ("drop table", "drop function", "alter table public.conversion_jobs",
                       "function public.release_job_quota", "function public.claim_free_conversion",
                       "alter table public.profiles", "drop policy"):
            self.assertNotIn(banned, code)
        for needed in ("alter table public.ad_rewards enable row level security",
                       "revoke all on table public.ad_rewards from public, anon, authenticated",
                       "transaction_id  text not null unique", "token_nonce     text not null unique",
                       "when (old.quota_released_at is null and new.quota_released_at is not null)",
                       "and a.consumed_at > now() - interval '24 hours'",
                       "begin;", "commit;"):
            self.assertIn(needed, code)
        for fn in ("claim_ad_reward(uuid, text, integer)", "return_ad_reward(text)",
                   "ad_reward_status(uuid, text)"):
            self.assertIn(f"revoke all on function public.{fn} from public, anon, authenticated", code)
            self.assertIn(f"grant execute on function public.{fn} to service_role", code)
        self.assertEqual(code.count("security definer"), code.count("set search_path = public"))


if __name__ == "__main__":
    unittest.main()
