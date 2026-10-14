import argparse
import importlib.util
import io
import json
import os
import shutil
import ssl
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
from unittest.mock import patch


ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("claude_acct", os.path.join(ROOT, "scripts", "claude_acct.py"))
acct = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = acct
SPEC.loader.exec_module(acct)


def account(five_hour, seven_day):
    return {
        "profile": {"emailAddress": "member@example.com"},
        "usage": {"fetchedAt": 1, "data": {
            "five_hour": {"utilization": five_hour},
            "seven_day": {"utilization": seven_day},
        }},
    }


class RotationCandidateTests(unittest.TestCase):
    def setUp(self):
        self.idx = {"accounts": {
            "info": account(10, 20),
            "sales": account(92, 40),
            "xtts": account(30, 45),
        }}

    def test_skips_over_threshold_account_and_selects_next_healthy_one(self):
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache"):
            name, skipped = acct._next_eligible_rotation_account(
                {"order": ["info", "sales", "xtts"], "premiumAccount": "info"},
                self.idx, 90)
        self.assertEqual(name, "xtts")
        self.assertEqual(skipped, ["sales (already at threshold)"])

    def test_returns_no_candidate_when_all_accounts_are_over_threshold(self):
        self.idx["accounts"]["xtts"] = account(95, 20)
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache"):
            name, skipped = acct._next_eligible_rotation_account(
                {"order": ["info", "sales", "xtts"], "premiumAccount": "info"},
                self.idx, 90)
        self.assertIsNone(name)
        self.assertEqual(len(skipped), 2)

    def test_skips_destination_when_usage_cannot_be_confirmed(self):
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="error: timeout"):
            name, skipped = acct._next_eligible_rotation_account(
                {"order": ["info", "sales", "xtts"], "premiumAccount": "info"},
                self.idx, 90)
        self.assertIsNone(name)
        self.assertEqual(skipped, ["sales (usage unavailable)", "xtts (usage unavailable)"])


class DailyOnlyThresholdTests(unittest.TestCase):
    def pick(self, idx, cfg):
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache"):
            return acct._next_eligible_rotation_account(cfg, idx, 75)

    def test_weekly_usage_alone_does_not_trigger_rotation(self):
        usage = account(10, 99)["usage"]["data"]
        self.assertFalse(acct._usage_reached_threshold(usage, 75))

    def test_daily_threshold_triggers_rotation(self):
        usage = account(75, 0)["usage"]["data"]
        self.assertTrue(acct._usage_reached_threshold(usage, 75))

    def test_used_up_weekly_window_still_triggers_rotation(self):
        usage = account(10, 100)["usage"]["data"]
        self.assertTrue(acct._usage_reached_threshold(usage, 75))

    def test_destination_weekly_usage_is_ignored(self):
        idx = {"accounts": {"sales": account(80, 0), "xtts": account(0, 100), "info": account(6, 100)}}
        name, skipped = self.pick(idx, {"order": ["info", "sales", "xtts"], "premiumAccount": "sales"})
        self.assertEqual(name, "xtts")
        self.assertEqual(skipped, [])

    def test_account_used_up_as_premium_is_skipped_until_reset(self):
        idx = {"accounts": {"sales": account(80, 0), "xtts": account(0, 100), "info": account(6, 100)}}
        cfg = {"order": ["info", "sales", "xtts"], "premiumAccount": "sales",
               "usedUpUntil": {"xtts": time.time() + 3600, "info": time.time() - 1}}
        name, skipped = self.pick(idx, cfg)
        self.assertEqual(name, "info")
        self.assertTrue(skipped[0].startswith("xtts (used up as Premium until"))
        self.assertNotIn("info", cfg["usedUpUntil"])  # expired entries are dropped

    def test_marks_used_up_premium_until_latest_reset(self):
        cfg = {}
        usage = {"five_hour": {"utilization": 100, "resets_at": "2026-10-12T15:00:00+00:00"},
                 "seven_day": {"utilization": 100, "resets_at": "2026-10-15T10:00:00+00:00"}}
        acct._mark_used_up(cfg, "sales", usage)
        self.assertEqual(cfg["usedUpUntil"]["sales"], acct._parse_reset("2026-10-15T10:00:00+00:00"))

    def test_daily_threshold_alone_does_not_mark_used_up(self):
        cfg = {}
        acct._mark_used_up(cfg, "sales", account(80, 40)["usage"]["data"])
        self.assertEqual(cfg, {})


class TokenRefreshTests(unittest.TestCase):
    """Idle saved accounts get a new access token; the live login is never touched."""

    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"CLAUDE_ACCT_HOME": self.dir})
        self.env.start()
        self.store = {"b": {"claudeAiOauth": {"accessToken": "old", "refreshToken": "r1", "expiresAt": 1,
                                              "scopes": ["user:inference", "user:profile"]}}}
        self.patches = [
            patch.object(acct, "read_stored_creds", side_effect=lambda n: json.loads(json.dumps(self.store[n]))),
            patch.object(acct, "write_stored_creds", side_effect=lambda n, c: self.store.__setitem__(n, c)),
        ]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.env.stop()
        shutil.rmtree(self.dir)

    def test_expired_token_without_refresh_name_is_left_alone(self):
        with patch.object(acct, "_post_token_refresh") as post:
            status = acct.refresh_usage({}, acct.read_stored_creds("b"))
        self.assertEqual(status, "expired")
        post.assert_not_called()

    def test_refreshes_saves_rotated_refresh_token_then_reads_usage(self):
        meta, creds = {}, acct.read_stored_creds("b")
        with patch.object(acct, "_post_token_refresh", return_value={
                "access_token": "new", "refresh_token": "r2", "expires_in": 28800}) as post, \
             patch.object(acct, "fetch_usage", return_value={"five_hour": {"utilization": 3}}) as fetch:
            status = acct.refresh_usage(meta, creds, refresh_name="b")
        self.assertEqual(status, "ok")
        self.assertEqual(post.call_args.args[0]["refreshToken"], "r1")
        saved = self.store["b"]["claudeAiOauth"]
        self.assertEqual((saved["accessToken"], saved["refreshToken"]), ("new", "r2"))
        self.assertGreater(saved["expiresAt"], time.time() * 1000 + 3600 * 1000)
        self.assertEqual(saved["scopes"], ["user:inference", "user:profile"])
        self.assertEqual(fetch.call_args.args[0], "new")
        self.assertEqual(creds["claudeAiOauth"]["accessToken"], "new")

    def test_rejected_refresh_token_flags_log_in_again(self):
        err = urllib.error.HTTPError(acct.TOKEN_URL, 400, "Bad Request", {}, io.BytesIO(b'{"error":"invalid_grant"}'))
        meta = {}
        with patch.object(acct, "_post_token_refresh", side_effect=err):
            status = acct.refresh_usage(meta, acct.read_stored_creds("b"), refresh_name="b")
        self.assertEqual(status, "revoked")
        self.assertTrue(meta["revoked"])
        self.assertEqual(self.store["b"]["claudeAiOauth"]["refreshToken"], "r1")

    def test_network_error_keeps_the_login(self):
        meta = {}
        with patch.object(acct, "_post_token_refresh", side_effect=OSError("offline")):
            status = acct.refresh_usage(meta, acct.read_stored_creds("b"), refresh_name="b")
        self.assertEqual(status, "refresh error: OSError")
        self.assertNotIn("revoked", meta)

    def test_list_never_refreshes_the_live_account(self):
        idx = {"accounts": {"a": {"profile": {"emailAddress": "a@x.com"}}, "b": {"profile": {"emailAddress": "b@x.com"}}}}
        self.store["a"] = {"claudeAiOauth": {"accessToken": "live", "refreshToken": "ra", "expiresAt": 1}}
        calls = []
        with patch.object(acct, "load_index", return_value=idx), \
             patch.object(acct, "save_index"), \
             patch.object(acct, "sync_current", return_value="a"), \
             patch.object(acct, "read_live_profile", return_value={}), \
             patch.object(acct, "refresh_seat"), \
             patch.object(acct, "refresh_usage", side_effect=lambda m, c, force=False, refresh_name=None: calls.append(refresh_name) or "expired"), \
             patch.object(acct.sys, "stdout", io.StringIO()):
            acct.cmd_list(argparse.Namespace(refresh=False))
        self.assertEqual(calls, [None, "b"])


class CertificateFallbackTests(unittest.TestCase):
    """A Python without CA certificates (python.org on macOS) still reaches the API."""

    def setUp(self):
        acct._ssl_context = None

    tearDown = setUp

    def cert_error(self):
        return urllib.error.URLError(ssl.SSLCertVerificationError(1, "certificate verify failed"))

    def test_retries_with_fallback_certificates(self):
        ctx = object()
        with patch.object(acct.urllib.request, "urlopen", side_effect=[self.cert_error(), "response"]) as op, \
             patch.object(acct, "_fallback_ssl_context", return_value=ctx):
            self.assertEqual(acct.urlopen("req", timeout=5), "response")
        self.assertIs(op.call_args.kwargs["context"], ctx)
        self.assertIs(acct._ssl_context, ctx)

    def test_http_errors_are_not_retried(self):
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with patch.object(acct.urllib.request, "urlopen", side_effect=err), \
             patch.object(acct, "_fallback_ssl_context") as fallback:
            with self.assertRaises(urllib.error.HTTPError):
                acct.urlopen("req", timeout=5)
        fallback.assert_not_called()

    def test_reports_certificate_problem_clearly(self):
        self.assertIn("SSL certificate", acct.net_error(self.cert_error()))
        self.assertEqual(acct.net_error(urllib.error.URLError("timed out")), "timed out")


class StopFailureRecoveryTests(unittest.TestCase):
    def recover(self, event, before, after):
        """Run cmd_auto_recover with the live account changing from `before` to `after` during the check."""
        with patch.object(acct, "load_rotation", return_value={"enabled": True}), \
             patch.object(acct, "load_index", return_value={"accounts": {}}), \
             patch.object(acct, "current_name", side_effect=[before, after]), \
             patch.object(acct, "cmd_auto_check") as check, \
             patch.object(acct.sys, "stdin", io.StringIO(event)), \
             patch.object(acct.sys, "stderr", io.StringIO()) as err:
            try:
                acct.cmd_auto_recover(None)
                code = None
            except SystemExit as e:
                code = e.code
        return check, code, err.getvalue()

    def test_rate_limit_triggers_forced_usage_check(self):
        check, _, _ = self.recover('{"error":"rate_limit"}', "info", "info")
        check.assert_called_once()
        self.assertTrue(check.call_args.args[0].force)
        self.assertGreater(check.call_args.kwargs["lock_wait"], 0)

    def test_rotation_wakes_the_model_with_exit_2(self):
        _, code, err = self.recover('{"error":"rate_limit"}', "info", "sales")
        self.assertEqual(code, 2)
        self.assertIn("switched this session to account sales", err)
        self.assertIn("Continue the task", err)

    def test_no_rotation_does_not_wake_the_model(self):
        _, code, err = self.recover('{"error":"rate_limit"}', "info", "info")
        self.assertIsNone(code)
        self.assertEqual(err, "")

    def test_other_errors_do_not_trigger_recovery(self):
        with patch.object(acct, "cmd_auto_check") as check, \
             patch.object(acct.sys, "stdin", io.StringIO('{"error":"overloaded"}')):
            acct.cmd_auto_recover(None)
        check.assert_not_called()


class RotationLockTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.lock = os.path.join(self.dir, "rotation.lock")

    def tearDown(self):
        shutil.rmtree(self.dir)

    def test_busy_lock_is_not_acquired_without_waiting(self):
        open(self.lock, "w").close()
        self.assertFalse(acct._acquire_lock(self.lock))

    def test_waits_for_a_running_check_to_release_the_lock(self):
        open(self.lock, "w").close()
        threading.Timer(0.5, os.unlink, [self.lock]).start()
        self.assertTrue(acct._acquire_lock(self.lock, wait=5))

    def test_stale_lock_is_replaced(self):
        open(self.lock, "w").close()
        old = time.time() - 301
        os.utime(self.lock, (old, old))
        self.assertTrue(acct._acquire_lock(self.lock))


class SeatTests(unittest.TestCase):
    def creds(self):
        return {"claudeAiOauth": {"accessToken": "t", "expiresAt": (time.time() + 3600) * 1000}}

    def test_labels_known_team_seat_tiers(self):
        self.assertEqual(acct.fmt_seat({"seat": {"tier": "team_tier_1"}}), "Premium")
        self.assertEqual(acct.fmt_seat({"seat": {"tier": "team_standard"}}), "Standard")
        self.assertEqual(acct.fmt_seat({"seat": {"tier": None}, "oauth": {"subscriptionType": "team"}}), "No seat")
        self.assertEqual(acct.fmt_seat({"seat": {"tier": None}, "oauth": {"subscriptionType": "pro"}}), "—")
        self.assertEqual(acct.fmt_seat({"seat": {"tier": "enterprise_x"}}), "enterprise_x")
        self.assertEqual(acct.fmt_seat({}), "?")

    def test_reads_seat_tier_from_profile(self):
        meta = {}
        with patch.object(acct, "fetch_usage", return_value={"organization": {"seat_tier": "team_tier_1"}}) as f:
            acct.refresh_seat(meta, self.creds())
        self.assertEqual(f.call_args.args[1], acct.PROFILE_URL)
        self.assertEqual(meta["seat"]["tier"], "team_tier_1")

    def test_keeps_last_known_seat_when_profile_fails(self):
        meta = {"seat": {"fetchedAt": 0, "tier": "team_standard"}}
        with patch.object(acct, "fetch_usage", side_effect=OSError("down")):
            acct.refresh_seat(meta, self.creds())
        self.assertEqual(meta["seat"]["tier"], "team_standard")


if __name__ == "__main__":
    unittest.main()
