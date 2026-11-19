"""
PDF2Wordly — Paddle Billing (subscriptions).
--------------------------------------------
Everything Paddle-related lives here; server.py only registers the blueprint.
This module does not import the PDF libraries, so it can be tested alone.

Environment (Railway variables only — never in code or files):
  PADDLE_ENV             "sandbox" (default) or "production"
  PADDLE_API_KEY         server-side API key (customer portal, customer email lookup)
  PADDLE_WEBHOOK_SECRET  secret of the webhook destination (signature check)
  PADDLE_PRICE_ID        expected price; other prices are only logged
If a variable is missing the server still starts; the endpoint that needs it
answers 503.

Endpoints:
  POST /api/paddle/webhook   Paddle -> us (signature verified, idempotent)
  POST /api/billing/portal   signed-in user -> customer portal links

Pro rule (is_pro): plan 'pro' AND
  status active / trialing / past_due        -> Pro
  status canceled, before current_period_end -> Pro (paid period not over)
  anything else                              -> Free
The same rule is in public.claim_free_conversion (docs/sql/002_paddle_billing.sql).

Lifecycle (tests: _tests/test_billing.py, LifecycleTests):
  renewal          subscription.updated / transaction.completed move
                   current_period_end forward
  payment failure  past_due stays Pro while Paddle retries; Paddle's later
                   subscription.canceled ends it
  scheduled cancel status stays active, cancel_at = effective date; a
                   resume (scheduled_change null) clears cancel_at
  canceled         the cancellation has TAKEN EFFECT: access ends at
                   canceled_at (or the event time), never later, even if the
                   payload still carries a future billing period. Used for
                   immediate cancels and refunds (manual: refund in Paddle,
                   then cancel immediately; adjustment.* is not handled).
  paused / resumed paused = Free; resumed sets the new period
  late transaction never moves the date of a canceled or paused subscription
  refund           adjustment.created / .updated with action "refund" and status
                   "approved" -> profiles.refunded_at + a public.paddle_refunds
                   row (kept even if the account is deleted). One refund per
                   customer: such a profile, Paddle customer or email can never
                   buy Pro again (/api/me can_purchase=false). If one still pays,
                   Pro is granted (they paid) and the event is stored as
                   "conflict" for review.

Logging: only event_id, event_type, whether a user was found, and the result.
Never the signature, a key or secret, the body, or personal data.
"""

import hashlib
import hmac
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import requests
from flask import Blueprint, jsonify, request

logger = logging.getLogger("pdf2wordly.billing")

SIGNATURE_TOLERANCE_SECONDS = 5 * 60
HTTP_TIMEOUT_SECONDS = 10

ALWAYS_PRO_STATUSES = {"active", "trialing", "past_due"}
CANCELED_STATUSES = {"canceled", "cancelled"}

SUBSCRIPTION_EVENTS = {
    "subscription.created",
    "subscription.activated",
    "subscription.updated",
    "subscription.canceled",
    "subscription.past_due",
    "subscription.paused",
    "subscription.resumed",
}
TRANSACTION_EVENTS = {"transaction.completed"}
ADJUSTMENT_EVENTS = {"adjustment.created", "adjustment.updated"}
HANDLED_EVENTS = SUBSCRIPTION_EVENTS | TRANSACTION_EVENTS | ADJUSTMENT_EVENTS
# Events that mean "this customer paid for a (new) subscription".
PURCHASE_EVENTS = {"subscription.created", "subscription.activated", "transaction.completed"}

# Columns read from public.profiles by this module and by server.py.
PROFILE_COLUMNS = (
    "id, email, plan, subscription_status, subscription_id, current_period_end, "
    "last_free_conversion_at, billing_source, paddle_customer_id, "
    "paddle_subscription_id, cancel_at, paddle_last_event_at, refunded_at"
)


# ---------------------------------------------------------------------------
# Configuration (read at call time, so tests and Railway changes apply)
# ---------------------------------------------------------------------------

def paddle_env():
    env = os.environ.get("PADDLE_ENV", "sandbox").strip().lower()
    return "production" if env in ("production", "live") else "sandbox"


def api_base_url():
    return "https://api.paddle.com" if paddle_env() == "production" else "https://sandbox-api.paddle.com"


def _api_key():
    return os.environ.get("PADDLE_API_KEY", "").strip()


def _webhook_secret():
    return os.environ.get("PADDLE_WEBHOOK_SECRET", "").strip()


def _expected_price_id():
    return os.environ.get("PADDLE_PRICE_ID", "").strip()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def parse_ts(value):
    """ISO-8601 string -> aware datetime (UTC), or None."""
    if not value:
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    try:
        dt = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _now():
    return datetime.now(timezone.utc)


def is_pro(profile, now=None):
    """The single Pro decision. `profile` is a public.profiles row (dict)."""
    if not profile:
        return False
    if str(profile.get("plan") or "free").lower() != "pro":
        return False
    status = str(profile.get("subscription_status") or "").lower()
    if status in ALWAYS_PRO_STATUSES:
        return True
    if status in CANCELED_STATUSES:
        end = parse_ts(profile.get("current_period_end"))
        return bool(end and (now or _now()) < end)
    return False


def blocks_account_deletion(profile):
    """True if a Paddle subscription could still charge after deletion.

    Blocked: a Paddle subscription that is active / trialing / past_due or
    paused (a pause can resume and charge) and NOT scheduled to cancel.
    Allowed: no Paddle subscription, canceled, or a cancellation scheduled.
    The user cancels in Paddle's portal first (the server's API key stays
    read-only for subscriptions).
    """
    if not profile or not profile.get("paddle_subscription_id"):
        return False
    status = str(profile.get("subscription_status") or "").lower()
    if status not in ALWAYS_PRO_STATUSES | {"paused"}:
        return False
    return not profile.get("cancel_at")


def billing_summary(profile, now=None):
    """Extra /api/me fields: renewal or end date, and a scheduled cancellation."""
    profile = profile or {}
    pro = is_pro(profile, now)
    status = str(profile.get("subscription_status") or "").lower()
    period_end = profile.get("current_period_end")
    cancel_at = profile.get("cancel_at")
    cancel_scheduled = bool(pro and cancel_at and status not in CANCELED_STATUSES)
    renews_at = None
    ends_at = None
    if pro:
        if cancel_scheduled:
            ends_at = cancel_at
        elif status in CANCELED_STATUSES:
            ends_at = period_end
        else:
            renews_at = period_end
    return {
        "renews_at": renews_at,
        "ends_at": ends_at,
        "cancel_scheduled": cancel_scheduled,
        "billing_source": profile.get("billing_source"),
    }


def verify_signature(header, raw_body, secret, now=None,
                     tolerance=SIGNATURE_TOLERANCE_SECONDS):
    """Check `Paddle-Signature: ts=<unix>;h1=<hex>` against the raw body.

    h1 = HMAC-SHA256(secret, "<ts>:<raw body>"). Several h1 values are
    accepted (Paddle sends two while a secret is being rotated).
    """
    if not header or not secret or raw_body is None:
        return False
    ts, h1s = None, []
    for part in header.split(";"):
        key, _, value = part.partition("=")
        key, value = key.strip(), value.strip()
        if key == "ts" and ts is None:
            ts = value
        elif key == "h1" and value:
            h1s.append(value)
    if not ts or not ts.isdigit() or not h1s:
        return False
    current = int(now if now is not None else time.time())
    if abs(current - int(ts)) > tolerance:
        return False
    expected = hmac.new(secret.encode("utf-8"), ts.encode("ascii") + b":" + raw_body,
                        hashlib.sha256).hexdigest()
    return any(hmac.compare_digest(expected, h1) for h1 in h1s)


def _valid_uuid(value):
    try:
        uuid.UUID(str(value))
        return True
    except (ValueError, TypeError, AttributeError):
        return False


def _event_ids(event_type, data):
    """(paddle_customer_id, paddle_subscription_id) from an event's data."""
    customer_id = data.get("customer_id")
    if event_type in SUBSCRIPTION_EVENTS:
        subscription_id = data.get("id")
    else:
        subscription_id = data.get("subscription_id")
    return customer_id or None, subscription_id or None


def _price_ids(data):
    ids = set()
    for item in data.get("items") or []:
        price = (item or {}).get("price") or {}
        if price.get("id"):
            ids.add(price["id"])
        elif (item or {}).get("price_id"):
            ids.add(item["price_id"])
    return ids


def _exc_name(exc):
    """Log only the exception class: messages can carry request data."""
    return type(exc).__name__


# ---------------------------------------------------------------------------
# Storage (Supabase, service role). Tests use an in-memory fake instead.
# ---------------------------------------------------------------------------

class DuplicateEvent(Exception):
    pass


class SupabaseStore:
    def __init__(self, admin):
        self.db = admin

    def insert_event(self, row):
        """Insert a paddle_events row; False if the event_id already exists."""
        try:
            self.db.table("paddle_events").insert(row).execute()
            return True
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "")
            text = str(exc)
            if code == "23505" or "23505" in text or "duplicate key" in text:
                return False
            raise

    def update_event(self, event_id, fields):
        fields = dict(fields, updated_at=_now().isoformat())
        self.db.table("paddle_events").update(fields).eq("event_id", event_id).execute()

    def delete_event(self, event_id):
        self.db.table("paddle_events").delete().eq("event_id", event_id).execute()

    def find_profile(self, column, value):
        resp = self.db.table("profiles").select(PROFILE_COLUMNS).eq(column, value).limit(1).execute()
        rows = getattr(resp, "data", None) or []
        return rows[0] if rows else None

    def update_profile(self, user_id, fields):
        fields = dict(fields, updated_at=_now().isoformat())
        self.db.table("profiles").update(fields).eq("id", user_id).execute()

    def insert_refund(self, row):
        """Record an approved refund once (adjustment_id is the key)."""
        try:
            self.db.table("paddle_refunds").insert(row).execute()
            return True
        except Exception as exc:
            code = str(getattr(exc, "code", "") or "")
            if code == "23505" or "23505" in str(exc) or "duplicate key" in str(exc):
                return False
            raise

    def find_refunds(self, column, value):
        resp = self.db.table("paddle_refunds").select(
            "adjustment_id, paddle_customer_id, paddle_subscription_id, refunded_at"
        ).eq(column, value).limit(20).execute()
        return getattr(resp, "data", None) or []


# ---------------------------------------------------------------------------
# Paddle API (server side)
# ---------------------------------------------------------------------------

def _paddle_headers():
    return {"Authorization": "Bearer " + _api_key(), "Content-Type": "application/json"}


def lookup_customer_email(customer_id):
    """Best effort: the customer's email for an unmatched event, or None."""
    if not customer_id or not _api_key():
        return None
    try:
        resp = requests.get(f"{api_base_url()}/customers/{customer_id}",
                            headers=_paddle_headers(), timeout=HTTP_TIMEOUT_SECONDS)
        if resp.status_code != 200:
            return None
        return ((resp.json() or {}).get("data") or {}).get("email")
    except Exception:
        return None


def create_portal_session(customer_id, subscription_id):
    """POST /customers/{id}/portal-sessions -> dict of links, or None."""
    body = {"subscription_ids": [subscription_id]} if subscription_id else {}
    resp = requests.post(f"{api_base_url()}/customers/{customer_id}/portal-sessions",
                         headers=_paddle_headers(), data=json.dumps(body),
                         timeout=HTTP_TIMEOUT_SECONDS)
    if resp.status_code not in (200, 201):
        logger.warning("Paddle portal session failed: HTTP %s", resp.status_code)
        return None
    urls = ((resp.json() or {}).get("data") or {}).get("urls") or {}
    overview = (urls.get("general") or {}).get("overview")
    links = {"overview_url": overview, "cancel_url": None, "update_payment_url": None}
    for sub in urls.get("subscriptions") or []:
        if not subscription_id or sub.get("id") == subscription_id:
            links["cancel_url"] = sub.get("cancel_subscription")
            links["update_payment_url"] = sub.get("update_subscription_payment_method")
            break
    return links if overview else None


# ---------------------------------------------------------------------------
# Event processing
# ---------------------------------------------------------------------------

def _refunds_for(store, profile, customer_id=None):
    """Refund rows for this profile's Paddle customer(s) or email."""
    rows = []
    seen = set()
    for column, value in (("paddle_customer_id", customer_id),
                          ("paddle_customer_id", (profile or {}).get("paddle_customer_id")),
                          ("customer_email", str((profile or {}).get("email") or "").strip().lower())):
        if value and (column, value) not in seen:
            seen.add((column, value))
            rows.extend(store.find_refunds(column, value))
    return rows


def purchase_blocked(store, profile, customer_id=None):
    """One refund per customer: True if this profile, its Paddle customer or
    its email already had an approved refund."""
    if not profile:
        return False
    if profile.get("refunded_at"):
        return True
    return bool(_refunds_for(store, profile, customer_id))


def can_purchase(store, profile):
    """For /api/me. If the check itself fails, allow (the webhook still flags
    a blocked purchase as "conflict")."""
    try:
        return not purchase_blocked(store, profile)
    except Exception as exc:
        logger.error("can_purchase check failed (%s)", _exc_name(exc))
        return True


def _is_refund_approved(data):
    return (str(data.get("action") or "").lower() == "refund"
            and str(data.get("status") or "").lower() == "approved")


def _find_user(store, data, customer_id, subscription_id):
    """Returns (profile, matched_by) or (None, None)."""
    uid = (data.get("custom_data") or {}).get("supabase_user_id")
    if uid and _valid_uuid(uid):
        profile = store.find_profile("id", str(uid))
        if profile:
            return profile, "custom_data"
    if subscription_id:
        profile = store.find_profile("paddle_subscription_id", subscription_id)
        if profile:
            return profile, "subscription_id"
    if customer_id:
        profile = store.find_profile("paddle_customer_id", customer_id)
        if profile:
            return profile, "customer_id"
    return None, None


def _is_conflict(profile, subscription_id, now):
    """True if the profile already has ANOTHER subscription that still gives Pro.

    custom_data comes from the browser, so it must not let one subscription
    overwrite (and later cancel) the Pro state of a different subscription.
    """
    current = profile.get("paddle_subscription_id")
    return bool(current and subscription_id and current != subscription_id
                and is_pro(profile, now))


def _cancel_end(data, occurred_at):
    """When access ends for a canceled subscription.

    Paddle sends status 'canceled' when the cancellation TAKES EFFECT: at the
    end of the paid period for a scheduled cancel, or right away for an
    immediate cancel (dashboard, refund). So access ends at canceled_at
    (fallback: the event time), and never later than the billing period in
    the payload, even if the payload still carries a future period.
    """
    candidates = [parse_ts(data.get("canceled_at")) or parse_ts(occurred_at)]
    candidates.append(parse_ts((data.get("current_billing_period") or {}).get("ends_at")))
    candidates = [c for c in candidates if c]
    if not candidates:
        return occurred_at
    end = min(candidates)
    return end.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def _subscription_fields(data, occurred_at, now):
    status = str(data.get("status") or "").lower()
    period_end = (data.get("current_billing_period") or {}).get("ends_at")
    if status in CANCELED_STATUSES:
        period_end = _cancel_end(data, occurred_at)
    scheduled = data.get("scheduled_change") or {}
    cancel_at = scheduled.get("effective_at") if scheduled.get("action") == "cancel" else None
    pro = is_pro({"plan": "pro", "subscription_status": status,
                  "current_period_end": period_end}, now)
    return {
        "plan": "pro" if pro else "free",
        "subscription_status": status or None,
        "current_period_end": period_end,
        "cancel_at": cancel_at,
        "billing_source": "paddle",
    }


def process_event(store, event, now=None):
    """Apply one verified Paddle event. Returns (result, user_found).

    result: duplicate | ignored | unmatched | conflict | stale | processed
    The event_id is stored first (idempotency). If applying fails, the row is
    removed again and the error is raised, so Paddle retries later.
    """
    now = now or _now()
    event_id = str(event.get("event_id") or "")
    event_type = str(event.get("event_type") or "")
    occurred_at = event.get("occurred_at")
    data = event.get("data") or {}
    customer_id, subscription_id = _event_ids(event_type, data)

    row = {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred_at,
        "status": "received",
        "paddle_customer_id": customer_id,
        "paddle_subscription_id": subscription_id,
    }
    if not store.insert_event(row):
        return "duplicate", None

    try:
        if event_type not in HANDLED_EVENTS:
            store.update_event(event_id, {"status": "ignored"})
            return "ignored", None

        expected_price = _expected_price_id()
        prices = _price_ids(data)
        if expected_price and prices and expected_price not in prices:
            logger.warning("Paddle event %s (%s): unexpected price", event_id, event_type)

        profile, _matched_by = _find_user(store, data, customer_id, subscription_id)

        if event_type in ADJUSTMENT_EVENTS:
            return _apply_adjustment(store, event_id, data, occurred_at, profile,
                                     customer_id, subscription_id, now)

        if not profile:
            store.update_event(event_id, {
                "status": "unmatched",
                "customer_email": lookup_customer_email(customer_id),
            })
            return "unmatched", False

        user_id = profile["id"]
        # One refund per customer: a refunded customer who still pays for a NEW
        # subscription gets Pro (they paid) but the event is kept for review.
        blocked_purchase = False
        if event_type in PURCHASE_EVENTS:
            refunds = _refunds_for(store, profile, customer_id)
            refunded_subs = {r.get("paddle_subscription_id") for r in refunds}
            if (profile.get("refunded_at") or refunds) and subscription_id not in refunded_subs:
                blocked_purchase = True

        if _is_conflict(profile, subscription_id, now):
            store.update_event(event_id, {"status": "conflict", "user_id": user_id})
            return "conflict", True

        if event_type in SUBSCRIPTION_EVENTS:
            last = parse_ts(profile.get("paddle_last_event_at"))
            occurred = parse_ts(occurred_at)
            if last and occurred and occurred < last:
                store.update_event(event_id, {"status": "stale", "user_id": user_id})
                return "stale", True
            fields = _subscription_fields(data, occurred_at, now)
            if occurred_at:
                fields["paddle_last_event_at"] = occurred_at
        else:
            # transaction.completed: link the IDs and extend the paid period.
            # The plan itself only changes with subscription events.
            # A late transaction must never reopen access: when this same
            # subscription is already canceled or paused, the date stays.
            fields = {"billing_source": "paddle"}
            period_end = (data.get("billing_period") or {}).get("ends_at")
            old_end = parse_ts(profile.get("current_period_end"))
            new_end = parse_ts(period_end)
            stopped = (str(profile.get("subscription_status") or "").lower()
                       in CANCELED_STATUSES | {"paused"}
                       and profile.get("paddle_subscription_id") == subscription_id)
            if new_end and not stopped and (not old_end or new_end > old_end):
                fields["current_period_end"] = period_end

        if customer_id:
            fields["paddle_customer_id"] = customer_id
        if subscription_id:
            fields["paddle_subscription_id"] = subscription_id
        store.update_profile(user_id, fields)
        if blocked_purchase:
            logger.warning("Paddle event %s (%s): purchase after a refund, review it", event_id, event_type)
            store.update_event(event_id, {"status": "conflict", "user_id": user_id})
            return "conflict", True
        store.update_event(event_id, {"status": "processed", "user_id": user_id})
        return "processed", True
    except Exception:
        try:
            store.delete_event(event_id)
        except Exception as exc:
            logger.error("Paddle event %s: could not remove the event row (%s)",
                         event_id, _exc_name(exc))
        raise


def _apply_adjustment(store, event_id, data, occurred_at, profile, customer_id, subscription_id, now):
    """adjustment.created / .updated. Only an APPROVED REFUND changes anything."""
    if not _is_refund_approved(data):
        store.update_event(event_id, {"status": "ignored",
                                      "user_id": profile["id"] if profile else None})
        return "ignored", bool(profile)
    refunded_at = occurred_at or now.isoformat()
    email = (profile or {}).get("email") or lookup_customer_email(customer_id)
    store.insert_refund({
        "adjustment_id": str(data.get("id") or event_id),
        "paddle_customer_id": customer_id,
        "paddle_subscription_id": subscription_id,
        "customer_email": str(email).strip().lower() if email else None,
        "user_id": profile["id"] if profile else None,
        "refunded_at": refunded_at,
    })
    if not profile:
        store.update_event(event_id, {"status": "unmatched", "customer_email": email})
        return "unmatched", False
    if not profile.get("refunded_at"):  # keep the first refund date
        store.update_profile(profile["id"], {"refunded_at": refunded_at})
    store.update_event(event_id, {"status": "processed", "user_id": profile["id"]})
    return "processed", True


# ---------------------------------------------------------------------------
# Flask blueprint
# ---------------------------------------------------------------------------

def create_blueprint(get_store, get_user_id):
    """get_store() -> store (raises RuntimeError if Supabase is not configured).
    get_user_id() -> (user_id, None) or (None, (response, status))."""
    bp = Blueprint("billing", __name__)

    def unavailable():
        return jsonify({"code": "BILLING_UNAVAILABLE",
                        "error": "Billing is not available right now."}), 503

    @bp.route("/api/paddle/webhook", methods=["POST"])
    def paddle_webhook():
        secret = _webhook_secret()
        if not secret:
            logger.error("Paddle webhook: PADDLE_WEBHOOK_SECRET is not set")
            return unavailable()
        raw = request.get_data(cache=True)
        if not verify_signature(request.headers.get("Paddle-Signature", ""), raw, secret):
            logger.warning("Paddle webhook rejected: invalid signature")
            return jsonify({"code": "INVALID_SIGNATURE"}), 401
        try:
            event = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            logger.warning("Paddle webhook rejected: body is not JSON")
            return jsonify({"code": "INVALID_BODY"}), 400
        if not isinstance(event, dict) or not event.get("event_id") or not event.get("event_type"):
            logger.warning("Paddle webhook rejected: missing event_id or event_type")
            return jsonify({"code": "INVALID_BODY"}), 400

        event_id, event_type = event["event_id"], event["event_type"]
        try:
            store = get_store()
        except RuntimeError:
            logger.error("Paddle event %s (%s): Supabase not configured", event_id, event_type)
            return unavailable()
        try:
            result, user_found = process_event(store, event)
        except Exception as exc:
            logger.error("Paddle event %s (%s): failed (%s), Paddle will retry",
                         event_id, event_type, _exc_name(exc))
            return jsonify({"code": "SERVER_ERROR"}), 500
        found = "n/a" if user_found is None else ("yes" if user_found else "no")
        logger.info("Paddle event %s (%s): user found=%s, result=%s",
                    event_id, event_type, found, result)
        return jsonify({"result": result}), 200

    @bp.route("/api/billing/portal", methods=["POST"])
    def billing_portal():
        if not _api_key():
            return unavailable()
        user_id, err = get_user_id()
        if err:
            return err
        try:
            store = get_store()
            profile = store.find_profile("id", user_id)
        except RuntimeError:
            return unavailable()
        except Exception as exc:
            logger.error("Billing portal: profile load failed (%s)", _exc_name(exc))
            return jsonify({"code": "SERVER_ERROR",
                            "error": "Something went wrong. Please try again later."}), 500
        customer_id = (profile or {}).get("paddle_customer_id")
        if not customer_id:
            return jsonify({"code": "NO_SUBSCRIPTION",
                            "error": "No subscription was found for this account."}), 404
        try:
            links = create_portal_session(customer_id, profile.get("paddle_subscription_id"))
        except Exception as exc:
            logger.error("Billing portal: Paddle request failed (%s)", _exc_name(exc))
            links = None
        if not links:
            return jsonify({"code": "PORTAL_FAILED",
                            "error": "We couldn't open the subscription page. Please try again later."}), 502
        return jsonify(links), 200

    return bp
