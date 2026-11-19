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
        self.refunds = {}
        self.fail_profile_update = False

    def insert_refund(self, row):
        if row["adjustment_id"] in self.refunds:
            return False
        self.refunds[row["adjustment_id"]] = dict(row)
        return True

    def find_refunds(self, column, value):
        return [dict(r) for r in self.refunds.values() if r.get(column) == value]

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

    def test_canceled_payload_with_future_period_ends_access_now(self):
        # status 'canceled' means the cancellation has taken effect; a future
        # billing period left in the payload must not keep Pro.
        end = iso(NOW + timedelta(days=10))
        self.post(subscription_event("subscription.canceled", status="canceled", period_end=end))
        p = self.profile()
        self.assertEqual(p["plan"], "free")
        self.assertEqual(p["current_period_end"], iso(NOW))
        self.assertFalse(billing.is_pro(p, now=NOW + timedelta(seconds=1)))

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
# 2b. Subscription lifecycle (renewal, payment failure, resume, immediate
#     cancel, pause). Events carry increasing occurred_at, like Paddle's.
# ---------------------------------------------------------------------------
class LifecycleTests(WebhookTestCase):
    SUB, CTM = "sub_01m7rdz63mw1d94dhaf7xhbj95", "ctm_live_test"

    def setUp(self):
        super().setUp()
        self.n = 0
        self.period1 = NOW + timedelta(days=30)
        self.send("subscription.created", "active", period_end=self.period1, at=NOW - timedelta(days=1))
        self.send("subscription.activated", "active", period_end=self.period1, at=NOW - timedelta(days=1))

    def send(self, event_type, status, period_end=None, at=None, scheduled=None, canceled_at=None, custom=False):
        self.n += 1
        ev = subscription_event(event_type, status=status, event_id=f"evt_{self.n:03d}", sub=self.SUB,
                                customer=self.CTM, period_end=iso(period_end) if period_end else None,
                                occurred=iso(at or NOW + timedelta(minutes=self.n)), scheduled=scheduled,
                                canceled_at=iso(canceled_at) if canceled_at else None, custom=custom or self.n <= 2)
        r = self.post(ev)
        self.assertEqual(r.status_code, 200, r.get_json())
        return r.get_json()["result"]

    def transaction(self, period_end, at=None):
        self.n += 1
        ev = transaction_event(event_id=f"evt_{self.n:03d}", sub=self.SUB, customer=self.CTM,
                               period_end=iso(period_end), custom=False)
        ev["occurred_at"] = iso(at or NOW + timedelta(minutes=self.n))
        return self.post(ev).get_json()["result"]

    def p(self):
        return self.store.profiles[USER]

    def me(self, now):
        return {"pro": billing.is_pro(self.p(), now), **billing.billing_summary(self.p(), now)}

    # 1. Renewal --------------------------------------------------------------
    def test_renewal_moves_period_forward_and_stays_pro(self):
        period2 = self.period1 + timedelta(days=30)
        at = self.period1 + timedelta(minutes=1)
        self.assertEqual(self.send("subscription.updated", "active", period_end=period2, at=at), "processed")
        self.assertEqual(self.transaction(period2, at=at + timedelta(seconds=5)), "processed")
        self.assertEqual(self.p()["current_period_end"], iso(period2))
        self.assertTrue(billing.is_pro(self.p(), now=self.period1 + timedelta(days=1)))
        self.assertEqual(self.me(self.period1 + timedelta(days=1))["renews_at"], iso(period2))

    def test_renewal_transaction_first_then_subscription_updated(self):
        period2 = self.period1 + timedelta(days=30)
        at = self.period1 + timedelta(minutes=1)
        self.transaction(period2, at=at)
        self.send("subscription.updated", "active", period_end=period2, at=at - timedelta(seconds=2))
        self.assertEqual(self.p()["current_period_end"], iso(period2))
        self.assertTrue(billing.is_pro(self.p(), now=self.period1 + timedelta(days=1)))

    # 2. Payment failure ------------------------------------------------------
    def test_past_due_stays_pro_then_paddle_cancel_ends_it(self):
        period2 = self.period1 + timedelta(days=30)
        t = self.period1 + timedelta(minutes=1)
        self.send("subscription.past_due", "past_due", period_end=period2, at=t)
        self.assertTrue(billing.is_pro(self.p(), now=t + timedelta(days=3)), "past_due keeps Pro while Paddle retries")
        cancel_time = t + timedelta(days=14)
        self.send("subscription.canceled", "canceled", canceled_at=cancel_time, at=cancel_time)
        self.assertEqual(self.p()["subscription_status"], "canceled")
        self.assertFalse(billing.is_pro(self.p(), now=cancel_time + timedelta(seconds=1)))

    def test_past_due_then_payment_recovered(self):
        period2 = self.period1 + timedelta(days=30)
        t = self.period1 + timedelta(minutes=1)
        self.send("subscription.past_due", "past_due", period_end=period2, at=t)
        self.send("subscription.updated", "active", period_end=period2, at=t + timedelta(days=2))
        self.assertEqual(self.p()["subscription_status"], "active")
        self.assertTrue(billing.is_pro(self.p(), now=t + timedelta(days=3)))

    # 3. Scheduled cancellation, then resume --------------------------------
    def test_scheduled_cancel_then_resume_clears_cancel_at(self):
        sched = {"action": "cancel", "effective_at": iso(self.period1), "resume_at": None}
        self.send("subscription.updated", "active", period_end=self.period1, scheduled=sched)
        self.assertTrue(self.me(NOW)["cancel_scheduled"])
        self.assertEqual(self.p()["cancel_at"], iso(self.period1))
        self.send("subscription.updated", "active", period_end=self.period1, scheduled=None)
        self.assertIsNone(self.p()["cancel_at"])
        m = self.me(NOW)
        self.assertFalse(m["cancel_scheduled"])
        self.assertEqual(m["renews_at"], iso(self.period1))
        self.assertTrue(m["pro"])

    def test_scheduled_cancel_takes_effect_at_period_end(self):
        sched = {"action": "cancel", "effective_at": iso(self.period1), "resume_at": None}
        self.send("subscription.updated", "active", period_end=self.period1, scheduled=sched)
        self.assertTrue(billing.is_pro(self.p(), now=self.period1 - timedelta(minutes=1)))
        self.send("subscription.canceled", "canceled", canceled_at=self.period1, at=self.period1)
        self.assertFalse(billing.is_pro(self.p(), now=self.period1 + timedelta(seconds=1)))
        self.assertIsNone(self.p()["cancel_at"])

    # 4. Immediate cancellation -----------------------------------------------
    def test_immediate_cancel_ends_pro_now_even_with_future_period(self):
        t = NOW + timedelta(days=5)
        # payload still carrying the (future) billing period
        self.send("subscription.canceled", "canceled", period_end=self.period1, canceled_at=t, at=t)
        self.assertEqual(self.p()["current_period_end"], iso(t))
        self.assertFalse(billing.is_pro(self.p(), now=t + timedelta(seconds=1)))
        m = self.me(t + timedelta(seconds=1))
        self.assertFalse(m["pro"])
        self.assertIsNone(m["renews_at"])

    def test_immediate_cancel_without_canceled_at_uses_event_time(self):
        t = NOW + timedelta(days=5)
        self.send("subscription.canceled", "canceled", period_end=self.period1, at=t)
        self.assertEqual(self.p()["current_period_end"], iso(t))
        self.assertFalse(billing.is_pro(self.p(), now=t + timedelta(seconds=1)))

    def test_late_transaction_after_cancel_does_not_reopen_access(self):
        t = NOW + timedelta(days=5)
        self.send("subscription.canceled", "canceled", canceled_at=t, at=t)
        self.assertEqual(self.transaction(self.period1 + timedelta(days=30), at=t + timedelta(minutes=1)), "processed")
        self.assertEqual(self.p()["current_period_end"], iso(t))
        self.assertFalse(billing.is_pro(self.p(), now=t + timedelta(minutes=2)))

    def test_old_updated_after_immediate_cancel_is_stale(self):
        t = NOW + timedelta(days=5)
        self.send("subscription.canceled", "canceled", canceled_at=t, at=t)
        self.assertEqual(self.send("subscription.updated", "active", period_end=self.period1,
                                   at=t - timedelta(minutes=1)), "stale")
        self.assertFalse(billing.is_pro(self.p(), now=t + timedelta(seconds=1)))

    # 5. Pause / resume --------------------------------------------------------
    def test_pause_then_resume(self):
        t = NOW + timedelta(days=2)
        self.send("subscription.paused", "paused", at=t)
        self.assertFalse(billing.is_pro(self.p(), now=t + timedelta(seconds=1)))
        self.assertEqual(self.p()["plan"], "free")
        self.transaction(self.period1 + timedelta(days=30), at=t + timedelta(minutes=1))
        self.assertFalse(billing.is_pro(self.p(), now=t + timedelta(minutes=2)), "late transaction does not unpause")
        period2 = t + timedelta(days=40)
        self.send("subscription.resumed", "active", period_end=period2, at=t + timedelta(days=10))
        self.assertTrue(billing.is_pro(self.p(), now=t + timedelta(days=11)))
        self.assertEqual(self.p()["current_period_end"], iso(period2))

    def test_scheduled_pause_keeps_pro_until_it_happens(self):
        sched = {"action": "pause", "effective_at": iso(self.period1), "resume_at": None}
        self.send("subscription.updated", "active", period_end=self.period1, scheduled=sched)
        self.assertTrue(billing.is_pro(self.p(), now=NOW + timedelta(days=1)))
        self.assertIsNone(self.p()["cancel_at"], "a scheduled pause is not a scheduled cancellation")

    # 6. Refund (manual): refund in Paddle, then cancel immediately -> case 4
    def test_refund_flow_ends_access_with_the_immediate_cancel(self):
        t = NOW + timedelta(days=3)
        self.assertEqual(self.send("subscription.canceled", "canceled", period_end=self.period1,
                                   canceled_at=t, at=t), "processed")
        self.assertFalse(billing.is_pro(self.p(), now=t + timedelta(seconds=1)))
        # adjustment.* events are not subscribed; if one arrives it is ignored
        ev = {"event_id": "evt_adj", "event_type": "adjustment.created", "occurred_at": iso(t),
              "data": {"id": "adj_01", "subscription_id": self.SUB, "customer_id": self.CTM}}
        self.assertEqual(self.post(ev).get_json()["result"], "ignored")


# ---------------------------------------------------------------------------
# 2c. Refunds: one refund per customer, ever
# ---------------------------------------------------------------------------
def adjustment_event(event_type="adjustment.created", status="approved", action="refund",
                     event_id="evt_adj", sub="sub_01", customer="ctm_01", adj="adj_01", occurred=None):
    return {"event_id": event_id, "event_type": event_type, "occurred_at": occurred or iso(NOW),
            "data": {"id": adj, "action": action, "status": status, "transaction_id": "txn_01",
                     "subscription_id": sub, "customer_id": customer, "reason": "Customer request",
                     "items": [{"item_id": "txnitm_01", "type": "full"}]}}


class RefundTests(WebhookTestCase):
    def setUp(self):
        super().setUp()
        self.store.profiles[USER].update(email="eve@example.com", paddle_customer_id="ctm_01",
                                         paddle_subscription_id="sub_01", plan="pro",
                                         subscription_status="active",
                                         current_period_end=iso(NOW + timedelta(days=20)))

    def p(self):
        return self.store.profiles[USER]

    def test_approved_refund_marks_profile_and_records_refund(self):
        r = self.post(adjustment_event())
        self.assertEqual(r.get_json()["result"], "processed")
        self.assertEqual(self.p()["refunded_at"], iso(NOW))
        row = self.store.refunds["adj_01"]
        self.assertEqual((row["paddle_customer_id"], row["paddle_subscription_id"], row["customer_email"], row["user_id"]),
                         ("ctm_01", "sub_01", "eve@example.com", USER))
        self.assertEqual(self.store.events["evt_adj"]["status"], "processed")

    def test_pending_or_other_adjustments_change_nothing(self):
        for i, (status, action) in enumerate([("pending_approval", "refund"), ("rejected", "refund"), ("approved", "credit"), ("approved", "chargeback")]):
            r = self.post(adjustment_event(status=status, action=action, event_id=f"evt_{i}", adj=f"adj_{i}"))
            self.assertEqual(r.get_json()["result"], "ignored", (status, action))
        self.assertNotIn("refunded_at", self.p())
        self.assertEqual(self.store.refunds, {})

    def test_created_then_updated_is_idempotent(self):
        self.post(adjustment_event(status="pending_approval", event_id="e1"))
        self.post(adjustment_event(event_type="adjustment.updated", status="approved", event_id="e2"))
        first = self.p()["refunded_at"]
        self.post(adjustment_event(event_type="adjustment.updated", status="approved", event_id="e3",
                                   occurred=iso(NOW + timedelta(hours=1))))
        self.assertEqual(self.p()["refunded_at"], first, "first refund date kept")
        self.assertEqual(len(self.store.refunds), 1)
        self.assertEqual(self.post(adjustment_event(event_type="adjustment.updated", event_id="e2")).get_json()["result"], "duplicate")

    def test_match_by_customer_when_subscription_unknown(self):
        self.p()["paddle_subscription_id"] = "sub_other"
        self.post(adjustment_event(sub="sub_old"))
        self.assertIsNotNone(self.p().get("refunded_at"))

    def test_unmatched_refund_is_stored_and_still_recorded(self):
        self.store.profiles.clear()
        r = self.post(adjustment_event(customer="ctm_x", sub="sub_x"))
        self.assertEqual(r.get_json()["result"], "unmatched")
        self.assertEqual(self.store.events["evt_adj"]["status"], "unmatched")
        self.assertIn("adj_01", self.store.refunds, "a refund is remembered even without an account")

    def test_purchase_after_refund_grants_pro_but_is_a_conflict(self):
        self.post(adjustment_event())
        # support cancels immediately (refund.md)
        self.post(subscription_event("subscription.canceled", status="canceled", event_id="evt_c",
                                     canceled_at=iso(NOW + timedelta(minutes=1)), occurred=iso(NOW + timedelta(minutes=1))))
        self.assertEqual(self.store.events["evt_c"]["status"], "processed", "the refunded subscription's own events are not flagged")
        later = NOW + timedelta(minutes=10)
        r = self.post(subscription_event("subscription.activated", event_id="evt_new", sub="sub_new",
                                         period_end=iso(later + timedelta(days=30)), occurred=iso(later)))
        self.assertEqual(r.get_json()["result"], "conflict")
        self.assertEqual(self.store.events["evt_new"]["status"], "conflict")
        self.assertEqual(self.p()["paddle_subscription_id"], "sub_new")
        self.assertTrue(billing.is_pro(self.p(), now=later + timedelta(minutes=1)), "they paid: Pro is granted")

    def test_new_account_same_paddle_customer_is_a_conflict(self):
        # refund recorded for a customer whose account was deleted since
        self.store.refunds["adj_old"] = {"adjustment_id": "adj_old", "paddle_customer_id": "ctm_01",
                                         "paddle_subscription_id": "sub_old", "customer_email": "old@example.com",
                                         "user_id": None, "refunded_at": iso(NOW - timedelta(days=40))}
        self.p().update(plan="free", subscription_status=None, paddle_subscription_id=None)
        r = self.post(transaction_event(event_id="evt_t", sub="sub_new", customer="ctm_01",
                                        period_end=iso(NOW + timedelta(days=30))))
        self.assertEqual(r.get_json()["result"], "conflict")

    def test_purchase_blocked_rules(self):
        st = FakeStore()
        self.assertFalse(billing.purchase_blocked(st, {"id": USER, "email": "a@b.co"}))
        self.assertTrue(billing.purchase_blocked(st, {"id": USER, "refunded_at": iso(NOW)}))
        st.refunds["x"] = {"adjustment_id": "x", "paddle_customer_id": "ctm_9", "customer_email": "a@b.co"}
        self.assertTrue(billing.purchase_blocked(st, {"id": USER, "email": "A@B.co "}), "same email (new account)")
        self.assertTrue(billing.purchase_blocked(st, {"id": USER, "paddle_customer_id": "ctm_9"}), "same Paddle customer")
        self.assertFalse(billing.purchase_blocked(st, None))

    def test_can_purchase_fails_open(self):
        class Broken(FakeStore):
            def find_refunds(self, column, value):
                raise RuntimeError("table missing")
        self.assertTrue(billing.can_purchase(Broken(), {"id": USER, "email": "a@b.co"}))
        self.assertFalse(billing.can_purchase(FakeStore(), {"id": USER, "refunded_at": iso(NOW)}))

    def test_supabase_store_refund_duplicate(self):
        class APIError(Exception):
            code = "23505"
        table = mock.Mock()
        table.insert.return_value.execute.side_effect = APIError("duplicate key value")
        admin = mock.Mock()
        admin.table.return_value = table
        self.assertFalse(billing.SupabaseStore(admin).insert_refund({"adjustment_id": "a"}))


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
        self.assertIs(body["can_purchase"], True)
        self.assertEqual(body["pro_daily_limit"], 100)

    def test_me_can_purchase_false_after_refund(self):
        row = {"id": USER, "email": "eve@example.com", "plan": "free", "subscription_status": "canceled",
               "refunded_at": iso(datetime.now(timezone.utc))}
        with mock.patch.object(self.server, "get_authenticated_user_id", return_value=(USER, None)), \
                mock.patch.object(self.server, "_supabase_admin", return_value=self.fake_admin(row)):
            body = self.server.app.test_client().get("/api/me").get_json()
        self.assertIs(body["can_purchase"], False)

    def test_server_pro_check_uses_date_rule(self):
        past = iso(datetime.now(timezone.utc) - timedelta(days=1))
        self.assertFalse(self.server.is_user_pro(
            {"plan": "pro", "subscription_status": "canceled", "current_period_end": past}))
        self.assertTrue(self.server.is_user_pro({"plan": "pro", "subscription_status": "past_due"}))


# ---------------------------------------------------------------------------
# 9. /api/convert with the claim_free_conversion results of migration 002
#    (RETURNS jsonb {allowed, reason, previous}; supabase-py gives a dict).
#    ClaimCompat is a mixin so the same cases can run against another copy of
#    server.py (e.g. the version live on Railway) via `self.server`.
# ---------------------------------------------------------------------------
def tiny_pdf():
    import fitz
    doc = fitz.open()
    doc.new_page().insert_text((72, 72), "Hello")
    return doc.tobytes()


class ClaimCompat:
    server = None  # set by subclasses

    def run_convert(self, profile, rpc_result=None, rpc_error=None, fail_conversion=False, filename="doc.pdf"):
        srv = self.server
        admin = mock.Mock()
        chain = mock.Mock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = mock.Mock(data=[profile])
        admin.table.return_value = chain
        calls = []

        def rpc(name, params):
            calls.append((name, params))
            call = mock.Mock()
            if name == "claim_free_conversion" and rpc_error:
                call.execute.side_effect = rpc_error
            else:
                call.execute.return_value = mock.Mock(data=rpc_result if name == "claim_free_conversion" else None)
            return call

        admin.rpc.side_effect = rpc

        def convert(_inp, out):
            if fail_conversion:
                raise RuntimeError("conversion failed")
            Path(out).write_bytes(b"PK fake docx")

        with mock.patch.object(srv, "get_authenticated_user_id", return_value=(USER, None)), \
                mock.patch.object(srv, "_supabase_admin", return_value=admin), \
                mock.patch.object(srv, "run_conversion", side_effect=convert):
            from io import BytesIO
            r = srv.app.test_client().post(
                "/api/convert", content_type="multipart/form-data",
                data={"file": (BytesIO(tiny_pdf()), filename)})
            body = r.get_data()
            self.last_headers = dict(r.headers)
            r.close()
        return r.status_code, body, calls

    def free(self, **extra):
        p = {"id": USER, "plan": "free", "subscription_status": None,
             "current_period_end": None, "last_free_conversion_at": None}
        p.update(extra)
        return p

    def test_first_conversion(self):
        code, _, calls = self.run_convert(self.free(), {"allowed": True, "reason": "first_conversion", "previous": None})
        self.assertEqual(code, 200)
        self.assertEqual([c[0] for c in calls], ["claim_free_conversion"])

    def test_24_hours_passed(self):
        prev = iso(NOW - timedelta(days=2))
        code, _, _ = self.run_convert(self.free(last_free_conversion_at=prev),
                                      {"allowed": True, "reason": "24_hours_passed", "previous": prev})
        self.assertEqual(code, 200)

    def test_within_24_hours_is_429(self):
        last = iso(datetime.now(timezone.utc) - timedelta(hours=1))
        code, body, _ = self.run_convert(self.free(last_free_conversion_at=last),
                                         {"allowed": False, "reason": "within_24_hours", "previous": last})
        self.assertEqual(code, 429)
        data = json.loads(body)
        self.assertEqual(data["code"], "FREE_LIMIT_REACHED")
        self.assertTrue(0 < data["retry_after_seconds"] <= 86400)

    def test_pro_reason_from_database_is_accepted(self):
        # Profile the server itself may not see as Pro (e.g. the live server's
        # older rule): the database answers reason 'pro', the file converts.
        code, _, calls = self.run_convert(
            self.free(plan="pro", subscription_status="past_due"),
            {"allowed": True, "reason": "pro", "previous": None})
        self.assertEqual(code, 200)

    def test_failed_conversion_releases_with_previous(self):
        prev = iso(NOW - timedelta(days=2))
        code, _, calls = self.run_convert(self.free(last_free_conversion_at=prev),
                                          {"allowed": True, "reason": "24_hours_passed", "previous": prev},
                                          fail_conversion=True)
        self.assertEqual(code, 500)
        self.assertEqual(calls[-1], ("release_free_conversion", {"p_user_id": USER, "p_previous": prev}))

    def test_profile_not_found_exception_is_500(self):
        code, _, _ = self.run_convert(self.free(), rpc_error=Exception("P0001: Profile not found"))
        self.assertEqual(code, 500)

    def test_list_shape_still_accepted(self):
        code, _, _ = self.run_convert(self.free(), [{"allowed": True, "reason": "claimed", "previous": None}])
        self.assertEqual(code, 200)


class ClaimCompatNewServerTests(ClaimCompat, unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401
        cls.server = sys.modules["server"]

    def test_pro_user_skips_free_claim_but_uses_fair_use(self):
        code, _, calls = self.run_convert(
            self.free(plan="pro", subscription_status="canceled",
                      current_period_end=iso(datetime.now(timezone.utc) + timedelta(days=3))))
        self.assertEqual(code, 200)
        self.assertEqual([c[0] for c in calls], ["claim_pro_conversion"])
        self.assertEqual(calls[0][1], {"p_user_id": USER, "p_limit": 100})

    # ---- Pro fair use (100 per rolling 24 h) --------------------------------
    def run_pro(self, pro_result=None, pro_error=None, fail_conversion=False):
        srv = self.server
        admin = mock.Mock()
        chain = mock.Mock()
        chain.select.return_value = chain
        chain.eq.return_value = chain
        chain.limit.return_value = chain
        chain.execute.return_value = mock.Mock(data=[self.free(plan="pro", subscription_status="active")])
        admin.table.return_value = chain
        calls = []

        def rpc(name, params):
            calls.append((name, params))
            call = mock.Mock()
            if name == "claim_pro_conversion" and pro_error:
                call.execute.side_effect = pro_error
            else:
                call.execute.return_value = mock.Mock(data=pro_result if name == "claim_pro_conversion" else None)
            return call

        admin.rpc.side_effect = rpc

        def convert(_inp, out):
            if fail_conversion:
                raise RuntimeError("conversion failed")
            Path(out).write_bytes(b"PK fake docx")

        with mock.patch.object(srv, "get_authenticated_user_id", return_value=(USER, None)), \
                mock.patch.object(srv, "_supabase_admin", return_value=admin), \
                mock.patch.object(srv, "run_conversion", side_effect=convert):
            from io import BytesIO
            r = srv.app.test_client().post("/api/convert", content_type="multipart/form-data",
                                           data={"file": (BytesIO(tiny_pdf()), "doc.pdf")})
            body = r.get_data()
            r.close()
        return r.status_code, body, calls

    def test_fair_use_allowed(self):
        code, _, calls = self.run_pro({"allowed": True, "reason": "ok", "count": 7, "claim_id": 41})
        self.assertEqual(code, 200)
        self.assertEqual([c[0] for c in calls], ["claim_pro_conversion"])

    def test_fair_use_limit_reached_is_429(self):
        code, body, calls = self.run_pro({"allowed": False, "reason": "daily_limit", "count": 100,
                                          "retry_after_seconds": 3600})
        self.assertEqual(code, 429)
        data = json.loads(body)
        self.assertEqual((data["code"], data["retry_after_seconds"], data["limit"]), ("DAILY_LIMIT_REACHED", 3600, 100))
        self.assertIn("fair-use limit of 100 files in 24 hours", data["error"])

    def test_fair_use_released_when_the_conversion_fails(self):
        code, _, calls = self.run_pro({"allowed": True, "claim_id": 42}, fail_conversion=True)
        self.assertEqual(code, 500)
        self.assertEqual(calls[-1], ("release_pro_conversion", {"p_user_id": USER, "p_claim_id": 42}))

    def test_fair_use_check_error_does_not_block_a_paying_user(self):
        code, _, calls = self.run_pro(pro_error=Exception("function claim_pro_conversion does not exist"))
        self.assertEqual(code, 200)
        self.assertNotIn("release_pro_conversion", [c[0] for c in calls])

    def test_free_user_has_no_fair_use_claim(self):
        code, _, calls = self.run_convert(self.free(), {"allowed": True, "reason": "first_conversion", "previous": None})
        self.assertEqual(code, 200)
        self.assertNotIn("claim_pro_conversion", [c[0] for c in calls])


# ---------------------------------------------------------------------------
# 10. Docker image contents (.dockerignore)
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
