"""Tests for DELETE /api/account (server.py) and billing.blocks_account_deletion.

Run from website/:   python -m unittest discover -s _tests -v
No network, no Supabase: a fake admin client records every call in order.
"""
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

SITE = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(SITE))

import billing  # noqa: E402

USER = "3f1c2a7e-6b1d-4c55-9a8e-2d3b4c5d6e7f"
EMAIL = "eve@example.com"


class FakeAdmin:
    """Enough of supabase-py for get_profile, the profile delete and auth.admin."""

    def __init__(self, profile, fail_auth=False, fail_profile_delete=False):
        self.profile = profile
        self.calls = []
        self.fail_auth = fail_auth
        self.fail_profile_delete = fail_profile_delete
        outer = self

        class Admin:
            def delete_user(self, uid, should_soft_delete=False):
                outer.calls.append(("auth.delete_user", uid))
                if outer.fail_auth:
                    raise RuntimeError("auth api down for " + EMAIL)

        class Auth:
            admin = Admin()

        self.auth = Auth()

    def table(self, name):
        outer = self

        class Query:
            def __init__(self):
                self.op = None

            def select(self, *_a):
                self.op = "select"
                return self

            def delete(self):
                self.op = "delete"
                return self

            def eq(self, col, val):
                self.filter = (col, val)
                return self

            def limit(self, _n):
                return self

            def execute(self):
                outer.calls.append((f"{name}.{self.op}", self.filter[1]))
                if self.op == "select":
                    return mock.Mock(data=[outer.profile] if outer.profile else [])
                if self.op == "delete" and outer.fail_profile_delete:
                    raise RuntimeError("db down")
                return mock.Mock(data=[])

        return Query()


def profile(**extra):
    p = {"id": USER, "email": EMAIL, "plan": "free", "subscription_status": None,
         "current_period_end": None, "cancel_at": None, "paddle_subscription_id": None,
         "billing_source": None}
    p.update(extra)
    return p


PADDLE = {"plan": "pro", "billing_source": "paddle", "paddle_subscription_id": "sub_01",
          "paddle_customer_id": "ctm_01", "current_period_end": "2099-01-01T00:00:00Z"}


class BlockRuleTests(unittest.TestCase):
    def test_rule(self):
        f = billing.blocks_account_deletion
        self.assertFalse(f(None))
        self.assertFalse(f(profile()))
        for st in ("active", "trialing", "past_due", "paused"):
            self.assertTrue(f(profile(subscription_status=st, **PADDLE)), st)
            self.assertFalse(f(profile(subscription_status=st, cancel_at="2099-01-01T00:00:00Z", **PADDLE)),
                             st + " + scheduled cancel")
        for st in ("canceled", "cancelled", None, ""):
            self.assertFalse(f(profile(subscription_status=st, **PADDLE)), str(st))
        # an old status without a Paddle subscription does not block
        self.assertFalse(f(profile(subscription_status="active", plan="pro")))


class DeleteAccountTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        import server  # noqa: F401
        cls.server = sys.modules["server"]

    def setUp(self):
        # fresh limiters for every test
        self.server.DELETE_USER_LIMITER = self.server.RateLimiter(5, 900)
        self.server.DELETE_IP_LIMITER = self.server.RateLimiter(20, 3600)
        self.client = self.server.app.test_client()

    def delete(self, admin, body=None, token=True, raw=None, headers=None):
        auth = (USER, None) if token else (None, ({"code": "AUTH_REQUIRED"}, 401))
        h = {"Content-Type": "application/json", **(headers or {})}
        data = raw if raw is not None else json.dumps({"confirm": "DELETE"} if body is None else body)
        with mock.patch.object(self.server, "get_authenticated_user_id", return_value=auth), \
                mock.patch.object(self.server, "_supabase_admin", return_value=admin), \
                self.assertLogs("pdf2wordly", level="DEBUG") as logs:
            self.server.logger.debug("start")
            r = self.client.delete("/api/account", data=data, headers=h)
        text = "\n".join(logs.output)
        self.assertNotIn(USER, text)
        self.assertNotIn(EMAIL, text)
        return r, admin.calls

    # -- allowed ---------------------------------------------------------------
    def test_no_subscription_deletes_profile_then_auth_user(self):
        r, calls = self.delete(FakeAdmin(profile()))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json(), {"deleted": True})
        self.assertEqual(calls, [("profiles.select", USER), ("profiles.delete", USER),
                                 ("auth.delete_user", USER)])

    def test_scheduled_cancel_is_allowed(self):
        r, calls = self.delete(FakeAdmin(profile(subscription_status="active",
                                                 cancel_at="2099-01-01T00:00:00Z", **PADDLE)))
        self.assertEqual(r.status_code, 200)
        self.assertIn(("auth.delete_user", USER), calls)

    def test_canceled_is_allowed(self):
        r, _ = self.delete(FakeAdmin(profile(subscription_status="canceled", **PADDLE)))
        self.assertEqual(r.status_code, 200)

    def test_paused_is_409(self):
        # a pause can resume and charge a deleted user
        r, calls = self.delete(FakeAdmin(profile(subscription_status="paused", **PADDLE)))
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.get_json()["code"], "SUBSCRIPTION_ACTIVE")
        self.assertIn("active or paused", r.get_json()["error"])
        self.assertEqual(calls, [("profiles.select", USER)], "nothing deleted")

    def test_paused_with_scheduled_cancel_is_allowed(self):
        r, _ = self.delete(FakeAdmin(profile(subscription_status="paused",
                                             cancel_at="2099-01-01T00:00:00Z", **PADDLE)))
        self.assertEqual(r.status_code, 200)

    def test_profile_already_gone_still_deletes_auth_user(self):
        r, calls = self.delete(FakeAdmin(None))
        self.assertEqual(r.status_code, 200)
        self.assertEqual(calls, [("profiles.select", USER), ("auth.delete_user", USER)])

    # -- blocked ---------------------------------------------------------------
    def test_active_renewing_subscription_is_409(self):
        for st in ("active", "trialing", "past_due"):
            admin = FakeAdmin(profile(subscription_status=st, **PADDLE))
            r, calls = self.delete(admin)
            self.assertEqual(r.status_code, 409, st)
            body = r.get_json()
            self.assertEqual(body["code"], "SUBSCRIPTION_ACTIVE")
            self.assertIn("Manage subscription", body["error"])
            self.assertEqual(calls, [("profiles.select", USER)], "nothing deleted")

    def test_wrong_or_missing_confirmation_is_400(self):
        for body in ({"confirm": "delete"}, {"confirm": "DELETE "}, {}, {"confirm": True}, ["DELETE"]):
            self.setUp()  # fresh rate limit
            admin = FakeAdmin(profile())
            r, calls = self.delete(admin, body=body)
            self.assertEqual(r.status_code, 400, body)
            self.assertEqual(r.get_json()["code"], "CONFIRMATION_REQUIRED")
            self.assertEqual(calls, [], "nothing read or deleted")
        self.setUp()  # fresh rate limit
        r, calls = self.delete(FakeAdmin(profile()), raw="not json")
        self.assertEqual(r.status_code, 400)
        self.assertEqual(calls, [])

    def test_no_token_is_401(self):
        admin = FakeAdmin(profile())
        r, calls = self.delete(admin, token=False)
        self.assertEqual(r.status_code, 401)
        self.assertEqual(calls, [])

    def test_real_auth_check_without_header(self):
        with mock.patch.object(self.server, "_supabase_admin") as adm:
            r = self.client.delete("/api/account", json={"confirm": "DELETE"})
        self.assertEqual(r.status_code, 401)
        self.assertEqual(r.get_json()["code"], "AUTH_REQUIRED")
        adm.assert_not_called()

    # -- failures --------------------------------------------------------------
    def test_auth_delete_failure_is_500_and_retry_works(self):
        admin = FakeAdmin(profile(), fail_auth=True)
        r, _ = self.delete(admin)
        self.assertEqual(r.status_code, 500)
        # retry: the profile is gone now, the auth user still exists
        r, calls = self.delete(FakeAdmin(None))
        self.assertEqual(r.status_code, 200)
        self.assertIn(("auth.delete_user", USER), calls)

    def test_profile_delete_failure_keeps_auth_user(self):
        admin = FakeAdmin(profile(), fail_profile_delete=True)
        r, calls = self.delete(admin)
        self.assertEqual(r.status_code, 500)
        self.assertNotIn(("auth.delete_user", USER), calls)

    def test_logs_only_result_codes(self):
        with mock.patch.object(self.server, "get_authenticated_user_id", return_value=(USER, None)), \
                mock.patch.object(self.server, "_supabase_admin", return_value=FakeAdmin(profile())), \
                self.assertLogs("pdf2wordly", level="INFO") as logs:
            self.client.delete("/api/account", json={"confirm": "DELETE"})
        self.assertIn("Account deleted", "\n".join(logs.output))

    # -- rate limit ------------------------------------------------------------
    def test_rate_limit_per_user(self):
        codes = []
        for _ in range(6):
            r, _ = self.delete(FakeAdmin(profile()), body={"confirm": "no"})
            codes.append(r.status_code)
        self.assertEqual(codes, [400] * 5 + [429])
        self.assertIn("Retry-After", r.headers)
        self.assertEqual(r.get_json()["code"], "RATE_LIMITED")

    def test_rate_limit_per_ip_before_auth(self):
        self.server.DELETE_IP_LIMITER = self.server.RateLimiter(2, 3600)
        codes = [self.delete(FakeAdmin(profile()), token=False,
                             headers={"X-Forwarded-For": "203.0.113.9"})[0].status_code for _ in range(3)]
        self.assertEqual(codes, [401, 401, 429])

    def test_limiter_window(self):
        lim = self.server.RateLimiter(2, 10)
        self.assertEqual([lim.hit("k", 0), lim.hit("k", 1)], [0, 0])
        self.assertGreater(lim.hit("k", 2), 0)
        self.assertEqual(lim.hit("k", 10.5), 0)

    # -- CORS ------------------------------------------------------------------
    def test_cors_preflight_allows_delete(self):
        r = self.client.options("/api/account", headers={
            "Origin": "https://pdf2wordly.com",
            "Access-Control-Request-Method": "DELETE",
            "Access-Control-Request-Headers": "authorization, content-type"})
        self.assertIn("DELETE", r.headers.get("Access-Control-Allow-Methods", ""))
        self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "https://pdf2wordly.com")


if __name__ == "__main__":
    unittest.main()
