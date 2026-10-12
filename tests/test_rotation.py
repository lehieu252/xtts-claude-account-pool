import importlib.util
import io
import os
import sys
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
                self.idx, 90, 90)
        self.assertEqual(name, "xtts")
        self.assertEqual(skipped, ["sales (already at threshold)"])

    def test_returns_no_candidate_when_all_accounts_are_over_threshold(self):
        self.idx["accounts"]["xtts"] = account(20, 95)
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="cache"):
            name, skipped = acct._next_eligible_rotation_account(
                {"order": ["info", "sales", "xtts"], "premiumAccount": "info"},
                self.idx, 90, 90)
        self.assertIsNone(name)
        self.assertEqual(len(skipped), 2)

    def test_skips_destination_when_usage_cannot_be_confirmed(self):
        with patch.object(acct, "read_stored_creds", return_value={"claudeAiOauth": {"refreshToken": "r"}}), \
             patch.object(acct, "token_state", return_value="ok"), \
             patch.object(acct, "refresh_usage", return_value="error: timeout"):
            name, skipped = acct._next_eligible_rotation_account(
                {"order": ["info", "sales", "xtts"], "premiumAccount": "info"},
                self.idx, 90, 90)
        self.assertIsNone(name)
        self.assertEqual(skipped, ["sales (usage unavailable)", "xtts (usage unavailable)"])


class StopFailureRecoveryTests(unittest.TestCase):
    def test_rate_limit_triggers_forced_usage_check(self):
        with patch.object(acct, "load_rotation", return_value={"enabled": True}), \
             patch.object(acct, "cmd_auto_check") as check, \
             patch.object(acct.sys, "stdin", io.StringIO('{"error":"rate_limit"}')):
            acct.cmd_auto_recover(None)
        check.assert_called_once()
        self.assertTrue(check.call_args.args[0].force)

    def test_other_errors_do_not_trigger_recovery(self):
        with patch.object(acct, "cmd_auto_check") as check, \
             patch.object(acct.sys, "stdin", io.StringIO('{"error":"overloaded"}')):
            acct.cmd_auto_recover(None)
        check.assert_not_called()


if __name__ == "__main__":
    unittest.main()
