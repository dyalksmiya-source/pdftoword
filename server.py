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
     Pro (active subscription) = unlimited.
  4. The Free timestamp is only consumed when conversion SUCCEEDS. On
     failure the claim is released (previous timestamp restored).

Endpoints:
  GET  /api/health   -> health check (public)
  GET  /api/me       -> { plan, subscription_status, ... } for the caller (auth)
  POST /api/convert  -> multipart/form-data, field "file" (auth + limit)

Run:
  python3 server.py
"""

import logging
import os
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from flask_cors import CORS

from pdf2docx import Converter
import fitz  # PyMuPDF
import pytesseract
from PIL import Image
from docx import Document
from supabase import create_client

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("pdf2wordly")

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
MIN_CHARS_PER_PAGE = 20
OCR_ZOOM = 2.0
OCR_LANGUAGES = os.environ.get("OCR_LANGUAGES", "eng+fra+ara")

# Plans / statuses treated as Pro. Centralized here — the single definition.
PRO_PLANS = {"pro"}
PRO_STATUSES = {"active", "trialing"}

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = MAX_CONTENT_LENGTH
CORS(
    app,
    origins=ALLOWED_ORIGINS,
    supports_credentials=False,
    allow_headers=["Content-Type", "Authorization"],
    methods=["GET", "POST", "OPTIONS"],
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

    Active Pro requires BOTH plan='pro' AND an active subscription status,
    so a stale plan value alone can never grant unlimited conversions.
    """
    if not profile:
        return False
    plan = str(profile.get("plan") or "free").lower()
    status = str(profile.get("subscription_status") or "").lower()
    if plan not in PRO_PLANS:
        return False
    return status in PRO_STATUSES


def get_profile(admin, user_id):
    """Load the caller's profile with the service-role client (bypasses RLS)."""
    resp = admin.table("profiles").select(
        "id, plan, subscription_status, subscription_id, "
        "current_period_end, last_free_conversion_at"
    ).eq("id", user_id).limit(1).execute()
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


def pdf_is_scanned(pdf_path):
    doc = fitz.open(pdf_path)
    try:
        if doc.page_count == 0:
            return False
        low_text_pages = 0
        for page in doc:
            text = page.get_text().strip()
            if len(text) < MIN_CHARS_PER_PAGE:
                low_text_pages += 1
        return (low_text_pages / doc.page_count) >= 0.6
    finally:
        doc.close()


def ocr_pdf_to_docx(pdf_path, output_path):
    doc = fitz.open(pdf_path)
    document = Document()
    try:
        matrix = fitz.Matrix(OCR_ZOOM, OCR_ZOOM)
        for i, page in enumerate(doc):
            pix = page.get_pixmap(matrix=matrix)
            img_path = str(pdf_path) + f".page{i}.png"
            pix.save(img_path)
            try:
                with Image.open(img_path) as img:
                    text = pytesseract.image_to_string(img, lang=OCR_LANGUAGES)
            finally:
                cleanup_files(img_path)
            if i > 0:
                document.add_page_break()
            for line in text.splitlines():
                document.add_paragraph(line)
    finally:
        doc.close()
    document.save(output_path)


def run_conversion(input_path, output_path):
    if pdf_is_scanned(input_path):
        logger.info("Detected scanned/image-only PDF, using OCR path")
        ocr_pdf_to_docx(input_path, output_path)
    else:
        converter = Converter(str(input_path))
        try:
            converter.convert(str(output_path))
        finally:
            converter.close()
    if not os.path.exists(output_path):
        raise RuntimeError("Conversion did not produce an output file.")


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.route("/api/health", methods=["GET"])
def health():
    return jsonify({"status": "ok"})


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
    })


@app.route("/api/convert", methods=["POST"])
def convert():
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
    safe_stem = Path(original_name).stem[:80] or "document"
    output_name = safe_stem + ".docx"

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

    # 4. Free users: atomically claim their 24h slot (race-safe RPC that
    #    locks the row). Pro users skip this entirely (unlimited).
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
                                     "24 hours. Try again later or upgrade to Pro for "
                                     "unlimited conversions.")}
                if retry_after is not None:
                    payload["retry_after_seconds"] = retry_after
                return jsonify(payload), 429
            claimed = True
        except Exception:
            logger.exception("Free-limit claim failed")
            return jsonify({"code": "SERVER_ERROR",
                            "error": "Something went wrong. Please try again later."}), 500

    # 5. Store upload in a safe temp dir under a random name; verify magic bytes.
    job_id = uuid.uuid4().hex
    tmp_dir = Path(tempfile.gettempdir()) / "pdf2wordly"
    try:
        tmp_dir.mkdir(parents=True, exist_ok=True)
    except OSError:
        logger.exception("Could not create temp dir")
        if claimed:
            _release_claim(admin, user_id, claim_prev)
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
            if claimed:
                _release_claim(admin, user_id, claim_prev)
            return jsonify({"code": "CONVERSION_FAILED",
                            "error": "Please upload a valid PDF file."}), 400
        try:
            probe = fitz.open(input_path)
            probe.close()
        except Exception:
            cleanup_files(input_path)
            if claimed:
                _release_claim(admin, user_id, claim_prev)
            return jsonify({"code": "CONVERSION_FAILED",
                            "error": "We couldn't read this PDF. Please try another file."}), 400

        try:
            size = os.path.getsize(input_path)
        except OSError:
            size = -1
        logger.info("Converting upload for user %s (%d bytes)", user_id, size)

        run_conversion(input_path, output_path)

        # 6. SUCCESS — the Free claim stands (timestamp already set by the
        #    atomic RPC). Stream the file, then clean up.
        response = send_file(
            output_path,
            as_attachment=True,
            download_name=output_name,
            mimetype="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        )
        response.call_on_close(lambda: cleanup_files(input_path, output_path))
        return response

    except Exception:
        # 7. FAILURE — release the Free claim so the user keeps their quota.
        logger.exception("Conversion failed")
        cleanup_files(input_path, output_path)
        if claimed:
            _release_claim(admin, user_id, claim_prev)
        return jsonify({"code": "CONVERSION_FAILED",
                        "error": "We couldn't convert this file. Please try again."}), 500


def _release_claim(admin, user_id, previous):
    """Best-effort rollback of a Free claim after a failed conversion."""
    try:
        admin.rpc("release_free_conversion",
                  {"p_user_id": user_id, "p_previous": previous}).execute()
    except Exception:
        logger.exception("Failed to release free-conversion claim")


@app.errorhandler(413)
def too_large(_exc):
    return jsonify({"code": "CONVERSION_FAILED",
                    "error": "This file is larger than the 25 MB limit."}), 413


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5001))
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
