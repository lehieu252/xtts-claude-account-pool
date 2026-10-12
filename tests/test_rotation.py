import importlib.util
import io
import os
import shutil
import sys
import tempfile
import threading
import time
import unittest
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
        self.assertFalse(acct._acquire_rotation_lock(self.lock))

    def test_waits_for_a_running_check_to_release_the_lock(self):
        open(self.lock, "w").close()
        threading.Timer(0.5, os.unlink, [self.lock]).start()
        self.assertTrue(acct._acquire_rotation_lock(self.lock, wait=5))

    def test_stale_lock_is_replaced(self):
        open(self.lock, "w").close()
        old = time.time() - 301
        os.utime(self.lock, (old, old))
        self.assertTrue(acct._acquire_rotation_lock(self.lock))


class SeatTests(unittest.TestCase):
    def creds(self):
        return {"claudeAiOauth": {"accessToken": "t", "expiresAt": (time.time() + 3600) * 1000}}

    def test_labels_known_team_seat_tiers(self):
        self.assertEqual(acct.fmt_seat({"seat": {"tier": "team_tier_1"}}), "Premium")
        self.assertEqual(acct.fmt_seat({"seat": {"tier": "team_standard"}}), "Standard")
        self.assertEqual(acct.fmt_seat({"seat": {"tier": None}}), "No seat")
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
