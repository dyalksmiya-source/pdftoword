"""Tests for billing.py (Paddle) and the billing parts of server.py.

Run from website/:   python -m unittest discover -s _tests -v
No network, no Supabase: an in-memory store and mocked HTTP calls.
This folder starts with "_": GitHub Pages does not publish it, and
.dockerignore keeps it out of the Docker image.
"""
import fnmatch
import hashlib
import hmac
import json
import os
import sys
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest import mock

SITE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SITE))

from flask import Flask  # noqa: E402

import billing  # noqa: E402

SECRET = "pdl_ntfset_test_secret_value"
USER = "3f1c2a7e-6b1d-4c55-9a8e-2d3b4c5d6e7f"
OTHER_USER = "9a8b7c6d-5e4f-4a3b-8c2d-1e0f9a8b7c6d"
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)


def iso(dt):
    return dt.strftime("%Y-%m-%dT%H:%M:%S.000000Z")


def sign(raw, secret=SECRET, ts=None):
    ts = int(time.time()) if ts is None else ts
    h1 = hmac.new(secret.encode(), str(ts).encode() + b":" + raw, hashlib.sha256).hexdigest()
    return f"ts={ts};h1={h1}"


def subscription_event(event_type, status="active", event_id="evt_01", user=USER,
                       sub="sub_01", customer="ctm_01", period_end=None,
                       occurred=None, scheduled=None, canceled_at=None, custom=True):
    data = {
        "id": sub,
        "status": status,
        "customer_id": customer,
        "items": [{"price": {"id": "pri_01m7qw0f7qr17f82dtkdx560wv"}, "quantity": 1}],
        "current_billing_period": (
            {"starts_at": iso(NOW - timedelta(days=1)), "ends_at": period_end}
            if period_end else None),
        "scheduled_change": scheduled,
        "canceled_at": canceled_at,
        "custom_data": {"supabase_user_id": user} if custom else None,
    }
    return {
        "event_id": event_id,
        "event_type": event_type,
        "occurred_at": occurred or iso(NOW),
        "notification_id": "ntf_01",
        "data": data,
    }


def transaction_event(event_id="evt_tx", user=USER, sub="sub_01", customer="ctm_01",
                      period_end=None, custom=True):
    return {
        "event_id": event_id,
        "event_type": "transaction.completed",
        "occurred_at": iso(NOW),
        "data": {
            "id": "txn_01",
            "status": "completed",
            "customer_id": customer,
            "subscription_id": sub,
            "items": [{"price": {"id": "pri_01m7qw0f7qr17f82dtkdx560wv"}, "quantity": 1}],
            "billing_period": {"starts_at": iso(NOW), "ends_at": period_end} if period_end else None,
            "custom_data": {"supabase_user_id": user} if custom else None,
        },
    }


class FakeStore:
    def __init__(self, profiles=()):
        self.profiles = {p["id"]: dict(p) for p in profiles}
        self.events = {}
        self.fail_profile_update = False

    def insert_event(self, row):
        if row["event_id"] in self.events:
            return False
        self.events[row["event_id"]] = dict(row)
        return True

    def update_event(self, event_id, fields):
        self.events[event_id].update(fields)

    def delete_event(self, event_id):
        self.events.pop(event_id, None)

    def find_profile(self, column, value):
        for p in self.profiles.values():
            if p.get(column) == value:
                return dict(p)
        return None

    def update_profile(self, user_id, fields):
        if self.fail_profile_update:
            raise ValueError("database down")
        self.profiles[user_id].update(fields)


def free_profile(uid=USER, **extra):
    p = {"id": uid, "plan": "free", "subscription_status": None, "current_period_end": None}
    p.update(extra)
    return p


class WebhookTestCase(unittest.TestCase):
    def setUp(self):
        self.env = mock.patch.dict(os.environ, {
            "PADDLE_WEBHOOK_SECRET": SECRET, "PADDLE_ENV": "sandbox",
            "PADDLE_API_KEY": "", "PADDLE_PRICE_ID": "pri_01m7qw0f7qr17f82dtkdx560wv",
        })
        self.env.start()
        self.addCleanup(self.env.stop)
        self.store = FakeStore([free_profile()])
        app = Flask(__name__)
        app.register_blueprint(billing.create_blueprint(
            get_store=lambda: self.store,
            get_user_id=lambda: (USER, None),
        ))
        self.client = app.test_client()

    def post(self, event, header=None, raw=None):
        raw = raw if raw is not None else json.dumps(event).encode()
        headers = {"Content-Type": "application/json"}
        if header is not False:
            headers["Paddle-Signature"] = header if header is not None else sign(raw)
        return self.client.post("/api/paddle/webhook", data=raw, headers=headers)


# ---------------------------------------------------------------------------
# 1. Signature verification
# ---------------------------------------------------------------------------
class SignatureTests(WebhookTestCase):
    def test_valid_signature(self):
        r = self.post(subscription_event("subscription.activated", period_end=iso(NOW + timedelta(days=30))))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["result"], "processed")

    def test_wrong_secret(self):
        ev = subscription_event("subscription.activated")
        raw = json.dumps(ev).encode()
        r = self.post(ev, header=sign(raw, secret="another_secret"), raw=raw)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(self.store.events, {})

    def test_old_timestamp(self):
        ev = subscription_event("subscription.activated")
        raw = json.dumps(ev).encode()
        r = self.post(ev, header=sign(raw, ts=int(time.time()) - 6 * 60), raw=raw)
        self.assertEqual(r.status_code, 401)

    def test_future_timestamp(self):
        ev = subscription_event("subscription.activated")
        raw = json.dumps(ev).encode()
        r = self.post(ev, header=sign(raw, ts=int(time.time()) + 6 * 60), raw=raw)
        self.assertEqual(r.status_code, 401)

    def test_body_changed_after_signing(self):
        ev = subscription_event("subscription.activated")
        raw = json.dumps(ev).encode()
        header = sign(raw)
        tampered = raw.replace(b'"active"', b'"trialing"')
        r = self.post(ev, header=header, raw=tampered)
        self.assertEqual(r.status_code, 401)

    def test_missing_or_malformed_header(self):
        ev = subscription_event("subscription.activated")
        self.assertEqual(self.post(ev, header=False).status_code, 401)
        self.assertEqual(self.post(ev, header="garbage").status_code, 401)
        self.assertEqual(self.post(ev, header="ts=abc;h1=00").status_code, 401)

    def test_rotation_two_h1_values(self):
        ev = subscription_event("subscription.activated")
        raw = json.dumps(ev).encode()
        ts = int(time.time())
        good = sign(raw, ts=ts).split(";h1=")[1]
        r = self.post(ev, header=f"ts={ts};h1={'0' * 64};h1={good}", raw=raw)
        self.assertEqual(r.status_code, 200)

    def test_secret_missing_returns_503(self):
        with mock.patch.dict(os.environ, {"PADDLE_WEBHOOK_SECRET": ""}):
            r = self.post(subscription_event("subscription.activated"))
        self.assertEqual(r.status_code, 503)

    def test_verify_signature_unit(self):
        raw = b'{"a":1}'
        self.assertTrue(billing.verify_signature(sign(raw, ts=1000), raw, SECRET, now=1000 + 299))
        self.assertFalse(billing.verify_signature(sign(raw, ts=1000), raw, SECRET, now=1000 + 301))
        self.assertFalse(billing.verify_signature("", raw, SECRET))
        self.assertFalse(billing.verify_signature(sign(raw), raw, ""))


# ---------------------------------------------------------------------------
# 2. Event handling
# ---------------------------------------------------------------------------
class EventTests(WebhookTestCase):
    def profile(self, uid=USER):
        return self.store.profiles[uid]

    def test_activated_gives_pro_and_saves_ids(self):
        end = iso(NOW + timedelta(days=30))
        self.post(subscription_event("subscription.activated", period_end=end))
        p = self.profile()
        self.assertEqual(p["plan"], "pro")
        self.assertEqual(p["subscription_status"], "active")
        self.assertEqual(p["paddle_customer_id"], "ctm_01")
        self.assertEqual(p["paddle_subscription_id"], "sub_01")
        self.assertEqual(p["current_period_end"], end)
        self.assertEqual(p["billing_source"], "paddle")
        self.assertIsNone(p["cancel_at"])
        self.assertTrue(billing.is_pro(p))
        self.assertEqual(self.store.events["evt_01"]["status"], "processed")
        self.assertEqual(self.store.events["evt_01"]["user_id"], USER)

    def test_created_trialing(self):
        self.post(subscription_event("subscription.created", status="trialing",
                                     period_end=iso(NOW + timedelta(days=7))))
        self.assertTrue(billing.is_pro(self.profile()))

    def test_updated_with_scheduled_cancel_stays_pro(self):
        end = iso(NOW + timedelta(days=20))
        self.post(subscription_event("subscription.activated", period_end=end, event_id="e1",
                                     occurred=iso(NOW - timedelta(minutes=5))))
        self.post(subscription_event("subscription.updated", period_end=end, event_id="e2",
                                     scheduled={"action": "cancel", "effective_at": end, "resume_at": None}))
        p = self.profile()
        self.assertEqual(p["cancel_at"], end)
        self.assertTrue(billing.is_pro(p))
        summary = billing.billing_summary(p)
        self.assertTrue(summary["cancel_scheduled"])
        self.assertEqual(summary["ends_at"], end)
        self.assertIsNone(summary["renews_at"])

    def test_canceled_at_period_end_is_free_after_date(self):
        # Paddle sends subscription.canceled when the cancellation takes effect;
        # current_billing_period is null then.
        self.post(subscription_event("subscription.canceled", status="canceled",
                                     canceled_at=iso(NOW)))
        p = self.profile()
        self.assertEqual(p["subscription_status"], "canceled")
        self.assertFalse(billing.is_pro(p, now=NOW + timedelta(seconds=1)))

    def test_canceled_with_paid_period_left_is_pro_until_end(self):
        end = iso(NOW + timedelta(days=10))
        self.post(subscription_event("subscription.canceled", status="canceled", period_end=end))
        p = self.profile()
        self.assertEqual(p["plan"], "pro")
        self.assertTrue(billing.is_pro(p, now=NOW))
        self.assertFalse(billing.is_pro(p, now=NOW + timedelta(days=11)))

    def test_past_due_is_pro(self):
        self.post(subscription_event("subscription.past_due", status="past_due",
                                     period_end=iso(NOW + timedelta(days=30))))
        self.assertTrue(billing.is_pro(self.profile()))

    def test_paused_is_free_and_resumed_is_pro(self):
        self.post(subscription_event("subscription.paused", status="paused", event_id="e1",
                                     occurred=iso(NOW - timedelta(minutes=1))))
        self.assertFalse(billing.is_pro(self.profile()))
        self.assertEqual(self.profile()["plan"], "free")
        self.post(subscription_event("subscription.resumed", status="active", event_id="e2",
                                     period_end=iso(NOW + timedelta(days=30))))
        self.assertTrue(billing.is_pro(self.profile()))

    def test_transaction_completed_links_ids_without_changing_plan(self):
        end = iso(NOW + timedelta(days=30))
        self.post(transaction_event(period_end=end))
        p = self.profile()
        self.assertEqual(p["plan"], "free")
        self.assertEqual(p["paddle_customer_id"], "ctm_01")
        self.assertEqual(p["paddle_subscription_id"], "sub_01")
        self.assertEqual(p["current_period_end"], end)
        self.assertFalse(billing.is_pro(p))

    def test_transaction_does_not_block_subscription_event(self):
        # transaction.completed often arrives first with a later occurred_at.
        self.post(transaction_event(period_end=iso(NOW + timedelta(days=30))))
        self.post(subscription_event("subscription.activated", event_id="evt_sub",
                                     occurred=iso(NOW - timedelta(seconds=2)),
                                     period_end=iso(NOW + timedelta(days=30))))
        self.assertTrue(billing.is_pro(self.profile()))

    def test_fallback_lookup_by_subscription_id(self):
        self.store.profiles[USER]["paddle_subscription_id"] = "sub_01"
        self.post(subscription_event("subscription.updated", custom=False,
                                     period_end=iso(NOW + timedelta(days=30))))
        self.assertEqual(self.store.events["evt_01"]["status"], "processed")
        self.assertTrue(billing.is_pro(self.profile()))

    def test_fallback_lookup_by_customer_id(self):
        self.store.profiles[USER]["paddle_customer_id"] = "ctm_01"
        self.post(subscription_event("subscription.updated", custom=False, sub="sub_new",
                                     period_end=iso(NOW + timedelta(days=30))))
        self.assertEqual(self.profile()["paddle_subscription_id"], "sub_new")

    def test_invalid_uuid_in_custom_data_is_not_queried(self):
        self.post(subscription_event("subscription.activated", user="not-a-uuid"))
        self.assertEqual(self.store.events["evt_01"]["status"], "unmatched")

    def test_unknown_event_type_is_ignored(self):
        ev = subscription_event("customer.updated")
        r = self.post(ev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(self.store.events["evt_01"]["status"], "ignored")
        self.assertEqual(self.profile()["plan"], "free")

    def test_second_active_subscription_is_a_conflict(self):
        p = self.store.profiles[USER]
        p.update(plan="pro", subscription_status="active", paddle_subscription_id="sub_old",
                 current_period_end=iso(NOW + timedelta(days=30)))
        r = self.post(subscription_event("subscription.canceled", status="canceled",
                                         sub="sub_other", canceled_at=iso(NOW)))
        self.assertEqual(r.get_json()["result"], "conflict")
        self.assertEqual(self.store.events["evt_01"]["status"], "conflict")
        self.assertEqual(self.profile()["subscription_status"], "active")
        self.assertEqual(self.profile()["paddle_subscription_id"], "sub_old")

    def test_new_subscription_after_old_one_ended_is_accepted(self):
        p = self.store.profiles[USER]
        p.update(plan="free", subscription_status="canceled", paddle_subscription_id="sub_old",
                 current_period_end=iso(NOW - timedelta(days=3)))
        self.post(subscription_event("subscription.activated", sub="sub_new",
                                     period_end=iso(NOW + timedelta(days=30))))
        self.assertEqual(self.profile()["paddle_subscription_id"], "sub_new")
        self.assertTrue(billing.is_pro(self.profile()))

    def test_processing_error_removes_event_so_paddle_retries(self):
        self.store.fail_profile_update = True
        r = self.post(subscription_event("subscription.activated"))
        self.assertEqual(r.status_code, 500)
        self.assertNotIn("evt_01", self.store.events)
        self.store.fail_profile_update = False
        r = self.post(subscription_event("subscription.activated"))
        self.assertEqual(r.get_json()["result"], "processed")

    def test_invalid_json_body(self):
        raw = b"not json"
        r = self.post(None, raw=raw, header=sign(raw))
        self.assertEqual(r.status_code, 400)


# ---------------------------------------------------------------------------
# 3. Unmatched events (no user found)
# ---------------------------------------------------------------------------
class UnmatchedTests(WebhookTestCase):
    def test_unmatched_saved_with_details_and_200(self):
        self.store.profiles.clear()
        resp = mock.Mock(status_code=200)
        resp.json.return_value = {"data": {"id": "ctm_77", "email": "buyer@example.com"}}
        with mock.patch.dict(os.environ, {"PADDLE_API_KEY": "pdl_sdbx_apikey_test"}), \
                mock.patch.object(billing.requests, "get", return_value=resp) as get:
            r = self.post(subscription_event("subscription.activated", user=OTHER_USER,
                                             customer="ctm_77", sub="sub_77"))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["result"], "unmatched")
        row = self.store.events["evt_01"]
        self.assertEqual(row["status"], "unmatched")
        self.assertEqual(row["event_type"], "subscription.activated")
        self.assertEqual(row["paddle_customer_id"], "ctm_77")
        self.assertEqual(row["paddle_subscription_id"], "sub_77")
        self.assertEqual(row["customer_email"], "buyer@example.com")
        self.assertNotIn("user_id", row)
        self.assertEqual(get.call_args[0][0], "https://sandbox-api.paddle.com/customers/ctm_77")

    def test_unmatched_without_api_key_still_saved(self):
        self.store.profiles.clear()
        r = self.post(transaction_event(custom=False, customer="ctm_88", sub="sub_88"))
        self.assertEqual(r.status_code, 200)
        row = self.store.events["evt_tx"]
        self.assertEqual((row["status"], row["paddle_customer_id"], row["paddle_subscription_id"]),
                         ("unmatched", "ctm_88", "sub_88"))
        self.assertIsNone(row["customer_email"])

    def test_unmatched_retry_is_a_duplicate(self):
        self.store.profiles.clear()
        ev = subscription_event("subscription.activated", user=OTHER_USER)
        self.post(ev)
        self.assertEqual(self.post(ev).get_json()["result"], "duplicate")


# ---------------------------------------------------------------------------
# 4. Idempotency and ordering
# ---------------------------------------------------------------------------
class IdempotencyTests(WebhookTestCase):
    def test_same_event_applied_once(self):
        ev = subscription_event("subscription.activated", period_end=iso(NOW + timedelta(days=30)))
        self.assertEqual(self.post(ev).get_json()["result"], "processed")
        self.store.profiles[USER]["plan"] = "free"  # would be undone if re-applied
        r = self.post(ev)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()["result"], "duplicate")
        self.assertEqual(self.store.profiles[USER]["plan"], "free")
        self.assertEqual(len(self.store.events), 1)

    def test_older_event_after_newer_is_ignored(self):
        self.post(subscription_event("subscription.paused", status="paused", event_id="new",
                                     occurred=iso(NOW)))
        r = self.post(subscription_event("subscription.activated", event_id="old",
                                         occurred=iso(NOW - timedelta(minutes=10)),
                                         period_end=iso(NOW + timedelta(days=30))))
        self.assertEqual(r.get_json()["result"], "stale")
        self.assertEqual(self.store.profiles[USER]["subscription_status"], "paused")

    def test_supabase_store_detects_duplicate_key(self):
        class APIError(Exception):
            code = "23505"

        table = mock.Mock()
        table.insert.return_value.execute.side_effect = APIError("duplicate key value")
        admin = mock.Mock()
        admin.table.return_value = table
        self.assertFalse(billing.SupabaseStore(admin).insert_event({"event_id": "x"}))
        table.insert.return_value.execute.side_effect = ValueError("other error")
        with self.assertRaises(ValueError):
            billing.SupabaseStore(admin).insert_event({"event_id": "x"})


# ---------------------------------------------------------------------------
# 5. Pro date rule
# ---------------------------------------------------------------------------
class ProRuleTests(unittest.TestCase):
    def check(self, status, end, expected, plan="pro"):
        p = {"plan": plan, "subscription_status": status, "current_period_end": end}
        self.assertEqual(billing.is_pro(p, now=NOW), expected, (plan, status, end))

    def test_table(self):
        future, past = iso(NOW + timedelta(days=1)), iso(NOW - timedelta(seconds=1))
        for status in ("active", "trialing", "past_due"):
            self.check(status, None, True)
            self.check(status, past, True)
        self.check("canceled", future, True)
        self.check("cancelled", future, True)
        self.check("canceled", past, False)
        self.check("canceled", None, False)
        self.check("canceled", "not a date", False)
        self.check("canceled", iso(NOW), False)  # ends exactly now -> Free
        for status in ("paused", "", None, "unknown"):
            self.check(status, future, False)
        self.check("active", future, False, plan="free")
        self.assertFalse(billing.is_pro(None))
        self.assertFalse(billing.is_pro({}))

    def test_summary_active(self):
        end = iso(NOW + timedelta(days=30))
        s = billing.billing_summary({"plan": "pro", "subscription_status": "active",
                                     "current_period_end": end, "billing_source": "paddle"}, now=NOW)
        self.assertEqual(s, {"renews_at": end, "ends_at": None, "cancel_scheduled": False,
                             "billing_source": "paddle"})

    def test_summary_free(self):
        s = billing.billing_summary({"plan": "free"}, now=NOW)
        self.assertEqual((s["renews_at"], s["ends_at"], s["cancel_scheduled"]), (None, None, False))


# ---------------------------------------------------------------------------
# 6. Logs: no signature, key, secret, body or personal data
# ---------------------------------------------------------------------------
class LogTests(WebhookTestCase):
    def test_logs_contain_only_safe_fields(self):
        ev = subscription_event("subscription.activated", period_end=iso(NOW + timedelta(days=30)))
        raw = json.dumps(ev).encode()
        header = sign(raw)
        bad = sign(raw, secret="wrong")
        with mock.patch.dict(os.environ, {"PADDLE_API_KEY": "pdl_sdbx_apikey_secretvalue"}), \
                self.assertLogs("pdf2wordly.billing", level="DEBUG") as logs:
            self.post(ev, header=header, raw=raw)
            self.post(ev, header=bad, raw=raw)
            self.store.profiles.clear()
            with mock.patch.object(billing.requests, "get", side_effect=OSError("no network")):
                self.post(subscription_event("subscription.updated", event_id="evt_02",
                                             user=OTHER_USER))
            self.store.profiles[USER] = free_profile()
            self.store.fail_profile_update = True
            self.post(subscription_event("subscription.updated", event_id="evt_03"))
        text = "\n".join(logs.output)
        for forbidden in (SECRET, "pdl_sdbx_apikey_secretvalue", header.split("h1=")[1],
                          bad.split("h1=")[1], USER, OTHER_USER, "ctm_01", "sub_01",
                          '"data"', "custom_data", "database down", "no network"):
            self.assertNotIn(forbidden, text)
        self.assertIn("evt_01 (subscription.activated): user found=yes, result=processed", text)
        self.assertIn("evt_02 (subscription.updated): user found=no, result=unmatched", text)
        self.assertIn("invalid signature", text)
        self.assertIn("evt_03 (subscription.updated): failed (ValueError)", text)


# ---------------------------------------------------------------------------
# 7. Customer portal
# ---------------------------------------------------------------------------
class PortalTests(WebhookTestCase):
    def portal(self):
        return self.client.post("/api/billing/portal")

    def test_503_without_api_key(self):
        self.assertEqual(self.portal().status_code, 503)

    def test_404_without_customer(self):
        with mock.patch.dict(os.environ, {"PADDLE_API_KEY": "k"}):
            self.assertEqual(self.portal().status_code, 404)

    def test_links_from_paddle(self):
        self.store.profiles[USER].update(paddle_customer_id="ctm_01", paddle_subscription_id="sub_01")
        resp = mock.Mock(status_code=201)
        resp.json.return_value = {"data": {"urls": {
            "general": {"overview": "https://customer-portal.paddle.com/cpl_1/overview"},
            "subscriptions": [{"id": "sub_01",
                               "cancel_subscription": "https://customer-portal.paddle.com/cpl_1/cancel",
                               "update_subscription_payment_method": "https://customer-portal.paddle.com/cpl_1/pay"}],
        }}}
        with mock.patch.dict(os.environ, {"PADDLE_API_KEY": "k", "PADDLE_ENV": "sandbox"}), \
                mock.patch.object(billing.requests, "post", return_value=resp) as post:
            r = self.portal()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {
            "overview_url": "https://customer-portal.paddle.com/cpl_1/overview",
            "cancel_url": "https://customer-portal.paddle.com/cpl_1/cancel",
            "update_payment_url": "https://customer-portal.paddle.com/cpl_1/pay",
        })
        self.assertEqual(post.call_args[0][0],
                         "https://sandbox-api.paddle.com/customers/ctm_01/portal-sessions")
        self.assertEqual(json.loads(post.call_args[1]["data"]), {"subscription_ids": ["sub_01"]})
        self.assertEqual(post.call_args[1]["headers"]["Authorization"], "Bearer k")

    def test_live_base_url(self):
        with mock.patch.dict(os.environ, {"PADDLE_ENV": "production"}):
            self.assertEqual(billing.api_base_url(), "https://api.paddle.com")
        with mock.patch.dict(os.environ, {"PADDLE_ENV": ""}):
            self.assertEqual(billing.api_base_url(), "https://sandbox-api.paddle.com")

    def test_paddle_error_gives_502(self):
        self.store.profiles[USER].update(paddle_customer_id="ctm_01")
        with mock.patch.dict(os.environ, {"PADDLE_API_KEY": "k"}), \
                mock.patch.object(billing.requests, "post", return_value=mock.Mock(status_code=403)):
            self.assertEqual(self.portal().status_code, 502)

    def test_signed_out_gets_401(self):
        app = Flask(__name__)
        app.register_blueprint(billing.create_blueprint(
            get_store=lambda: self.store,
            get_user_id=lambda: (None, ({"code": "AUTH_REQUIRED"}, 401)),
        ))
        with mock.patch.dict(os.environ, {"PADDLE_API_KEY": "k"}):
            self.assertEqual(app.test_client().post("/api/billing/portal").status_code, 401)


# ---------------------------------------------------------------------------
# 8. server.py: /api/me fields, routes registered, starts without Paddle vars
# ---------------------------------------------------------------------------
class ServerTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        env = {k: "" for k in ("PADDLE_ENV", "PADDLE_API_KEY", "PADDLE_WEBHOOK_SECRET", "PADDLE_PRICE_ID")}
        with mock.patch.dict(os.environ, env):
            import server  # noqa: F401  (must import with no Paddle variables)
        cls.server = sys.modules["server"]

    def fake_admin(self, row):
        chain = mock.Mock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = mock.Mock(data=[row])
        admin = mock.Mock()
        admin.table.return_value = chain
        return admin

    def test_routes_registered(self):
        rules = {r.rule for r in self.server.app.url_map.iter_rules()}
        self.assertTrue({"/api/paddle/webhook", "/api/billing/portal", "/api/me"} <= rules)

    def test_billing_endpoints_503_without_variables(self):
        with mock.patch.dict(os.environ, {"PADDLE_WEBHOOK_SECRET": "", "PADDLE_API_KEY": ""}):
            c = self.server.app.test_client()
            self.assertEqual(c.post("/api/paddle/webhook", data=b"{}").status_code, 503)
            self.assertEqual(c.post("/api/billing/portal").status_code, 503)

    def test_me_returns_dates_and_cancel_flag(self):
        end = iso(datetime.now(timezone.utc) + timedelta(days=12))
        row = {"id": USER, "plan": "pro", "subscription_status": "active", "current_period_end": end,
               "cancel_at": end, "billing_source": "paddle"}
        with mock.patch.object(self.server, "get_authenticated_user_id", return_value=(USER, None)), \
                mock.patch.object(self.server, "_supabase_admin", return_value=self.fake_admin(row)):
            body = self.server.app.test_client().get("/api/me").get_json()
        self.assertEqual(body["plan"], "pro")
        self.assertTrue(body["cancel_scheduled"])
        self.assertEqual(body["ends_at"], end)
        self.assertIsNone(body["renews_at"])
        self.assertEqual(body["billing_source"], "paddle")

    def test_server_pro_check_uses_date_rule(self):
        past = iso(datetime.now(timezone.utc) - timedelta(days=1))
        self.assertFalse(self.server.is_user_pro(
            {"plan": "pro", "subscription_status": "canceled", "current_period_end": past}))
        self.assertTrue(self.server.is_user_pro({"plan": "pro", "subscription_status": "past_due"}))


# ---------------------------------------------------------------------------
# 9. Docker image contents (.dockerignore)
# ---------------------------------------------------------------------------
def docker_ignored(rel, patterns):
    """Docker's rule: the last matching pattern wins; a path is also excluded
    when one of its parent folders matches."""
    parts = rel.split("/")
    candidates = ["/".join(parts[:i]) for i in range(1, len(parts) + 1)]
    ignored = False
    for pat in patterns:
        neg = pat.startswith("!")
        pat = pat[1:] if neg else pat
        pat = pat.rstrip("/")
        if any(fnmatch.fnmatchcase(c, pat) for c in candidates):
            ignored = not neg
    return ignored


class DockerignoreTests(unittest.TestCase):
    def test_image_contents(self):
        lines = (SITE / ".dockerignore").read_text(encoding="utf-8").splitlines()
        patterns = [l.strip() for l in lines if l.strip() and not l.strip().startswith("#")]
        for f in ("server.py", "billing.py", "requirements.txt"):
            self.assertFalse(docker_ignored(f, patterns), f + " must be in the image")
        for f in ("_tests/test_billing.py", "_tests/billing_js_test.js", "_tools/apply_site_config.py",
                  "billing.js", "index.html", ".env", "supabase/migrations/001_profiles_billing.sql"):
            self.assertTrue(docker_ignored(f, patterns), f + " must NOT be in the image")


if __name__ == "__main__":
    unittest.main()
