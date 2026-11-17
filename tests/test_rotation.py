import argparse
import datetime as dt
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

XTTS, SALES, CONTACT, XTG1, XTG6, INFO = acct.POOL_EMAILS
CAREERS = "careers@xtgroup.vn"  # a former backup account, outside the pool since 1.0.31
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
    """Every pool account saved under its email; usage={email: (5h, 7d)}, premium=<email> and standard=<email> (the
    seats claude.ai shows; standard defaults to info, None: nobody). The others have no seat."""
    premium = usage.pop("premium", None)
    standard = usage.pop("standard", INFO)
    idx = {"accounts": {}}
    for email in acct.POOL_EMAILS:
        five, seven = usage.get(email, (0, 0))
        tier = acct.PREMIUM_TIER if email == premium else acct.STANDARD_TIER if email == standard else None
        idx["accounts"][email] = account(five, seven, email, tier)
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


def snap(five, seven, five_in=None, seven_in=None, tier="premium"):
    """A usage snapshot; five_in/seven_in: hours until that window resets (negative: already reset)."""
    iso = lambda h: None if h is None else dt.datetime.fromtimestamp(time.time() + h * 3600, dt.timezone.utc).isoformat()
    return {"tier": tier, "five_hour": five, "five_hour_reset": iso(five_in),
            "seven_day": seven, "seven_day_reset": iso(seven_in), "at": time.time() - 60}


class CandidateTests(unittest.TestCase):
    """The next Premium is ranked by the last usage read of each account, estimated for now."""

    def pick(self, idx, premium, snaps=None, spent=False, backup=INFO):
        with Patches(*stored(), usage=patch.object(acct, "refresh_usage", return_value="cache")) as p:
            got = acct._next_premium(idx, premium, snaps or {}, premium_spent=spent, backup=backup)
            p["usage"].assert_not_called()  # accounts without a seat have no usage to read
        return got

    def test_lowest_7_day_then_5_hour_then_earliest_resets_then_the_list_order(self):
        idx = pool_index(premium=XTTS)
        full = {e: snap(50, 80, 3, 90) for e in acct.POOL_EMAILS}  # all alike, ranked by the list order
        self.assertEqual(self.pick(idx, XTTS, full)[0], SALES)
        snaps = dict(full, **{XTG6: snap(50, 45, 3, 90), XTG1: snap(50, 47, 3, 90)})
        self.assertEqual(self.pick(idx, XTTS, snaps)[0], XTG6)  # 45 < 47: no tolerance
        snaps = dict(full, **{XTG6: snap(30, 45, 3, 90), XTG1: snap(20, 45, 3, 90)})
        self.assertEqual(self.pick(idx, XTTS, snaps)[0], XTG1)  # same 7-day: lower 5-hour
        snaps = dict(full, **{XTG6: snap(20, 45, 4, 90), XTG1: snap(20, 45, 1, 90)})
        self.assertEqual(self.pick(idx, XTTS, snaps)[0], XTG1)  # same usage: the 5-hour that resets first
        snaps = dict(full, **{XTG6: snap(20, 45, 1, 50), XTG1: snap(20, 45, 1, 90)})
        self.assertEqual(self.pick(idx, XTTS, snaps)[0], XTG6)  # then the 7-day that resets first

    def test_a_window_past_its_reset_counts_as_0_and_a_never_measured_account_is_never_taken(self):
        idx = pool_index(premium=XTTS)
        snaps = {e: snap(50, 80, 3, 90) for e in acct.POOL_EMAILS}
        snaps[CONTACT] = snap(95, 92, -1, -2)  # both windows reset since: 0 %
        self.assertEqual(self.pick(idx, XTTS, snaps)[0], CONTACT)
        self.assertEqual(acct._estimate(snaps[CONTACT], "premium")[::2], (0.0, 0.0))
        del snaps[CONTACT]  # never measured
        name, notes = self.pick(idx, XTTS, snaps)
        self.assertEqual(name, SALES)
        self.assertIn("never measured, taken only with the dashboard button: %s" % CONTACT, notes)
        self.assertEqual(acct._premium_order(snaps, XTTS, backup=INFO)[-1], CONTACT)  # last, after spent ones
        snaps = {e: snap(95, 95, 3, 90) for e in acct.POOL_EMAILS}
        del snaps[XTG1]
        self.assertIsNone(self.pick(idx, XTTS, snaps, spent=False)[0])  # every measured one spent: not xtg1
        self.assertIsNone(self.pick(idx, XTTS, {})[0])

    def test_a_reading_with_the_standard_seat_counts_a_fifth_for_premium_and_the_other_way_5_times(self):
        on_standard = snap(50, 80, 3, 90, tier="standard")
        self.assertEqual(acct._estimate(on_standard, "premium")[::2], (10.0, 16.0))
        self.assertEqual(acct._estimate(on_standard, "standard")[::2], (50.0, 80.0))
        self.assertEqual(acct._estimate(snap(10, 30, 3, 90), "standard")[::2], (50.0, 100.0))  # at most 100 %
        idx = pool_index(premium=XTTS)
        snaps = {e: snap(50, 60, 3, 90) for e in acct.POOL_EMAILS}
        snaps[XTG6] = on_standard  # the account that held Standard last: ranked by what it used, in Premium figures
        self.assertEqual(self.pick(idx, XTTS, snaps)[0], XTG6)

    def test_an_account_at_a_threshold_ranks_last_even_with_a_low_7_day(self):
        # 25/10: truongnv had 7-day 47 % but 5-hour 81 %; the others 7-day 90-91 %
        idx = pool_index(premium=XTTS)
        snaps = {e: snap(3, 91, 2, 90) for e in acct.POOL_EMAILS}
        snaps[XTG6] = snap(81, 47, 2, 90)
        snaps[XTG1] = snap(10, 60, 2, 90)
        self.assertEqual(self.pick(idx, XTTS, snaps)[0], XTG1)
        order = acct._premium_order(snaps, XTTS, backup=INFO)
        self.assertEqual(order[0], XTG1)
        self.assertIn(XTG6, order[1:])

    def test_when_every_account_is_at_a_threshold_premium_keeps_the_seat_until_it_uses_a_window_up(self):
        idx = pool_index(premium=XTTS)
        snaps = {e: snap(3, 91, 2, 90) for e in acct.POOL_EMAILS}
        snaps[XTG6] = snap(81, 47, 2, 90)
        name, notes = self.pick(idx, XTTS, snaps)
        self.assertIsNone(name)
        self.assertIn("every account is at a threshold", notes[-1])
        self.assertEqual(self.pick(idx, XTTS, snaps, spent=True)[0], XTG6)  # P used up: the best ranked one
        snaps = {e: snap(100, 100, 2, 90) for e in acct.POOL_EMAILS}
        self.assertIsNone(self.pick(idx, XTTS, snaps, spent=True)[0])  # every one used up: nothing better

    def test_the_standard_account_and_premium_are_never_candidates_and_unusable_ones_are_skipped(self):
        idx = pool_index(premium=XTTS)
        del idx["accounts"][SALES]
        snaps = {e: snap(0, 10) for e in acct.POOL_EMAILS}
        snaps[INFO] = snap(0, 0)  # the best, but every machine waits on it while the seat moves
        with Patches(patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", side_effect=lambda m, c: "relogin" if m["profile"]["emailAddress"] == CONTACT else "ok")):
            name, notes = acct._next_premium(idx, XTTS, snaps, backup=INFO)
        self.assertEqual(name, XTG1)
        self.assertIn("%s (not logged in on this machine)" % SALES, notes)
        self.assertIn("%s (login expired/revoked)" % CONTACT, notes)

    def test_dashboard_lists_never_measured_then_not_logged_in_accounts_last_without_figures(self):
        idx = pool_index(premium=XTTS)
        del idx["accounts"][XTG1]
        snaps = {e: snap(10, 95, 2, 90) for e in acct.POOL_EMAILS}  # all spent
        for e in (XTG1, XTG6):
            del snaps[e]
        with Patches(*stored()):
            rows = acct._account_rows(idx, None, {"premium": XTTS, "backup": INFO, "snapshots": snaps})
        pool = [r for r in rows if r["role"] == "rotation"]
        self.assertEqual([r["email"] for r in pool], [SALES, CONTACT, XTG6, XTG1])  # never measured, then not logged in
        self.assertEqual((pool[-1]["five_hour"] if "five_hour" in pool[-1] else None, pool[-2].get("seven_day")), (None, None))
        self.assertEqual(pool[-1]["status"], "not logged in on machine 2")

    def test_dashboard_rows_show_estimates_for_accounts_without_a_seat(self):
        idx = pool_index(**{XTTS: (40, 10), INFO: (5, 6)}, premium=XTTS)
        del idx["accounts"][XTG1]
        snaps = {e: snap(3, 91, 2, 90) for e in acct.POOL_EMAILS}
        snaps.update({XTG6: snap(20, 30, 2, 90), SALES: snap(50, 95, -1, -1), CONTACT: snap(50, 95, 2, 90, "standard")})
        state = {"premium": XTTS, "backup": INFO, "snapshots": snaps}
        with Patches(*stored(), usage=patch.object(acct, "refresh_usage", return_value="cache")) as p:
            rows = acct._account_rows(idx, None, state)
        self.assertEqual({c.args[0]["profile"]["emailAddress"] for c in p["usage"].call_args_list}, {XTTS, INFO})
        by = {r["email"]: r for r in rows}
        self.assertEqual([r["email"] for r in rows[:4]], [XTTS, SALES, CONTACT, XTG6])  # sales reset: 0 %
        self.assertEqual((by[SALES]["five_hour"], by[SALES]["seven_day"], by[SALES]["eligible"]), (0.0, 0.0, True))
        # read with the Standard seat: shown in Premium figures
        self.assertEqual((by[CONTACT]["five_hour"], by[CONTACT]["seven_day"], by[CONTACT]["estimated"]), (10.0, 19.0, True))
        self.assertEqual(by[XTG1]["status"], "not logged in on machine 2")
        self.assertEqual((rows[-1]["email"], rows[-1]["role"], rows[-1]["seat"]), (INFO, "backup", "Standard"))
        self.assertEqual(self.pick(idx, XTTS, snaps)[0], rows[1]["email"])  # rank 1 is the next Premium
        # nobody else fits the Standard seat (contact and xtg6 would be at a threshold on it): the next Premium
        self.assertEqual([r["email"] for r in rows if r.get("standard_next")], [SALES])

    def test_dashboard_rows_carry_when_each_login_ends_and_flag_expired_ones(self):
        idx = pool_index(**{XTTS: (40, 10)}, premium=XTTS)
        ends = time.time() + 3 * 86400
        creds = {"claudeAiOauth": {"refreshToken": "r", "refreshTokenExpiresAt": ends * 1000}}
        with Patches(patch.object(acct, "read_stored_creds", return_value=creds),
                     patch.object(acct, "token_state", side_effect=lambda m, c: "relogin" if m["profile"]["emailAddress"] in (SALES, INFO) else "ok"),
                     patch.object(acct, "refresh_usage", return_value="cache"), patch.object(acct, "refresh_seat", return_value="cache")):
            rows = acct._account_rows(idx, None, {"premium": XTTS, "backup": INFO, "snapshots": {}})
        by = {r["email"]: r for r in rows}
        self.assertAlmostEqual(by[XTG1]["login_expires"], ends, delta=1)
        self.assertAlmostEqual(by[XTTS]["login_expires"], ends, delta=1)  # the seated rows too
        self.assertEqual(by[SALES]["status"], "log in again")  # no seat, expired login: shown before anyone picks it
        self.assertEqual(by[INFO]["status"], "log in again")  # the account with the Standard seat
        self.assertIsNone(by[XTG1]["status"])


class StandardPickTests(unittest.TestCase):
    """Which account gets the Standard seat (_next_standard): readings in Premium figures."""

    def pick(self, snaps, premium=XTTS, backup=INFO, idx=None, **kw):
        with Patches(*stored()):
            return acct._next_standard(idx or pool_index(premium=premium, standard=backup), snaps, premium, backup, **kw)

    def test_the_example_skips_the_next_premium_and_takes_5_hour_0_and_7_day_at_most_15(self):
        # Premium xtts, Standard info; #1 sales 0/0, contact 0/10, xtg6 0/21, xtg1 20/22: contact
        snaps = {SALES: snap(0, 0), CONTACT: snap(0, 10), XTG6: snap(0, 21), XTG1: snap(20, 22)}
        self.assertEqual(acct._premium_order(snaps, XTTS, backup=INFO)[0], SALES)
        self.assertEqual(self.pick(snaps), CONTACT)

    def test_the_best_ranked_of_those_that_fit(self):
        snaps = {SALES: snap(0, 0), CONTACT: snap(0, 12), XTG6: snap(0, 5), XTG1: snap(0, 15)}
        self.assertEqual(self.pick(snaps), XTG6)
        snaps[XTG6] = snap(1, 5)  # 5-hour not 0
        self.assertEqual(self.pick(snaps), CONTACT)

    def test_a_5_hour_window_past_its_reset_counts_as_0(self):
        snaps = {SALES: snap(0, 0), CONTACT: snap(60, 10, -1, 90), XTG6: snap(0, 30), XTG1: snap(5, 5)}
        self.assertEqual(self.pick(snaps), CONTACT)

    def test_else_the_best_one_still_below_the_thresholds_on_standard(self):
        # nobody at 0 % and <= 15 %: xtg1 2 %/16 % reads about 10 %/80 % on Standard, below 70 %/90 %
        snaps = {SALES: snap(0, 0), CONTACT: snap(0, 40), XTG6: snap(5, 30), XTG1: snap(2, 16)}
        self.assertEqual(self.pick(snaps), XTG1)

    def test_the_next_premium_only_when_no_other_one_fits(self):
        snaps = {SALES: snap(0, 0), CONTACT: snap(0, 40), XTG6: snap(5, 30), XTG1: snap(30, 60)}
        self.assertEqual(self.pick(snaps), SALES)

    def test_when_none_fits_the_holder_keeps_it_unless_it_used_a_window_up_or_a_premium_move_needs_one(self):
        snaps = {SALES: snap(10, 40), CONTACT: snap(0, 40), XTG6: snap(5, 30), XTG1: snap(30, 60)}
        self.assertIsNone(self.pick(snaps))
        self.assertIsNone(self.pick(snaps, backup_spent=True))  # each would be full on Standard too (7-day x5)
        self.assertIsNone(self.pick(snaps, must=True))  # not even for a Premium move: no room on Standard
        snaps[XTG6] = snap(5, 18)  # 25 %/90 % on Standard: at a threshold, but room for a few minutes
        self.assertIsNone(self.pick(snaps))
        self.assertEqual(self.pick(snaps, backup_spent=True), XTG6)
        self.assertEqual(self.pick(snaps, must=True), XTG6)
        snaps[XTG6] = snap(5, 19)  # 95 % on Standard: full
        self.assertIsNone(self.pick(snaps, must=True))

    def test_never_premium_the_holder_an_excluded_one_or_one_machine_2_cannot_use(self):
        idx = pool_index(premium=XTTS)
        del idx["accounts"][SALES]
        snaps = {e: snap(0, 0) for e in acct.POOL_EMAILS}
        # order without contact (excluded: about to become Premium): xtg1 (#1), xtg6, then sales (not logged in)
        self.assertEqual(self.pick(snaps, idx=idx, exclude=(CONTACT,)), XTG6)


class ThresholdTests(unittest.TestCase):
    def reached(self, five, seven):
        return acct._usage_reached_threshold(account(five, seven)["usage"]["data"])

    def test_default_rule(self):
        # 5-hour >= 70% or 7-day >= 90% (or any window used up).
        self.assertFalse(self.reached(69, 89))
        self.assertTrue(self.reached(70, 0))
        self.assertTrue(self.reached(0, 90))
        self.assertTrue(self.reached(100, 0))

    def test_pool_is_fixed(self):
        # claude.ai gives a seat to at most 4 new people a week: 2 seats + 4 accounts
        self.assertEqual(acct.POOL_EMAILS, ("xtts@xtgroup.vn", "sales@xtgroup.vn", "contact@xtgroup.vn",
                                            "xtg1@xtgroup.vn", "xtg6@xtgroup.vn", "info@xtgroup.vn"))
        self.assertEqual(acct.ROTATION_EMAILS, acct.POOL_EMAILS)
        self.assertFalse(set(acct.FORMER_POOL_EMAILS) & set(acct.POOL_EMAILS))
        self.assertIn("hieule252uet@gmail.com", acct.FORMER_POOL_EMAILS)
        self.assertIn(CAREERS, acct.FORMER_POOL_EMAILS)
        self.assertEqual((acct.STANDARD_PICK_FIVE_HOUR, acct.STANDARD_PICK_SEVEN_DAY), (0, 15))


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

    def http_429(self, retry_after=None):
        headers = {} if retry_after is None else {"Retry-After": retry_after}
        return urllib.error.HTTPError(acct.USAGE_URL, 429, "Too Many Requests", headers, io.BytesIO(b""))

    def test_a_429_with_retry_after_0_waits_and_backs_off(self):
        # /api/oauth/usage answers "retry-after: 0" while it limits: that must not mean "ask again at once"
        meta = {}
        creds = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": (time.time() + 3600) * 1000}}
        for expected in (300, 600, 1200, 1800, 1800):  # doubled at each 429 in a row, up to USAGE_RETRY_MAX
            with patch.object(acct, "fetch_usage", side_effect=self.http_429("0")):
                self.assertTrue(acct.refresh_usage(meta, creds, force=True).startswith("rate limited until"))
            self.assertAlmostEqual(meta["usage_blocked_until"], time.time() + expected, delta=5)
            meta["usage_blocked_until"] = time.time() - 1  # the wait is over
        with patch.object(acct, "fetch_usage", side_effect=self.http_429()):  # no header: the same rule
            acct.refresh_usage(meta, creds, force=True)
        self.assertAlmostEqual(meta["usage_blocked_until"], time.time() + 1800, delta=5)
        meta["usage_blocked_until"] = time.time() - 1
        with patch.object(acct, "fetch_usage", return_value={"five_hour": {"utilization": 12}}):
            self.assertEqual(acct.refresh_usage(meta, creds, force=True), "ok")
        self.assertNotIn("usage_429_streak", meta)  # a reading starts the next streak over at 300 s
        with patch.object(acct, "fetch_usage", side_effect=self.http_429("0")):
            acct.refresh_usage(meta, creds, force=True)
        self.assertAlmostEqual(meta["usage_blocked_until"], time.time() + 300, delta=5)

    def test_a_seat_read_429_is_not_asked_again_before_the_wait(self):
        meta = {"seat": {"fetchedAt": 0, "tier": "team_tier_1"}}
        creds = {"claudeAiOauth": {"accessToken": "a", "refreshToken": "r", "expiresAt": (time.time() + 3600) * 1000}}
        with patch.object(acct, "fetch_usage", side_effect=self.http_429("0")) as fetch:
            self.assertTrue(acct.refresh_seat(meta, creds, force=True).startswith("rate limited until"))
            self.assertTrue(acct.refresh_seat(meta, creds, force=True).startswith("rate limited until"))
            self.assertEqual(fetch.call_count, 1)
        self.assertAlmostEqual(meta["seat_blocked_until"], time.time() + 300, delta=5)
        self.assertEqual(meta["seat"]["tier"], "team_tier_1")  # the last known seat is kept
        self.assertNotIn("usage_blocked_until", meta)  # usage and seat are limited apart
        meta["seat_blocked_until"] = time.time() - 1
        with patch.object(acct, "fetch_usage", return_value={"organization": {"seat_tier": "team_standard"}}):
            self.assertEqual(acct.refresh_seat(meta, creds, force=True), "ok")
        self.assertEqual(meta["seat"]["tier"], "team_standard")
        self.assertNotIn("seat_blocked_until", meta)
        self.assertNotIn("seat_429_streak", meta)

    def test_the_seat_manager_has_time_for_its_retries_and_rollback(self):
        # setTier tries 2 clicks of about 40 s each, and a failed change is rolled back the same way: a timeout
        # would kill it half way, with no rollback at all. The whole move must still fit the mover lock (600 s).
        ok = acct.subprocess.CompletedProcess([], 0, "Seats set: a -> Premium.", "")
        with patch.object(acct, "ensure_node_deps"), patch.object(acct, "seat_manager_env", return_value={}), \
             patch.object(acct.subprocess, "run", return_value=ok) as run, patch.object(acct.sys, "stdout", io.StringIO()):
            acct._set_seats([("a@x.vn", "No seat"), ("b@x.vn", "Premium")], {})
        self.assertEqual(run.call_args.kwargs["timeout"], acct.SEAT_CHANGE_TIMEOUT)
        self.assertGreaterEqual(acct.SEAT_CHANGE_TIMEOUT, 2 * 2 * 80)
        self.assertLess(acct.PREPARE_TIMEOUT + acct.SEAT_CHANGE_TIMEOUT + 60, 600)

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

    def test_a_login_ending_within_a_week_is_flagged(self):
        now = time.time()
        creds = lambda days: {"claudeAiOauth": {"refreshToken": "r", "refreshTokenExpiresAt": (now + days * 86400) * 1000}}
        self.assertEqual(acct.login_ends_soon(creds(3.5), now), 3)
        self.assertEqual(acct.login_ends_soon(creds(0.2), now), 0)  # today
        self.assertIsNone(acct.login_ends_soon(creds(8), now))  # more than RELOGIN_WARN_DAYS left
        self.assertIsNone(acct.login_ends_soon(creds(-1), now))  # already ended: "log in again" instead
        self.assertIsNone(acct.login_ends_soon({"claudeAiOauth": {"refreshToken": "r"}}, now))  # unknown
        self.assertAlmostEqual(acct.login_expires_at(creds(2)), now + 2 * 86400, delta=1)

    def test_list_flags_a_login_that_ends_soon(self):
        idx = {"accounts": {"a": {"profile": {"emailAddress": "a@x.com"}}, "b": {"profile": {"emailAddress": "b@x.com"}}}}
        ends = (time.time() + 2.5 * 86400) * 1000
        self.store["a"] = {"claudeAiOauth": {"accessToken": "x", "refreshToken": "ra", "expiresAt": 1}}
        self.store["b"] = {"claudeAiOauth": {"accessToken": "y", "refreshToken": "rb", "expiresAt": 1,
                                             "refreshTokenExpiresAt": ends}}
        out = io.StringIO()
        with patch.object(acct, "load_index", return_value=idx), patch.object(acct, "save_index"), \
             patch.object(acct, "current_name", return_value=None), patch.object(acct, "read_live_profile", return_value={}), \
             patch.object(acct, "_pool_view", return_value=({}, [])), \
             patch.object(acct, "refresh_seat"), patch.object(acct, "refresh_usage", return_value="cache"), \
             patch.object(acct.sys, "stdout", out):
            acct.cmd_list(argparse.Namespace(refresh=False))
        text = out.getvalue()
        self.assertIn("🟠 Login ends in 2 day(s): `/acct:login b`", text)
        self.assertNotIn("/acct:login a`", text)
        self.assertIn("1 login(s) end within 7 days", text)

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

    def test_preparing_switches_to_the_standard_account_and_reports_it(self):
        switched, reports = self.run_check(SALES, {"phase": "PREPARING", "premium": SALES, "target": XTG1, "backup": INFO})
        self.assertEqual(switched, [INFO])
        self.assertEqual(reports[-1], (INFO, True))

    def test_preparing_without_a_standard_account_stays(self):
        switched, _ = self.run_check(SALES, {"phase": "PREPARING", "premium": SALES, "target": XTG1})
        self.assertEqual(switched, [])

    def test_moving_keeps_the_backup_account(self):
        switched, _ = self.run_check(INFO, {"phase": "MOVING", "premium": SALES, "target": XTG1})
        self.assertEqual(switched, [])

    def test_after_the_move_the_backup_account_switches_to_the_new_premium(self):
        switched, _ = self.run_check(INFO, {"phase": "STABLE", "premium": XTG1})
        self.assertEqual(switched, [XTG1])

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

    def test_preparing_switches_to_the_standard_account_machine_2_picked(self):
        switched, reports = self.run_check(SALES, {"phase": "PREPARING", "premium": SALES, "target": XTG1, "backup": CONTACT})
        self.assertEqual(switched, [CONTACT])
        self.assertEqual(reports[-1], (CONTACT, True))

    def test_a_machine_left_on_the_former_backup_account_returns_to_premium(self):
        idx = pool_index(premium=SALES)
        idx["accounts"]["old"] = account(0, 0, "hieule252uet@gmail.com")
        switched, _ = self.run_check("old", {"phase": "STABLE", "premium": SALES}, idx=idx)
        self.assertEqual(switched, [SALES])

    def test_a_machine_left_on_an_account_of_the_former_pool_returns_to_premium(self):
        idx = pool_index(premium=SALES)
        idx["accounts"]["careers"] = account(0, 0, CAREERS)
        switched, _ = self.run_check("careers", {"phase": "STABLE", "premium": SALES, "backup": INFO}, idx=idx)
        self.assertEqual(switched, [SALES])

    def test_wanted_account_not_logged_in_here_is_reported_not_switched(self):
        idx = pool_index(premium=SALES)
        del idx["accounts"][XTG1]
        switched, _ = self.run_check(SALES, {"phase": "STABLE", "premium": XTG1}, idx=idx)
        self.assertEqual(switched, [])
        with self.assertRaises(acct.AcctError):
            self.run_check(SALES, {"phase": "STABLE", "premium": XTG1}, idx=idx, manual=True)


class FakePool:
    """The pool server as machine 2 sees it."""

    def __init__(self, phase="STABLE", premium=None, workers=()):
        self.state = {"phase": phase, "premium": premium, "target": None, "backup": INFO, "last_used": {},
                      "updated_at": 100.0}
        # every account measured at 0 % unless a test says otherwise (a never-measured one is not taken)
        self.state["snapshots"] = {e: snap(0, 0, tier="standard" if e == INFO else "premium") for e in acct.POOL_EMAILS}
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
            self.published = getattr(self, "published", []) + [(self.state["phase"], self.state["premium"], body["accounts"])]
            return "ok"
        if path == "/api/request/result":
            self.state["request"] = dict(self.state["request"], status=body["status"], message=body.get("message"))
            self.results = getattr(self, "results", []) + [body["status"]]
            return "ok"
        if path == "/api/snapshots":
            self.state["snapshots"] = dict(self.state["snapshots"], **body["snapshots"])
            return "ok"
        if path == "/api/hold" and method == "DELETE":
            self.state["hold"] = None
            return dict(self.state)
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

    def run_once_raw(self, idx, pool, set_seats=None):
        """Like run_once, but leaves refresh_seat to the caller's patch; set_seats: what changing seats does."""
        with Patches(patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", return_value="ok"),
                     patch.object(acct, "refresh_usage", return_value="cache"),
                     patch.object(acct, "load_index", return_value=idx), patch.object(acct, "save_index"),
                     patch.object(acct, "current_name", return_value=None),
                     patch.object(acct, "pool_call", side_effect=pool),
                     patch.object(acct.time, "sleep"),
                     patch.object(acct.sys, "stdout", io.StringIO()),
                     patch.object(acct, "_spawn_log_flush"),
                     move=patch.object(acct, "_set_seats", side_effect=set_seats)) as p:
            acct._mover_once(argparse.Namespace(force=False, manual=False))
        return p["move"]

    def test_below_threshold_only_publishes_the_premium_account(self):
        pool = FakePool()
        move = self.run_once(pool_index(**{SALES: (50, 40)}, premium=SALES), pool)
        self.assertEqual(pool.puts, [("STABLE", SALES, None)])
        move.assert_not_called()

    def test_full_move_goes_through_preparing_moving_stable(self):
        pool = FakePool("STABLE", SALES, workers=[{"name": "Hiếu", "role": "worker", "active": True, "at_info": False, "last_seen": 150.0}])
        pool.state["snapshots"] = {e: snap(5, 60, 2, 90) for e in acct.POOL_EMAILS}
        pool.state["snapshots"][CONTACT] = snap(5, 20, 2, 90)
        idx = pool_index(**{SALES: (85, 40)}, premium=SALES)
        move = self.run_once(idx, pool)
        self.assertEqual(pool.puts, [("PREPARING", SALES, CONTACT), ("MOVING", SALES, CONTACT), ("STABLE", CONTACT, None)])
        # one Premium seat: freed first, then given to the best ranked account (lowest 7-day)
        self.assertEqual(move.call_args.args[0], [(SALES, "No seat"), (CONTACT, "Premium")])
        self.assertEqual(pool.state["snapshots"][SALES]["five_hour"], 85)  # P's last usage is kept

    def test_no_move_while_every_account_is_at_a_threshold(self):
        pool = FakePool("STABLE", SALES)
        pool.state["snapshots"] = {e: snap(3, 91, 2, 90) for e in acct.POOL_EMAILS}
        move = self.run_once(pool_index(**{SALES: (85, 40)}, premium=SALES), pool)
        move.assert_not_called()
        self.assertEqual(pool.puts, [])
        move = self.run_once(pool_index(**{SALES: (100, 40)}, premium=SALES), pool)  # P used up: least spent one
        self.assertEqual(move.call_args.args[0], [(SALES, "No seat"), (XTTS, "Premium")])

    def test_the_dashboard_table_shows_the_reading_behind_a_move_and_the_seats_after_it(self):
        pool = FakePool("STABLE", SALES)
        acct._save_mover_state({"accounts_at": time.time()})  # a table was just sent: the reading forces a new one
        self.run_once(pool_index(**{SALES: (85, 40)}, premium=SALES), pool)
        phases = [(ph, prem) for ph, prem, _ in pool.published]
        self.assertEqual(phases[0], ("STABLE", SALES))  # sent before PREPARING, with sales at 85 %
        first = {r["email"]: r for r in pool.published[0][2]}
        self.assertEqual(first[SALES]["five_hour"], 85)
        self.assertEqual(phases[-1], ("STABLE", XTTS))  # and again once the seat is on the new Premium
        self.assertEqual(pool.published[-1][2][0]["email"], XTTS)

    def test_one_usage_reading_per_minute_and_seats_every_2_minutes(self):
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{SALES: (10, 10)}, premium=SALES)
        with Patches(patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", return_value="ok"),
                     patch.object(acct, "load_index", return_value=idx), patch.object(acct, "save_index"),
                     patch.object(acct, "current_name", return_value=None),
                     patch.object(acct, "pool_call", side_effect=pool),
                     patch.object(acct.sys, "stdout", io.StringIO()),
                     patch.object(acct, "_spawn_log_flush"),
                     usage=patch.object(acct, "refresh_usage", return_value="cache"),
                     seat=patch.object(acct, "refresh_seat", return_value="cache")) as p:
            acct._mover_once(argparse.Namespace(force=False, manual=False))
        self.assertEqual({c.kwargs.get("ttl") for c in p["usage"].call_args_list}, {acct.USAGE_READ_EVERY})
        self.assertEqual({c.kwargs.get("ttl") for c in p["seat"].call_args_list}, {acct.SEAT_CHECK_EVERY})
        self.assertEqual((acct.USAGE_READ_EVERY, acct.SEAT_CHECK_EVERY), (180, 180))

    def test_the_standard_account_keeps_the_seat_until_it_is_spent(self):
        pool = FakePool("STABLE", SALES)
        move = self.run_once(pool_index(**{SALES: (50, 40), INFO: (69, 89)}, premium=SALES), pool)
        self.assertEqual(pool.state["backup"], INFO)  # below 70% and 90%: kept
        move.assert_not_called()
        for usage in ((70, 50), (10, 90)):  # the Premium thresholds: 5-hour 70% or 7-day 90%
            os.unlink(os.path.join(acct.store_dir(), "mover-state.json")) if os.path.exists(os.path.join(acct.store_dir(), "mover-state.json")) else None
            pool = FakePool("STABLE", SALES)
            move = self.run_once(pool_index(**{SALES: (50, 40), INFO: usage}, premium=SALES), pool)
            # every account at 0 %: xtts is the next Premium (#1), so contact, the next in the list
            self.assertEqual(move.call_args.args[0], [(INFO, "No seat"), (CONTACT, "Standard")])
            self.assertEqual(pool.state["backup"], CONTACT)
            self.assertEqual(pool.puts[-1], ("STABLE", SALES, None))

    def test_the_first_reading_on_standard_is_logged_against_the_last_premium_one(self):
        pool = FakePool("STABLE", SALES)
        pool.state["snapshots"].update({CONTACT: snap(0, 10), XTG1: snap(0, 40), XTG6: snap(0, 40)})
        idx = pool_index(**{SALES: (50, 40), INFO: (75, 30)}, premium=SALES)
        move = self.run_once(idx, pool)
        self.assertEqual(move.call_args.args[0], [(INFO, "No seat"), (CONTACT, "Standard")])
        self.assertEqual(pool.state["snapshots"][INFO]["tier"], "standard")  # what info used, kept to rank it
        idx["accounts"][CONTACT].update(account(0, 50, CONTACT, acct.STANDARD_TIER))  # claude.ai now
        idx["accounts"][INFO]["seat"] = {"fetchedAt": time.time(), "tier": None}
        self.run_once(idx, pool)
        log = [json.loads(l) for l in open(os.path.join(acct.store_dir(), "rotation.log"), encoding="utf-8")]
        msg = [e for e in log if e["event"] == "standard_reading"][-1]["message"]
        self.assertIn("%s on the Standard seat: 5-hour 0%%, 7-day 50%%" % CONTACT, msg)
        self.assertIn("5-hour 0%, 7-day 10%", msg)
        self.assertNotIn("standard_probe", acct._mover_state())  # once

    def test_when_no_pool_account_holds_standard_machine_2_gives_it_to_one(self):
        pool = FakePool("STABLE", SALES)
        move = self.run_once(pool_index(**{SALES: (50, 40)}, premium=SALES, standard=None), pool)
        self.assertEqual(move.call_args.args[0], [(CONTACT, "Standard")])
        self.assertEqual(pool.state["backup"], CONTACT)

    def test_a_standard_seat_moved_by_hand_is_followed(self):
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{SALES: (50, 40)}, premium=SALES)
        idx["accounts"][INFO]["seat"]["tier"] = None  # xtg6 got the Standard seat by hand
        idx["accounts"][XTG6]["seat"]["tier"] = acct.STANDARD_TIER
        move = self.run_once(idx, pool)
        self.assertEqual(pool.state["backup"], XTG6)
        self.assertEqual(pool.puts[-1], ("STABLE", SALES, None))
        move.assert_not_called()

    def test_the_backup_account_stays_when_it_still_holds_standard(self):
        pool = FakePool("STABLE", SALES)
        self.run_once(pool_index(**{SALES: (50, 40)}, premium=SALES), pool)
        self.assertEqual((pool.state["backup"], pool.puts), (INFO, []))

    def test_nothing_moves_while_a_machine_holds_the_seats(self):
        pool = FakePool("STABLE", SALES)
        pool.state["hold"] = {"name": "Hiếu", "account": XTG1, "backup": INFO, "backup_tier": "Standard",
                              "until": time.time() + 300}
        move = self.run_once(pool_index(**{SALES: (95, 40), INFO: (90, 10)}, premium=SALES), pool)
        move.assert_not_called()
        self.assertEqual(pool.puts, [])

    def test_an_expired_hold_is_given_back_by_machine_2(self):
        pool = FakePool("STABLE", SALES)
        pool.state["hold"] = {"name": "Hiếu", "account": XTG1, "backup": INFO, "backup_tier": "Standard", "until": 1}
        move = self.run_once(pool_index(**{SALES: (95, 40)}, premium=SALES), pool)
        self.assertEqual(move.call_args.args[0], [(XTG1, "No seat"), (INFO, "Standard")])
        self.assertIsNone(pool.state["hold"])

    def test_the_standard_seat_is_read_again_right_before_it_changes(self):
        # cached readings say info holds Standard and is spent; claude.ai now says xtg6 got it by hand
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{SALES: (50, 40), INFO: (90, 10)}, premium=SALES)

        def tier(meta, creds, force=False, refresh_name=None, ttl=None):
            email = meta["profile"]["emailAddress"]
            if force and email != SALES:
                meta["seat"] = {"fetchedAt": time.time(), "tier": "team_standard" if email == XTG6 else None}
            return "ok"

        with patch.object(acct, "refresh_seat", side_effect=tier):
            move = self.run_once_raw(idx, pool)
        move.assert_not_called()  # no seat changed: the one moved by hand is followed
        self.assertEqual(pool.state["backup"], XTG6)

    def test_a_backup_given_standard_by_hand_keeps_it_for_3_minutes_even_when_spent(self):
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{SALES: (50, 40), XTG6: (100, 10)}, premium=SALES, standard=XTG6)
        move = self.run_once(idx, pool)  # follows xtg6, which is spent: kept
        self.assertEqual(pool.state["backup"], XTG6)
        move.assert_not_called()
        ms = acct._mover_state()
        ms["backup_manual"]["at"] -= acct.MANUAL_BACKUP_GRACE + 1  # 3 minutes later
        acct._save_mover_state(ms)
        move = self.run_once(idx, pool)
        self.assertEqual(move.call_args.args[0], [(XTG6, "No seat"), (CONTACT, "Standard")])

    def test_a_premium_given_by_hand_keeps_the_seat_for_3_minutes_above_the_threshold(self):
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{XTG1: (95, 40)}, premium=XTG1)  # moved by hand to xtg1, already at 95%
        move = self.run_once(idx, pool)
        self.assertEqual(pool.state["premium"], XTG1)
        move.assert_not_called()
        ms = acct._mover_state()
        ms["premium_manual"]["at"] -= acct.MANUAL_PREMIUM_GRACE + 1
        acct._save_mover_state(ms)
        move = self.run_once(idx, pool)
        self.assertEqual(move.call_args.args[0][0], (XTG1, "No seat"))

    def test_the_standard_seat_does_not_go_to_an_account_that_would_be_at_a_threshold_on_it(self):
        pool = FakePool("STABLE", SALES)
        # 7-day 40 % (Premium) is about 200 % on Standard; contact's 5-hour is not reset yet
        pool.state["snapshots"] = {XTTS: snap(0, 40), XTG1: snap(0, 40), XTG6: snap(0, 40), CONTACT: snap(95, 10, 2, 90)}
        move = self.run_once(pool_index(**{SALES: (50, 40), INFO: (85, 30)}, premium=SALES), pool)
        move.assert_not_called()
        self.assertEqual(pool.state["backup"], INFO)
        pool.state["snapshots"][CONTACT] = snap(95, 10, -1, 90)  # its 5-hour has reset since
        move = self.run_once(pool_index(**{SALES: (50, 40), INFO: (85, 30)}, premium=SALES), pool)
        self.assertEqual(move.call_args.args[0], [(INFO, "No seat"), (CONTACT, "Standard")])
        self.assertEqual(pool.state["snapshots"][INFO]["five_hour"], 85)  # info's last usage is kept

    def test_the_dashboard_button_moves_premium_through_the_full_flow(self):
        pool = FakePool("STABLE", SALES)
        pool.state["request"] = {"id": "1", "kind": "premium", "email": XTG6, "status": "pending"}
        acct._save_mover_state({"backoff_until": time.time() + 600})  # not held back by a failed automatic move
        move = self.run_once(pool_index(**{SALES: (10, 10)}, premium=SALES), pool)  # below the thresholds
        self.assertEqual(pool.puts, [("PREPARING", SALES, XTG6), ("MOVING", SALES, XTG6), ("STABLE", XTG6, None)])
        self.assertEqual(move.call_args.args[0], [(SALES, "No seat"), (XTG6, "Premium")])
        self.assertEqual((pool.results, pool.state["request"]["status"]), (["running", "ok"], "ok"))
        self.assertEqual(acct._mover_state()["premium_manual"]["email"], XTG6)  # kept like a seat given by hand

    def test_premium_for_the_standard_account_moves_the_standard_seat_first(self):
        pool = FakePool("STABLE", SALES)
        pool.state["request"] = {"id": "1", "kind": "premium", "email": INFO, "status": "pending"}
        move = self.run_once(pool_index(**{SALES: (10, 10)}, premium=SALES), pool)
        self.assertEqual([c.args[0] for c in move.call_args_list],
                         [[(INFO, "No seat"), (CONTACT, "Standard")], [(SALES, "No seat"), (INFO, "Premium")]])
        self.assertEqual((pool.state["premium"], pool.state["backup"]), (INFO, CONTACT))
        self.assertEqual(pool.puts[1:], [("PREPARING", SALES, INFO), ("MOVING", SALES, INFO), ("STABLE", INFO, None)])
        self.assertEqual(pool.state["request"]["status"], "ok")

    def test_a_premium_move_waits_until_an_account_holds_the_standard_seat(self):
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{SALES: (85, 40)}, premium=SALES, standard=None)
        acct._save_mover_state({"backup_backoff_until": time.time() + 600})  # cannot give the Standard seat now
        move = self.run_once(idx, pool)
        move.assert_not_called()  # the Premium seat stays: nowhere for the machines to wait
        self.assertEqual(pool.puts, [])
        acct._save_mover_state({})
        # claude.ai as machine 2 reads it: the seats change when the seat manager changes them
        seats = {e: (idx["accounts"][e].get("seat") or {}).get("tier") for e in acct.POOL_EMAILS}
        labels = {"Premium": acct.PREMIUM_TIER, "Standard": acct.STANDARD_TIER, "No seat": None}

        def read(meta, creds, force=False, refresh_name=None, ttl=None):
            meta["seat"] = {"fetchedAt": time.time(), "tier": seats[meta["profile"]["emailAddress"]]}
            return "ok"

        with patch.object(acct, "refresh_seat", side_effect=read):
            # gives Standard first (before the threshold check), then moves Premium
            self.run_once_raw(idx, pool, lambda changes, cfg: seats.update({e: labels[t] for e, t in changes}))
        self.assertEqual(seats, {XTTS: acct.PREMIUM_TIER, CONTACT: acct.STANDARD_TIER, SALES: None, XTG1: None,
                                 XTG6: None, INFO: None})
        self.assertEqual((pool.state["premium"], pool.state["backup"]), (XTTS, CONTACT))

    def test_a_full_standard_account_hands_its_seat_to_one_with_room_before_premium_moves(self):
        # 17/11: info held Standard at 5-hour 100 %; the machines would have waited on it, blocked
        pool = FakePool("STABLE", SALES)
        pool.state["snapshots"].update({XTTS: snap(0, 50), CONTACT: snap(0, 5), XTG1: snap(0, 10), XTG6: snap(0, 50)})
        acct._save_mover_state({"backup_manual": {"email": INFO, "at": time.time()}})  # not handed on while at rest
        move = self.run_once(pool_index(**{SALES: (85, 40), INFO: (100, 44)}, premium=SALES), pool)
        self.assertEqual([c.args[0] for c in move.call_args_list],
                         [[(INFO, "No seat"), (XTG1, "Standard")], [(SALES, "No seat"), (CONTACT, "Premium")]])
        self.assertEqual(pool.puts[1], ("PREPARING", SALES, CONTACT))
        self.assertEqual(pool.state["backup"], XTG1)

    def test_with_no_account_with_room_on_standard_the_premium_seat_waits_unless_it_is_used_up(self):
        pool = FakePool("STABLE", SALES)
        pool.state["snapshots"].update({XTTS: snap(80, 54), CONTACT: snap(0, 40), XTG1: snap(0, 48), XTG6: snap(27, 44)})
        pool.state["request"] = {"id": "1", "kind": "premium", "email": CONTACT, "status": "pending"}
        acct._save_mover_state({"backup_manual": {"email": INFO, "at": time.time()}})
        idx = pool_index(**{SALES: (22, 43), INFO: (100, 44)}, premium=SALES)
        move = self.run_once(idx, pool)  # the dashboard button, with the numbers of 17/11 16:39
        move.assert_not_called()
        self.assertEqual(pool.puts, [])
        self.assertEqual(pool.state["request"]["status"], "fail")
        self.assertIn("%s, the Standard account, is full (5-hour 100%%)" % INFO, pool.state["request"]["message"])
        # Premium used up: staying blocks everyone for hours, so it moves, the machines waiting on info meanwhile
        pool = FakePool("STABLE", SALES)
        pool.state["snapshots"].update({XTTS: snap(80, 54), CONTACT: snap(0, 40), XTG1: snap(0, 48), XTG6: snap(27, 44)})
        move = self.run_once(pool_index(**{SALES: (100, 43), INFO: (100, 44)}, premium=SALES), pool)
        self.assertEqual(move.call_args.args[0], [(SALES, "No seat"), (CONTACT, "Premium")])
        self.assertEqual(pool.state["backup"], INFO)

    def test_the_dashboard_button_gives_the_standard_seat_to_a_pool_account(self):
        pool = FakePool("STABLE", SALES)
        pool.state["request"] = {"id": "2", "kind": "standard", "email": XTG1, "status": "pending"}
        move = self.run_once(pool_index(**{SALES: (10, 10)}, premium=SALES), pool)
        self.assertEqual(move.call_args.args[0], [(INFO, "No seat"), (XTG1, "Standard")])
        self.assertEqual((pool.state["backup"], pool.state["request"]["status"]), (XTG1, "ok"))

    def test_a_dashboard_request_for_a_wrong_or_unusable_account_fails_without_moving(self):
        idx = pool_index(**{SALES: (10, 10)}, premium=SALES)
        del idx["accounts"][XTG1]
        for kind, email, why in (("premium", CAREERS, "not a pool account"), ("standard", SALES, "holds the Premium seat"),
                                 ("premium", XTG1, "not logged in"), ("premium", SALES, "already holds"),
                                 ("standard", INFO, "already holds")):
            pool = FakePool("STABLE", SALES)
            pool.state["request"] = {"id": "3", "kind": kind, "email": email, "status": "pending"}
            move = self.run_once(idx, pool)
            move.assert_not_called()
            self.assertEqual(pool.state["request"]["status"], "fail")
            self.assertIn(why, pool.state["request"]["message"])

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
        pool.state["backup"] = CONTACT
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
            self.run_once(pool_index(**{SALES: (85, 40)}, premium=SALES, standard=CONTACT), pool)
        log = [json.loads(l) for l in open(os.path.join(acct.store_dir(), "rotation.log"), encoding="utf-8")]
        self.assertIn("still not on %s" % CONTACT, [e for e in log if e["event"] == "prepare_timeout"][-1]["message"])

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

    def test_a_change_claude_ai_applies_late_is_followed_while_backing_off(self):
        # 2026-11-14: the seat manager gave up, claude.ai gave the seat to the target minutes later, and the pool kept
        # every machine on the old account (without a seat) for the 15 minutes machine 2 backed off.
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{SALES: (85, 40)}, premium=SALES)
        self.run_once(idx, pool, move_error=acct.AcctError("did not become Premium"))
        recheck = acct._mover_state()["recheck"]
        self.assertEqual(recheck["emails"][0], SALES)
        target = recheck["emails"][1]
        self.assertGreater(recheck["until"], time.time() + acct.RECHECK_FOR - 5)
        # claude.ai applies the change late: the next readings show it
        idx["accounts"][SALES]["seat"] = {"fetchedAt": time.time(), "tier": None}
        idx["accounts"][target]["seat"] = {"fetchedAt": time.time(), "tier": acct.PREMIUM_TIER}
        pool.puts.clear()
        move = self.run_once(idx, pool)
        self.assertEqual(pool.puts, [("STABLE", target, None)])  # followed at once, not after the back-off
        move.assert_not_called()  # still backing off: no seat is changed

    def test_backing_off_changes_no_seat_but_still_sends_the_usage_table(self):
        acct._save_mover_state({"backoff_until": time.time() + 600})
        pool = FakePool("STABLE", SALES)
        move = self.run_once(pool_index(**{SALES: (95, 40), INFO: (90, 50)}, premium=SALES), pool)  # both spent
        move.assert_not_called()
        self.assertEqual(pool.puts, [])
        self.assertTrue(getattr(pool, "published", []))  # the dashboard does not go stale meanwhile

    def test_after_a_failed_change_its_seats_are_read_every_minute_for_a_while(self):
        acct._note_failed_change(SALES, XTG6)
        ttls = []

        def tier(meta, creds, force=False, refresh_name=None, ttl=None):
            ttls.append((meta["profile"]["emailAddress"], ttl))
            return "ok"

        idx = pool_index(**{SALES: (10, 10)}, premium=SALES)
        with patch.object(acct, "refresh_seat", side_effect=tier):
            self.run_once_raw(idx, FakePool("STABLE", SALES))
        self.assertEqual(ttls[:2], [(SALES, acct.RECHECK_EVERY), (XTG6, acct.RECHECK_EVERY)])
        ms = acct._mover_state()
        ms["recheck"]["until"] = time.time() - 1  # the window is over: back to SEAT_CHECK_EVERY
        acct._save_mover_state(ms)
        ttls.clear()
        with patch.object(acct, "refresh_seat", side_effect=tier):
            self.run_once_raw(idx, FakePool("STABLE", SALES))
        self.assertNotIn(acct.RECHECK_EVERY, [t for _, t in ttls])

    def test_seat_moved_by_hand_during_the_move_is_published_not_moved_again(self):
        pool = FakePool("STABLE", SALES)
        move = self.run_once(pool_index(**{SALES: (85, 40)}, premium=SALES), pool, seat_after={XTG6: acct.PREMIUM_TIER})
        move.assert_not_called()
        self.assertEqual(pool.puts[-1], ("STABLE", XTG6, None))

    def test_no_destination_does_not_start_a_move(self):
        pool = FakePool("STABLE", SALES)
        idx = pool_index(**{SALES: (85, 40)}, premium=SALES)
        for e in acct.POOL_EMAILS:
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
        del idx["accounts"][XTG1]
        with patch.object(acct, "_mover_state", return_value={}):
            self.run_once(idx, pool)
        rows = {r["email"]: r for r in pool.accounts}
        self.assertEqual(len(rows), 6)
        self.assertEqual((rows[SALES]["seat"], rows[SALES]["five_hour"], rows[SALES]["seven_day"]), ("Premium", 50, 40))
        self.assertEqual((rows[INFO]["role"], rows[INFO]["seat"]), ("backup", "Standard"))
        self.assertEqual((rows[CONTACT]["seat"], rows[XTTS]["seat"]), ("No seat", "No seat"))
        self.assertEqual(rows[XTG1]["status"], "not logged in on machine 2")

    def test_a_test_move_runs_the_full_flow_once_below_the_thresholds(self):
        acct.save_pool_settings({"mover_key": "mk", "machine_id": "m2",
                                 "test_move": {"target": XTG6, "until": time.time() + 60}})
        pool = FakePool("STABLE", XTG1)
        move = self.run_once(pool_index(**{XTG1: (31, 32)}, premium=XTG1), pool)
        self.assertEqual(pool.puts, [("PREPARING", XTG1, XTG6), ("MOVING", XTG1, XTG6), ("STABLE", XTG6, None)])
        self.assertEqual(move.call_args.args[0], [(XTG1, "No seat"), (XTG6, "Premium")])
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
        del idx["accounts"][XTG1]
        with Patches(patch.object(acct, "load_index", return_value=idx),
                     patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", side_effect=lambda m, c: "relogin" if m["profile"]["emailAddress"] == XTTS else "ok"),
                     patch.object(acct, "claude_bin", return_value="/bin/claude"),
                     patch.object(acct.sys, "stdout", io.StringIO()),
                     term=patch.object(acct, "open_terminal", return_value=True)) as _p:
            term = _p["term"]
            acct.cmd_login(argparse.Namespace(name=None, email=None))
        script = open(term.call_args.args[0], encoding="utf-8").read()
        self.assertIn("--email xtg1@xtgroup.vn", script.replace("'", ""))
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
        self.assertIn("All 6 pool accounts", out.getvalue())


class RotationPreviewTests(unittest.TestCase):
    def preview(self, idx, state):
        with Patches(*stored(), patch.object(acct, "load_rotation", return_value={"enabled": True}),
                     out=patch.object(acct.sys, "stdout", io.StringIO())) as _p:
            acct._print_rotation_preview(idx, None, state, [])
            return _p["out"].getvalue()

    def test_explains_why_no_account_is_eligible(self):
        idx = pool_index(**{SALES: (85, 46)}, premium=SALES)
        for e in acct.POOL_EMAILS:
            if e != SALES:
                del idx["accounts"][e]
        text = self.preview(idx, {"phase": "STABLE", "premium": SALES, "last_used": {}})
        self.assertIn("Premium %s: 5-hour 85%%, 7-day 46%%; threshold reached" % SALES, text)
        self.assertIn("Next Premium: none below the thresholds", text)
        self.assertIn("%s (not logged in on this machine)" % XTTS, text)

    def test_names_the_next_premium_and_the_order(self):
        idx = pool_index(**{SALES: (85, 46)}, premium=SALES)
        snaps = {XTTS: snap(5, 95, 2, 90), CONTACT: snap(10, 50, 2, 90), XTG1: snap(10, 50, 1, 90)}
        text = self.preview(idx, {"phase": "STABLE", "premium": SALES, "backup": INFO, "snapshots": snaps})
        self.assertIn("Next Premium: **%s**" % XTG1, text)  # never measured ones are not taken
        self.assertIn(" → ".join([XTG1, CONTACT, XTTS, XTG6]) + ")", text)  # info holds Standard: not in the order


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
