"""
PDF2Wordly — secure conversion backend.
---------------------------------------
Flask API that accepts a PDF upload and returns a converted .docx file.

Security model (server-enforced, never trust the frontend):
  1. Every /api/convert and /api/me request must carry the user's Supabase
     JWT as `Authorization: Bearer <access_token>`.
  2. The token is validated against Supabase Auth (auth.getUser). The real
     user id comes from that verification — never from query params, body
     fields, or headers set by the client.
  3. Free vs Pro is read from public.profiles in Supabase (service-role).
     Free = one successful conversion per rolling 24h, enforced with an
     atomic claim/release RPC so simultaneous requests cannot double-spend.
     Pro = fair use: at most PRO_DAILY_LIMIT (100) successful conversions per
     rolling 24h, counted atomically in Postgres (claim_pro_conversion) and
     released again when a conversion fails. Paddle subscriptions update the
     profile through the signed webhook in billing.py (Pro rule, refunds).
  4. The Free timestamp is only consumed when conversion SUCCEEDS. On
     failure the claim is released (previous timestamp restored).

Endpoints:
  GET  /api/health   -> health check (public)
  GET  /api/me       -> { plan, subscription_status, ... } for the caller (auth)
  POST /api/convert  -> multipart/form-data, field "file" (auth + limit)
  POST /api/jobs, GET/DELETE /api/jobs/<id>, GET /api/jobs/<id>/download
                     -> conversion jobs (jobs_api.py, run by jobs_worker.py)
  POST /api/paddle/webhook, POST /api/billing/portal -> see billing.py
  DELETE /api/account -> JSON {"confirm": "DELETE"} (auth + rate limit):
                       deletes the profile row, then the Supabase auth user

Run:
  python3 server.py
"""

import logging
import os
import re
import sys
import tempfile
import threading
import time
import uuid
from collections import deque
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

import fitz  # PyMuPDF
from supabase import create_client

import billing
import conversion
import jobs_api
from conversion import ConversionTimeoutError, EmptyDocumentError

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pdf2wordly")
# supabase-py talks to Supabase through httpx, which logs every request URL
# at INFO, user ids in query strings included (profiles?...&id=eq.<uuid>).
# Keep those libraries at WARNING so request URLs are never logged.
QUIET_LOGGERS = ("httpx", "httpcore")
for _name in QUIET_LOGGERS:
    logging.getLogger(_name).setLevel(logging.WARNING)

# ---------------------------------------------------------------------------
# Configuration (all secrets come from environment — never from frontend code)
# ---------------------------------------------------------------------------

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_ANON_KEY = os.environ.get("SUPABASE_ANON_KEY", "")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")

# Comma-separated extra origins allowed (e.g. preview deployments).
EXTRA_ORIGINS = [
    o.strip()
    for o in os.environ.get("EXTRA_CORS_ORIGINS", "").split(",")
    if o.strip()
]

ALLOWED_ORIGINS = [
    "https://pdf2wordly.com",
    "https://www.pdf2wordly.com",
    "http://localhost:5001",
    "http://localhost:8000",
    "http://127.0.0.1:5001",
    "http://127.0.0.1:8000",
] + EXTRA_ORIGINS

MAX_CONTENT_LENGTH = 25 * 1024 * 1024  # 25 MB, matches the frontend limit
# Conversion settings (page routing, OCR zoom, OCR_LANGUAGES env) live in conversion.py.

# Max pages per request, checked before conversion.
FREE_MAX_PAGES = 20
PRO_MAX_PAGES = 200

# Pro fair use: successful conversions per rolling 24 hours. The site shows
# the same number from _tools/site-config.json ("pro-daily-limit").
PRO_DAILY_LIMIT = int(os.environ.get("PRO_DAILY_LIMIT", "100"))

# Temp files older than this are purged (startup + before each conversion).
TEMP_DIR = Path(tempfile.gettempdir()) / "pdf2wordly"
TEMP_MAX_AGE_SECONDS = 30 * 60

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
CORS(
    app,
    origins=ALLOWED_ORIGINS,
    supports_credentials=False,
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "DELETE", "OPTIONS"],
)


def _supabase_anon():
    if not SUPABASE_URL or not SUPABASE_ANON_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_ANON_KEY are not configured")
    return create_client(SUPABASE_URL, SUPABASE_ANON_KEY)


def _supabase_admin():
    if not SUPABASE_URL or not SUPABASE_SERVICE_ROLE_KEY:
        raise RuntimeError("SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY are not configured")
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_ROLE_KEY)


# ---------------------------------------------------------------------------
# Auth + authorization (the ONLY place Free/Pro is decided)
# ---------------------------------------------------------------------------

def get_authenticated_user_id():
    """Validate the Supabase JWT from the Authorization header.

    Returns (user_id, None) on success, or (None, (json, status)) on failure.
    """
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        return None, (jsonify({"code": "AUTH_REQUIRED",
                               "error": "Please log in to continue."}), 401)
    token = auth_header[len("Bearer "):].strip()
    if not token:
        return None, (jsonify({"code": "AUTH_REQUIRED",
                               "error": "Please log in to continue."}), 401)
    try:
        anon = _supabase_anon()
        user_resp = anon.auth.get_user(token)
        user = getattr(user_resp, "user", None)
        if not user or not getattr(user, "id", None):
            raise ValueError("no user for token")
        return user.id, None
    except RuntimeError as exc:
        logger.error("Supabase not configured: %s", exc)
        return None, (jsonify({"code": "SERVER_ERROR",
                               "error": "Something went wrong. Please try again later."}), 500)
    except Exception:
        logger.info("Invalid/expired access token on %s", request.path)
        return None, (jsonify({"code": "INVALID_TOKEN",
                               "error": "Your session has expired. Please log in again."}), 401)


def is_user_pro(profile):
    """Centralized Pro check. `profile` is the public.profiles row (dict).

    The rule (plan, status and the paid-period end date) lives in
    billing.is_pro, so a stale plan value alone never grants Pro.
    """
    return billing.is_pro(profile)


def get_profile(admin, user_id):
    """Load the caller's profile with the service-role client (bypasses RLS).

    Falls back to older column sets while a migration has not been run yet
    (003 adds refunded_at, 002 the Paddle columns)."""
    column_sets = (
        billing.PROFILE_COLUMNS,
        billing.PROFILE_COLUMNS.replace(", refunded_at", ""),
        "id, plan, subscription_status, subscription_id, current_period_end, last_free_conversion_at",
    )
    resp = None
    for i, columns in enumerate(column_sets):
        try:
            resp = admin.table("profiles").select(columns).eq("id", user_id).limit(1).execute()
            break
        except Exception:
            if i == len(column_sets) - 1:
                raise
            logger.warning("profiles columns missing; run the latest migration")
    rows = getattr(resp, "data", None) or []
    return rows[0] if rows else None


# ---------------------------------------------------------------------------
# Temp-file helpers
# ---------------------------------------------------------------------------

def cleanup_files(*paths):
    for path in paths:
        try:
            if path and os.path.exists(path):
                os.remove(path)
        except OSError as exc:
            logger.warning("Could not remove temp file %s: %s", path, exc)


def purge_old_temp_files(max_age=TEMP_MAX_AGE_SECONDS):
    """Delete files in TEMP_DIR last modified more than `max_age` seconds ago."""
    if not TEMP_DIR.is_dir():
        return
    cutoff = time.time() - max_age
    try:
        entries = list(TEMP_DIR.iterdir())
    except OSError as exc:
        logger.warning("Could not list temp dir %s: %s", TEMP_DIR, exc)
        return
    for entry in entries:
        try:
            if entry.is_file() and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError as exc:
            logger.warning("Could not purge temp file %s: %s", entry, exc)


# ---------------------------------------------------------------------------
# Download name (same rules as filename.js on the site)
# ---------------------------------------------------------------------------

DOWNLOAD_NAME_MAX = 80
_HEX_RUN = re.compile(r"(^|[^0-9A-Fa-f])((?:[0-9A-Fa-f]{2}-)+[0-9A-Fa-f]{2})(?=$|[^0-9A-Fa-f])")
_PERCENT_RUN = re.compile(r"(?:%[0-9A-Fa-f]{2})+")
_CONTROL = re.compile(r"[\x00-\x1f\x7f]")


def _utf8_or_none(data):
    try:
        text = bytes(data).decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if _CONTROL.search(text) else text


def decode_file_name(name):
    """Show percent-encoded UTF-8 ("%D9%82") and Scribd-like hex runs
    ("D9-82-D8-B5") decoded when the bytes are valid UTF-8; else unchanged."""
    name = str(name or "")

    def percent(m):
        text = _utf8_or_none(int(h, 16) for h in m.group(0)[1:].split("%"))
        return m.group(0) if text is None else text

    def hexrun(m):
        data = [int(h, 16) for h in m.group(2).split("-")]
        if not any(b >= 0x80 for b in data):
            return m.group(0)
        text = _utf8_or_none(data)
        return m.group(0) if text is None else m.group(1) + text

    return _HEX_RUN.sub(hexrun, _PERCENT_RUN.sub(percent, name))


def docx_download_name(original_name):
    """Same base name, decoded, unsafe characters removed, <= 80 characters
    before ".docx", never empty. Flask adds filename* (RFC 5987) for non-ASCII."""
    name = decode_file_name(original_name)
    name = re.split(r"[\\/]", name)[-1]                  # never a path
    base = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", name)
    base = _CONTROL.sub("", base)
    base = re.sub(r'[\\/:*?"<>|]', "-", base)
    base = re.sub(r"\s+", " ", base)
    base = re.sub(r"-{2,}", "-", base)
    base = base.strip(" .-_")
    if not any(ch.isalnum() for ch in base):
        base = ""
    if len(base) > DOWNLOAD_NAME_MAX:
        base = base[:DOWNLOAD_NAME_MAX].rstrip(" .-_")
    return (base or "converted") + ".docx"


def check_pdf_limits(pdf_path, max_pages):
    """Return (code, extra) if the PDF is rejected, else (None, None).

    Raises if PyMuPDF cannot open the file at all.
    """
    doc = fitz.open(pdf_path)
    try:
        if doc.needs_pass:
            return "PASSWORD_REQUIRED", None
        if doc.page_count > max_pages:
            return "PAGE_LIMIT_EXCEEDED", {"max_pages": max_pages,
                                           "pages": doc.page_count}
        return None, None
    finally:
        doc.close()


def run_conversion(input_path, output_path, plan=None, deadline=None):
    """Page-by-page conversion (conversion.py). Raises EmptyDocumentError when
    nothing in the PDF is readable, ConversionTimeoutError past `deadline`.
    Returns the per-page report."""
    report = conversion.convert_pdf_to_docx(input_path, output_path, plan=plan, deadline=deadline)
    if not os.path.exists(output_path):
        raise RuntimeError("Conversion did not produce an output file.")
    logger.info("Converted %d pages: %s", len(report["pages"]),
                ", ".join(f"{k}={v}" for k, v in sorted(report["counts"].items())))
    return report


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    # runner: "alive" / "dead" (jobs runner heartbeat, see jobs_worker.py)
    return jsonify({"status": "ok", **jobs_api.health_fields()})


@app.route("/api/me", methods=["GET"])
def me():
    """Return the caller's authoritative plan state for UX display only."""
    user_id, err = get_authenticated_user_id()
    if err:
        return err
    try:
        admin = _supabase_admin()
        profile = get_profile(admin, user_id)
    except RuntimeError:
        return jsonify({"code": "SERVER_ERROR",
                        "error": "Something went wrong. Please try again later."}), 500
    except Exception:
        logger.exception("Failed to load profile for /api/me")
        return jsonify({"code": "SERVER_ERROR",
                        "error": "Something went wrong. Please try again later."}), 500
    if profile is None:
        return jsonify({"code": "PROFILE_NOT_FOUND",
                        "error": "Something went wrong. Please try again later."}), 403
    pro = is_user_pro(profile)
    return jsonify({
        "plan": "pro" if pro else "free",
        "subscription_status": profile.get("subscription_status"),
        "current_period_end": profile.get("current_period_end"),
        **billing.billing_summary(profile),
        # One refund per customer: False after a refund (billing.js refuses
        # to open the checkout).
        "can_purchase": billing.can_purchase(billing.SupabaseStore(admin), profile),
        "pro_daily_limit": PRO_DAILY_LIMIT,
    })


# ---------------------------------------------------------------------------
# Account deletion
# ---------------------------------------------------------------------------

class RateLimiter:
    """Sliding-window limit per key, in memory (per gunicorn worker)."""

    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window = window_seconds
        self._hits = {}
        self._lock = threading.Lock()

    def hit(self, key, now=None):
        """Count one attempt. Returns 0 if allowed, else seconds to wait."""
        now = time.monotonic() if now is None else now
        with self._lock:
            q = self._hits.setdefault(key, deque())
            while q and q[0] <= now - self.window:
                q.popleft()
            if len(q) >= self.limit:
                return max(1, int(q[0] + self.window - now) + 1)
            q.append(now)
            if len(self._hits) > 10000:  # keep memory bounded
                for k in [k for k, v in self._hits.items() if not v]:
                    del self._hits[k]
            return 0


DELETE_USER_LIMITER = RateLimiter(limit=5, window_seconds=15 * 60)
DELETE_IP_LIMITER = RateLimiter(limit=20, window_seconds=60 * 60)


def _client_ip():
    forwarded = request.headers.get("X-Forwarded-For", "")
    return (forwarded.split(",")[0].strip() if forwarded else "") or (request.remote_addr or "?")


def _rate_limited(wait):
    resp = jsonify({"code": "RATE_LIMITED",
                    "error": "Too many attempts. Please try again later.",
                    "retry_after_seconds": wait})
    resp.headers["Retry-After"] = str(wait)
    return resp, 429


@app.route("/api/account", methods=["DELETE"])
def delete_account():
    """Delete the caller's account: profile row first, then the auth user.

    Order: if deleting the auth user fails after the profile is gone, the
    user can sign in and simply retry (a missing profile is allowed here).
    paddle_events rows stay; their user_id becomes NULL through the FK.
    Logs never contain the user id or the email.
    """
    wait = DELETE_IP_LIMITER.hit(_client_ip())
    if wait:
        logger.warning("Account deletion: RATE_LIMITED")
        return _rate_limited(wait)

    user_id, err = get_authenticated_user_id()
    if err:
        return err

    wait = DELETE_USER_LIMITER.hit(user_id)
    if wait:
        logger.warning("Account deletion: RATE_LIMITED")
        return _rate_limited(wait)

    body = request.get_json(silent=True)
    if not isinstance(body, dict) or body.get("confirm") != "DELETE":
        logger.info("Account deletion: CONFIRMATION_REQUIRED")
        return jsonify({"code": "CONFIRMATION_REQUIRED",
                        "error": 'Type DELETE to confirm.'}), 400

    try:
        admin = _supabase_admin()
        profile = get_profile(admin, user_id)
    except Exception as exc:
        logger.error("Account deletion: SERVER_ERROR (%s)", type(exc).__name__)
        return jsonify({"code": "SERVER_ERROR",
                        "error": "Something went wrong. Please try again later."}), 500

    if billing.blocks_account_deletion(profile):
        logger.info("Account deletion: SUBSCRIPTION_ACTIVE")
        return jsonify({"code": "SUBSCRIPTION_ACTIVE",
                        "error": ("You have a Pro subscription that is still active or paused. "
                                  "Cancel it first in Manage subscription, then delete your account.")}), 409

    try:
        if profile is not None:
            admin.table("profiles").delete().eq("id", user_id).execute()
        admin.auth.admin.delete_user(user_id)
    except Exception as exc:
        logger.error("Account deletion: SERVER_ERROR (%s)", type(exc).__name__)
        return jsonify({"code": "SERVER_ERROR",
                        "error": "Something went wrong. Please try again later."}), 500

    logger.info("Account deleted")
    return jsonify({"deleted": True}), 200


@app.route("/api/convert", methods=["POST"])
def convert():
    # The time budget starts with the request: past it the conversion stops
    # cleanly (CONVERSION_TIMEOUT, quota released) before gunicorn's 300 s
    # timeout kills the worker.
    deadline = conversion.make_deadline()

    # 1. Authenticate — the user id comes ONLY from the verified token.
    user_id, err = get_authenticated_user_id()
    if err:
        return err

    # 2. Validate upload BEFORE touching quota.
    if "file" not in request.files:
        return jsonify({"code": "CONVERSION_FAILED",
                        "error": "No file provided."}), 400
    uploaded = request.files["file"]
    original_name = (uploaded.filename or "").strip()
    if original_name == "":
        return jsonify({"code": "CONVERSION_FAILED",
                        "error": "No file selected."}), 400

    # Never trust the client filename for storage; only its extension + stem
    # for the download name. Reject non-PDF names here; magic bytes below.
    if not original_name.lower().endswith(".pdf"):
        return jsonify({"code": "CONVERSION_FAILED",
                        "error": "Please upload a valid PDF file."}), 400
    output_name = docx_download_name(original_name)

    try:
        admin = _supabase_admin()
    except RuntimeError:
        logger.error("Service-role key not configured")
        return jsonify({"code": "SERVER_ERROR",
                        "error": "Something went wrong. Please try again later."}), 500

    # 3. Load authoritative profile; decide Free vs Pro server-side.
    try:
        profile = get_profile(admin, user_id)
    except Exception:
        logger.exception("Failed to load profile for convert")
        return jsonify({"code": "SERVER_ERROR",
                        "error": "Something went wrong. Please try again later."}), 500
    if profile is None:
        logger.warning("No profile row for authenticated user")
        return jsonify({"code": "PROFILE_NOT_FOUND",
                        "error": "Something went wrong. Please try again later."}), 403

    pro = is_user_pro(profile)
    claimed = False
    claim_prev = None
    pro_claim_id = None

    # 4. Free users: atomically claim their 24h slot (race-safe RPC that
    #    locks the row). Pro users skip this (fair use below).
    if not pro:
        try:
            claim = admin.rpc("claim_free_conversion",
                              {"p_user_id": user_id}).execute()
            claim_data = getattr(claim, "data", None)
            # RPC returns a single row: {allowed, reason, previous}
            row = claim_data[0] if isinstance(claim_data, list) and claim_data else claim_data
            allowed = bool(row.get("allowed")) if isinstance(row, dict) else False
            claim_prev = row.get("previous") if isinstance(row, dict) else None
            if not allowed:
                retry_after = None
                try:
                    last = profile.get("last_free_conversion_at")
                    if last:
                        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                        now = datetime.now(timezone.utc)
                        retry_after = max(
                            0, int(86400 - (now - last_dt).total_seconds()))
                except Exception:
                    retry_after = None
                payload = {"code": "FREE_LIMIT_REACHED",
                           "error": ("You have used your free conversion for the last "
                                     "24 hours. Try again later or upgrade to Pro "
                                     f"(up to {PRO_DAILY_LIMIT} files per day).")}
                if retry_after is not None:
                    payload["retry_after_seconds"] = retry_after
                return jsonify(payload), 429
            claimed = True
        except Exception:
            logger.exception("Free-limit claim failed")
            return jsonify({"code": "SERVER_ERROR",
                            "error": "Something went wrong. Please try again later."}), 500

    # 4b. Pro fair use: at most PRO_DAILY_LIMIT successful conversions per
    #     rolling 24h (atomic RPC with a row lock; released if the conversion
    #     fails). If the check itself fails, a paying user is not blocked.
    if pro:
        try:
            claim = admin.rpc("claim_pro_conversion",
                              {"p_user_id": user_id, "p_limit": PRO_DAILY_LIMIT}).execute()
            claim_data = getattr(claim, "data", None)
            row = claim_data[0] if isinstance(claim_data, list) and claim_data else claim_data
            if isinstance(row, dict) and row.get("allowed") is False:
                payload = {"code": "DAILY_LIMIT_REACHED",
                           "error": (f"You have reached the fair-use limit of {PRO_DAILY_LIMIT} "
                                     "files in 24 hours. Please try again later."),
                           "limit": PRO_DAILY_LIMIT}
                try:
                    payload["retry_after_seconds"] = max(0, int(row.get("retry_after_seconds")))
                except (TypeError, ValueError):
                    pass
                logger.info("Pro fair-use limit reached")
                return jsonify(payload), 429
            if isinstance(row, dict) and row.get("claim_id") is not None:
                pro_claim_id = row.get("claim_id")
        except Exception as exc:
            logger.error("Pro fair-use claim failed (%s); conversion allowed", type(exc).__name__)

    # 5. Store upload in a safe temp dir under a random name; verify magic bytes.
    job_id = uuid.uuid4().hex
    tmp_dir = TEMP_DIR
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
        purge_old_temp_files()
    except OSError:
        logger.exception("Could not create temp dir")
        _release_all(admin, user_id, claimed, claim_prev, pro_claim_id)
        return jsonify({"code": "SERVER_ERROR",
                        "error": "Something went wrong. Please try again later."}), 500
    input_path = str(tmp_dir / f"{job_id}.pdf")
    output_path = str(tmp_dir / f"{job_id}.docx")

    try:
        uploaded.save(input_path)

        # Validate: real PDF magic bytes + readable by PyMuPDF (rejects
        # renamed executables / corrupt files before conversion).
        with open(input_path, "rb") as fh:
            magic = fh.read(5)
        if magic != b"%PDF-":
            cleanup_files(input_path)
            _release_all(admin, user_id, claimed, claim_prev, pro_claim_id)
            return jsonify({"code": "CONVERSION_FAILED",
                            "error": "Please upload a valid PDF file."}), 400
        try:
            reject_code, reject_extra = check_pdf_limits(
                input_path, PRO_MAX_PAGES if pro else FREE_MAX_PAGES)
        except Exception:
            cleanup_files(input_path)
            _release_all(admin, user_id, claimed, claim_prev, pro_claim_id)
            return jsonify({"code": "CONVERSION_FAILED",
                            "error": "We couldn't read this PDF. Please try another file."}), 400
        if reject_code:
            cleanup_files(input_path)
            _release_all(admin, user_id, claimed, claim_prev, pro_claim_id)
            if reject_code == "PASSWORD_REQUIRED":
                payload = {"code": "PASSWORD_REQUIRED",
                           "error": ("This PDF is password-protected. Remove the "
                                     "password, then upload it again.")}
            else:
                payload = {"code": "PAGE_LIMIT_EXCEEDED",
                           "error": (f"This PDF has too many pages. The limit is "
                                     f"{reject_extra['max_pages']} pages per file."),
                           **reject_extra}
            return jsonify(payload), 400

        # Scanned pages are OCR'd (2-5 s each): cap them so a request stays far
        # below the 300 s worker timeout. Checked before converting.
        try:
            plan = conversion.plan_pdf(input_path, deadline=deadline)
        except ConversionTimeoutError:
            raise
        except Exception:
            cleanup_files(input_path)
            _release_all(admin, user_id, claimed, claim_prev, pro_claim_id)
            return jsonify({"code": "CONVERSION_FAILED",
                            "error": "We couldn't read this PDF. Please try another file."}), 400
        scanned_pages = conversion.count_ocr_pages(plan)
        if scanned_pages > conversion.MAX_OCR_PAGES:
            cleanup_files(input_path)
            _release_all(admin, user_id, claimed, claim_prev, pro_claim_id)
            logger.info("Scanned page limit: %d scanned pages", scanned_pages)
            return jsonify({"code": "SCANNED_PAGE_LIMIT",
                            "error": (f"This PDF has {scanned_pages} scanned pages. Scanned PDFs can "
                                      f"have up to {conversion.MAX_OCR_PAGES} pages per file. Split the "
                                      f"PDF or convert the first {conversion.MAX_OCR_PAGES} pages. This "
                                      "file was not counted toward your limit."),
                            "scanned_pages": scanned_pages,
                            "max_scanned_pages": conversion.MAX_OCR_PAGES}), 413

        try:
            size = os.path.getsize(input_path)
        except OSError:
            size = -1
        logger.info("Converting upload (%d bytes, %d pages, %d scanned)", size, len(plan), scanned_pages)

        run_conversion(input_path, output_path, plan=plan, deadline=deadline)

        # 6. SUCCESS — the Free claim / Pro fair-use slot stands (set by the
        #    atomic RPC). Stream the file, then clean up.
        response = send_file(
            output_path,
            as_attachment=True,
            download_name=output_name,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        response.call_on_close(lambda: cleanup_files(input_path, output_path))
        return response

    except EmptyDocumentError:
        # Nothing readable (blank pages, or images without any text): no
        # empty DOCX, and the user keeps their quota.
        logger.info("Empty document: nothing readable")
        cleanup_files(input_path, output_path)
        _release_all(admin, user_id, claimed, claim_prev, pro_claim_id)
        return jsonify({"code": "EMPTY_DOCUMENT",
                        "error": ("We couldn't find any text in this PDF. It may be blank "
                                  "or contain only images without text. This file was "
                                  "not counted toward your limit.")}), 422

    except ConversionTimeoutError:
        # Too long for one request: no DOCX (conversion.py writes it only when
        # complete), temp files deleted, the user keeps their quota.
        logger.warning("Conversion stopped: time budget of %d s used up",
                       int(conversion.CONVERSION_TIME_BUDGET))
        cleanup_files(input_path, output_path, output_path + ".part")
        _release_all(admin, user_id, claimed, claim_prev, pro_claim_id)
        return jsonify({"code": "CONVERSION_TIMEOUT",
                        "error": ("This file took too long to convert. Try a shorter file, or "
                                  "split the PDF into smaller parts. This file was not counted "
                                  "toward your limit.")}), 413

    except Exception:
        # 7. FAILURE — release the claim so the user keeps their quota.
        logger.exception("Conversion failed")
        cleanup_files(input_path, output_path)
        _release_all(admin, user_id, claimed, claim_prev, pro_claim_id)
        return jsonify({"code": "CONVERSION_FAILED",
                        "error": "We couldn't convert this file. Please try again."}), 500


def _release_all(admin, user_id, claimed, previous, pro_claim_id):
    """A conversion failed: give back the Free claim and/or the Pro fair-use slot."""
    if claimed:
        _release_claim(admin, user_id, previous)
    if pro_claim_id is not None:
        try:
            admin.rpc("release_pro_conversion",
                      {"p_user_id": user_id, "p_claim_id": pro_claim_id}).execute()
        except Exception:
            logger.exception("Failed to release the Pro fair-use slot")


def _release_claim(admin, user_id, previous):
    """Best-effort rollback of a Free claim after a failed conversion."""
    try:
        admin.rpc("release_free_conversion",
                  {"p_user_id": user_id, "p_previous": previous}).execute()
    except Exception:
        logger.exception("Failed to release free-conversion claim")


app.register_blueprint(billing.create_blueprint(
    get_store=lambda: billing.SupabaseStore(_supabase_admin()),
    get_user_id=get_authenticated_user_id,
))

# Conversion jobs (POST /api/jobs, status, download, delete): jobs_api.py. /api/convert above
# stays unchanged until the owner approves its removal.
app.register_blueprint(jobs_api.create_blueprint(sys.modules[__name__]))


# Runs once per gunicorn worker at import time.
try:
    purge_old_temp_files()
except Exception:
    logger.exception("Startup temp purge failed")


@app.errorhandler(413)
def too_large(_exc):
    return jsonify({"code": "CONVERSION_FAILED",
                    "error": "This file is larger than the 25 MB limit."}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
