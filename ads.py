"""
Rewarded ads for the Android app (AdMob Server-Side Verification). Plan: WORD
docs/plans/step8.5-ads.md. Database: WORD docs/sql/005_ad_rewards.sql.

  POST /api/ads/reward-token  (signed in, Free)  -> {custom_data, reward_id, expires_in_seconds}
  GET  /api/ads/ssv           (called by Google)  -> 200 / 403 / 503
  GET  /api/ads/status        (signed in)         -> {free_available, rewards_available,
                                                      can_watch_ad, reward_arrived}

custom_data is a short token signed with HMAC-SHA256 (secret ADS_TOKEN_SECRET): it holds the
user id, a one-time id (reward_id) and an expiry. The SSV callback is trusted only when
Google's ECDSA signature is valid (official verifier keys, cached), the ad unit is ours
(ADS_REWARDED_AD_UNITS) and the token is valid; the user id comes ONLY from the token.
transaction_id and reward_id are unique in ad_rewards: a replay adds nothing.

A reward is used by POST /api/jobs (jobs_api.claim_quota) when the free conversion is used.
Nothing here logs a user id, a token or a transaction id.
"""

import base64
import hashlib
import hmac
import logging
import os
import re
import secrets
import struct
import threading
import time
import uuid
from datetime import datetime, timezone
from urllib.parse import parse_qs

from flask import Blueprint, jsonify, request

logger = logging.getLogger("pdf2wordly.ads")

VERIFIER_KEYS_URL = "https://www.gstatic.com/admob/reward/verifier-keys.json"
KEYS_TTL = 24 * 3600          # verifier keys cached 24 h
KEYS_REFETCH_MIN = 300        # unknown key_id: fetch again at most every 5 minutes
TOKEN_TTL = 3600              # custom_data valid 1 hour (ad loaded, watched, callback sent)
TOKEN_VERSION = 1
TOKEN_CONTEXT = b"pdf2wordly-ad-reward"
NONCE_RE = re.compile(r"^[0-9a-f]{16}$")
GENERIC = "Something went wrong. Please try again later."


def token_secret():
    return os.environ.get("ADS_TOKEN_SECRET", "")


def max_rewards_per_day():
    """0 = no cap (default). Hidden: never shown as a number to the user."""
    try:
        return max(0, int(os.environ.get("ADS_MAX_REWARDS_PER_DAY", "0") or 0))
    except ValueError:
        return 0


def rewarded_ad_units():
    """Our rewarded ad unit ids, numeric part only ("ca-app-pub-1/234" or "234" -> "234")."""
    raw = os.environ.get("ADS_REWARDED_AD_UNITS", "")
    return {u.strip().rsplit("/", 1)[-1] for u in raw.split(",") if u.strip()}


def enabled():
    return len(token_secret()) >= 32


# ---------------------------------------------------------------------------
# custom_data token: b64url(version | user uuid | nonce | exp) "." b64url(hmac[:16])
# ---------------------------------------------------------------------------
def _b64e(raw):
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text):
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _mac(secret, payload):
    return hmac.new(secret.encode("utf-8"), TOKEN_CONTEXT + payload, hashlib.sha256).digest()[:16]


def make_token(user_id, secret, now=None, ttl=TOKEN_TTL):
    """Returns (token, nonce_hex, exp). About 61 characters, URL-safe."""
    now = time.time() if now is None else now
    nonce = secrets.token_bytes(8)
    exp = int(now + ttl)
    payload = bytes([TOKEN_VERSION]) + uuid.UUID(str(user_id)).bytes + nonce + struct.pack(">I", exp)
    return f"{_b64e(payload)}.{_b64e(_mac(secret, payload))}", nonce.hex(), exp


def read_token(token, secret, at=None):
    """{"user_id", "nonce", "exp"} when the signature is ours and the token was valid at `at`
    (epoch seconds; the ad's own time for an SSV callback), else None."""
    if not token or not secret or not isinstance(token, str) or len(token) > 120:
        return None
    try:
        body, sig = token.split(".")
        payload, mac = _b64d(body), _b64d(sig)
    except (ValueError, TypeError):
        return None
    if len(payload) != 29 or payload[0] != TOKEN_VERSION:
        return None
    if not hmac.compare_digest(mac, _mac(secret, payload)):
        return None
    exp = struct.unpack(">I", payload[25:29])[0]
    if (time.time() if at is None else at) > exp:
        return None
    return {"user_id": str(uuid.UUID(bytes=payload[1:17])), "nonce": payload[17:25].hex(), "exp": exp}


# ---------------------------------------------------------------------------
# Google's signature (ECDSA P-256 / SHA-256), verifier keys cached
# ---------------------------------------------------------------------------
class VerifierKeys:
    """key_id (str) -> public key. Fetched from Google, cached KEYS_TTL; an unknown key_id
    triggers one new fetch at most every KEYS_REFETCH_MIN seconds."""

    def __init__(self, fetch=None, clock=time.time):
        self.fetch = fetch or self._fetch
        self.clock = clock
        self.keys = {}
        self.fetched_at = None
        self.last_try = None
        self._lock = threading.Lock()

    @staticmethod
    def _fetch():
        import requests
        resp = requests.get(VERIFIER_KEYS_URL, timeout=5)
        resp.raise_for_status()
        return resp.json()

    @staticmethod
    def parse(doc):
        from cryptography.hazmat.primitives.serialization import load_pem_public_key, load_der_public_key
        out = {}
        for k in (doc or {}).get("keys", []):
            try:
                if k.get("pem"):
                    key = load_pem_public_key(k["pem"].encode("ascii"))
                else:
                    key = load_der_public_key(base64.b64decode(k["base64"]))
                out[str(k["keyId"])] = key
            except Exception:
                logger.warning("SSV: one verifier key could not be read")
        return out

    def get(self, key_id):
        """The key, or None when Google does not list it. Raises when the keys can't be loaded."""
        now = self.clock()
        with self._lock:
            fresh = self.fetched_at is not None and now - self.fetched_at < KEYS_TTL
            if fresh and key_id in self.keys:
                return self.keys[key_id]
            if fresh and self.last_try is not None and now - self.last_try < KEYS_REFETCH_MIN:
                return None
            self.last_try = now
            try:
                keys = self.parse(self.fetch())
            except Exception as exc:
                logger.error("SSV: verifier keys fetch failed (%s)", type(exc).__name__)
                if self.keys and self.fetched_at is not None and now - self.fetched_at < 7 * KEYS_TTL:
                    return self.keys.get(key_id)       # keep using the last good keys for a while
                raise
            if keys:
                self.keys, self.fetched_at = keys, now
            return self.keys.get(key_id)


def split_signed_query(raw_query):
    """(content_to_verify, signature_b64, key_id) from the raw query string, or None.
    Google puts signature and key_id last; everything before "&signature=" is signed."""
    if isinstance(raw_query, bytes):
        raw_query = raw_query.decode("utf-8", "replace")
    i = raw_query.find("&signature=")
    if i <= 0:
        return None
    tail = parse_qs(raw_query[i + 1:], keep_blank_values=True)
    sig, key_id = tail.get("signature", [""])[0], tail.get("key_id", [""])[0]
    if not sig or not key_id.isdigit():
        return None
    return raw_query[:i], sig, key_id


def signature_ok(content, sig_b64, public_key):
    from cryptography.exceptions import InvalidSignature
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric import ec
    try:
        sig = base64.urlsafe_b64decode(sig_b64 + "=" * (-len(sig_b64) % 4))
        public_key.verify(sig, content.encode("utf-8"), ec.ECDSA(hashes.SHA256()))
        return True
    except (InvalidSignature, ValueError, TypeError):
        return False


# ---------------------------------------------------------------------------
# Supabase store (service role). Tests use a fake with the same methods.
# ---------------------------------------------------------------------------
def _one(resp):
    data = getattr(resp, "data", None)
    row = data[0] if isinstance(data, list) and data else data
    return row if isinstance(row, dict) else {}


class AdStore:
    TABLE = "ad_rewards"

    def __init__(self, admin):
        self.db = admin

    def add_reward(self, row):
        """True when stored, False for a repeated transaction_id / reward_id (or a deleted user)."""
        try:
            self.db.table(self.TABLE).insert(row).execute()
            return True
        except Exception as exc:
            text = str(exc).lower()
            if "duplicate" in text or "23505" in text or "23503" in text or "foreign key" in text:
                return False
            raise

    def status(self, user_id, nonce=None):
        return _one(self.db.rpc("ad_reward_status", {"p_user_id": user_id, "p_nonce": nonce}).execute())

    def claim(self, user_id, job_id, max_per_day):
        return _one(self.db.rpc("claim_ad_reward", {"p_user_id": user_id, "p_job_id": job_id,
                                                    "p_max_per_day": max_per_day}).execute())

    def give_back(self, job_id):
        return bool(_one(self.db.rpc("return_ad_reward", {"p_job_id": job_id}).execute()).get("returned"))


def free_available(profile, now=None):
    """Same rule as claim_free_conversion: one per rolling 24 h (display only; the RPC decides)."""
    last = (profile or {}).get("last_free_conversion_at")
    if not last:
        return True
    try:
        last_dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        return True
    now = now or datetime.now(timezone.utc)
    return (now - last_dt).total_seconds() >= 86400


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------
def create_blueprint(server, verifier=None):
    """`server` is the server module (looked up at call time so tests can patch it)."""
    bp = Blueprint("ads", __name__)
    keys = verifier or VerifierKeys()
    token_limiter = server.RateLimiter(limit=30, window_seconds=3600)
    status_limiter = server.RateLimiter(limit=30, window_seconds=60)
    bp.limiters = {"token": token_limiter, "status": status_limiter}
    bp.verifier = keys

    def error(code, status, message=None, **extra):
        body = {"code": code, "error": message or GENERIC}
        body.update(extra)
        return jsonify(body), status

    def limited(limiter, key):
        wait = limiter.hit(key)
        if wait:
            resp = jsonify({"code": "RATE_LIMITED", "error": "Too many requests. Please slow down.",
                            "retry_after_seconds": wait})
            resp.headers["Retry-After"] = str(wait)
            return resp, 429
        return None

    def store():
        return AdStore(server._supabase_admin())

    def user_and_profile(limiter):
        user_id, err = server.get_authenticated_user_id()
        if err:
            return None, None, err
        hit = limited(limiter, user_id)
        if hit:
            return None, None, hit
        try:
            profile = server.get_profile(server._supabase_admin(), user_id)
        except Exception:
            logger.exception("Ads: profile load failed")
            return None, None, error("SERVER_ERROR", 500)
        if profile is None:
            return None, None, error("PROFILE_NOT_FOUND", 403)
        return user_id, profile, None

    # ------------------------------------------------------------------ reward token
    @bp.route("/api/ads/reward-token", methods=["POST"])
    def reward_token():
        user_id, profile, err = user_and_profile(token_limiter)
        if err:
            return err
        if server.is_user_pro(profile):
            return error("ADS_NOT_FOR_PRO", 409, "Pro accounts have no ads.")
        if not enabled():
            return error("ADS_UNAVAILABLE", 503, "Ads are not available right now. Please try again later.")
        cap = max_rewards_per_day()
        if cap:
            try:
                st = store().status(user_id)
            except Exception:
                logger.exception("Ads: status failed")
                return error("SERVER_ERROR", 500)
            if int(st.get("used_24h") or 0) >= cap:
                return error("AD_LIMIT_REACHED", 429, "You can't convert more files today. "
                                                      "Please try again tomorrow.")
        token, nonce, exp = make_token(user_id, token_secret())
        return jsonify({"custom_data": token, "reward_id": nonce,
                        "expires_in_seconds": int(exp - time.time())}), 200

    # ------------------------------------------------------------------ status
    @bp.route("/api/ads/status", methods=["GET"])
    def ad_status():
        user_id, profile, err = user_and_profile(status_limiter)
        if err:
            return err
        nonce = request.args.get("reward_id") or None
        if nonce is not None and not NONCE_RE.match(nonce):
            return error("BAD_REQUEST", 400, "Invalid reward id.")
        pro = server.is_user_pro(profile)
        body = {"plan": "pro" if pro else "free", "free_available": (not pro) and free_available(profile),
                "rewards_available": 0, "can_watch_ad": False, "reward_arrived": False}
        if pro:
            return jsonify(body), 200
        try:
            st = store().status(user_id, nonce)
        except Exception:
            logger.exception("Ads: status failed")
            return error("SERVER_ERROR", 500)
        cap = max_rewards_per_day()
        body["rewards_available"] = int(st.get("available") or 0)
        body["reward_arrived"] = bool(st.get("arrived"))
        body["can_watch_ad"] = enabled() and not (cap and int(st.get("used_24h") or 0) >= cap)
        return jsonify(body), 200

    # ------------------------------------------------------------------ SSV callback (Google)
    @bp.route("/api/ads/ssv", methods=["GET"])
    def ssv():
        # No rate limit: a shared limit would let anyone block Google's callbacks, and a bad
        # request costs one signature check (key fetches are throttled in VerifierKeys).
        signed = split_signed_query(request.query_string)
        if signed is None:
            return error("BAD_SIGNATURE", 403, "Invalid signature.")
        content, sig, key_id = signed
        try:
            key = keys.get(key_id)
        except Exception:
            return error("SERVER_BUSY", 503)                   # Google retries later
        if key is None or not signature_ok(content, sig, key):
            logger.warning("SSV: invalid signature")
            return error("BAD_SIGNATURE", 403, "Invalid signature.")

        # Signed by Google from here on. 200 = "received" (no retry), even when we grant nothing.
        q = parse_qs(content, keep_blank_values=True)
        arg = lambda name: (q.get(name) or [""])[0]           # noqa: E731
        if not enabled():
            logger.error("SSV: ADS_TOKEN_SECRET missing; reward not stored")
            return error("SERVER_BUSY", 503)
        units = rewarded_ad_units()
        if arg("ad_unit").rsplit("/", 1)[-1] not in units:
            logger.warning("SSV: ad unit not ours (or ADS_REWARDED_AD_UNITS empty); no reward")
            return jsonify({"ok": True, "rewarded": False}), 200
        try:
            ad_ms = int(arg("timestamp"))
        except ValueError:
            ad_ms = int(time.time() * 1000)
        claims = read_token(arg("custom_data"), token_secret(), at=ad_ms / 1000.0)
        if claims is None:
            logger.warning("SSV: custom_data missing, invalid or expired; no reward")
            return jsonify({"ok": True, "rewarded": False}), 200
        txn = arg("transaction_id")
        if not txn or len(txn) > 200:
            return jsonify({"ok": True, "rewarded": False}), 200
        try:
            amount = int(arg("reward_amount") or 0)
        except ValueError:
            amount = 0
        row = {"user_id": claims["user_id"], "transaction_id": txn, "token_nonce": claims["nonce"],
               "ad_unit": arg("ad_unit")[:100], "reward_item": arg("reward_item")[:100] or None,
               "reward_amount": amount,
               "ad_time": datetime.fromtimestamp(ad_ms / 1000.0, timezone.utc).isoformat()}
        try:
            stored = store().add_reward(row)
        except Exception as exc:
            logger.error("SSV: reward insert failed (%s)", type(exc).__name__)
            return error("SERVER_BUSY", 503)                   # Google retries later
        logger.info("SSV: reward %s", "stored" if stored else "already stored")
        return jsonify({"ok": True, "rewarded": stored}), 200

    return bp
