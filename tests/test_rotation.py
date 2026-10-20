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

# Keep the rotation log and its state out of the real ~/.claude-acct while tests run.
os.environ["CLAUDE_ACCT_HOME"] = tempfile.mkdtemp()
os.environ.pop("CLAUDE_ACCT_LOG_URL", None)
acct.LOG_URL = ""  # never upload test events to the shared sheet


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
            "sales": account(80, 40),  # 80% Standard ≈ 16% Premium
            "xtts": account(30, 45),
        }}

    def test_skips_over_threshold_account_and_selects_next_healthy_one(self):
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache"):
            name, skipped = acct._next_eligible_rotation_account(
                {"order": ["info", "sales", "xtts"], "premiumAccount": "info"},
                self.idx, 15, 15)
        self.assertEqual(name, "xtts")
        self.assertEqual(skipped, ["sales (already at threshold: 5-hour 80% ≥ 75%)"])

    def test_returns_no_candidate_when_all_accounts_are_over_threshold(self):
        self.idx["accounts"]["xtts"] = account(10, 100)  # 7-day ≈ 20% Premium
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache"):
            name, skipped = acct._next_eligible_rotation_account(
                {"order": ["info", "sales", "xtts"], "premiumAccount": "info"},
                self.idx, 15, 15)
        self.assertIsNone(name)
        self.assertEqual(len(skipped), 2)

    def test_skips_destination_when_usage_cannot_be_confirmed(self):
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="error: timeout"):
            name, skipped = acct._next_eligible_rotation_account(
                {"order": ["info", "sales", "xtts"], "premiumAccount": "info"},
                self.idx, 15, 15)
        self.assertIsNone(name)
        self.assertEqual(skipped, ["sales (usage unavailable)", "xtts (usage unavailable)"])


class ThresholdTests(unittest.TestCase):
    def pick(self, idx, cfg):
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache"):
            return acct._next_eligible_rotation_account(cfg, idx, 75)

    def test_weekly_usage_below_threshold_does_not_trigger_rotation(self):
        usage = account(10, 89)["usage"]["data"]
        self.assertFalse(acct._usage_reached_threshold(usage, 75, 90))

    def test_weekly_threshold_triggers_rotation(self):
        usage = account(10, 90)["usage"]["data"]
        self.assertTrue(acct._usage_reached_threshold(usage, 75, 90))

    def test_weekly_threshold_defaults_for_existing_configs(self):
        self.assertEqual(acct._weekly_threshold({"dailyThreshold": 90, "order": ["a", "b"]}), 90)
        self.assertEqual(acct._weekly_threshold({"weeklyThreshold": 80}), 80)

    def test_old_default_daily_threshold_follows_the_new_default(self):
        self.assertEqual(acct._rotation_threshold({"dailyThreshold": 90}), 80)
        self.assertEqual(acct._rotation_threshold({}), 80)
        self.assertEqual(acct._rotation_threshold({"dailyThreshold": 75}), 75)

    def check(self, five_hour, seven_day):
        cfg = {"dailyThreshold": 90}  # as saved by `configure` before 0.9.1
        return acct._usage_reached_threshold(account(five_hour, seven_day)["usage"]["data"],
                                             acct._rotation_threshold(cfg), acct._weekly_threshold(cfg))

    def test_default_rule(self):
        # 5-hour >= 80% or 7-day >= 90% (or any window used up).
        self.assertFalse(self.check(79, 89))
        self.assertTrue(self.check(80, 0))
        self.assertTrue(self.check(0, 90))
        self.assertTrue(self.check(100, 0))

    def test_destination_limits_are_5x_the_thresholds_on_standard(self):
        idx = {"accounts": {"p": account(10, 0), "a": account(24, 49), "b": account(25, 0), "c": account(0, 50)}}
        cfg = {"order": ["p", "a", "b", "c"], "premiumAccount": "p"}
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache"):
            name, skipped = acct._next_eligible_rotation_account(
                cfg, idx, 5, 10)
        self.assertEqual(name, "a")
        self.assertEqual(skipped, ["b (already at threshold: 5-hour 25% ≥ 25%)", "c (already at threshold: 7-day 50% ≥ 50%)"])

    def test_destination_is_judged_by_its_premium_equivalent_usage(self):
        idx = {"accounts": {"sales": account(20, 5), "a": account(74, 10), "b": account(10, 75), "c": account(0, 80)}}
        cfg = {"order": ["sales", "a", "b", "c"], "premiumAccount": "sales"}
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache"):
            name, skipped = acct._next_eligible_rotation_account(cfg, idx, 15, 15)
        self.assertEqual(name, "a")  # 74% Standard 5-hour ≈ 14.8% Premium: still below 15%
        self.assertEqual(skipped, ["b (already at threshold: 7-day 75% ≥ 75%)", "c (already at threshold: 7-day 80% ≥ 75%)"])

    def test_daily_threshold_triggers_rotation(self):
        usage = account(75, 0)["usage"]["data"]
        self.assertTrue(acct._usage_reached_threshold(usage, 75))

    def test_used_up_weekly_window_still_triggers_rotation(self):
        usage = account(10, 100)["usage"]["data"]
        self.assertTrue(acct._usage_reached_threshold(usage, 75))

    def test_destination_weekly_usage_ranks_but_does_not_exclude(self):
        idx = {"accounts": {"sales": account(80, 0), "xtts": account(0, 100), "info": account(6, 100)}}
        name, skipped = self.pick(idx, {"order": ["info", "sales", "xtts"], "premiumAccount": "sales"})
        self.assertEqual(name, "xtts")  # both at 100% 7-day: lower 5-hour wins
        self.assertEqual(skipped, [])

    def test_picks_lowest_weekly_usage_not_next_in_order(self):
        idx = {"accounts": {"sales": account(95, 84), "contact": account(0, 27), "careers": account(0, 100),
                            "hieunx": account(0, 0), "truongnv": account(42, 93)}}
        cfg = {"order": ["sales", "contact", "careers", "hieunx", "truongnv"], "premiumAccount": "sales"}
        self.assertEqual(self.pick(idx, cfg)[0], "hieunx")

    def test_equal_weekly_usage_prefers_lower_five_hour(self):
        idx = {"accounts": {"sales": account(95, 50), "contact": account(40, 10), "careers": account(5, 10)}}
        cfg = {"order": ["sales", "contact", "careers"], "premiumAccount": "sales"}
        self.assertEqual(self.pick(idx, cfg)[0], "careers")

    def test_full_tie_follows_order_after_current_premium(self):
        idx = {"accounts": {"a": account(0, 0), "b": account(0, 0), "c": account(95, 50)}}
        cfg = {"order": ["a", "b", "c"], "premiumAccount": "c"}
        self.assertEqual(self.pick(idx, cfg)[0], "a")

    def test_account_used_up_as_premium_is_not_skipped(self):
        idx = {"accounts": {"sales": account(80, 0), "xtts": account(0, 90), "info": account(6, 100)}}
        cfg = {"order": ["info", "sales", "xtts"], "premiumAccount": "sales",
               "usedUpUntil": {"xtts": time.time() + 3600}}
        name, skipped = self.pick(idx, cfg)
        self.assertEqual((name, skipped), ("xtts", []))
        self.assertNotIn("usedUpUntil", cfg)  # left by earlier versions; dropped

    def test_tie_goes_to_the_earliest_weekly_reset(self):
        def acc(reset):
            a = account(0, 100)
            a["usage"]["data"]["seven_day"]["resets_at"] = reset
            return a
        idx = {"accounts": {"p": account(80, 0), "a": acc("2026-10-24T07:00:00+00:00"),
                            "b": acc("2026-10-22T03:59:00+00:00"), "c": account(0, 100)}}
        cfg = {"order": ["p", "a", "b", "c"], "premiumAccount": "p"}
        self.assertEqual(self.pick(idx, cfg)[0], "b")


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
             patch.object(acct, "current_name", return_value="a"), \
             patch.object(acct, "sync_current", return_value="a"), \
             patch.object(acct, "read_live_profile", return_value={}), \
             patch.object(acct, "refresh_seat"), \
             patch.object(acct, "refresh_usage", side_effect=lambda m, c, force=False, refresh_name=None: calls.append(refresh_name) or "expired"), \
             patch.object(acct.sys, "stdout", io.StringIO()):
            acct.cmd_list(argparse.Namespace(refresh=False))
        self.assertEqual(calls, [None, "b"])

    def test_list_marks_the_live_account_even_when_its_token_sync_fails(self):
        idx = {"accounts": {"a": {"profile": {"emailAddress": "a@x.com"}}}}
        self.store["a"] = {"claudeAiOauth": {"accessToken": "live", "refreshToken": "ra", "expiresAt": 1}}
        with patch.object(acct, "load_index", return_value=idx), \
             patch.object(acct, "save_index"), \
             patch.object(acct, "current_name", return_value="a"), \
             patch.object(acct, "sync_current", side_effect=acct.AcctError("keychain locked")), \
             patch.object(acct, "read_live_profile", return_value={"emailAddress": "a@x.com"}), \
             patch.object(acct, "refresh_seat"), \
             patch.object(acct, "refresh_usage", return_value="cache"), \
             patch.object(acct.sys, "stdout", io.StringIO()) as out:
            acct.cmd_list(argparse.Namespace(refresh=False))
        self.assertIn("▶ a", out.getvalue())
        self.assertNotIn("not saved", out.getvalue())
        self.assertIn("keychain locked", out.getvalue())

    def test_list_names_the_saved_account_with_the_same_email_but_another_id(self):
        idx = {"accounts": {"a": {"profile": {"emailAddress": "a@x.com", "accountUuid": "old"}}}}
        self.store["a"] = {"claudeAiOauth": {"accessToken": "t", "refreshToken": "ra", "expiresAt": 1}}
        with patch.object(acct, "load_index", return_value=idx), \
             patch.object(acct, "save_index"), \
             patch.object(acct, "read_live_profile", return_value={"emailAddress": "A@x.com", "accountUuid": "new"}), \
             patch.object(acct, "refresh_seat"), \
             patch.object(acct, "refresh_usage", return_value="cache"), \
             patch.object(acct.sys, "stdout", io.StringIO()) as out:
            acct.cmd_list(argparse.Namespace(refresh=False))
        self.assertIn("matches saved account a by email", out.getvalue())


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


class FollowPremiumSeatTests(unittest.TestCase):
    """The seat can move outside this machine; the session follows it instead of rotating a Standard account."""

    def run_check(self, cur, tiers, cfg=None, statuses=None, manual=False):
        """Run one check with `cur` live and `tiers` {name: seat tier}. Return (cfg, switched_to, rotated)."""
        self.cfg = cfg or {"enabled": True, "order": ["info", "sales", "hieunx"], "premiumAccount": "sales"}
        idx = {"accounts": {n: account(0, 0) for n in ("info", "sales", "hieunx", "personal")}}
        statuses = statuses or {}

        def seat(meta, creds, force=False, refresh_name=None, ttl=None):
            name = creds["name"]
            if name in tiers:
                meta["seat"] = {"fetchedAt": time.time(), "tier": tiers[name]}
            return statuses.get(name, "ok")

        switched = []
        with patch.object(acct, "load_rotation", return_value=self.cfg), \
             patch.object(acct, "load_index", return_value=idx), \
             patch.object(acct, "save_index"), patch.object(acct, "save_rotation"), \
             patch.object(acct, "sync_current", return_value=cur), \
             patch.object(acct, "read_stored_creds", side_effect=lambda n: {"name": n}), \
             patch.object(acct, "refresh_seat", side_effect=seat), \
             patch.object(acct, "refresh_usage", return_value="cache"), \
             patch.object(acct, "_set_premium_seat") as rotate, \
             patch.object(acct, "cmd_switch", side_effect=lambda a: switched.append(a.name)), \
             patch.object(acct.sys, "stdout", io.StringIO()):
            acct._cmd_auto_check_locked(argparse.Namespace(force=False, manual=manual))
        return self.cfg, switched, rotate.called

    def test_follows_seat_moved_by_hand(self):
        cfg, switched, rotated = self.run_check("sales", {"sales": "team_standard", "hieunx": "team_tier_1"})
        self.assertEqual(cfg["premiumAccount"], "hieunx")
        self.assertEqual(switched, ["hieunx"])
        self.assertFalse(rotated)

    def test_usage_read_as_standard_is_dropped_when_following(self):
        idx = {"accounts": {"sales": account(0, 0), "hieunx": account(0, 17)}}
        tiers = {"sales": "team_standard", "hieunx": "team_tier_1"}
        def seat(meta, creds, force=False, refresh_name=None, ttl=None):
            meta["seat"] = {"fetchedAt": time.time(), "tier": tiers[creds["name"]]}
            return "ok"
        cfg = {"order": ["sales", "hieunx"], "premiumAccount": "sales"}
        with patch.object(acct, "read_stored_creds", side_effect=lambda n: {"name": n}), \
             patch.object(acct, "refresh_seat", side_effect=seat), \
             patch.object(acct, "save_index"), patch.object(acct, "save_rotation"), \
             patch.object(acct, "cmd_switch"), patch.object(acct.sys, "stdout", io.StringIO()):
            acct._follow_premium_seat(cfg, idx, "sales", argparse.Namespace(force=False, manual=False))
        self.assertNotIn("usage", idx["accounts"]["hieunx"])  # its 17% 7-day was a Standard reading
        self.assertEqual(idx["accounts"]["hieunx"]["seat"]["tier"], "team_tier_1")

    def test_pool_account_that_is_not_premium_switches_to_the_holder(self):
        cfg, switched, _ = self.run_check("info", {"sales": "team_tier_1"})
        self.assertEqual(cfg["premiumAccount"], "sales")
        self.assertEqual(switched, ["sales"])

    def test_session_already_on_the_holder_stays(self):
        _, switched, rotated = self.run_check("sales", {"sales": "team_tier_1"})
        self.assertEqual(switched, [])
        self.assertFalse(rotated)  # usage is 0%: no rotation either

    def test_login_outside_the_pool_is_left_alone(self):
        _, switched, _ = self.run_check("personal", {"sales": "team_standard", "hieunx": "team_tier_1"})
        self.assertEqual(switched, [])

    def test_unreadable_seat_keeps_the_configured_premium(self):
        cfg, switched, _ = self.run_check("sales", {}, statuses={"sales": "error: timeout"})
        self.assertEqual(cfg["premiumAccount"], "sales")
        self.assertEqual(switched, [])

    def test_no_saved_holder_changes_nothing_and_does_not_rotate(self):
        cfg = {"enabled": True, "order": ["info", "sales", "hieunx"], "premiumAccount": "sales"}
        cfg, switched, rotated = self.run_check(
            "sales", {"sales": "team_standard", "info": "team_standard", "hieunx": "team_standard"}, cfg=cfg)
        self.assertEqual(cfg["premiumAccount"], "sales")
        self.assertEqual(switched, [])
        self.assertFalse(rotated)

    def test_right_after_own_rotation_never_follows_back_to_the_account_it_left(self):
        cfg = {"enabled": True, "order": ["info", "sales", "hieunx"], "premiumAccount": "hieunx",
               "rotatedAt": time.time(), "rotatedFrom": "sales"}
        cfg, switched, rotated = self.run_check("hieunx", {"hieunx": "team_standard", "sales": "team_tier_1"}, cfg=cfg)
        self.assertEqual(cfg["premiumAccount"], "hieunx")
        self.assertEqual(switched, [])
        self.assertFalse(rotated)

    def test_right_after_own_rotation_still_follows_a_move_to_another_account(self):
        cfg = {"enabled": True, "order": ["info", "sales", "hieunx"], "premiumAccount": "hieunx",
               "rotatedAt": time.time(), "rotatedFrom": "sales"}
        cfg, switched, _ = self.run_check(
            "hieunx", {"hieunx": "team_standard", "sales": "team_tier_1", "info": "team_tier_1"}, cfg=cfg)
        self.assertEqual(cfg["premiumAccount"], "info")
        self.assertEqual(switched, ["info"])

    def test_follow_back_is_allowed_once_the_grace_period_is_over(self):
        cfg = {"enabled": True, "order": ["info", "sales", "hieunx"], "premiumAccount": "hieunx",
               "rotatedAt": time.time() - acct.FOLLOW_GRACE - 1, "rotatedFrom": "sales"}
        cfg, switched, _ = self.run_check("hieunx", {"hieunx": "team_standard", "sales": "team_tier_1"}, cfg=cfg)
        self.assertEqual(switched, ["sales"])

    def rotate_check(self, tiers_after_wait):
        """Premium sales is over the threshold; return (cfg, switched, rotated) when the fresh
        seat reading taken right before the transfer shows `tiers_after_wait`."""
        cfg = {"enabled": True, "order": ["info", "sales", "hieunx"], "premiumAccount": "sales"}
        idx = {"accounts": {"info": account(0, 0), "sales": account(85, 5), "hieunx": account(0, 0)}}
        reads = {"n": 0}

        def seat(meta, creds, force=False, refresh_name=None, ttl=None):
            name = creds["name"]
            reads["n"] += 1
            tiers = {"sales": "team_tier_1"} if not force or name != "sales" else tiers_after_wait
            if name in tiers:
                meta["seat"] = {"fetchedAt": time.time(), "tier": tiers[name]}
            elif force:
                meta["seat"] = {"fetchedAt": time.time(), "tier": tiers_after_wait.get(name, "team_standard")}
            return "ok"

        switched = []
        with patch.object(acct, "load_rotation", return_value=cfg), \
             patch.object(acct, "load_index", return_value=idx), \
             patch.object(acct, "save_index"), patch.object(acct, "save_rotation"), \
             patch.object(acct, "sync_current", return_value="sales"), \
             patch.object(acct, "read_stored_creds", side_effect=lambda n: {"name": n}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_seat", side_effect=seat), \
             patch.object(acct, "refresh_usage", return_value="cache"), \
             patch.object(acct.random, "uniform", return_value=0), \
             patch.object(acct, "_set_premium_seat") as rotate, \
             patch.object(acct, "cmd_switch", side_effect=lambda a: switched.append(a.name)), \
             patch.object(acct.sys, "stdout", io.StringIO()):
            acct._cmd_auto_check_locked(argparse.Namespace(force=False, manual=False))
        return cfg, switched, rotate.called

    def test_rotates_when_the_fresh_reading_still_shows_premium(self):
        cfg, switched, rotated = self.rotate_check({"sales": "team_tier_1"})
        self.assertTrue(rotated)
        self.assertEqual(switched, ["hieunx"])  # tie with info: the order after sales decides
        self.assertEqual(cfg["premiumAccount"], "hieunx")

    def test_seat_moved_by_another_machine_meanwhile_is_followed_not_moved_again(self):
        cfg, switched, rotated = self.rotate_check({"sales": "team_standard", "info": "team_tier_1"})
        self.assertFalse(rotated)
        self.assertEqual(switched, ["info"])  # another machine chose info; this one follows
        self.assertEqual(cfg["premiumAccount"], "info")

    def test_seat_refresh_renews_the_token_of_an_idle_account_only(self):
        expired = {"claudeAiOauth": {"accessToken": "old", "expiresAt": 1, "refreshToken": "r"}}
        with patch.object(acct, "refresh_stored_token", return_value="revoked") as refresh:
            self.assertEqual(acct.refresh_seat({}, dict(expired)), "expired")
            refresh.assert_not_called()
            self.assertEqual(acct.refresh_seat({}, dict(expired), refresh_name="b"), "revoked")
            refresh.assert_called_once()


class RotationPreviewTests(unittest.TestCase):
    def preview(self, accounts):
        cfg = {"enabled": True, "order": ["sales", "a", "b"], "premiumAccount": "sales",
               "dailyThreshold": 5, "weeklyThreshold": 10}
        idx = {"accounts": accounts}
        with patch.object(acct, "load_rotation", return_value=cfg), \
             patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache") as usage, \
             patch.object(acct.sys, "stdout", io.StringIO()) as out:
            acct._print_rotation_preview(idx, live="a")
        return out.getvalue(), usage

    def test_names_the_next_premium(self):
        out, _ = self.preview({"sales": account(70, 46), "a": account(0, 10), "b": account(0, 5)})
        self.assertIn("threshold reached", out)
        self.assertIn("Next Premium: **b**", out)

    def test_explains_why_no_account_is_eligible(self):
        out, _ = self.preview({"sales": account(70, 46), "a": account(100, 10), "b": account(0, 53)})
        self.assertIn("Next Premium: none eligible; sales keeps the seat", out)
        self.assertIn("- a (already at threshold: 5-hour 100% ≥ 25%)", out)
        self.assertIn("- b (already at threshold: 7-day 53% ≥ 50%)", out)

    def test_never_refreshes_the_token_of_the_live_login(self):
        _, usage = self.preview({"sales": account(70, 46), "a": account(0, 10), "b": account(0, 5)})
        refresh_names = {c.kwargs.get("refresh_name") for c in usage.call_args_list}
        self.assertNotIn("a", refresh_names)


class RotationLogTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.env = patch.dict(os.environ, {"CLAUDE_ACCT_HOME": self.dir})
        self.env.start()
        self.machine = patch.object(acct, "machine_name", return_value="Tester")
        self.machine.start()

    def tearDown(self):
        self.machine.stop()
        self.env.stop()
        shutil.rmtree(self.dir)

    def lines(self, name="rotation.log"):
        path = os.path.join(self.dir, name)
        if not os.path.exists(path):
            return []
        with open(path, encoding="utf-8") as f:
            return [json.loads(l) for l in f]

    def test_writes_one_json_line_with_machine_version_and_fields(self):
        acct.log_event("move_ok", "12s", live="a@x.vn", target="b@x.vn", p5h=6.0)
        (rec,) = self.lines()
        self.assertEqual((rec["event"], rec["machine"], rec["live"], rec["target"], rec["p5h"], rec["message"]),
                         ("move_ok", "Tester", "a@x.vn", "b@x.vn", 6.0, "12s"))
        self.assertIn("version", rec)
        self.assertEqual(self.lines("log-outbox.jsonl"), [])  # no URL: local only

    def test_throttled_events_are_logged_once_per_period(self):
        for _ in range(3):
            acct.log_event("no_destination", "x", every=1800)
        acct.log_event("heartbeat", "y", every=600)
        self.assertEqual([r["event"] for r in self.lines()], ["no_destination", "heartbeat"])

    def test_with_url_events_go_to_the_outbox_and_a_flush_starts(self):
        with patch.dict(os.environ, {"CLAUDE_ACCT_LOG_URL": "https://example.invalid/exec"}), \
             patch.object(acct, "_spawn_log_flush") as spawn:
            acct.log_event("follow", "seat moved")
        self.assertEqual(len(self.lines("log-outbox.jsonl")), 1)
        spawn.assert_called_once()

    def flush(self, reply):
        class Resp(io.BytesIO):
            def __enter__(self): return self
            def __exit__(self, *a): return False
        sent = []
        def fake_urlopen(req, timeout):
            sent.append(json.loads(req.data.decode("utf-8")))
            if isinstance(reply, Exception):
                raise reply
            return Resp(reply)
        with patch.dict(os.environ, {"CLAUDE_ACCT_LOG_URL": "https://example.invalid/exec"}), \
             patch.object(acct, "_spawn_log_flush"), patch.object(acct, "urlopen", side_effect=fake_urlopen):
            acct.log_event("a", "1")
            acct.log_event("b", "2")
            acct.cmd_log_flush(None)
        return sent

    def test_flush_sends_the_outbox_in_one_batch_and_clears_it(self):
        sent = self.flush(b"ok")
        self.assertEqual([e["event"] for e in sent[0]["events"]], ["a", "b"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "log-outbox.jsonl.sending")))

    def test_failed_upload_keeps_the_events_for_the_next_flush(self):
        self.flush(OSError("offline"))
        self.assertEqual(len(self.lines("log-outbox.jsonl.sending")), 2)

    def test_logging_never_raises(self):
        with patch.object(acct, "store_dir", side_effect=OSError("read-only")):
            acct.log_event("error", "x")

    def test_failed_seat_move_is_logged_once(self):
        cfg = {"enabled": True, "order": ["info", "sales"], "premiumAccount": "sales"}
        idx = {"accounts": {"info": account(0, 0), "sales": account(85, 5)}}
        def seat(meta, creds, force=False, refresh_name=None, ttl=None):
            meta["seat"] = {"fetchedAt": time.time(), "tier": "team_tier_1" if creds["name"] == "sales" else "team_standard"}
            return "ok"
        with patch.object(acct, "load_rotation", return_value=cfg), \
             patch.object(acct, "load_index", return_value=idx), \
             patch.object(acct, "save_index"), patch.object(acct, "save_rotation"), \
             patch.object(acct, "sync_current", return_value="sales"), \
             patch.object(acct, "current_name", return_value="sales"), \
             patch.object(acct, "read_stored_creds", side_effect=lambda n: {"name": n}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_seat", side_effect=seat), \
             patch.object(acct, "refresh_usage", return_value="cache"), \
             patch.object(acct.random, "uniform", return_value=0), \
             patch.object(acct, "_set_premium_seat", side_effect=acct.AcctError("not signed in")):
            with self.assertRaises(acct.AcctError):
                acct.cmd_auto_check(argparse.Namespace(force=False, manual=False))
        events = [r["event"] for r in self.lines()]
        self.assertEqual(events, ["heartbeat", "move_start", "move_fail"])
        self.assertIn("not signed in", self.lines()[-1]["message"])


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
