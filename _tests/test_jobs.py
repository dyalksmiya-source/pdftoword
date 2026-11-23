"""
Conversion jobs (jobs.py, jobs_api.py, jobs_worker.py, start.py).
Run from website/:  python -m unittest _tests.test_jobs   (or discover -s _tests)

Supabase is replaced by FakeStore (same rules as SQL 004: one active job per user, quota
released once and never after a download).
"""
import io
import os
import shutil
import sys
import tempfile
import time
import unittest
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SITE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SITE))

import fitz  # noqa: E402

import jobs  # noqa: E402
import jobs_worker  # noqa: E402
import start  # noqa: E402

USER = "3f1c2a7e-6b1d-4c55-9a8e-2d3b4c5d6e7f"
OTHER = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=timezone.utc)


class FakeStore:
    """In-memory conversion_jobs + release_job_quota, as in docs/sql/004_conversion_jobs.sql."""

    def __init__(self):
        self.rows = {}
        self.released = []        # job ids whose quota was really given back

    def insert(self, row):
        if any(r["user_id"] == row["user_id"] and r["status"] in ("queued", "running")
               for r in self.rows.values()):
            raise Exception('duplicate key value violates unique constraint (23505)')
        self.rows[row["id"]] = {"created_at": jobs.iso(datetime.now(timezone.utc)), "downloaded_at": None,
                                "quota_released_at": None, "heartbeat_at": None, "error_code": None, **row}

    def get(self, job_id, user_id=None):
        r = self.rows.get(job_id)
        if r is None or (user_id is not None and r["user_id"] != user_id):
            return None
        return dict(r)

    def open_jobs(self):
        return [dict(r) for r in self.rows.values() if r["status"] in ("queued", "running", "done")]

    def mark_running(self, job_id, slot, now):
        r = self.rows.get(job_id)
        if r and r["status"] == "queued":
            r.update(status="running", slot=slot, started_at=jobs.iso(now), heartbeat_at=jobs.iso(now))
            return True
        return False

    def heartbeat(self, job_ids, now):
        for j in job_ids:
            if self.rows.get(j, {}).get("status") == "running":
                self.rows[j]["heartbeat_at"] = jobs.iso(now)

    def mark_done(self, job_id, pages_total, now):
        r = self.rows.get(job_id)
        if r and r["status"] == "running":
            r.update(status="done", pages_done=pages_total,
                     expires_at=jobs.iso(now + timedelta(seconds=jobs.JOB_RESULT_TTL)))
            return True
        return False

    def release(self, job_id, status, code):
        r = self.rows.get(job_id)
        if r is None:
            return False
        if r["quota_released_at"] is None and r["downloaded_at"] is None:
            r.update(status=status, error_code=code, quota_released_at="now")
            self.released.append(job_id)
            return True
        r["status"] = status
        r["error_code"] = r["error_code"] or code
        return False

    def set_expired(self, job_id, code="JOB_EXPIRED"):
        r = self.rows.get(job_id)
        if r and r["status"] == "done":
            r.update(status="expired", error_code=code)

    def mark_downloaded(self, job_id, now):
        r = self.rows.get(job_id)
        if not r or r["status"] != "done" or r["quota_released_at"] is not None:
            return False
        r["downloaded_at"] = r["downloaded_at"] or jobs.iso(now)
        return True


def text_pdf(path, pages=1):
    doc = fitz.open()
    for k in range(pages):
        page = doc.new_page()
        page.insert_text((72, 100), f"Page {k + 1} has enough plain words for the converter here.", fontsize=12)
    doc.save(str(path))
    doc.close()


def blank_pdf(path):
    doc = fitz.open()
    doc.new_page()
    doc.save(str(path))
    doc.close()


class TempJobsDir(unittest.TestCase):
    def setUp(self):
        self.dir = Path(tempfile.mkdtemp(prefix="jobs-test-"))
        self.patch = mock.patch.object(jobs, "JOBS_DIR", self.dir)
        self.patch.start()
        self.store = FakeStore()

    def tearDown(self):
        self.patch.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def add_job(self, user=USER, status="queued", size="small", cost=6, created=None, pdf=True, docx=False,
                row_extra=None, state_extra=None):
        job_id = jobs.new_job_id()
        p = jobs.paths(job_id)
        jobs.ensure_dir()
        if pdf:
            text_pdf(p["pdf"])
        if docx:
            p["docx"].write_bytes(b"PK docx")
        created = time.time() if created is None else created
        jobs.write_json(p["meta"], {"user_id": user, "download_name": "a.docx", "created": created,
                                    "size": size, "cost": cost, "pages": 1, "scanned": 0, "max_ocr_pages": 20})
        jobs.write_json(p["state"], {"status": status, "pages_done": 0, "pages_total": 1, **(state_extra or {})})
        self.store.rows[job_id] = {
            "id": job_id, "user_id": user, "status": status, "created_at": jobs.iso(NOW),
            "downloaded_at": None, "quota_released_at": None, "heartbeat_at": None, "error_code": None,
            "expires_at": jobs.iso(NOW + timedelta(hours=1)), **(row_extra or {})}
        return job_id


# ---------------------------------------------------------------------------
# Scheduling: reserved small-job slot
# ---------------------------------------------------------------------------
class SchedulingTests(unittest.TestCase):
    def q(self, job_id, size, cost, created):
        return {"id": job_id, "size": size, "cost": cost, "created": created}

    def test_small_slot_runs_a_small_job_while_a_large_one_runs(self):
        queued = [self.q("big2", "large", 300, 0), self.q("one", "small", 6, 50)]
        self.assertEqual(jobs.pick_next(queued, [{"slot": "any"}], 2, 60), [("one", "small")])

    def test_two_large_jobs_never_run_together(self):
        queued = [self.q("big2", "large", 300, 0), self.q("big3", "large", 200, 0)]
        self.assertEqual(jobs.pick_next(queued, [{"slot": "any"}], 2, 10), [])

    def test_both_slots_free(self):
        queued = [self.q("big", "large", 300, 0), self.q("one", "small", 6, 0)]
        self.assertEqual(jobs.pick_next(queued, [], 2, 1), [("one", "small"), ("big", "any")])

    def test_small_job_can_also_use_the_any_slot(self):
        queued = [self.q("a", "small", 6, 0), self.q("b", "small", 12, 0)]
        self.assertEqual(jobs.pick_next(queued, [], 2, 1), [("a", "small"), ("b", "any")])

    def test_one_slot_shortest_first_with_aging(self):
        queued = [self.q("big", "large", 300, 0), self.q("one", "small", 6, 100)]
        self.assertEqual(jobs.pick_next(queued, [], 1, 110), [("one", "any")])
        # a large job that waited longer than its extra work beats a small job that just arrived
        late = [self.q("big", "large", 300, 0), self.q("new", "small", 6, 990)]
        self.assertEqual(jobs.pick_next(late, [], 1, 1000), [("big", "any")])
        self.assertEqual(jobs.pick_next(queued, [{"slot": "any"}], 1, 110), [])

    def test_queue_position_follows_the_same_order(self):
        queued = [self.q("big", "large", 300, 0), self.q("one", "small", 6, 100)]
        self.assertEqual(jobs.queue_position("one", queued, 110), 1)
        self.assertEqual(jobs.queue_position("big", queued, 110), 2)
        self.assertIsNone(jobs.queue_position("x", queued, 110))

    def test_size_class_and_estimate(self):
        self.assertEqual(jobs.size_class(5, 20), "small")
        self.assertEqual(jobs.size_class(6, 6), "large")
        self.assertEqual(jobs.size_class(0, 21), "large")
        self.assertEqual(jobs.estimate_seconds(10, 12), 61)

    def test_ids_are_random_and_checked(self):
        a, b = jobs.new_job_id(), jobs.new_job_id()
        self.assertNotEqual(a, b)
        self.assertTrue(jobs.valid_id(a))
        for bad in ("../../etc/passwd", "x" * 42, "", None, a + "a", "a" * 42 + "/"):
            self.assertFalse(jobs.valid_id(bad))

    def test_owner_limits(self):
        self.assertEqual((jobs.JOB_MAX_OCR_PAGES_PRO, jobs.JOB_MAX_OCR_PAGES_FREE), (50, 20))
        self.assertEqual((jobs.JOB_TIME_BUDGET, jobs.JOB_QUEUE_TIMEOUT, jobs.JOB_RESULT_TTL), (600, 900, 3600))


# ---------------------------------------------------------------------------
# Token cache
# ---------------------------------------------------------------------------
def jwt(exp):
    import base64
    import json
    body = base64.urlsafe_b64encode(json.dumps({"exp": exp}).encode()).decode().rstrip("=")
    return f"eyJhbGciOiJIUzI1NiJ9.{body}.sig"


class TokenCacheTests(unittest.TestCase):
    def test_at_most_60_seconds(self):
        c = jobs.TokenCache(ttl=60)
        tok = jwt(10_000)
        c.put(tok, USER, now=1000)
        self.assertEqual(c.get(tok, now=1059), USER)
        self.assertIsNone(c.get(tok, now=1060))

    def test_never_past_the_token_expiry(self):
        c = jobs.TokenCache(ttl=60)
        tok = jwt(1010)
        c.put(tok, USER, now=1000)
        self.assertEqual(c.get(tok, now=1009), USER)
        self.assertIsNone(c.get(tok, now=1010))

    def test_expired_token_is_not_cached_and_keys_are_hashes(self):
        c = jobs.TokenCache(ttl=60)
        c.put(jwt(900), USER, now=1000)
        self.assertEqual(c._items, {})
        tok = jwt(5000)
        c.put(tok, USER, now=1000)
        self.assertNotIn(tok, c._items)
        self.assertIn(jobs.TokenCache.key(tok), c._items)

    def test_size_is_bounded(self):
        c = jobs.TokenCache(ttl=60, max_size=3)
        for i in range(10):
            c.put(jwt(5000 + i), USER, now=1000)
        self.assertLessEqual(len(c._items), 3)


# ---------------------------------------------------------------------------
# Sweeper and expiry
# ---------------------------------------------------------------------------
class SweepTests(TempJobsDir):
    def test_startup_ends_running_jobs_and_releases_once(self):
        job = self.add_job(status="running")
        self.assertEqual(jobs.sweep(self.store, now=NOW, startup=True), 1)
        self.assertEqual(self.store.rows[job]["status"], "failed")
        self.assertEqual(self.store.rows[job]["error_code"], "CONVERSION_INTERRUPTED")
        jobs.sweep(self.store, now=NOW, startup=True)
        jobs.fail_job(self.store, job, "CONVERSION_INTERRUPTED")
        self.assertEqual(self.store.released, [job])                  # never twice
        self.assertFalse(jobs.paths(job)["pdf"].exists())

    def test_running_job_of_this_runner_is_left_alone(self):
        job = self.add_job(status="running", row_extra={"heartbeat_at": jobs.iso(NOW - timedelta(hours=1))})
        jobs.sweep(self.store, now=NOW, running_here={job})
        self.assertEqual(self.store.rows[job]["status"], "running")

    def test_stale_heartbeat_is_interrupted(self):
        job = self.add_job(status="running", row_extra={"heartbeat_at": jobs.iso(NOW - timedelta(minutes=4))})
        jobs.sweep(self.store, now=NOW)
        self.assertEqual(self.store.rows[job]["error_code"], "CONVERSION_INTERRUPTED")

    def test_queued_too_long_fails_and_releases(self):
        job = self.add_job(row_extra={"created_at": jobs.iso(NOW - timedelta(minutes=16))})
        jobs.sweep(self.store, now=NOW)
        self.assertEqual(self.store.rows[job]["error_code"], "QUEUE_TIMEOUT")
        self.assertEqual(self.store.released, [job])

    def test_wiped_disk_queued_job_fails(self):
        job = self.add_job(pdf=False)
        jobs.sweep(self.store, now=NOW, startup=True)
        self.assertEqual(self.store.rows[job]["error_code"], "CONVERSION_INTERRUPTED")
        self.assertEqual(self.store.released, [job])

    def test_wiped_disk_done_job_expires_released_if_never_downloaded(self):
        never = self.add_job(status="done", pdf=False, docx=False)
        got = self.add_job(user=OTHER, status="done", pdf=False, docx=False,
                           row_extra={"downloaded_at": jobs.iso(NOW)})
        jobs.sweep(self.store, now=NOW, startup=True)
        self.assertEqual(self.store.rows[never]["status"], "expired")
        self.assertEqual(self.store.rows[got]["status"], "expired")
        self.assertEqual(self.store.released, [never])                # downloaded: still counted

    def test_done_job_after_its_hour(self):
        job = self.add_job(status="done", pdf=False, docx=True,
                           row_extra={"expires_at": jobs.iso(NOW - timedelta(seconds=1))})
        jobs.sweep(self.store, now=NOW)
        self.assertEqual(self.store.rows[job]["status"], "expired")
        self.assertFalse(jobs.paths(job)["docx"].exists())
        self.assertEqual(jobs.local_job(job)["state"]["code"], "JOB_EXPIRED")


# ---------------------------------------------------------------------------
# One job (child process code, run in this process)
# ---------------------------------------------------------------------------
class RunOneTests(TempJobsDir):
    def test_text_pdf_is_done_and_the_upload_deleted(self):
        job = self.add_job(status="running")
        self.assertEqual(jobs_worker.run_one(job, store=self.store), "done")
        p = jobs.paths(job)
        self.assertTrue(p["docx"].exists())
        self.assertFalse(p["pdf"].exists())
        state = jobs.local_job(job)["state"]
        self.assertEqual((state["status"], state["pages_done"], state["pages_total"]), ("done", 1, 1))
        self.assertEqual(self.store.rows[job]["status"], "done")
        self.assertEqual(self.store.released, [])

    def test_blank_pdf_fails_and_releases(self):
        job = self.add_job(status="running", pdf=False)
        blank_pdf(jobs.paths(job)["pdf"])
        self.assertEqual(jobs_worker.run_one(job, store=self.store), "failed")
        self.assertEqual(self.store.rows[job]["error_code"], "EMPTY_DOCUMENT")
        self.assertEqual(self.store.released, [job])

    def test_cancel_flag_stops_the_job(self):
        job = self.add_job(status="running")
        jobs.touch(jobs.paths(job)["cancel"])
        jobs_worker.run_one(job, store=self.store)
        self.assertEqual(self.store.rows[job]["error_code"], "CANCELLED")
        self.assertEqual(self.store.released, [job])
        self.assertFalse(jobs.paths(job)["docx"].exists())

    def test_time_budget(self):
        job = self.add_job(status="running")
        with mock.patch.object(jobs, "JOB_TIME_BUDGET", -1):
            jobs_worker.run_one(job, store=self.store)
        self.assertEqual(self.store.rows[job]["error_code"], "CONVERSION_TIMEOUT")
        self.assertEqual(self.store.released, [job])

    def test_record_gone_deletes_the_result(self):
        job = self.add_job(status="running")
        del self.store.rows[job]                     # account deleted meanwhile (on delete cascade)
        self.assertEqual(jobs_worker.run_one(job, store=self.store), "gone")
        self.assertFalse(jobs.paths(job)["docx"].exists())


# ---------------------------------------------------------------------------
# Runner loop (children faked)
# ---------------------------------------------------------------------------
class FakeProc:
    def __init__(self, code=None):
        self.code = code
        self.killed = False
        self.terminated = False

    def poll(self):
        return self.code

    def kill(self):
        self.killed = True
        self.code = -9

    def terminate(self):
        self.terminated = True
        self.code = 0

    def send_signal(self, _sig):
        self.terminate()

    def wait(self, timeout=None):
        return self.code


class RunnerTests(TempJobsDir):
    def runner(self):
        self.procs = {}

        def spawn(job_id):
            self.procs[job_id] = FakeProc()
            return self.procs[job_id]
        r = jobs_worker.Runner(self.store, spawn=spawn)
        r.last_sweep = time.time()
        return r

    def test_starts_one_large_and_one_small_job(self):
        big = self.add_job(size="large", cost=300)
        big2 = self.add_job(user=OTHER, size="large", cost=200)
        small = self.add_job(user="u3", size="small", cost=6)
        r = self.runner()
        r.tick()
        self.assertEqual(set(r.children), {small, big2})
        self.assertEqual(self.store.rows[small]["slot"], "small")
        self.assertEqual(self.store.rows[big]["status"], "queued")

    def test_crashed_child_ends_its_job(self):
        job = self.add_job()
        r = self.runner()
        r.tick()
        self.procs[job].code = 1                      # killed by out-of-memory, for example
        r.tick()
        self.assertEqual(self.store.rows[job]["error_code"], "CONVERSION_FAILED")
        self.assertEqual(self.store.released, [job])
        self.assertEqual(r.children, {})

    def test_overdue_child_is_killed(self):
        job = self.add_job()
        r = self.runner()
        r.tick()
        r.children[job]["started"] -= jobs.JOB_KILL_AFTER + 1
        r.tick()
        self.assertTrue(self.procs[job].killed)
        self.assertEqual(self.store.rows[job]["error_code"], "CONVERSION_TIMEOUT")

    def test_cancelled_queued_job(self):
        job = self.add_job()
        jobs.touch(jobs.paths(job)["cancel"])
        self.runner().tick()
        self.assertEqual(self.store.rows[job]["error_code"], "CANCELLED")
        self.assertEqual(self.store.released, [job])

    def test_deleted_result_expires_and_counts_only_if_downloaded(self):
        kept = self.add_job(status="done", pdf=False, docx=False, state_extra={"expires_at": time.time() + 999})
        got = self.add_job(user=OTHER, status="done", pdf=False, docx=False,
                           state_extra={"expires_at": time.time() + 999}, row_extra={"downloaded_at": "x"})
        jobs.touch(jobs.paths(got)["downloaded"])
        self.runner().tick()
        self.assertEqual(self.store.released, [kept])
        self.assertEqual(self.store.rows[got]["status"], "expired")

    def test_idle_runner_does_not_call_supabase(self):
        store = mock.Mock()
        r = jobs_worker.Runner(store, spawn=lambda j: FakeProc())
        r.last_sweep = time.time() - 3600
        r.tick()
        self.assertEqual(store.mock_calls, [])


# ---------------------------------------------------------------------------
# Supervisor (start.py)
# ---------------------------------------------------------------------------
class SupervisorTests(unittest.TestCase):
    def make(self):
        self.t = [0.0]
        self.started = []

        def popen(cmd):
            proc = FakeProc()
            self.started.append((cmd, proc))
            return proc
        return start.Supervisor(["gunicorn"], ["runner"], popen=popen, clock=lambda: self.t[0])

    def test_runner_restarts_with_backoff(self):
        s = self.make()
        s.start()
        self.assertEqual([c for c, _ in self.started], [["gunicorn"], ["runner"]])
        delays = []
        for _ in range(6):
            self.started[-1][1].code = 1          # runner dies at once
            s.step()
            delays.append(s.delay)
            self.t[0] = s.restart_at
            s.step()
        self.assertEqual(delays, [2, 4, 8, 16, 30, 30])
        self.assertEqual(len([c for c, _ in self.started if c == ["runner"]]), 7)

    def test_backoff_resets_after_a_healthy_run(self):
        self.assertEqual(start.next_delay(30, 301), 2)
        self.assertEqual(start.next_delay(4, 10), 8)

    def test_web_exit_stops_everything(self):
        s = self.make()
        s.start()
        self.started[0][1].code = 3
        self.assertEqual(s.step(), 3)
        self.assertTrue(self.started[1][1].code is not None)


# ---------------------------------------------------------------------------
# API (Flask test client, Supabase faked)
# ---------------------------------------------------------------------------
class ApiTests(TempJobsDir):
    @classmethod
    def setUpClass(cls):
        env = {k: "" for k in ("PADDLE_ENV", "PADDLE_API_KEY", "PADDLE_WEBHOOK_SECRET", "PADDLE_PRICE_ID")}
        with mock.patch.dict(os.environ, env):
            import server  # noqa: F401
        cls.server = sys.modules["server"]

    def setUp(self):
        super().setUp()
        self.rpc_calls = []
        self.free_allowed = True
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
        self.profile = {"id": USER, "plan": "free"}
        self.users = {}
        self.patches = [
            mock.patch.object(self.server, "_supabase_admin", return_value=admin),
            mock.patch.object(self.server, "get_profile", side_effect=lambda a, u: {**self.profile, "id": u}),
            mock.patch.object(self.server, "get_authenticated_user_id",
                              side_effect=lambda: (self.users[self.server.request.headers["Authorization"]], None)),
            mock.patch.object(jobs, "JobStore", lambda admin: self.store),
        ]
        for p in self.patches:
            p.start()
        jobs.write_runner_beat()
        for limiter in self.server.app.blueprints["jobs"].limiters.values():
            limiter._hits.clear()
        self.client = self.server.app.test_client()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        super().tearDown()

    def auth(self, user=USER):
        header = f"Bearer tok-{uuid.uuid4().hex}"
        self.users[header] = user
        return {"Authorization": header}

    def post_pdf(self, headers, pages=1, name="Report.pdf"):
        path = self.dir / f"upload-{uuid.uuid4().hex}.pdf"
        text_pdf(path, pages)
        data = {"file": (io.BytesIO(path.read_bytes()), name)}
        path.unlink()
        return self.client.post("/api/jobs", data=data, headers=headers, content_type="multipart/form-data")

    def test_create_claims_quota_and_queues(self):
        r = self.post_pdf(self.auth())
        self.assertEqual(r.status_code, 202, r.get_json())
        job = r.get_json()["job_id"]
        self.assertTrue(jobs.valid_id(job))
        row = self.store.rows[job]
        self.assertEqual((row["plan"], row["free_claimed"], row["status"]), ("free", True, "queued"))
        self.assertEqual(row["free_previous"], "2026-11-01T00:00:00+00:00")
        self.assertEqual(self.rpc_calls, ["claim_free_conversion"])
        self.assertTrue(jobs.paths(job)["pdf"].exists())

    def test_runner_dead_is_server_busy_before_any_claim(self):
        jobs.beat_file().write_text(str(time.time() - 31))
        r = self.post_pdf(self.auth())
        self.assertEqual((r.status_code, r.get_json()["code"]), (503, "SERVER_BUSY"))
        self.assertEqual(self.rpc_calls, [])

    def test_queue_full_is_server_busy(self):
        for i in range(jobs.JOB_QUEUE_MAX):
            self.add_job(user=f"user-{i}")
        r = self.post_pdf(self.auth())
        self.assertEqual((r.status_code, r.get_json()["code"]), (503, "SERVER_BUSY"))
        self.assertEqual(self.rpc_calls, [])

    def test_one_active_job_per_user(self):
        h = self.auth()
        first = self.post_pdf(h).get_json()["job_id"]
        r = self.post_pdf(self.auth())
        self.assertEqual((r.status_code, r.get_json()["code"], r.get_json()["job_id"]), (409, "JOB_ACTIVE", first))

    def test_free_limit_reached(self):
        self.free_allowed = False
        r = self.post_pdf(self.auth())
        self.assertEqual((r.status_code, r.get_json()["code"]), (429, "FREE_LIMIT_REACHED"))
        self.assertEqual(self.store.rows, {})

    def test_scanned_caps_free_20_pro_50(self):
        for plan, pages, ok in (("free", 21, False), ("pro", 50, True), ("pro", 51, False)):
            self.profile = {"plan": "pro", "subscription_status": "active"} if plan == "pro" else {"plan": "free"}
            self.rpc_calls.clear()
            with mock.patch.object(self.server.conversion, "plan_pdf",
                                   return_value=[("ocr", {"images": 1})] * pages):
                r = self.post_pdf(self.auth(user=f"{plan}-{pages}"))
            if ok:
                self.assertEqual(r.status_code, 202, (plan, pages))
            else:
                body = r.get_json()
                self.assertEqual((r.status_code, body["code"]), (413, "SCANNED_PAGE_LIMIT"), (plan, pages))
                self.assertEqual(body["max_scanned_pages"], 20 if plan == "free" else 50)
                self.assertIn("release_free_conversion" if plan == "free" else "release_pro_conversion",
                              self.rpc_calls)

    def test_status_owner_only(self):
        job = self.post_pdf(self.auth()).get_json()["job_id"]
        mine = self.client.get(f"/api/jobs/{job}", headers=self.auth())
        self.assertEqual(mine.status_code, 200)
        self.assertEqual((mine.get_json()["status"], mine.get_json()["position"]), ("queued", 1))
        other = self.client.get(f"/api/jobs/{job}", headers=self.auth(OTHER))
        self.assertEqual((other.status_code, other.get_json()["code"]), (404, "JOB_NOT_FOUND"))
        self.assertEqual(self.client.get(f"/api/jobs/{job}/download", headers=self.auth(OTHER)).status_code, 404)
        self.assertEqual(self.client.delete(f"/api/jobs/{job}", headers=self.auth(OTHER)).status_code, 404)
        self.assertEqual(self.client.get("/api/jobs/not-a-job", headers=self.auth()).status_code, 404)
        self.assertEqual(self.client.get(f"/api/jobs/{job}").status_code, 401)

    def test_status_rate_limit_per_user(self):
        job = self.add_job()
        h = self.auth()
        codes = [self.client.get(f"/api/jobs/{job}", headers=h).status_code for _ in range(16)]
        self.assertEqual(codes[:15], [200] * 15)
        self.assertEqual(codes[15], 429)
        other = self.add_job(user=OTHER)
        self.assertEqual(self.client.get(f"/api/jobs/{other}", headers=self.auth(OTHER)).status_code, 200)

    def test_token_cache_skips_supabase_on_polls(self):
        job = self.add_job()
        h = self.auth()
        for _ in range(5):
            self.client.get(f"/api/jobs/{job}", headers=h)
        self.assertEqual(self.server.get_authenticated_user_id.call_count, 1)

    def test_download_several_times_then_delete(self):
        job = self.add_job(status="done", pdf=False, docx=True,
                           state_extra={"expires_at": time.time() + 3000, "pages_done": 1},
                           row_extra={})
        self.store.rows[job]["status"] = "done"
        h = self.auth()
        st = self.client.get(f"/api/jobs/{job}", headers=h).get_json()
        self.assertEqual(st["status"], "done")
        self.assertGreater(st["expires_in_seconds"], 0)
        for _ in range(3):                                  # mobile network dropped: download again
            r = self.client.get(f"/api/jobs/{job}/download", headers=h)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.data, b"PK docx")
            self.assertIn("a.docx", r.headers["Content-Disposition"])
            r.close()
        self.assertIsNotNone(self.store.rows[job]["downloaded_at"])
        r = self.client.delete(f"/api/jobs/{job}", headers=h)
        self.assertEqual(r.get_json()["deleted"], True)
        self.assertFalse(jobs.paths(job)["docx"].exists())
        st = self.client.get(f"/api/jobs/{job}", headers=h).get_json()
        self.assertEqual((st["status"], st["code"], st["counted"]), ("expired", "JOB_EXPIRED", True))
        jobs_worker.Runner(self.store, spawn=lambda j: FakeProc()).tick()
        self.assertEqual(self.store.released, [])          # downloaded: the conversion counts

    def test_cancel_a_queued_job(self):
        job = self.post_pdf(self.auth()).get_json()["job_id"]
        r = self.client.delete(f"/api/jobs/{job}", headers=self.auth())
        self.assertEqual((r.status_code, r.get_json()["status"]), (202, "cancelling"))
        runner = jobs_worker.Runner(self.store, spawn=lambda j: FakeProc())
        runner.tick()
        self.assertEqual(self.store.rows[job]["error_code"], "CANCELLED")
        self.assertEqual(self.store.released, [job])

    def test_disk_wiped_done_job_says_expired_and_not_counted(self):
        job = self.add_job(status="done", pdf=False)
        self.store.rows[job]["status"] = "done"
        jobs.delete_job_files(job, keep_records=False)       # Serverless sleep: local disk empty
        r = self.client.get(f"/api/jobs/{job}", headers=self.auth())
        body = r.get_json()
        self.assertEqual((body["status"], body["code"], body["counted"]), ("expired", "JOB_EXPIRED", False))
        self.assertIn("does not count", body["error"])
        self.assertEqual(self.store.released, [job])
        self.assertEqual(self.client.get(f"/api/jobs/{job}", headers=self.auth(OTHER)).status_code, 404)

    def test_disk_wiped_running_job_is_interrupted(self):
        job = self.add_job(status="running")
        jobs.delete_job_files(job, keep_records=False)
        body = self.client.get(f"/api/jobs/{job}", headers=self.auth()).get_json()
        self.assertEqual((body["status"], body["code"]), ("failed", "CONVERSION_INTERRUPTED"))
        self.assertEqual(self.store.released, [job])

    def test_failed_job_status_carries_the_code(self):
        job = self.add_job(status="failed", state_extra={"code": "SCANNED_PAGE_LIMIT", "scanned_pages": 60,
                                                          "max_scanned_pages": 50})
        body = self.client.get(f"/api/jobs/{job}", headers=self.auth()).get_json()
        self.assertEqual((body["code"], body["scanned_pages"], body["counted"]), ("SCANNED_PAGE_LIMIT", 60, False))

    def test_health_reports_the_runner(self):
        self.assertEqual(self.client.get("/api/health").get_json()["runner"], "alive")
        jobs.beat_file().unlink()
        body = self.client.get("/api/health").get_json()
        self.assertEqual((body["status"], body["runner"]), ("ok", "dead"))

    def test_old_convert_endpoint_still_registered(self):
        rules = {r.rule for r in self.server.app.url_map.iter_rules()}
        self.assertTrue({"/api/convert", "/api/jobs", "/api/jobs/<job_id>", "/api/jobs/<job_id>/download"} <= rules)


class ImageTests(unittest.TestCase):
    def test_dockerfile_starts_the_supervisor(self):
        docker = (SITE / "Dockerfile").read_text(encoding="utf-8")
        self.assertIn("CMD exec python start.py gunicorn ", docker)
        self.assertIn("--timeout 300", docker)          # /api/convert unchanged

    def test_new_modules_are_in_the_image(self):
        sys.path.insert(0, str(SITE / "_tests"))
        from test_billing import docker_ignored
        lines = (SITE / ".dockerignore").read_text(encoding="utf-8").splitlines()
        patterns = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
        for f in ("jobs.py", "jobs_api.py", "jobs_worker.py", "start.py"):
            self.assertFalse(docker_ignored(f, patterns), f)


if __name__ == "__main__":
    unittest.main()
