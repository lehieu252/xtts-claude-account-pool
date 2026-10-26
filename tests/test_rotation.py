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


# Keep the rotation log, the pool settings and their state out of the real ~/.claude-acct while tests run.
os.environ["CLAUDE_ACCT_HOME"] = tempfile.mkdtemp()
os.environ.pop("CLAUDE_ACCT_POOL_URL", None)

INFO, CAREERS, SUPPORT = acct.BACKUP_EMAILS
XTTS, SALES, CONTACT, HIEUNX, TRUONGNV, THANGLT, KIENLT, XTG1, XTG2 = acct.ROTATION_EMAILS
CREDS = {"claudeAiOauth": {"refreshToken": "r"}}


def account(five_hour, seven_day, email="member@example.com", tier="team_standard", reset=None):
    seven = {"utilization": seven_day}
    if reset:
        seven["resets_at"] = reset
    return {
        "profile": {"emailAddress": email},
        "usage": {"fetchedAt": time.time(), "data": {"five_hour": {"utilization": five_hour}, "seven_day": seven}},
        "seat": {"fetchedAt": time.time(), "tier": tier},
    }


def pool_index(**usage):
    """Every pool account saved under its email; usage={email: (5h, 7d)} and premium=<email>."""
    premium = usage.pop("premium", None)
    idx = {"accounts": {}}
    for email in acct.POOL_EMAILS:
        five, seven = usage.get(email, (0, 0))
        idx["accounts"][email] = account(five, seven, email, acct.PREMIUM_TIER if email == premium else "team_standard")
    return idx


def stored(**extra):
    """Patches for reading saved logins without a Keychain or the network."""
    return [patch.object(acct, "read_stored_creds", return_value=CREDS),
            patch.object(acct, "token_state", return_value="ok"),
            patch.object(acct, "refresh_usage", return_value="cache"),
            patch.object(acct, "refresh_seat", return_value="cache")]


class Patches:
    """Start several patches; named ones are returned by name (`with Patches(a, out=b) as m: m["out"]`)."""

    def __init__(self, *ps, **named):
        self.ps = list(ps) + list(named.values())
        self.names = list(named)

    def __enter__(self):
        started = [p.start() for p in self.ps]
        return dict(zip(self.names, started[len(self.ps) - len(self.names):]))

    def __exit__(self, *a):
        for p in reversed(self.ps):
            p.stop()


class CandidateTests(unittest.TestCase):
    """The next Premium is the rotation account that was Premium least recently; usage is never read."""

    def pick(self, idx, premium, last_used=None):
        with Patches(*stored(), usage=patch.object(acct, "refresh_usage", return_value="cache")) as p:
            got = acct._next_premium(idx, premium, last_used)
            p["usage"].assert_not_called()  # accounts without a seat have no usage to read
        return got

    def test_never_premium_first_then_the_oldest_then_the_list_order(self):
        idx = pool_index(premium=XTTS)
        used = {SALES: 300, CONTACT: 100, HIEUNX: 200, TRUONGNV: 400, THANGLT: 500, KIENLT: 600, XTG1: 800, XTG2: 900}
        self.assertEqual(self.pick(idx, XTTS, used)[0], CONTACT)
        used[CONTACT] = 700
        self.assertEqual(self.pick(idx, XTTS, used)[0], HIEUNX)
        self.assertEqual(self.pick(idx, XTTS, {})[0], SALES)  # all equal: the list order, top first
        self.assertEqual(self.pick(idx, SALES, {SALES: 1, XTTS: 2})[0], CONTACT)  # never Premium beats any date

    def test_usage_does_not_matter(self):
        idx = pool_index(**{SALES: (100, 100)}, premium=XTTS)
        self.assertEqual(self.pick(idx, XTTS, {})[0], SALES)

    def test_backup_accounts_and_premium_are_never_candidates(self):
        idx = pool_index(premium=XTTS)
        name, _ = self.pick(idx, XTTS, {e: 1000 for e in acct.ROTATION_EMAILS})
        self.assertNotIn(name, (INFO, CAREERS, XTTS))

    def test_unsaved_and_expired_accounts_are_skipped(self):
        idx = pool_index(premium=XTTS)
        del idx["accounts"][SALES]
        with Patches(patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", side_effect=lambda m, c: "relogin" if m["profile"]["emailAddress"] == CONTACT else "ok")):
            name, skipped = acct._next_premium(idx, XTTS, {})
        self.assertEqual(name, HIEUNX)
        self.assertIn("%s (not logged in on this machine)" % SALES, skipped)
        self.assertIn("%s (login expired/revoked)" % CONTACT, skipped)

    def test_dashboard_rows_read_usage_only_for_the_two_accounts_with_a_seat(self):
        idx = pool_index(**{XTTS: (40, 10), INFO: (5, 6)}, premium=XTTS)
        del idx["accounts"][TRUONGNV]
        state = {"premium": XTTS, "backup": INFO, "last_used": {XTTS: 900, SALES: 300, CONTACT: 100}}
        with Patches(*stored(), usage=patch.object(acct, "refresh_usage", return_value="cache")) as p:
            rows = acct._account_rows(idx, None, state)
        self.assertEqual({c.args[0]["profile"]["emailAddress"] for c in p["usage"].call_args_list}, {XTTS, INFO})
        self.assertEqual([r["email"] for r in rows],
                         [XTTS, HIEUNX, TRUONGNV, THANGLT, KIENLT, XTG1, XTG2, CONTACT, SALES, INFO, CAREERS, SUPPORT])
        self.assertEqual([r["rank"] for r in rows[1:9]], [1, 2, 3, 4, 5, 6, 7, 8])
        self.assertEqual((rows[0]["five_hour"], rows[9]["five_hour"]), (40, 5))
        self.assertEqual((rows[1]["seat"], rows[10]["seat"], rows[11]["seat"]), ("No seat", "No seat", "No seat"))
        self.assertNotIn("five_hour", rows[1])
        self.assertEqual((rows[7]["last_used"], rows[1]["last_used"]), (100, None))
        self.assertEqual(rows[2]["status"], "not logged in on machine 2")
        self.assertEqual(self.pick(idx, XTTS, state["last_used"])[0], HIEUNX)  # rank 1 is the next Premium


class ThresholdTests(unittest.TestCase):
    def reached(self, five, seven):
        return acct._usage_reached_threshold(account(five, seven)["usage"]["data"])

    def test_default_rule(self):
        # 5-hour >= 80% or 7-day >= 90% (or any window used up).
        self.assertFalse(self.reached(79, 89))
        self.assertTrue(self.reached(80, 0))
        self.assertTrue(self.reached(0, 90))
        self.assertTrue(self.reached(100, 0))

    def test_pool_is_fixed(self):
        self.assertEqual(acct.BACKUP_EMAILS, ("info@xtgroup.vn", "careers@xtgroup.vn", "support@xtgroup.vn"))
        self.assertFalse(set(acct.BACKUP_EMAILS) & set(acct.ROTATION_EMAILS))
        self.assertNotIn("hieule252uet@gmail.com", acct.POOL_EMAILS)
        self.assertEqual(len(acct.ROTATION_EMAILS), 9)
        self.assertEqual(acct.ROTATION_EMAILS[-2:], ("xtg1@xtgroup.vn", "xtg2@xtgroup.vn"))
        self.assertIn("kienlt@xtgroup.vn", acct.ROTATION_EMAILS)


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

    def test_a_429_is_not_asked_again_before_retry_after(self):
        meta = {}
        creds = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": (time.time() + 3600) * 1000}}
        err = urllib.error.HTTPError(acct.USAGE_URL, 429, "Too Many Requests", {"Retry-After": "1172"}, io.BytesIO(b""))
        with patch.object(acct, "fetch_usage", side_effect=err) as fetch:
            self.assertTrue(acct.refresh_usage(meta, creds, force=True).startswith("rate limited until"))
            self.assertAlmostEqual(meta["usage_blocked_until"], time.time() + 1172, delta=5)
            self.assertTrue(acct.refresh_usage(meta, creds, force=True).startswith("rate limited until"))
            self.assertEqual(fetch.call_count, 1)  # the second check did not ask
        meta["usage_blocked_until"] = time.time() - 1
        with patch.object(acct, "fetch_usage", return_value={"five_hour": {"utilization": 12}}):
            self.assertEqual(acct.refresh_usage(meta, creds, force=True), "ok")
        self.assertNotIn("usage_blocked_until", meta)

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



class WorkerTests(unittest.TestCase):
    """Hooks on every working machine switch account; they never move the seat."""

    def run_check(self, cur, state=None, error=None, idx=None, manual=False):
        idx = idx or pool_index(premium=SALES)
        switched, reports = [], []

        def fake_state(account, force=False):
            reports.append((account, force))
            if error:
                raise acct.AcctError(error)
            return state

        with Patches(*stored(),
                     patch.object(acct, "load_index", return_value=idx), patch.object(acct, "save_index"),
                     patch.object(acct, "sync_current", return_value=cur),
                     patch.object(acct, "pool_state", side_effect=fake_state),
                     patch.object(acct, "cmd_switch", side_effect=lambda a: switched.append(a.name)),
                     patch.object(acct.sys, "stdout", io.StringIO()),
                     move=patch.object(acct, "_set_seats")) as _p:
            move = _p["move"]
            acct._worker_check(argparse.Namespace(force=False, manual=manual))
        move.assert_not_called()
        return switched, reports

    def test_stable_switches_to_the_premium_account(self):
        switched, reports = self.run_check(XTTS, {"phase": "STABLE", "premium": SALES})
        self.assertEqual(switched, [SALES])
        self.assertEqual(reports[-1], (SALES, True))  # reports the new account at once

    def test_preparing_switches_to_the_backup_account_and_reports_it(self):
        switched, reports = self.run_check(SALES, {"phase": "PREPARING", "premium": SALES, "target": HIEUNX})
        self.assertEqual(switched, [INFO])
        self.assertEqual(reports[-1], (INFO, True))

    def test_moving_keeps_the_backup_account(self):
        switched, _ = self.run_check(INFO, {"phase": "MOVING", "premium": SALES, "target": HIEUNX})
        self.assertEqual(switched, [])

    def test_after_the_move_the_backup_account_switches_to_the_new_premium(self):
        switched, _ = self.run_check(INFO, {"phase": "STABLE", "premium": HIEUNX})
        self.assertEqual(switched, [HIEUNX])

    def test_already_on_premium_stays(self):
        switched, _ = self.run_check(SALES, {"phase": "STABLE", "premium": SALES})
        self.assertEqual(switched, [])

    def test_login_outside_the_pool_is_left_alone(self):
        idx = pool_index(premium=SALES)
        idx["accounts"]["personal"] = account(0, 0, "me@gmail.com")
        switched, reports = self.run_check("personal", {"phase": "PREPARING", "premium": SALES, "target": XTTS}, idx=idx)
        self.assertEqual((switched, reports), ([], []))

    def test_server_down_follows_the_seat_on_claude_ai(self):
        switched, _ = self.run_check(XTTS, error="pool server unreachable: timed out")
        self.assertEqual(switched, [SALES])

    def test_server_down_and_no_confirmed_holder_changes_nothing(self):
        switched, _ = self.run_check(XTTS, error="pool server unreachable", idx=pool_index())
        self.assertEqual(switched, [])

    def test_preparing_switches_to_the_backup_account_machine_2_picked(self):
        switched, reports = self.run_check(SALES, {"phase": "PREPARING", "premium": SALES, "target": HIEUNX, "backup": CAREERS})
        self.assertEqual(switched, [CAREERS])
        self.assertEqual(reports[-1], (CAREERS, True))

    def test_a_machine_left_on_the_former_backup_account_returns_to_premium(self):
        idx = pool_index(premium=SALES)
        idx["accounts"]["old"] = account(0, 0, "hieule252uet@gmail.com")
        switched, _ = self.run_check("old", {"phase": "STABLE", "premium": SALES}, idx=idx)
        self.assertEqual(switched, [SALES])

    def test_wanted_account_not_logged_in_here_is_reported_not_switched(self):
        idx = pool_index(premium=SALES)
        del idx["accounts"][HIEUNX]
        switched, _ = self.run_check(SALES, {"phase": "STABLE", "premium": HIEUNX}, idx=idx)
        self.assertEqual(switched, [])
        with self.assertRaises(acct.AcctError):
            self.run_check(SALES, {"phase": "STABLE", "premium": HIEUNX}, idx=idx, manual=True)


class FakePool:
    """The pool server as machine 2 sees it."""

    def __init__(self, phase="STABLE", premium=None, workers=()):
        self.state = {"phase": phase, "premium": premium, "target": None, "backup": INFO, "last_used": {}, "updated_at": 100.0}
        self.workers = list(workers)
        self.puts = []

    def __call__(self, method, path, body=None, mover=False, timeout=4):
        if path == "/api/machine":
            return dict(self.state)
        if path == "/api/state" and method == "PUT":
            self.puts.append((body["phase"], body["premium"], body.get("target")))
            self.state.update(phase=body["phase"], premium=body["premium"], target=body.get("target"), updated_at=200.0)
            if body.get("backup"):
                self.state["backup"] = body["backup"]
            if body["phase"] == "STABLE" and body["premium"] != self.state.get("last_premium"):
                self.state["last_used"] = dict(self.state["last_used"], **{body["premium"]: 1000.0 + len(self.puts)})
            self.state["last_premium"] = body["premium"] if body["phase"] == "STABLE" else self.state.get("last_premium")
            for w in self.workers:  # every working machine reaches the backup account after PREPARING
                if body["phase"] == "PREPARING":
                    w.update(account=self.state["backup"], at_info=True, last_seen=201.0)
            return dict(self.state)
        if path == "/api/machines":
            return [dict(w) for w in self.workers]
        if path == "/api/state":
            return dict(self.state)
        if path == "/api/accounts":
            self.accounts = body["accounts"]
            return "ok"
        raise AssertionError(path)


class MoverTests(unittest.TestCase):
    """The service on machine 2 moves the seat; it never switches account."""

    def setUp(self):
        acct.save_pool_settings({"mover_key": "mk", "machine_id": "m2"})
        for f in ("mover-state.json",):
            try:
                os.unlink(os.path.join(acct.store_dir(), f))
            except FileNotFoundError:
                pass

    def tearDown(self):
        acct.save_pool_settings({})

    def run_once(self, idx, pool, seat_after=None, move_error=None):
        """seat_after: tiers read after MOVING ({email: tier}), to simulate a seat moved by hand."""
        reads = {"moving": False}

        def tier(meta, creds, force=False, refresh_name=None, ttl=None):
            if force and seat_after and pool.state["phase"] in ("MOVING", "STABLE") and pool.puts:
                meta["seat"] = {"fetchedAt": time.time(), "tier": seat_after.get(meta["profile"]["emailAddress"], "team_standard")}
            return "ok"

        with Patches(patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", return_value="ok"),
                     patch.object(acct, "refresh_usage", return_value="cache"),
                     patch.object(acct, "refresh_seat", side_effect=tier),
                     patch.object(acct, "load_index", return_value=idx), patch.object(acct, "save_index"),
                     patch.object(acct, "current_name", return_value=None),
                     patch.object(acct, "pool_call", side_effect=pool),
                     patch.object(acct.time, "sleep"),
                     patch.object(acct.sys, "stdout", io.StringIO()),
                     patch.object(acct, "_spawn_log_flush"),  # no background upload racing the next test's pool.json
                     move=patch.object(acct, "_set_seats", side_effect=move_error),
                     switch=patch.object(acct, "cmd_switch")) as _p:
            move = _p["move"]
            switch = _p["switch"]
            acct._mover_once(argparse.Namespace(force=False, manual=False))
        switch.assert_not_called()
        return move

    def test_below_threshold_only_publishes_the_premium_account(self):
        pool = FakePool()
        move = self.run_once(pool_index(**{SALES: (50, 40)}, premium=SALES), pool)
        self.assertEqual(pool.puts, [("STABLE", SALES, None)])
        move.assert_not_called()

    def test_full_move_goes_through_preparing_moving_stable(self):
        pool = FakePool("STABLE", SALES, workers=[{"name": "Hiếu", "role": "worker", "active": True, "at_info": False, "last_seen": 150.0}])
        pool.state["last_used"] = {XTTS: 500, SALES: 900, HIEUNX: 600, TRUONGNV: 700, THANGLT: 800, KIENLT: 550}
        idx = pool_index(**{SALES: (85, 40)}, premium=SALES)
        move = self.run_once(idx, pool)
        self.assertEqual(pool.puts, [("PREPARING", SALES, CONTACT), ("MOVING", SALES, CONTACT), ("STABLE", CONTACT, None)])
        # one Premium seat: freed first, then given to the account that was Premium least recently (never)
        self.assertEqual(move.call_args.args[0], [(SALES, "No seat"), (CONTACT, "Premium")])
        self.assertIn(CONTACT, pool.state["last_used"])

    def test_the_backup_account_keeps_the_standard_seat_until_it_is_spent(self):
        pool = FakePool("STABLE", SALES)
        move = self.run_once(pool_index(**{SALES: (50, 40), INFO: (84, 50)}, premium=SALES), pool)
        self.assertEqual(pool.state["backup"], INFO)  # 84% < 85%: kept
        move.assert_not_called()
        for usage in ((85, 50), (10, 100)):  # 5-hour at the limit, or the week used up
            os.unlink(os.path.join(acct.store_dir(), "mover-state.json")) if os.path.exists(os.path.join(acct.store_dir(), "mover-state.json")) else None
            pool = FakePool("STABLE", SALES)
            # careers' own usage is not read (it has no seat): even a spent reading does not stop the change
            move = self.run_once(pool_index(**{SALES: (50, 40), INFO: usage, CAREERS: (100, 100)}, premium=SALES), pool)
            self.assertEqual(move.call_args.args[0], [(INFO, "No seat"), (CAREERS, "Standard")])
            self.assertEqual(pool.state["backup"], CAREERS)
            self.assertEqual(pool.puts[-1], ("STABLE", SALES, None))

    def test_the_standard_seat_goes_round_the_backup_accounts_in_order(self):
        for current, nxt in ((INFO, CAREERS), (CAREERS, SUPPORT), (SUPPORT, INFO)):
            if os.path.exists(os.path.join(acct.store_dir(), "mover-state.json")):
                os.unlink(os.path.join(acct.store_dir(), "mover-state.json"))
            pool = FakePool("STABLE", SALES)
            pool.state["backup"] = current
            move = self.run_once(pool_index(**{SALES: (50, 40), current: (90, 10)}, premium=SALES), pool)
            self.assertEqual(move.call_args.args[0], [(current, "No seat"), (nxt, "Standard")])
            self.assertEqual(pool.state["backup"], nxt)

    def test_a_standard_seat_moved_by_hand_is_followed(self):
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{SALES: (50, 40)}, premium=SALES)
        idx["accounts"][INFO]["seat"]["tier"] = None  # info lost its seat by hand
        move = self.run_once(idx, pool)
        self.assertEqual(pool.state["backup"], CAREERS)  # the first backup account holding Standard (all do in pool_index)
        idx = pool_index(**{SALES: (50, 40)}, premium=SALES)
        for e in (INFO, CAREERS):
            idx["accounts"][e]["seat"]["tier"] = None
        pool = FakePool("STABLE", SALES)
        move = self.run_once(idx, pool)
        self.assertEqual(pool.state["backup"], SUPPORT)
        self.assertEqual(pool.puts[-1], ("STABLE", SALES, None))
        move.assert_not_called()

    def test_the_backup_account_stays_when_it_still_holds_standard(self):
        pool = FakePool("STABLE", SALES)
        self.run_once(pool_index(**{SALES: (50, 40)}, premium=SALES), pool)
        self.assertEqual((pool.state["backup"], pool.puts), (INFO, []))

    def test_a_failed_backup_seat_change_keeps_the_backup_and_backs_off(self):
        pool = FakePool("PREPARING", SALES)
        pool.state["target"] = CONTACT
        idx = pool_index(**{SALES: (50, 40), INFO: (90, 50)}, premium=SALES)
        move = self.run_once(idx, pool, move_error=acct.AcctError("picker"))  # resets to STABLE first, then tries
        self.assertEqual(move.call_count, 1)
        self.assertEqual(pool.state["backup"], INFO)
        self.assertGreater(acct._mover_state()["backup_backoff_until"], time.time())
        move = self.run_once(idx, FakePool("STABLE", SALES))
        move.assert_not_called()  # backing off

    def test_a_move_waits_on_the_backup_account_it_picked_not_on_at_info(self):
        pool = FakePool("STABLE", SALES)
        pool.state["backup"] = CAREERS
        # an older version reports at_info on the former backup account: still waited for
        pool.workers = [{"name": "Cũ", "role": "worker", "active": True, "at_info": True,
                         "account": "hieule252uet@gmail.com", "last_seen": 300.0}]
        pool.__class__ = type("Old", (FakePool,), {})

        def stay(self_, method, path, body=None, mover=False, timeout=4):
            if path == "/api/state" and method == "PUT":
                self_.puts.append((body["phase"], body["premium"], body.get("target")))
                self_.state.update(phase=body["phase"], premium=body["premium"], updated_at=200.0)
                return dict(self_.state)
            return FakePool.__call__(self_, method, path, body, mover, timeout)

        pool.__class__.__call__ = stay
        with patch.object(acct, "PREPARE_TIMEOUT", 0):
            self.run_once(pool_index(**{SALES: (85, 40)}, premium=SALES), pool)
        log = [json.loads(l) for l in open(os.path.join(acct.store_dir(), "rotation.log"), encoding="utf-8")]
        self.assertIn("still not on %s" % CAREERS, [e for e in log if e["event"] == "prepare_timeout"][-1]["message"])

    def test_waits_for_working_machines_then_gives_up_after_the_timeout(self):
        pool = FakePool("STABLE", SALES)
        pool.workers = [{"name": "Thắng", "role": "worker", "active": True, "at_info": False, "last_seen": 150.0}]
        pool.__class__ = type("Stuck", (FakePool,), {})  # this worker never reports the backup account

        def no_update(self_, method, path, body=None, mover=False, timeout=4):
            if path == "/api/state" and method == "PUT":
                self_.puts.append((body["phase"], body["premium"], body.get("target")))
                self_.state.update(phase=body["phase"], premium=body["premium"], updated_at=200.0)
                return dict(self_.state)
            return FakePool.__call__(self_, method, path, body, mover, timeout)

        pool.__class__.__call__ = no_update
        with patch.object(acct, "PREPARE_TIMEOUT", 0):
            self.run_once(pool_index(**{SALES: (85, 40)}, premium=SALES), pool)
        self.assertEqual([p[0] for p in pool.puts], ["PREPARING", "MOVING", "STABLE"])
        events = [json.loads(l)["event"] for l in open(os.path.join(acct.store_dir(), "rotation.log"), encoding="utf-8")]
        self.assertIn("prepare_timeout", events)

    def test_failed_move_returns_everyone_to_the_old_premium_and_backs_off(self):
        pool = FakePool("STABLE", SALES)
        move = self.run_once(pool_index(**{SALES: (85, 40)}, premium=SALES), pool, move_error=acct.AcctError("not signed in"))
        self.assertEqual(pool.puts[-1], ("STABLE", SALES, None))
        self.assertGreater(acct._mover_state()["backoff_until"], time.time())
        move.reset_mock()
        pool.puts.clear()
        move = self.run_once(pool_index(**{SALES: (85, 40)}, premium=SALES), pool)
        self.assertEqual(pool.puts, [])
        move.assert_not_called()

    def test_seat_moved_by_hand_during_the_move_is_published_not_moved_again(self):
        pool = FakePool("STABLE", SALES)
        move = self.run_once(pool_index(**{SALES: (85, 40)}, premium=SALES), pool, seat_after={KIENLT: acct.PREMIUM_TIER})
        move.assert_not_called()
        self.assertEqual(pool.puts[-1], ("STABLE", KIENLT, None))

    def test_no_destination_does_not_start_a_move(self):
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{SALES: (85, 40)}, premium=SALES)
        for e in acct.ROTATION_EMAILS:
            if e != SALES:
                del idx["accounts"][e]
        self.run_once(idx, pool)
        self.assertEqual(pool.puts, [])

    def test_a_move_cut_short_is_reset_to_where_the_seat_is(self):
        pool = FakePool("MOVING", SALES)
        self.run_once(pool_index(**{CONTACT: (1, 1)}, premium=CONTACT), pool)
        self.assertEqual(pool.puts, [("STABLE", CONTACT, None)])

    def test_publishes_the_usage_of_every_pool_account(self):
        pool = FakePool()
        idx = pool_index(**{SALES: (50, 40)}, premium=SALES)
        del idx["accounts"][KIENLT]
        with patch.object(acct, "_mover_state", return_value={}):
            self.run_once(idx, pool)
        rows = {r["email"]: r for r in pool.accounts}
        self.assertEqual(len(rows), 12)
        self.assertEqual((rows[SALES]["seat"], rows[SALES]["five_hour"], rows[SALES]["seven_day"]), ("Premium", 50, 40))
        self.assertEqual((rows[INFO]["role"], rows[INFO]["seat"]), ("backup", "Standard"))
        self.assertEqual((rows[CAREERS]["seat"], rows[XTTS]["seat"]), ("No seat", "No seat"))
        self.assertEqual(rows[KIENLT]["status"], "not logged in on machine 2")

    def test_a_test_move_runs_the_full_flow_once_below_the_thresholds(self):
        acct.save_pool_settings({"mover_key": "mk", "machine_id": "m2",
                                 "test_move": {"target": THANGLT, "until": time.time() + 60}})
        pool = FakePool("STABLE", HIEUNX)
        move = self.run_once(pool_index(**{HIEUNX: (31, 32)}, premium=HIEUNX), pool)
        self.assertEqual(pool.puts, [("PREPARING", HIEUNX, THANGLT), ("MOVING", HIEUNX, THANGLT), ("STABLE", THANGLT, None)])
        self.assertEqual(move.call_args.args[0], [(HIEUNX, "No seat"), (THANGLT, "Premium")])
        self.assertNotIn("test_move", acct.pool_settings())  # once only

    def test_needs_the_mover_key(self):
        acct.save_pool_settings({"api_key": "k", "machine_id": "w"})
        with self.assertRaises(acct.AcctError):
            acct._mover_once(argparse.Namespace(force=False, manual=False))


class JoinAndServiceTests(unittest.TestCase):
    def tearDown(self):
        acct.save_pool_settings({})

    def test_join_saves_the_key_and_turns_switching_on(self):
        with Patches(patch.object(acct, "pool_call", return_value={"phase": "STABLE", "premium": SALES}),
                     out=patch.object(acct.sys, "stdout", io.StringIO())) as _p:
            out = _p["out"]
            acct.cmd_auto_join(argparse.Namespace(key=" k1 ", url="http://10.0.0.1:5680/"))
        st = acct.pool_settings()
        self.assertEqual((st["api_key"], st["url"]), ("k1", "http://10.0.0.1:5680"))
        self.assertTrue(acct.load_rotation()["enabled"])
        self.assertIn(SALES, out.getvalue())

    def test_join_with_a_rejected_key_says_so(self):
        with patch.object(acct, "pool_call", side_effect=acct.AcctError("pool server answered HTTP 403 for /api/state")):
            with self.assertRaises(acct.AcctError):
                acct.cmd_auto_join(argparse.Namespace(key="bad", url=None))

    def test_a_name_in_pool_settings_overrides_the_git_user_name(self):
        acct.save_pool_settings({"machine_id": "x", "name": "STG"})
        self.assertEqual(acct.machine_name(), "STG")

    def test_launcher_runs_the_installed_plugin_version_once(self):
        path = acct._write_mover_launcher()
        text = open(path, encoding="utf-8").read()
        self.assertIn("installed_plugins.json", text)
        self.assertIn("auto serve-once", text)
        self.assertTrue(os.access(path, os.X_OK))

    def test_mover_service_is_a_launch_agent_every_30_seconds(self):
        import plistlib
        plist = os.path.join(acct.store_dir(), "agent.plist")
        with Patches(patch.object(acct.sys, "platform", "darwin"),
                     patch.object(acct, "_launch_agent_path", return_value=plist),
                     patch.object(acct.subprocess, "run", return_value=argparse.Namespace(returncode=0, stdout="", stderr=""))):
            acct._install_mover_service()
        with open(plist, "rb") as f:
            p = plistlib.load(f)
        self.assertEqual((p["Label"], p["StartInterval"], p["RunAtLoad"]), (acct.MOVER_LABEL, 30, True))

    def test_pool_requests_carry_the_right_key(self):
        acct.save_pool_settings({"api_key": "k", "mover_key": "mk", "machine_id": "x", "url": "http://pool"})
        seen = []

        class Resp(io.BytesIO):
            headers = argparse.Namespace(get_content_type=lambda: "application/json")
            def __enter__(self): return self
            def __exit__(self, *a): return False

        def fake(req, timeout):
            seen.append((req.full_url, {k.lower(): v for k, v in req.header_items()}))
            return Resp(b"{}")

        with patch.object(acct, "urlopen", side_effect=fake):
            acct.pool_call("GET", "/api/state")
            acct.pool_call("PUT", "/api/state", {"phase": "STABLE"}, mover=True)
        self.assertEqual(seen[0][0], "http://pool/api/state")
        self.assertEqual(seen[0][1].get("x-pool-key"), "k")
        self.assertEqual(seen[1][1].get("x-pool-mover-key"), "mk")


class WindowsServiceTests(unittest.TestCase):
    """Machine 2 on Windows: a Task Scheduler task runs a loop that calls `auto serve-once` every 30 seconds."""

    def test_launcher_is_valid_python_and_runs_serve_once_every_interval(self):
        import ast
        path = acct._write_windows_mover_launcher()
        text = open(path, encoding="utf-8").read()
        ast.parse(text)
        self.assertIn('"auto", "serve-once"', text)
        self.assertIn("INTERVAL = 30", text)
        self.assertIn("installed_plugins.json", text)
        self.assertIn(repr(os.path.abspath(acct.__file__)), text)

    def test_launcher_skips_an_installed_version_without_serve_once(self):
        import subprocess as sp
        home = tempfile.mkdtemp()
        old = os.path.join(home, "cache", "acct", "0.8.3", "scripts")
        os.makedirs(old)
        open(os.path.join(old, "claude_acct.py"), "w").write("# 0.8.3, no seat service\n")
        os.makedirs(os.path.join(home, ".claude", "plugins"))
        json.dump({"plugins": {"acct@x": [{"installPath": os.path.dirname(old)}]}},
                  open(os.path.join(home, ".claude", "plugins", "installed_plugins.json"), "w"))
        text = open(acct._write_windows_mover_launcher(), encoding="utf-8").read()
        probe = text.split("log_path =")[0] + "print(script())\n"
        out = sp.run([sys.executable, "-c", probe], env=dict(os.environ, HOME=home, USERPROFILE=home),
                     capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, os.path.abspath(acct.__file__))
        shutil.rmtree(home)

    def test_task_runs_at_logon_in_the_interactive_session_and_starts_now(self):
        seen = []
        with Patches(patch.object(acct, "_is_windows", return_value=True),
                     patch.object(acct, "_ps", side_effect=lambda script: seen.append(script) or argparse.Namespace(returncode=0, stdout="", stderr=""))):
            acct._install_mover_service()
        script = seen[0]
        for part in ("-AtLogOn", "-LogonType Interactive", "-MultipleInstances IgnoreNew", "Register-ScheduledTask -TaskName 'XTTS_Acct_Mover'",
                     "Start-ScheduledTask -TaskName 'XTTS_Acct_Mover'", "acct-mover.py"):
            self.assertIn(part, script)

    def test_task_failure_is_reported(self):
        with Patches(patch.object(acct, "_is_windows", return_value=True),
                     patch.object(acct, "_ps", return_value=argparse.Namespace(returncode=1, stdout="", stderr="Access is denied"))):
            with self.assertRaises(acct.AcctError) as e:
                acct._install_mover_service()
        self.assertIn("Access is denied", str(e.exception))

    def test_service_state_and_removal(self):
        with Patches(patch.object(acct, "_is_windows", return_value=True),
                     patch.object(acct, "_ps", return_value=argparse.Namespace(returncode=0, stdout="Running\n", stderr=""))):
            self.assertTrue(acct._mover_service_loaded())
        seen = []
        with Patches(patch.object(acct, "_is_windows", return_value=True),
                     patch.object(acct, "_ps", side_effect=lambda script: seen.append(script))):
            acct._uninstall_mover_service()
        self.assertIn("Unregister-ScheduledTask -TaskName 'XTTS_Acct_Mover'", seen[0])


class LoginAllTests(unittest.TestCase):
    def test_logs_in_only_the_missing_or_expired_pool_accounts_in_one_terminal(self):
        idx = pool_index()
        del idx["accounts"][KIENLT]
        with Patches(patch.object(acct, "load_index", return_value=idx),
                     patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", side_effect=lambda m, c: "relogin" if m["profile"]["emailAddress"] == XTTS else "ok"),
                     patch.object(acct, "claude_bin", return_value="/bin/claude"),
                     patch.object(acct.sys, "stdout", io.StringIO()),
                     term=patch.object(acct, "open_terminal", return_value=True)) as _p:
            term = _p["term"]
            acct.cmd_login(argparse.Namespace(name=None, email=None))
        script = open(term.call_args.args[0], encoding="utf-8").read()
        self.assertIn("--email kienlt@xtgroup.vn", script.replace("'", ""))
        self.assertIn("--email xtts@xtgroup.vn", script.replace("'", ""))
        self.assertNotIn("sales@xtgroup.vn", script)
        self.assertIn("(1/2)", script)

    def test_nothing_to_do_when_every_pool_account_is_logged_in(self):
        with Patches(*stored(), patch.object(acct, "load_index", return_value=pool_index()),
                     term=patch.object(acct, "open_terminal"),
                     out=patch.object(acct.sys, "stdout", io.StringIO())) as _p:
            term = _p["term"]
            out = _p["out"]
            acct.cmd_login(argparse.Namespace(name=None, email=None))
        term.assert_not_called()
        self.assertIn("All 12 pool accounts", out.getvalue())


class RotationPreviewTests(unittest.TestCase):
    def preview(self, idx, state):
        with Patches(*stored(), patch.object(acct, "load_rotation", return_value={"enabled": True}),
                     out=patch.object(acct.sys, "stdout", io.StringIO())) as _p:
            acct._print_rotation_preview(idx, None, state, [])
            return _p["out"].getvalue()

    def test_explains_why_no_account_is_eligible(self):
        idx = pool_index(**{SALES: (85, 46)}, premium=SALES)
        for e in acct.ROTATION_EMAILS:
            if e != SALES:
                del idx["accounts"][e]
        text = self.preview(idx, {"phase": "STABLE", "premium": SALES, "last_used": {}})
        self.assertIn("Premium %s: 5-hour 85%%, 7-day 46%%; threshold reached" % SALES, text)
        self.assertIn("Next Premium: none eligible", text)
        self.assertIn("%s (not logged in on this machine)" % XTTS, text)

    def test_names_the_next_premium_and_the_order(self):
        idx = pool_index(**{SALES: (85, 46)}, premium=SALES)
        text = self.preview(idx, {"phase": "STABLE", "premium": SALES, "last_used": {XTTS: 5, CONTACT: 9}})
        self.assertIn("Next Premium: **%s**" % HIEUNX, text)
        self.assertIn(" → ".join([HIEUNX, TRUONGNV, THANGLT, KIENLT, XTG1, XTG2, XTTS, CONTACT]), text)


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

    def test_writes_one_json_line_and_stays_local_before_joining(self):
        acct.log_event("move_ok", "12s", live="a@x.vn", target="b@x.vn", p5h=6.0)
        (rec,) = self.lines()
        self.assertEqual((rec["event"], rec["machine"], rec["live"], rec["target"], rec["p5h"], rec["message"]),
                         ("move_ok", "Tester", "a@x.vn", "b@x.vn", 6.0, "12s"))
        self.assertEqual(self.lines("log-outbox.jsonl"), [])

    def test_throttled_events_are_logged_once_per_period(self):
        for _ in range(3):
            acct.log_event("no_destination", "x", every=1800)
        acct.log_event("heartbeat", "y", every=600)
        self.assertEqual([r["event"] for r in self.lines()], ["no_destination", "heartbeat"])

    def test_joined_machines_queue_events_and_start_an_upload(self):
        acct.save_pool_settings({"api_key": "k", "machine_id": "x"})
        with patch.object(acct, "_spawn_log_flush") as spawn:
            acct.log_event("follow", "seat moved")
        self.assertEqual(len(self.lines("log-outbox.jsonl")), 1)
        spawn.assert_called_once()

    def flush(self, reply):
        acct.save_pool_settings({"api_key": "k", "machine_id": "x"})
        sent = []

        def fake(method, path, body=None, mover=False, timeout=4):
            sent.append((path, body))
            if isinstance(reply, Exception):
                raise reply
            return reply

        with patch.object(acct, "_spawn_log_flush"), patch.object(acct, "pool_call", side_effect=fake):
            acct.log_event("a", "1")
            acct.log_event("b", "2")
            acct.cmd_log_flush(None)
        return sent

    def test_flush_sends_the_outbox_to_the_pool_server_in_one_batch(self):
        sent = self.flush("ok")
        self.assertEqual(sent[0][0], "/api/log")
        self.assertEqual([e["event"] for e in sent[0][1]["events"]], ["a", "b"])
        self.assertFalse(os.path.exists(os.path.join(self.dir, "log-outbox.jsonl.sending")))

    def test_failed_upload_keeps_the_events_for_the_next_flush(self):
        self.flush(acct.AcctError("pool server unreachable"))
        self.assertEqual(len(self.lines("log-outbox.jsonl.sending")), 2)

    def test_logging_never_raises(self):
        with patch.object(acct, "store_dir", side_effect=OSError("read-only")):
            acct.log_event("error", "x")


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
