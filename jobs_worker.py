"""
Jobs runner (started and restarted by start.py).

  python jobs_worker.py            -> the runner loop
  python jobs_worker.py --run ID   -> one job in a child process (started by the runner)

The runner:
  - writes a local heartbeat every 5 s (/api/health and POST /api/jobs read it);
  - at startup, sweeps Supabase: jobs left running/queued by a crash, a redeploy or a
    Serverless sleep end as failed/expired with their quota released (jobs.sweep);
  - watches the local jobs folder (no network while idle, so Railway can sleep);
  - starts at most JOB_SLOTS children (slot "any" + slot "small", see jobs.pick_next);
  - while a job runs, updates heartbeat_at in Supabase every 60 s;
  - kills a child that runs past JOB_KILL_AFTER and ends a crashed child's job;
  - on SIGTERM (redeploy) stops the children; they end their jobs as interrupted.
"""

import logging
import os
import signal
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone

import jobs

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pdf2wordly.runner")
for _name in ("httpx", "httpcore"):
    logging.getLogger(_name).setLevel(logging.WARNING)

SWEEP_EVERY = 60
SUPABASE_BEAT_EVERY = 60


def make_store():
    from supabase import create_client
    url = os.environ.get("SUPABASE_URL", "").rstrip("/")
    key = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
    if not url or not key:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not configured")
    return jobs.JobStore(create_client(url, key))


def _die_with_parent():
    """Linux: a child is killed if the runner dies, so no orphan keeps converting."""
    try:
        import ctypes
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(1, signal.SIGKILL)  # PR_SET_PDEATHSIG
    except Exception:
        pass


class JobStopped(Exception):
    def __init__(self, code):
        super().__init__(code)
        self.code = code


# ---------------------------------------------------------------------------
# One job (child process)
# ---------------------------------------------------------------------------
def run_one(job_id, store=None, base=None):
    """Converts one job. Returns the final status."""
    store = store or make_store()
    p = jobs.paths(job_id, base)
    meta = jobs.read_json(p["meta"]) or {}
    stop = {"code": None}

    def on_term(_sig, _frame):
        stop["code"] = "CONVERSION_INTERRUPTED"

    try:
        old_handler = signal.signal(signal.SIGTERM, on_term)
    except ValueError:
        old_handler = None  # not the main thread (tests)
    try:
        return _run_one(job_id, store, base, p, meta, stop)
    finally:
        if old_handler is not None:
            signal.signal(signal.SIGTERM, old_handler)


def _run_one(job_id, store, base, p, meta, stop):
    import conversion
    from conversion import ConversionTimeoutError, EmptyDocumentError, ScannedPageLimitError

    def progress(done, total):
        if p["cancel"].exists():
            raise JobStopped("CANCELLED")
        if stop["code"]:
            raise JobStopped(stop["code"])
        jobs.write_state(job_id, base, status="running", pages_done=done, pages_total=total)

    deadline = conversion.make_deadline(jobs.JOB_TIME_BUDGET)
    try:
        if not p["pdf"].exists():
            raise JobStopped("CONVERSION_INTERRUPTED")
        plan = conversion.plan_pdf(str(p["pdf"]), deadline=deadline)
        jobs.write_state(job_id, base, status="running", pages_done=0, pages_total=len(plan))
        conversion.convert_pdf_to_docx(str(p["pdf"]), str(p["docx"]), plan=plan,
                                       max_ocr_pages=int(meta.get("max_ocr_pages") or jobs.JOB_MAX_OCR_PAGES_FREE),
                                       deadline=deadline, progress=progress)
        if not p["docx"].exists():
            raise RuntimeError("no output")
        now = datetime.now(timezone.utc)
        if not store.mark_done(job_id, len(plan), now):
            # The row was ended meanwhile (account deleted, swept): nothing to keep.
            jobs.delete_job_files(job_id, base)
            logger.info("Job finished but its record is gone; result deleted")
            return "gone"
        jobs.remove(p["pdf"])
        expires = now.timestamp() + jobs.JOB_RESULT_TTL
        jobs.write_state(job_id, base, status="done", pages_done=len(plan), pages_total=len(plan),
                         expires_at=expires)
        logger.info("Job done (%d pages)", len(plan))
        return "done"
    except JobStopped as exc:
        jobs.fail_job(store, job_id, exc.code, base)
    except EmptyDocumentError:
        jobs.fail_job(store, job_id, "EMPTY_DOCUMENT", base)
    except ConversionTimeoutError:
        jobs.fail_job(store, job_id, "CONVERSION_TIMEOUT", base)
    except ScannedPageLimitError as exc:
        jobs.fail_job(store, job_id, "SCANNED_PAGE_LIMIT", base,
                      extra={"scanned_pages": exc.scanned_pages, "max_scanned_pages": exc.limit})
    except Exception as exc:
        logger.error("Job conversion failed (%s)", type(exc).__name__)
        jobs.fail_job(store, job_id, "CONVERSION_FAILED", base)
    return "failed"


# ---------------------------------------------------------------------------
# Runner loop
# ---------------------------------------------------------------------------
class Runner:
    def __init__(self, store, base=None, spawn=None, clock=time.time):
        self.store = store
        self.base = base
        self.clock = clock
        self.children = {}          # job id -> {"proc", "slot", "started"}
        self.spawn = spawn or self._spawn
        self.stopping = False
        self.last_sweep = 0.0
        self.last_supabase_beat = 0.0

    def _spawn(self, job_id):
        return subprocess.Popen([sys.executable, os.path.abspath(__file__), "--run", job_id],
                                preexec_fn=_die_with_parent if os.name == "posix" else None)

    def startup(self):
        jobs.ensure_dir(self.base)
        jobs.write_runner_beat(self.base)
        try:
            n = jobs.sweep(self.store, startup=True, base=self.base)
            if n:
                logger.info("Startup sweep ended %d job(s)", n)
        except Exception as exc:
            logger.error("Startup sweep failed (%s); retrying later", type(exc).__name__)
            self.last_sweep = 0.0
        else:
            self.last_sweep = self.clock()
        jobs.local_cleanup(self.base)

    def reap(self):
        now = self.clock()
        for job_id, child in list(self.children.items()):
            code = child["proc"].poll()
            if code is None and now - child["started"] > jobs.JOB_KILL_AFTER:
                child["proc"].kill()
                child["proc"].wait()
                code = -9
                self._end_if_open(job_id, "CONVERSION_TIMEOUT")
            elif code is not None and code != 0:
                self._end_if_open(job_id, "CONVERSION_FAILED")   # crashed (e.g. out of memory)
            if code is not None:
                del self.children[job_id]

    def _end_if_open(self, job_id, code):
        job = jobs.local_job(job_id, self.base)
        if job and job["state"].get("status") not in jobs.FINAL:
            try:
                jobs.fail_job(self.store, job_id, code, self.base)
            except Exception:
                pass  # the next sweep retries (the upload is gone, so it ends as interrupted)

    def tick(self):
        """One loop step: reap, cancel queued jobs, start jobs, heartbeats, expiry."""
        self.reap()
        local = jobs.local_jobs(self.base)
        now_dt = datetime.now(timezone.utc)
        now = self.clock()
        for job_id, job in local.items():
            status = job["state"].get("status")
            if status == "queued" and job["cancelled"]:
                self._safe_fail(job_id, "CANCELLED")
            elif status == "queued" and now - job["meta"]["created"] > jobs.JOB_QUEUE_TIMEOUT:
                self._safe_fail(job_id, "QUEUE_TIMEOUT")
            elif status == "done" and (not job["has_result"] or
                                       now >= float(job["state"].get("expires_at") or 0)):
                try:
                    jobs.expire_done_job(self.store, job_id, job["downloaded"], self.base)
                except Exception as exc:
                    logger.error("Expiry failed (%s); retrying", type(exc).__name__)
        if not self.stopping:
            local = jobs.local_jobs(self.base)
            running = [{"slot": c["slot"]} for c in self.children.values()]
            for job_id, slot in jobs.pick_next(jobs.queued_entries(local), running, jobs.JOB_SLOTS, now):
                self._start(job_id, slot, now_dt, now)
        if self.children and now - self.last_supabase_beat >= SUPABASE_BEAT_EVERY:
            try:
                self.store.heartbeat(self.children.keys(), now_dt)
                self.last_supabase_beat = now
            except Exception as exc:
                logger.warning("Job heartbeat failed (%s)", type(exc).__name__)
        # Supabase sweep only while there is work (or the startup sweep failed): idle = no network.
        has_work = bool(jobs.active_jobs(jobs.local_jobs(self.base))) or self.last_sweep == 0.0
        if has_work and now - self.last_sweep >= SWEEP_EVERY:
            try:
                jobs.sweep(self.store, base=self.base, running_here=set(self.children))
                self.last_sweep = now
            except Exception as exc:
                logger.error("Sweep failed (%s)", type(exc).__name__)

    def _safe_fail(self, job_id, code):
        try:
            jobs.fail_job(self.store, job_id, code, self.base)
        except Exception as exc:
            logger.error("Could not end a job (%s); retrying", type(exc).__name__)

    def _start(self, job_id, slot, now_dt, now):
        if not jobs.is_own(job_id):
            return   # never: local files are always this container's jobs (defence in depth)
        try:
            ok = self.store.mark_running(job_id, slot, now_dt)
        except Exception as exc:
            logger.error("Could not start a job (%s); retrying", type(exc).__name__)
            return
        if not ok:
            # Not queued in Supabase any more (account deleted, swept): drop the local job.
            jobs.delete_job_files(job_id, self.base)
            jobs.write_state(job_id, self.base, status="failed", code="CONVERSION_INTERRUPTED", counted=False)
            return
        jobs.write_state(job_id, self.base, status="running", slot=slot, started=now, pages_done=0)
        self.children[job_id] = {"proc": self.spawn(job_id), "slot": slot, "started": now}
        logger.info("Job started (slot %s)", slot)

    def stop_children(self, wait=20):
        for child in self.children.values():
            try:
                child["proc"].terminate()
            except OSError:
                pass
        end = time.time() + wait
        for child in self.children.values():
            try:
                child["proc"].wait(timeout=max(0.1, end - time.time()))
            except subprocess.TimeoutExpired:
                child["proc"].kill()
        self.reap()
        # Redeploy / shutdown: jobs still waiting here would never run (the next container has
        # another disk). End them now so their quota comes back at once.
        for job_id, job in jobs.local_jobs(self.base).items():
            if job["state"].get("status") == "queued":
                self._safe_fail(job_id, "CONVERSION_INTERRUPTED")


def beat_forever(base, stop_event):
    while not stop_event.is_set():
        try:
            jobs.write_runner_beat(base)
        except OSError as exc:
            logger.error("Runner heartbeat failed (%s)", type(exc).__name__)
        stop_event.wait(jobs.RUNNER_BEAT_SECONDS)


def main():
    runner = Runner(make_store())
    stop_event = threading.Event()

    def on_term(_sig, _frame):
        runner.stopping = True
        stop_event.set()

    signal.signal(signal.SIGTERM, on_term)
    signal.signal(signal.SIGINT, on_term)
    runner.startup()
    threading.Thread(target=beat_forever, args=(None, stop_event), daemon=True).start()
    logger.info("Jobs runner started (%d slot(s))", jobs.JOB_SLOTS)
    while not runner.stopping:
        try:
            runner.tick()
        except Exception as exc:
            logger.error("Runner tick failed (%s)", type(exc).__name__)
        stop_event.wait(1.0)   # local folder check only: no network while idle
    runner.stop_children()
    logger.info("Jobs runner stopped")


if __name__ == "__main__":
    if len(sys.argv) == 3 and sys.argv[1] == "--run" and jobs.valid_id(sys.argv[2]):
        if os.name == "posix":
            _die_with_parent()
        sys.exit(0 if run_one(sys.argv[2]) in ("done", "failed", "gone") else 1)
    main()
