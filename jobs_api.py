"""
Job endpoints (registered by server.py; /api/convert is unchanged and separate):

  POST   /api/jobs               multipart "file" -> 202 {job_id, status: "queued", poll_after_seconds}
  GET    /api/jobs/<id>          status: queued (position) / running (pages_done, pages_total) /
                                 done (expires_in_seconds) / failed (code) / expired (code, counted)
  GET    /api/jobs/<id>/download the DOCX, as many times as needed within the hour
  DELETE /api/jobs/<id>          cancel (queued / running) or delete the result after a successful save

Same rules as /api/convert: user id only from the verified token, quota claimed when the job
is created (Free 1 per 24 h, 20 pages; Pro 100 per 24 h, 200 pages) and released once if the
job fails, is cancelled, or expires without a download. After the free conversion, a Free user
can convert again with one verified rewarded ad (ads.py, SQL 005): one reward is consumed
atomically, the job has the Free limits, and the reward comes back once if the job fails
(trigger on the quota release). Only the owner sees a job: any other
caller gets 404 JOB_NOT_FOUND. Polling: token verification cached (jobs.TokenCache) and a
per-user rate limit on status requests.
"""

import logging
import time
from datetime import datetime, timedelta, timezone

from flask import Blueprint, jsonify, request, send_file

import ads
import jobs

logger = logging.getLogger("pdf2wordly.jobs_api")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
GENERIC = "Something went wrong. Please try again later."

MESSAGES = {
    "SERVER_BUSY": "The converter is busy right now. Please try again in a few minutes. "
                   "This file was not counted toward your limit.",
    "JOB_ACTIVE": "You already have a file converting. Wait for it to finish, then convert the next one.",
    "JOB_NOT_FOUND": "This conversion was not found.",
    "JOB_EXPIRED": "This file expired. Convert it again; it does not count toward your limit.",
    "JOB_EXPIRED_COUNTED": "This file expired. Convert it again.",
    "QUEUE_TIMEOUT": "The converter was too busy to start your file. Please try again. "
                     "This file was not counted toward your limit.",
    "CONVERSION_INTERRUPTED": "The conversion was interrupted. Please try again. "
                              "This file was not counted toward your limit.",
    "CANCELLED": "The conversion was cancelled. This file was not counted toward your limit.",
    "CONVERSION_TIMEOUT": "This file took too long to convert. Try a shorter file, or split the PDF "
                          "into smaller parts. This file was not counted toward your limit.",
    "EMPTY_DOCUMENT": "We couldn't find any text in this PDF. It may be blank or contain only images "
                      "without text. This file was not counted toward your limit.",
    "CONVERSION_FAILED": "We couldn't convert this file. Please try again.",
    "SCANNED_PAGE_LIMIT": "This PDF has too many scanned pages. This file was not counted toward your limit.",
}


def create_blueprint(server):
    """`server` is the server module (looked up at call time so tests can patch it)."""
    bp = Blueprint("jobs", __name__)
    token_cache = jobs.TokenCache(ttl=60)
    status_limiter = server.RateLimiter(limit=15, window_seconds=30)     # ~1 request / 2 s per user
    download_limiter = server.RateLimiter(limit=20, window_seconds=600)
    create_limiter = server.RateLimiter(limit=10, window_seconds=600)
    bp.limiters = {"status": status_limiter, "download": download_limiter, "create": create_limiter}

    def error(code, status, message=None, **extra):
        body = {"code": code, "error": message or MESSAGES.get(code, GENERIC)}
        body.update(extra)
        return jsonify(body), status

    def authenticate():
        """Like server.get_authenticated_user_id, with the 60 s token cache."""
        header = request.headers.get("Authorization", "")
        token = header[len("Bearer "):].strip() if header.startswith("Bearer ") else ""
        if not token:
            return None, error("AUTH_REQUIRED", 401, "Please log in to continue.")
        cached = token_cache.get(token)
        if cached:
            return cached, None
        user_id, err = server.get_authenticated_user_id()
        if user_id:
            token_cache.put(token, user_id)
        return user_id, err

    def limited(limiter, key):
        wait = limiter.hit(key)
        if wait:
            resp = jsonify({"code": "RATE_LIMITED", "error": "Too many requests. Please slow down.",
                            "retry_after_seconds": wait})
            resp.headers["Retry-After"] = str(wait)
            return resp, 429
        return None

    def store():
        return jobs.JobStore(server._supabase_admin())

    # ------------------------------------------------------------------ create
    @bp.route("/api/jobs", methods=["POST"])
    def create_job():
        user_id, err = authenticate()
        if err:
            return err
        hit = limited(create_limiter, user_id)
        if hit:
            return hit
        if not jobs.runner_alive():
            logger.error("Job refused: runner not alive")
            return error("SERVER_BUSY", 503)
        local = jobs.local_jobs()
        active = jobs.active_jobs(local)
        mine = [k for k, v in active.items() if v["meta"].get("user_id") == user_id]
        if mine:
            return error("JOB_ACTIVE", 409, job_id=mine[0])
        if len(active) >= jobs.JOB_QUEUE_MAX:
            logger.warning("Job refused: queue full")
            return error("SERVER_BUSY", 503)

        if "file" not in request.files:
            return error("CONVERSION_FAILED", 400, "No file provided.")
        uploaded = request.files["file"]
        original_name = (uploaded.filename or "").strip()
        if original_name == "":
            return error("CONVERSION_FAILED", 400, "No file selected.")
        if not original_name.lower().endswith(".pdf"):
            return error("CONVERSION_FAILED", 400, "Please upload a valid PDF file.")
        download_name = server.docx_download_name(original_name)

        try:
            admin = server._supabase_admin()
            profile = server.get_profile(admin, user_id)
        except Exception:
            logger.exception("Job: profile load failed")
            return error("SERVER_ERROR", 500)
        if profile is None:
            return error("PROFILE_NOT_FOUND", 403, GENERIC)
        pro = server.is_user_pro(profile)

        job_id = jobs.new_job_id()
        claim, refused = claim_quota(server, admin, user_id, profile, pro, job_id)
        if refused:
            return refused

        def give_back():
            server._release_all(admin, user_id, claim["free_claimed"], claim["free_previous"],
                                claim["pro_claim_id"])
            if claim["ad_reward"]:
                try:
                    ads.AdStore(admin).give_back(job_id)    # no job row yet: return_ad_reward
                except Exception:
                    logger.exception("Job: ad reward give-back failed (healed by the next claim)")

        p = jobs.paths(job_id)
        step = "save"                     # logged when the PDF can't be read (no name, no user id)
        try:
            jobs.ensure_dir()
            uploaded.save(str(p["pdf"]))
            step = "magic"
            with open(p["pdf"], "rb") as fh:
                magic = fh.read(5)
            if magic != b"%PDF-":
                raise ValueError("not a pdf")
            step = "open"
            reject_code, reject_extra = server.check_pdf_limits(
                str(p["pdf"]), server.PRO_MAX_PAGES if pro else server.FREE_MAX_PAGES)
            if reject_code == "PASSWORD_REQUIRED":
                jobs.remove(p["pdf"])
                give_back()
                return error("PASSWORD_REQUIRED", 400, "This PDF is password-protected. Remove the "
                                                       "password, then upload it again.")
            if reject_code:
                jobs.remove(p["pdf"])
                give_back()
                return error("PAGE_LIMIT_EXCEEDED", 400,
                             f"This PDF has too many pages. The limit is {reject_extra['max_pages']} "
                             "pages per file.", **reject_extra)
            step = "plan"
            plan = server.conversion.plan_pdf(str(p["pdf"]), deadline=server.conversion.make_deadline(60))
        except Exception as exc:
            logger.warning("Job: PDF could not be read (step %s, %s)", step, type(exc).__name__)
            jobs.remove(p["pdf"])
            give_back()
            return error("CONVERSION_FAILED", 400, "We couldn't read this PDF. Please try another file.")

        scanned = server.conversion.count_ocr_pages(plan)
        max_scanned = jobs.JOB_MAX_OCR_PAGES_PRO if pro else jobs.JOB_MAX_OCR_PAGES_FREE
        if scanned > max_scanned:
            jobs.remove(p["pdf"])
            give_back()
            return error("SCANNED_PAGE_LIMIT", 413,
                         f"This PDF has {scanned} scanned pages. Scanned PDFs can have up to {max_scanned} "
                         f"pages per file. Split the PDF or convert the first {max_scanned} pages. This "
                         "file was not counted toward your limit.",
                         scanned_pages=scanned, max_scanned_pages=max_scanned)

        now = datetime.now(timezone.utc)
        size = jobs.size_class(scanned, len(plan))
        cost = jobs.estimate_seconds(scanned, len(plan))
        row = {
            "id": job_id, "user_id": user_id, "status": "queued", "plan": "pro" if pro else "free",
            "size_class": size, "pages_total": len(plan), "scanned_pages": scanned,
            "free_claimed": claim["free_claimed"], "free_previous": claim["free_previous"],
            "pro_claim_id": claim["pro_claim_id"],
            "expires_at": jobs.iso(now + timedelta(seconds=jobs.JOB_QUEUE_TIMEOUT + jobs.JOB_KILL_AFTER
                                                   + jobs.JOB_RESULT_TTL)),
        }
        try:
            jobs.JobStore(admin).insert(row)
        except Exception as exc:
            jobs.remove(p["pdf"])
            give_back()
            if "duplicate" in str(exc).lower() or "23505" in str(exc):
                return error("JOB_ACTIVE", 409)          # one active job per user (unique index)
            logger.error("Job insert failed (%s)", type(exc).__name__)
            return error("SERVER_ERROR", 500)
        jobs.write_json(p["meta"], {"user_id": user_id, "download_name": download_name, "created": time.time(),
                                    "size": size, "cost": cost, "pages": len(plan), "scanned": scanned,
                                    "max_ocr_pages": max_scanned})
        jobs.write_json(p["state"], {"status": "queued", "pages_done": 0, "pages_total": len(plan)})
        logger.info("Job queued (%d pages, %d scanned, %s%s)", len(plan), scanned, size,
                    ", ad reward" if claim["ad_reward"] else "")
        return jsonify({"job_id": job_id, "status": "queued",
                        "poll_after_seconds": jobs.POLL_AFTER_SECONDS}), 202

    # ------------------------------------------------------------------ status
    @bp.route("/api/jobs/<job_id>", methods=["GET"])
    def job_status(job_id):
        user_id, err = authenticate()
        if err:
            return err
        hit = limited(status_limiter, user_id)
        if hit:
            return hit
        view, err = owned_view(job_id, user_id)
        return err if err else (jsonify(view), 200)

    def owned_view(job_id, user_id):
        """(view, None) for the owner, else (None, 404). Reconciles jobs whose files are gone."""
        if not jobs.valid_id(job_id):
            return None, error("JOB_NOT_FOUND", 404)
        local = jobs.local_job(job_id)
        if local is not None:
            if local["meta"].get("user_id") != user_id:
                return None, error("JOB_NOT_FOUND", 404)
            return local_view(job_id, local), None
        # No local record (disk wiped by a redeploy / Serverless sleep): Supabase decides.
        try:
            st = store()
            row = st.get(job_id, user_id)
        except Exception:
            logger.exception("Job status lookup failed")
            return None, error("SERVER_ERROR", 500)
        if row is None:
            return None, error("JOB_NOT_FOUND", 404)
        status = row.get("status")
        if status in ("queued", "running") and not jobs.is_own(job_id):
            # Another container (redeploy overlap) still has it; it ends the job itself.
            return {"job_id": job_id, "status": status, "pages_total": row.get("pages_total"),
                    "pages_done": row.get("pages_done") or 0,
                    "poll_after_seconds": jobs.POLL_AFTER_SECONDS}, None
        try:
            if status in ("queued", "running"):
                jobs.fail_job(st, job_id, "CONVERSION_INTERRUPTED")
                row = {**row, "status": "failed", "error_code": "CONVERSION_INTERRUPTED"}
            elif status == "done":
                jobs.expire_done_job(st, job_id, bool(row.get("downloaded_at")))
                row = {**row, "status": "expired", "error_code": "JOB_EXPIRED"}
        except Exception:
            logger.exception("Job reconcile failed")
            return None, error("SERVER_ERROR", 500)
        return row_view(job_id, row), None

    def local_view(job_id, job):
        state = job["state"]
        status = state.get("status", "queued")
        view = {"job_id": job_id, "status": status, "pages_total": state.get("pages_total"),
                "pages_done": state.get("pages_done", 0), "poll_after_seconds": jobs.POLL_AFTER_SECONDS}
        if status == "queued":
            view["position"] = jobs.queue_position(job_id, jobs.queued_entries(jobs.local_jobs()), time.time())
        elif status == "done":
            left = float(state.get("expires_at") or 0) - time.time()
            if not job["has_result"] or left <= 0:
                # Deleted or expired, the runner has not recorded it yet.
                return expired_view(job_id, counted=job["downloaded"])
            view["expires_in_seconds"] = int(left)
        elif status in ("failed", "expired"):
            code = state.get("code") or "CONVERSION_FAILED"
            if status == "expired":
                return expired_view(job_id, counted=bool(state.get("counted")))
            view.update(code=code, error=MESSAGES.get(code, GENERIC), counted=False)
            for k in ("scanned_pages", "max_scanned_pages"):
                if k in state:
                    view[k] = state[k]
        return view

    def row_view(job_id, row):
        if row["status"] == "expired":
            return expired_view(job_id, counted=bool(row.get("downloaded_at")))
        code = row.get("error_code") or "CONVERSION_FAILED"
        return {"job_id": job_id, "status": row["status"], "code": code,
                "error": MESSAGES.get(code, GENERIC), "counted": False}

    def expired_view(job_id, counted):
        code = "JOB_EXPIRED"
        return {"job_id": job_id, "status": "expired", "code": code, "counted": counted,
                "error": MESSAGES["JOB_EXPIRED_COUNTED" if counted else "JOB_EXPIRED"]}

    # ------------------------------------------------------------------ download
    @bp.route("/api/jobs/<job_id>/download", methods=["GET"])
    def job_download(job_id):
        user_id, err = authenticate()
        if err:
            return err
        hit = limited(download_limiter, user_id)
        if hit:
            return hit
        view, err = owned_view(job_id, user_id)
        if err:
            return err
        if view["status"] != "done":
            if view["status"] == "expired":
                return jsonify(view), 410
            return error("JOB_NOT_READY", 409, "This file is not ready yet.", status=view["status"])
        p = jobs.paths(job_id)
        local = jobs.local_job(job_id)
        if not local["downloaded"]:
            try:
                ok = store().mark_downloaded(job_id, datetime.now(timezone.utc))
            except Exception:
                logger.exception("Job download record failed")
                return error("SERVER_ERROR", 500)
            if not ok:
                return jsonify(expired_view(job_id, counted=False)), 410
            jobs.touch(p["downloaded"])
        try:
            return send_file(str(p["docx"]), as_attachment=True, download_name=local["meta"]["download_name"],
                             mimetype=DOCX_MIME, max_age=0)
        except FileNotFoundError:
            return jsonify(expired_view(job_id, counted=True)), 410

    # ------------------------------------------------------------------ delete / cancel
    @bp.route("/api/jobs/<job_id>", methods=["DELETE"])
    def job_delete(job_id):
        user_id, err = authenticate()
        if err:
            return err
        if not jobs.valid_id(job_id):
            return error("JOB_NOT_FOUND", 404)
        local = jobs.local_job(job_id)
        if local is None or local["meta"].get("user_id") != user_id:
            return error("JOB_NOT_FOUND", 404)
        status = local["state"].get("status")
        p = jobs.paths(job_id)
        if status in ("queued", "running"):
            jobs.touch(p["cancel"])            # the runner / the job process ends it and releases
            return jsonify({"job_id": job_id, "status": "cancelling"}), 202
        if status == "done":
            # After a successful save: delete now. The runner records the expiry (quota kept if
            # downloaded, released if not).
            jobs.delete_job_files(job_id)
        return jsonify({"job_id": job_id, "deleted": True}), 200

    return bp


def claim_quota(server, admin, user_id, profile, pro, job_id=None):
    """Same claims and refusals as /api/convert (that endpoint keeps its own copy), plus the
    rewarded-ad conversion for Free users (only here, not in /api/convert).
    Returns ({"free_claimed", "free_previous", "pro_claim_id", "ad_reward"}, None) or (None, response)."""
    from flask import jsonify as _j
    claim = {"free_claimed": False, "free_previous": None, "pro_claim_id": None, "ad_reward": False}
    if not pro:
        try:
            resp = admin.rpc("claim_free_conversion", {"p_user_id": user_id}).execute()
            data = getattr(resp, "data", None)
            row = data[0] if isinstance(data, list) and data else data
            allowed = bool(row.get("allowed")) if isinstance(row, dict) else False
            if not allowed:
                ad = claim_ad_reward(admin, user_id, job_id)
                if ad.get("allowed"):
                    claim["ad_reward"] = True
                    return claim, None
                payload = {"code": "FREE_LIMIT_REACHED",
                           "error": ("You have used your free conversion for the last 24 hours. Try again "
                                     f"later or upgrade to Pro (up to {server.PRO_DAILY_LIMIT} files per day).")}
                try:
                    last = profile.get("last_free_conversion_at")
                    if last:
                        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
                        payload["retry_after_seconds"] = max(
                            0, int(86400 - (datetime.now(timezone.utc) - last_dt).total_seconds()))
                except Exception:
                    pass
                # For the app: can a rewarded ad unlock one more conversion? (The site ignores it.)
                payload["ad_allowed"] = ads.enabled() and ad.get("reason") != "cap"
                return None, (_j(payload), 429)
            claim["free_claimed"] = True
            claim["free_previous"] = row.get("previous")
        except Exception:
            logger.exception("Job: free-limit claim failed")
            return None, (_j({"code": "SERVER_ERROR", "error": GENERIC}), 500)
    else:
        try:
            resp = admin.rpc("claim_pro_conversion",
                             {"p_user_id": user_id, "p_limit": server.PRO_DAILY_LIMIT}).execute()
            data = getattr(resp, "data", None)
            row = data[0] if isinstance(data, list) and data else data
            if isinstance(row, dict) and row.get("allowed") is False:
                payload = {"code": "DAILY_LIMIT_REACHED", "limit": server.PRO_DAILY_LIMIT,
                           "error": (f"You have reached the fair-use limit of {server.PRO_DAILY_LIMIT} "
                                     "files in 24 hours. Please try again later.")}
                try:
                    payload["retry_after_seconds"] = max(0, int(row.get("retry_after_seconds")))
                except (TypeError, ValueError):
                    pass
                return None, (_j(payload), 429)
            if isinstance(row, dict) and row.get("claim_id") is not None:
                claim["pro_claim_id"] = row.get("claim_id")
        except Exception as exc:
            logger.error("Job: Pro fair-use claim failed (%s); allowed", type(exc).__name__)
    return claim, None


def claim_ad_reward(admin, user_id, job_id):
    """{"allowed": True, "reward_id"} or {"allowed": False, "reason"}. Never raises: before
    SQL 005 runs (or on any error) the answer is simply "no reward"."""
    if job_id is None:
        return {"allowed": False, "reason": "no_reward"}
    try:
        row = ads.AdStore(admin).claim(user_id, job_id, ads.max_rewards_per_day())
    except Exception as exc:
        logger.error("Job: ad reward claim failed (%s)", type(exc).__name__)
        return {"allowed": False, "reason": "error"}
    return row if row.get("allowed") else {"allowed": False, "reason": row.get("reason") or "no_reward"}


def health_fields():
    """Extra /api/health fields: is the jobs runner alive (local heartbeat)."""
    age = jobs.runner_age()
    return {"runner": "alive" if jobs.runner_alive() else "dead",
            "runner_heartbeat_age_seconds": None if age is None else int(age)}


