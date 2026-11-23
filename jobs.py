"""
Conversion jobs: upload -> job id -> status polling -> download -> files deleted.

Where things live (one Railway replica, see docs/HANDOFF.md):
  - Supabase table public.conversion_jobs (SQL 004): the durable record. It keeps the quota
    claim of each job, so the quota is released exactly once (RPC release_job_quota) even after
    a crash, a redeploy or a Serverless sleep that wipes the local disk.
  - Local disk JOBS_DIR: the PDF, the DOCX and small JSON files for fast status polling
    (<id>.meta.json written once by the API, <id>.state.json written by the runner, and the
    markers <id>.cancel / <id>.downloaded written by the API).

Who runs jobs: jobs_worker.py, a separate process supervised by start.py. It runs at most
JOB_SLOTS jobs at a time, each in its own child process: slot "any" takes any job, slot "small"
(when JOB_SLOTS = 2) only small jobs, so two large OCR jobs never run together and a 1-page job
does not wait behind a 50-page scan. Measured: a 25-page OCR job peaks at about 450 MB.

Nothing here logs a job id, a user id or a file name.
"""

import base64
import hashlib
import json
import logging
import os
import re
import secrets
import tempfile
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

logger = logging.getLogger("pdf2wordly.jobs")

# ---------------------------------------------------------------------------
# Settings (owner decisions, 2026-11-23). /api/convert keeps its own limits (30 pages, 240 s).
# The scanned caps are the site numbers "scanned-pages-limit-pro" / "-free" in _tools/site-config.json
# (checked by tests).
# ---------------------------------------------------------------------------
JOBS_DIR = Path(os.environ.get("JOBS_DIR") or (Path(tempfile.gettempdir()) / "pdf2wordly-jobs"))
JOB_MAX_OCR_PAGES_PRO = int(os.environ.get("JOB_MAX_OCR_PAGES_PRO", "50"))
JOB_MAX_OCR_PAGES_FREE = int(os.environ.get("JOB_MAX_OCR_PAGES_FREE", "20"))
JOB_TIME_BUDGET = float(os.environ.get("JOB_TIME_BUDGET", "600"))      # seconds per job
JOB_QUEUE_TIMEOUT = int(os.environ.get("JOB_QUEUE_TIMEOUT", "900"))    # queued longer: failed
JOB_RESULT_TTL = 3600                                                   # files kept 1 hour at most
JOB_QUEUE_MAX = int(os.environ.get("JOB_QUEUE_MAX", "20"))             # queued + running
JOB_SLOTS = max(1, min(2, int(os.environ.get("JOB_SLOTS", "2"))))
SMALL_JOB_MAX_SCANNED = 5
SMALL_JOB_MAX_PAGES = 20
SECONDS_PER_SCANNED_PAGE = 6.0     # estimate on Railway (3.2-3.4 s measured on the dev PC)
SECONDS_PER_TEXT_PAGE = 0.5
RUNNER_BEAT_SECONDS = 5
RUNNER_DEAD_AFTER = 30             # no runner heartbeat for this long: POST /api/jobs -> SERVER_BUSY
JOB_STALE_AFTER = 180              # running job without a heartbeat in Supabase: interrupted
JOB_KILL_AFTER = JOB_TIME_BUDGET + 90
POLL_AFTER_SECONDS = 3

FINAL = ("done", "failed", "expired")
ID_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")


# ---------------------------------------------------------------------------
# Ids, files
# ---------------------------------------------------------------------------
INSTANCE_TAG_LEN = 6


def instance_tag():
    """6 characters naming this container (hostname + Railway deployment / replica). During a
    redeploy Railway runs the old and the new container for a short time, both on the same
    Supabase table: each one only ends the jobs it created (their files are on its own disk)."""
    import socket
    raw = "|".join((socket.gethostname(), os.environ.get("RAILWAY_DEPLOYMENT_ID", ""),
                    os.environ.get("RAILWAY_REPLICA_ID", "")))
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")[:INSTANCE_TAG_LEN]


def new_job_id():
    """43 URL-safe characters: this container's tag (6) + 37 random characters (222 bits)."""
    return instance_tag() + secrets.token_urlsafe(28)[:43 - INSTANCE_TAG_LEN]


def is_own(job_id):
    """Was this job created by this container?"""
    return isinstance(job_id, str) and job_id.startswith(instance_tag())


def valid_id(job_id):
    return isinstance(job_id, str) and bool(ID_RE.match(job_id))


def paths(job_id, base=None):
    base = Path(base or JOBS_DIR)
    return {
        "pdf": base / f"{job_id}.pdf",
        "docx": base / f"{job_id}.docx",
        "part": base / f"{job_id}.docx.part",
        "meta": base / f"{job_id}.meta.json",
        "state": base / f"{job_id}.state.json",
        "cancel": base / f"{job_id}.cancel",
        "downloaded": base / f"{job_id}.downloaded",
    }


def ensure_dir(base=None):
    Path(base or JOBS_DIR).mkdir(parents=True, exist_ok=True)


def write_json(path, obj):
    path = Path(path)
    tmp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    tmp.write_text(json.dumps(obj), encoding="utf-8")
    os.replace(tmp, path)


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def touch(path):
    Path(path).write_text(str(time.time()), encoding="utf-8")


def remove(*files):
    for f in files:
        try:
            Path(f).unlink()
        except FileNotFoundError:
            pass
        except OSError as exc:
            logger.warning("Could not remove a job file: %s", type(exc).__name__)


def delete_job_files(job_id, base=None, keep_records=True):
    """Deletes the PDF and the DOCX; with keep_records=False also the JSON files and markers."""
    p = paths(job_id, base)
    remove(p["pdf"], p["docx"], p["part"])
    if not keep_records:
        remove(p["meta"], p["state"], p["cancel"], p["downloaded"])


def local_job(job_id, base=None):
    """{"meta":..., "state":..., "cancelled":bool, "downloaded":bool, "has_result":bool} or None."""
    p = paths(job_id, base)
    meta = read_json(p["meta"])
    if meta is None:
        return None
    return {
        "meta": meta,
        "state": read_json(p["state"]) or {"status": "queued"},
        "cancelled": p["cancel"].exists(),
        "downloaded": p["downloaded"].exists(),
        "has_result": p["docx"].exists(),
        "has_upload": p["pdf"].exists(),
    }


def local_jobs(base=None):
    base = Path(base or JOBS_DIR)
    if not base.is_dir():
        return {}
    out = {}
    for f in base.glob("*.meta.json"):
        job_id = f.name[: -len(".meta.json")]
        if valid_id(job_id):
            job = local_job(job_id, base)
            if job:
                out[job_id] = job
    return out


def active_jobs(jobs):
    return {k: v for k, v in jobs.items() if v["state"].get("status") in ("queued", "running")}


# ---------------------------------------------------------------------------
# Runner heartbeat (local file: no network, so Railway Serverless can still sleep when idle)
# ---------------------------------------------------------------------------
def beat_file(base=None):
    return Path(base or JOBS_DIR) / "runner.beat"


def write_runner_beat(base=None, now=None):
    ensure_dir(base)
    beat_file(base).write_text(str(time.time() if now is None else now), encoding="utf-8")


def runner_age(base=None, now=None):
    """Seconds since the runner's last heartbeat, or None when there is none."""
    try:
        last = float(beat_file(base).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return max(0.0, (time.time() if now is None else now) - last)


def runner_alive(base=None, now=None):
    age = runner_age(base, now)
    return age is not None and age <= RUNNER_DEAD_AFTER


# ---------------------------------------------------------------------------
# Scheduling (pure, unit tested)
# ---------------------------------------------------------------------------
def estimate_seconds(scanned_pages, pages):
    text_pages = max(0, pages - scanned_pages)
    return round(scanned_pages * SECONDS_PER_SCANNED_PAGE + text_pages * SECONDS_PER_TEXT_PAGE)


def size_class(scanned_pages, pages):
    small = scanned_pages <= SMALL_JOB_MAX_SCANNED and pages <= SMALL_JOB_MAX_PAGES
    return "small" if small else "large"


def priority(job, now):
    """Shortest job first, with aging: each second waited counts as one second less of work,
    so a large job is never starved by a stream of small ones."""
    return job["cost"] - (now - job["created"])


def pick_next(queued, running, slots, now):
    """queued: [{"id", "size", "cost", "created"}], running: [{"slot"}].
    Returns [(job_id, slot)] to start now. Slot "small" exists only when slots == 2."""
    busy = [r.get("slot") for r in running]
    pool = sorted(queued, key=lambda j: (priority(j, now), j["created"]))
    starts = []
    if slots >= 2 and "small" not in busy:
        small = [j for j in pool if j["size"] == "small"]
        if small:
            starts.append((small[0]["id"], "small"))
            pool.remove(small[0])
    if "any" not in busy and pool:
        starts.append((pool[0]["id"], "any"))
    return starts


def queue_position(job_id, queued, now):
    """1-based place in the queue (same order as pick_next), or None."""
    order = [j["id"] for j in sorted(queued, key=lambda j: (priority(j, now), j["created"]))]
    return order.index(job_id) + 1 if job_id in order else None


def queued_entries(jobs):
    return [{"id": k, "size": v["meta"]["size"], "cost": v["meta"]["cost"], "created": v["meta"]["created"]}
            for k, v in jobs.items() if v["state"].get("status") == "queued"]


# ---------------------------------------------------------------------------
# Token cache for the job endpoints (status is polled every few seconds)
# ---------------------------------------------------------------------------
def jwt_exp(token):
    """The JWT 'exp' claim (epoch seconds), or None. Not a verification: the token was
    verified by Supabase before it is cached; exp only shortens the cache life."""
    try:
        payload = token.split(".")[1]
        payload += "=" * (-len(payload) % 4)
        exp = json.loads(base64.urlsafe_b64decode(payload.encode("ascii"))).get("exp")
        return float(exp) if isinstance(exp, (int, float)) else None
    except Exception:
        return None


class TokenCache:
    """sha256(token) -> user id for at most `ttl` seconds and never past the token's exp."""

    def __init__(self, ttl=60, max_size=5000):
        self.ttl = ttl
        self.max_size = max_size
        self._items = {}
        self._lock = threading.Lock()

    @staticmethod
    def key(token):
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    def get(self, token, now=None):
        now = time.time() if now is None else now
        k = self.key(token)
        with self._lock:
            item = self._items.get(k)
            if item is None:
                return None
            user_id, until = item
            if now >= until:
                del self._items[k]
                return None
            return user_id

    def put(self, token, user_id, now=None):
        now = time.time() if now is None else now
        until = now + self.ttl
        exp = jwt_exp(token)
        if exp is not None:
            until = min(until, exp)
        if until <= now:
            return
        with self._lock:
            if len(self._items) >= self.max_size:
                for k in [k for k, (_, u) in self._items.items() if u <= now]:
                    del self._items[k]
                if len(self._items) >= self.max_size:
                    self._items.clear()
            self._items[self.key(token)] = (user_id, until)


# ---------------------------------------------------------------------------
# Supabase store (service role). Tests use a fake with the same methods.
# ---------------------------------------------------------------------------
def iso(dt):
    return dt.astimezone(timezone.utc).isoformat()


def parse_time(value):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


class JobStore:
    TABLE = "conversion_jobs"

    def __init__(self, admin):
        self.db = admin

    def _rows(self, resp):
        return getattr(resp, "data", None) or []

    def insert(self, row):
        self.db.table(self.TABLE).insert(row).execute()

    def get(self, job_id, user_id=None):
        q = self.db.table(self.TABLE).select("*").eq("id", job_id)
        if user_id is not None:
            q = q.eq("user_id", user_id)
        rows = self._rows(q.limit(1).execute())
        return rows[0] if rows else None

    def open_jobs(self):
        return self._rows(self.db.table(self.TABLE).select("*")
                          .in_("status", ["queued", "running", "done"]).execute())

    def mark_running(self, job_id, slot, now):
        rows = self._rows(self.db.table(self.TABLE)
                          .update({"status": "running", "slot": slot, "started_at": iso(now),
                                   "heartbeat_at": iso(now)})
                          .eq("id", job_id).eq("status", "queued").execute())
        return bool(rows)

    def heartbeat(self, job_ids, now):
        if job_ids:
            self.db.table(self.TABLE).update({"heartbeat_at": iso(now)}) \
                .in_("id", list(job_ids)).eq("status", "running").execute()

    def mark_done(self, job_id, pages_total, now):
        rows = self._rows(self.db.table(self.TABLE)
                          .update({"status": "done", "pages_done": pages_total, "finished_at": iso(now),
                                   "expires_at": iso(now + timedelta(seconds=JOB_RESULT_TTL))})
                          .eq("id", job_id).eq("status", "running").execute())
        return bool(rows)

    def release(self, job_id, status, code):
        """failed / expired, quota given back once (only if the result was never downloaded)."""
        resp = self.db.rpc("release_job_quota",
                           {"p_job_id": job_id, "p_status": status, "p_error_code": code}).execute()
        data = getattr(resp, "data", None)
        row = data[0] if isinstance(data, list) and data else data
        return bool(row.get("released")) if isinstance(row, dict) else False

    def set_expired(self, job_id, code="JOB_EXPIRED"):
        """A downloaded result expires: the quota stays used."""
        self.db.table(self.TABLE).update({"status": "expired", "error_code": code}) \
            .eq("id", job_id).eq("status", "done").execute()

    def mark_downloaded(self, job_id, now):
        """First download: the conversion counts. False if the job was already released/expired."""
        rows = self._rows(self.db.table(self.TABLE).update({"downloaded_at": iso(now)})
                          .eq("id", job_id).eq("status", "done").is_("downloaded_at", "null")
                          .is_("quota_released_at", "null").execute())
        if rows:
            return True
        row = self.get(job_id)
        return bool(row and row.get("status") == "done" and row.get("downloaded_at"))


# ---------------------------------------------------------------------------
# Ending jobs (used by the runner, the child process, the sweeper and the API)
# ---------------------------------------------------------------------------
def write_state(job_id, base=None, **state):
    p = paths(job_id, base)
    if p["meta"].exists():
        old = read_json(p["state"]) or {}
        old.update(state)
        write_json(p["state"], old)


def fail_job(store, job_id, code, base=None, status="failed", extra=None):
    """failed / expired + quota released once + files deleted. Safe to call twice."""
    released = False
    try:
        released = store.release(job_id, status, code)
    except Exception as exc:
        logger.error("Job release failed (%s)", type(exc).__name__)
        raise
    finally:
        delete_job_files(job_id, base)
    write_state(job_id, base, status=status, code=code, counted=False, **(extra or {}))
    logger.info("Job ended: %s %s (quota released: %s)", status, code, released)
    return released


def expire_done_job(store, job_id, downloaded, base=None):
    """A finished job's files are gone or its hour is over."""
    if downloaded:
        store.set_expired(job_id)
        delete_job_files(job_id, base)
        write_state(job_id, base, status="expired", code="JOB_EXPIRED", counted=True)
        return False
    return fail_job(store, job_id, "JOB_EXPIRED", base, status="expired")


def sweep(store, now=None, startup=False, base=None, running_here=()):
    """Ends every job that can no longer finish and releases its quota once.

    - queued for longer than JOB_QUEUE_TIMEOUT -> failed (QUEUE_TIMEOUT); a job of THIS container
      whose upload is gone -> failed (CONVERSION_INTERRUPTED);
    - running without a heartbeat for JOB_STALE_AFTER -> failed (CONVERSION_INTERRUPTED); at startup
      also every running job of THIS container (its runner restarted), unless this runner runs it;
    - done past expires_at, or whose DOCX is gone (Serverless sleep / redeploy wiped the disk)
      -> expired; quota released if it was never downloaded.
    Queued / running jobs of ANOTHER container (redeploy overlap: the old one still runs them,
    and on SIGTERM it ends them itself) are only ended by the two time limits.
    Returns the number of jobs ended."""
    now = now or datetime.now(timezone.utc)
    ended = 0
    for row in store.open_jobs():
        job_id = row.get("id")
        if not valid_id(job_id):
            continue
        status = row.get("status")
        p = paths(job_id, base)
        own = is_own(job_id)
        try:
            if status == "queued":
                created = parse_time(row.get("created_at")) or now
                if (now - created).total_seconds() > JOB_QUEUE_TIMEOUT:
                    fail_job(store, job_id, "QUEUE_TIMEOUT", base)
                    ended += 1
                elif own and not p["pdf"].exists():
                    fail_job(store, job_id, "CONVERSION_INTERRUPTED", base)
                    ended += 1
            elif status == "running":
                if job_id in running_here:
                    continue
                beat = parse_time(row.get("heartbeat_at")) or parse_time(row.get("started_at")) or now
                if (startup and own) or (now - beat).total_seconds() > JOB_STALE_AFTER:
                    fail_job(store, job_id, "CONVERSION_INTERRUPTED", base)
                    ended += 1
            elif status == "done":
                expires = parse_time(row.get("expires_at")) or now
                if now >= expires or not p["docx"].exists():
                    expire_done_job(store, job_id, bool(row.get("downloaded_at")), base)
                    ended += 1
        except Exception as exc:
            logger.error("Sweep failed for one job (%s)", type(exc).__name__)
    return ended


def local_cleanup(base=None, now=None, max_age=2 * JOB_RESULT_TTL + JOB_QUEUE_TIMEOUT):
    """Deletes any local job file older than max_age (records of ended jobs included)."""
    base = Path(base or JOBS_DIR)
    if not base.is_dir():
        return
    cutoff = (time.time() if now is None else now) - max_age
    for f in base.iterdir():
        if f.name == "runner.beat":
            continue
        try:
            if f.is_file() and f.stat().st_mtime < cutoff:
                f.unlink()
        except OSError:
            pass
