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

P = acct.PREMIUM_EMAIL  # sales@xtgroup.vn: Premium, xtgroup.vn
T1, T2 = acct.FIXED_STANDARD_EMAILS  # truongnv@, xtg2@xtgroup.vn: Standard for good
X0, X1, X2, X3 = acct.SWAP_EMAILS  # xtg@, xtg1@, xtg2@, xtg3@xtgroup.com.vn: 2 Standard seats
XTTS = "xtts@xtgroup.vn"  # an account of the pool before 1.0.33
CREDS = {"claudeAiOauth": {"refreshToken": "r"}}
SEATS = {P: acct.PREMIUM_TIER, T1: acct.STANDARD_TIER, T2: acct.STANDARD_TIER, X0: acct.STANDARD_TIER,
         X1: acct.STANDARD_TIER}  # X2, X3: no seat
ORG_B = "0b0b0b0b-0000-4000-8000-000000000000"


def iso_in(hours):
    return None if hours is None else dt.datetime.fromtimestamp(time.time() + hours * 3600, dt.timezone.utc).isoformat()


def usage_data(u):
    """(5h, 7d[, hours until the 5h resets, hours until the 7d resets]) → the usage API's answer."""
    five_in, seven_in = u[2] if len(u) > 2 else 3, u[3] if len(u) > 3 else 90
    return {"five_hour": {"utilization": u[0], "resets_at": iso_in(five_in)},
            "seven_day": {"utilization": u[1], "resets_at": iso_in(seven_in)}}


def account(five_hour, seven_day, email="member@example.com", tier="team_standard", reset=None):
    seven = {"utilization": seven_day}
    if reset:
        seven["resets_at"] = reset
    return {
        "profile": {"emailAddress": email},
        "usage": {"fetchedAt": time.time(), "data": {"five_hour": {"utilization": five_hour}, "seven_day": seven}},
        "seat": {"fetchedAt": time.time(), "tier": tier},
    }


def pool_index(emails=None):
    """Every pool account saved under its email (no seat or usage read yet)."""
    return {"accounts": {e: {"profile": {"emailAddress": e,
                                         "organizationUuid": ORG_B if e.endswith(".com.vn") else "org-a"}}
                         for e in (acct.POOL_EMAILS if emails is None else emails)}}


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


def snap(five, seven, five_in=None, seven_in=None, tier="standard"):
    """A usage snapshot; five_in/seven_in: hours until that window resets (negative: already reset)."""
    return {"tier": tier, "five_hour": five, "five_hour_reset": iso_in(five_in),
            "seven_day": seven, "seven_day_reset": iso_in(seven_in), "at": time.time() - 60}


def rows(usage=None, seated=(P, T1, T2, X0, X1), why=None, seat=None):
    """Pool rows as _read_pool returns them. usage={email: (5h, 7d[, hours to the 5h reset, to the 7d reset])},
    default 0 %; seated: the accounts with their seat; why={email: why the machines cannot use it};
    seat={email: seat}, to override (None: unknown)."""
    at = lambda h: None if h is None else time.time() + h * 3600
    out = []
    for pos, e in enumerate(acct.POOL_EMAILS):
        u = tuple((usage or {}).get(e, (0, 0))) + (None, None)
        s = ("Premium" if e == P else "Standard") if e in seated else "No seat"
        s = (seat or {}).get(e, s)
        out.append({"email": e, "pos": pos, "name": e, "seat": s,
                    "est": (float(u[0]), at(u[2]), float(u[1]), at(u[3])), "why": (why or {}).get(e), "status": None,
                    "fresh": False, "estimated": s not in ("Premium", "Standard"), "measured_at": None, "read_at": None,
                    "login_expires": None})
    return out


class ThresholdTests(unittest.TestCase):
    def test_an_account_is_left_at_80_or_90_and_near_them_10_points_before(self):
        self.assertFalse(acct._over((79, None, 89, None)))
        self.assertTrue(acct._over((80, None, 0, None)))
        self.assertTrue(acct._over((0, None, 90, None)))
        self.assertTrue(acct._over((100, None, 0, None)))
        self.assertFalse(acct._near((69, None, 79, None)))
        self.assertTrue(acct._near((70, None, 0, None)))
        self.assertTrue(acct._near((0, None, 80, None)))

    def test_pool_is_fixed(self):
        self.assertEqual(acct.POOL_EMAILS, ("sales@xtgroup.vn", "truongnv@xtgroup.vn", "xtg2@xtgroup.vn",
                                            "xtg@xtgroup.com.vn", "xtg1@xtgroup.com.vn", "xtg2@xtgroup.com.vn",
                                            "xtg3@xtgroup.com.vn"))
        self.assertEqual((acct.DAILY_THRESHOLD, acct.WEEKLY_THRESHOLD, acct.PREPARE_MARGIN), (80, 90, 10))
        self.assertEqual(acct.SWAP_SEATS, 2)
        self.assertFalse(set(acct.FORMER_POOL_EMAILS) & set(acct.POOL_EMAILS))
        self.assertIn(XTTS, acct.FORMER_POOL_EMAILS)

    def test_a_window_past_its_reset_counts_as_0_and_a_never_read_account_as_0(self):
        self.assertEqual(acct._estimate(snap(95, 92, -1, -2))[::2], (0.0, 0.0))
        self.assertEqual(acct._estimate(snap(95, 92, 1, -2))[::2], (95.0, 0.0))
        self.assertIsNone(acct._estimate({}))
        self.assertEqual(acct.NEVER_READ[::2], (0.0, 0.0))

    def test_the_weekly_limit_date_comes_from_claude_ai_s_message(self):
        msg = ("Seat change failed. xtg2@xtgroup.com.vn -> Standard: This organization has reached its weekly limit on "
               "moving this seat type between people. An open seat may still go to someone who had this seat type "
               "recently. You can try again after 2026-11-24 (UTC). [org_weekly_seat_limit_reached/pass_backs]")
        self.assertEqual(acct._limit_until(msg), dt.datetime(2026, 11, 24, tzinfo=dt.timezone.utc).timestamp())
        self.assertIsNone(acct._limit_until("Seat change failed. HTTP 500"))
        monday = dt.datetime(2026, 11, 16, tzinfo=dt.timezone.utc).timestamp()
        self.assertEqual(acct._week_start(monday + 3 * 86400 + 5), monday)
        self.assertEqual(acct._limit_until("weekly limit on how many people", monday + 86400), monday + 7 * 86400)


class ChooseActiveTests(unittest.TestCase):
    """Which account every machine works on (_choose_active)."""

    def test_premium_while_it_is_below_the_thresholds(self):
        self.assertEqual(acct._choose_active(rows({P: (79, 89)}), T1), (P, "Premium"))

    def test_premium_at_a_threshold_the_best_standard_account_with_a_seat(self):
        r = rows({P: (80, 40, 2, 90), T1: (0, 30), T2: (0, 10), X0: (0, 20), X1: (0, 25)})
        email, why = acct._choose_active(r, P)
        self.assertEqual(email, T2)  # the lowest 7-day
        self.assertIn("Premium at a threshold until", why)
        # never an account without a seat, even at 0 %, nor one machine 2 cannot use
        r = rows({P: (85, 40)}, why={T1: "log in again", T2: "not logged in on machine 2", X0: "log in again",
                                     X1: "log in again"})
        email, why = acct._choose_active(r, P)
        self.assertEqual(email, P)  # at a threshold, but nothing else to work on
        self.assertIn("every account with a seat is at a threshold", why)

    def test_the_current_standard_account_is_kept_until_it_reaches_a_threshold(self):
        r = rows({P: (85, 40), T1: (60, 70), T2: (0, 0)})
        self.assertEqual(acct._choose_active(r, T1)[0], T1)
        r = rows({P: (85, 40), T1: (80, 70), T2: (0, 5), X0: (0, 3), X1: (0, 10)})
        self.assertEqual(acct._choose_active(r, T1)[0], X0)

    def test_back_to_premium_once_its_window_resets(self):
        self.assertEqual(acct._choose_active(rows({P: (0, 41), T1: (10, 10)}), T1)[0], P)

    def test_an_account_picked_on_the_dashboard_while_it_is_below_the_thresholds(self):
        self.assertEqual(acct._choose_active(rows({X1: (50, 50)}), P, pinned=X1), (X1, "picked on the dashboard"))
        self.assertEqual(acct._choose_active(rows({X1: (80, 50)}), P, pinned=X1)[0], P)
        self.assertEqual(acct._choose_active(rows(), P, pinned=X2)[0], P)  # no seat: not usable

    def test_every_account_with_a_seat_at_a_threshold_the_current_one_unless_it_used_a_window_up(self):
        usage = {P: (85, 40), T1: (85, 50), T2: (81, 20), X0: (90, 95), X1: (100, 10)}
        email, why = acct._choose_active(rows(usage), T1)
        self.assertEqual(email, T1)
        self.assertIn("every account with a seat is at a threshold", why)
        self.assertEqual(acct._choose_active(rows(usage), X1)[0], T2)  # X1 used a window up: the best ranked one
        usage = {e: (100, 50) for e in acct.POOL_EMAILS}
        self.assertEqual(acct._choose_active(rows(usage), T1)[0], None)

    def test_a_premium_account_without_its_seat_or_unreadable_is_skipped(self):
        email, why = acct._choose_active(rows(seat={P: "No seat"}), P)
        self.assertEqual((email, why), (T1, "Premium %s: it has no Premium seat" % P))
        r = rows()
        r[0]["est"], r[0]["status"] = None, "rate limited until 10:00"
        self.assertIn("usage unknown (rate limited", acct._choose_active(r, P)[1])


class PlanSwapTests(unittest.TestCase):
    """When machine 2 moves a seat of xtgroup.com.vn (_plan_swap): the active account near a threshold and no other
    account with a seat below that level; then from the swap account with the least left to the free one with the
    most room."""

    NEAR = {P: (85, 40, 3, 90), T1: (85, 30), T2: (75, 20), X0: (72, 50), X1: (82, 60)}

    def test_from_the_swap_account_with_the_least_left_to_the_free_one_with_the_most_room(self):
        frm, to, why = acct._plan_swap(rows(self.NEAR), X0)
        self.assertEqual((frm, to), (X1, X2))  # X2, X3 never read: 0 %, the list order
        self.assertIn("near a threshold", why)
        usage = dict(self.NEAR, **{X2: (0, 30)})
        self.assertEqual(acct._plan_swap(rows(usage), X0)[:2], (X1, X3))

    def test_never_the_seat_of_the_active_account(self):
        usage = dict(self.NEAR, **{X0: (85, 60), X1: (72, 50)})
        self.assertEqual(acct._plan_swap(rows(usage), X1)[:2], (X0, X2))

    def test_nothing_while_the_active_one_is_not_near_or_another_one_with_a_seat_has_room(self):
        self.assertEqual(acct._plan_swap(rows(dict(self.NEAR, **{X0: (69, 79)})), X0), (None, None, None))
        self.assertEqual(acct._plan_swap(rows(dict(self.NEAR, **{T2: (69, 20)})), X0), (None, None, None))
        self.assertEqual(acct._plan_swap(rows(self.NEAR, why={X0: "log in again"}), X0), (None, None, None))

    def test_nothing_when_premium_is_back_within_30_minutes(self):
        usage = dict(self.NEAR, **{P: (85, 40, 0.4, 90)})
        frm, to, why = acct._plan_swap(rows(usage), X0)
        self.assertEqual((frm, to), (None, None))
        self.assertIn("Premium is back at", why)
        usage = dict(self.NEAR, **{P: (85, 95, 0.4, 50)})  # its 7-day is at a threshold for days
        self.assertEqual(acct._plan_swap(rows(usage), X0)[:2], (X1, X2))

    def test_premium_near_a_threshold_too(self):
        usage = dict(self.NEAR, **{P: (72, 40, 2, 90)})
        self.assertEqual(acct._plan_swap(rows(usage), P)[:2], (X1, X2))
        usage = dict(self.NEAR, **{P: (72, 40, 0.3, 90)})  # its 5-hour resets in 18 minutes
        self.assertEqual(acct._plan_swap(rows(usage), P)[:2], (None, None))

    def test_nothing_when_no_free_account_has_room(self):
        usage = dict(self.NEAR, **{X2: (70, 0), X3: (0, 80)})
        frm, to, why = acct._plan_swap(rows(usage), X0)
        self.assertEqual((frm, to), (None, None))
        self.assertIn("no account of xtgroup.com.vn without a seat has room", why)
        self.assertEqual(acct._plan_swap(rows(self.NEAR, why={X2: "not logged in on machine 2"}), X0)[:2], (X1, X3))

    def test_a_free_seat_is_given_without_taking_one(self):
        self.assertEqual(acct._plan_swap(rows(self.NEAR, seated=(P, T1, T2, X0)), X0)[:2], (None, X2))

    def test_nothing_while_the_seat_of_a_swap_account_is_unknown(self):
        frm, to, why = acct._plan_swap(rows(self.NEAR, seated=(P, T1, T2, X0), seat={X1: None}), X0)
        self.assertEqual((frm, to), (None, None))
        self.assertIn("seat of %s is unknown" % X1, why)


class ReadPoolTests(unittest.TestCase):
    """Machine 2 reads the seat of every account and the usage of the ones with a seat."""

    def read(self, idx, seats, usage, snaps=None, relogin=()):
        asked = []

        def seat(meta, creds, force=False, refresh_name=None, ttl=None):
            meta["seat"] = {"fetchedAt": time.time(), "tier": seats.get(meta["profile"]["emailAddress"])}
            return "ok"

        def read_usage(meta, creds, force=False, refresh_name=None, ttl=None):
            asked.append(meta["profile"]["emailAddress"])
            meta["usage"] = {"fetchedAt": time.time(), "data": usage_data(usage.get(meta["profile"]["emailAddress"], (0, 0)))}
            return "ok"

        with Patches(patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", side_effect=lambda m, c: "relogin" if m["profile"]["emailAddress"] in relogin else "ok"),
                     patch.object(acct, "refresh_seat", side_effect=seat),
                     patch.object(acct, "refresh_usage", side_effect=read_usage)):
            return acct._read_pool(idx, None, {"snapshots": snaps or {}}), asked

    def test_usage_is_read_only_for_accounts_with_a_seat_the_others_keep_their_last_reading(self):
        r, asked = self.read(pool_index(), SEATS, {P: (50, 20), X1: (30, 40)},
                             snaps={X2: snap(60, 50, -1, 20), X3: snap(20, 95, 1, -1)})
        self.assertEqual(asked, [P, T1, T2, X0, X1])
        by = {x["email"]: x for x in r}
        self.assertEqual((by[P]["seat"], by[P]["est"][::2], by[P]["fresh"]), ("Premium", (50.0, 20.0), True))
        self.assertEqual((by[X2]["seat"], by[X2]["est"][::2], by[X2]["estimated"]), ("No seat", (0.0, 50.0), True))
        self.assertEqual(by[X3]["est"][::2], (20.0, 0.0))
        r, _ = self.read(pool_index(), SEATS, {})
        self.assertEqual(r[-1]["est"], acct.NEVER_READ)

    def test_an_account_not_logged_in_or_to_log_in_again_cannot_be_used(self):
        idx = pool_index()
        del idx["accounts"][T1]
        r, asked = self.read(idx, SEATS, {}, relogin=(X0,))
        by = {x["email"]: x for x in r}
        self.assertEqual((by[T1]["why"], by[X0]["why"]), ("not logged in on machine 2", "log in again"))
        self.assertNotIn(X0, asked)
        self.assertFalse(acct._ready(by[X0]))


class WorkerTests(unittest.TestCase):
    """Hooks on every working machine switch account; they never move a seat."""

    def run_check(self, cur, state=None, error=None, idx=None, manual=False):
        idx = idx or pool_index()
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
                     patch.object(acct, "_spawn_log_flush"),
                     patch.object(acct.sys, "stdout", io.StringIO()),
                     move=patch.object(acct, "_set_seats")) as _p:
            acct._worker_check(argparse.Namespace(force=False, manual=manual))
        _p["move"].assert_not_called()
        return switched, reports

    def test_switches_to_the_active_account_and_reports_it(self):
        switched, reports = self.run_check(P, {"active": X1, "fallbacks": [T2]})
        self.assertEqual(switched, [X1])
        self.assertEqual(reports[-1], (X1, True))

    def test_already_on_the_active_account_stays(self):
        self.assertEqual(self.run_check(X1, {"active": X1, "fallbacks": [P]})[0], [])

    def test_the_next_one_logged_in_here_when_the_active_one_is_not(self):
        idx = pool_index([e for e in acct.POOL_EMAILS if e != X3])
        self.assertEqual(self.run_check(P, {"active": X3, "fallbacks": [T1, T2]}, idx=idx)[0], [T1])
        self.assertEqual(self.run_check(T2, {"active": X3, "fallbacks": [T2, T1]}, idx=idx)[0], [])
        self.assertEqual(self.run_check(P, {"active": X3, "fallbacks": []}, idx=idx)[0], [])

    def test_a_login_outside_the_pool_is_left_alone_and_one_of_a_former_pool_goes_to_the_active_one(self):
        idx = pool_index()
        idx["accounts"]["personal"] = account(0, 0, "me@gmail.com")
        idx["accounts"]["old"] = account(0, 0, XTTS)
        self.assertEqual(self.run_check("personal", {"active": P}, idx=idx), ([], []))
        self.assertEqual(self.run_check("old", {"active": P}, idx=idx)[0], [P])

    def test_nothing_changes_while_the_pool_server_is_down_or_has_no_active_account(self):
        self.assertEqual(self.run_check(X0, error="pool server unreachable: timed out")[0], [])
        self.assertEqual(self.run_check(X0, {"phase": "STABLE", "premium": P})[0], [])  # before 1.0.33's machine 2

    def test_same_local_part_in_both_organizations_is_told_apart_by_the_whole_email(self):
        self.assertEqual(self.run_check(T2, {"active": X2})[0], [X2])
        self.assertEqual(self.run_check(X2, {"active": T2})[0], [T2])


class FakePool:
    """The pool server as machine 2 sees it."""

    def __init__(self, active=None, request=None):
        self.state = {"phase": "STABLE", "premium": P, "active": active, "fallbacks": None, "seated": None,
                      "message": None, "snapshots": {}, "request": request}
        self.puts, self.accounts, self.pool, self.results = [], None, None, []

    def __call__(self, method, path, body=None, mover=False, timeout=4):
        if path == "/api/machine":
            return dict(self.state)
        if path == "/api/state" and method == "PUT":
            self.puts.append(body)
            self.state.update({k: body[k] for k in ("active", "fallbacks", "seated", "message")})
            return dict(self.state)
        if path == "/api/accounts":
            self.accounts, self.pool = body["accounts"], body["pool"]
            return "ok"
        if path == "/api/snapshots":
            self.state["snapshots"] = dict(self.state["snapshots"], **body["snapshots"])
            return "ok"
        if path == "/api/request/result":
            self.state["request"] = dict(self.state["request"], status=body["status"], message=body.get("message"))
            self.results.append((body["status"], body.get("message")))
            return "ok"
        raise AssertionError(path)


class Claude:
    """claude.ai as machine 2 reads it: the seat of each account and the usage of the ones with a seat."""

    def __init__(self, usage=None, seats=None, refuse=None):
        self.seats = dict(SEATS if seats is None else seats)
        self.usage = dict(usage or {})
        self.refuse, self.changes = refuse, []

    def seat(self, meta, creds, force=False, refresh_name=None, ttl=None):
        meta["seat"] = {"fetchedAt": time.time(), "tier": self.seats.get(meta["profile"]["emailAddress"])}
        return "ok"

    def read_usage(self, meta, creds, force=False, refresh_name=None, ttl=None):
        email = meta["profile"]["emailAddress"]
        meta["usage"] = {"fetchedAt": time.time(), "data": usage_data(self.usage.get(email, (0, 0)))}
        return "ok"

    def set_seats(self, changes, cfg, org):
        self.changes.append((changes, org))
        if self.refuse:
            raise acct.AcctError(self.refuse)
        for email, tier in changes:
            self.seats[email] = {"No seat": None, "Standard": acct.STANDARD_TIER, "Premium": acct.PREMIUM_TIER}[tier]


LIMIT_DAY = (dt.datetime.now(dt.timezone.utc) + dt.timedelta(days=6)).replace(hour=0, minute=0, second=0, microsecond=0)
LIMIT = ("Seat change failed. xtg2@xtgroup.com.vn -> Standard: This organization has reached its weekly limit on moving "
         "this seat type between people. An open seat may still go to someone who had this seat type recently. You can "
         "try again after %s (UTC). [org_weekly_seat_limit_reached/pass_backs]" % LIMIT_DAY.strftime("%Y-%m-%d"))


class MoverTests(unittest.TestCase):
    """The service on machine 2 picks the active account and moves the seats of xtgroup.com.vn; it never switches
    account itself."""

    def setUp(self):
        acct.save_pool_settings({"mover_key": "mk", "machine_id": "m2"})
        for f in ("mover-state.json", "log-state.json"):
            try:
                os.unlink(os.path.join(acct.store_dir(), f))
            except FileNotFoundError:
                pass

    def tearDown(self):
        acct.save_pool_settings({})

    def run_once(self, pool, claude, idx=None):
        idx = idx or pool_index()
        with Patches(patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", return_value="ok"),
                     patch.object(acct, "refresh_seat", side_effect=claude.seat),
                     patch.object(acct, "refresh_usage", side_effect=claude.read_usage),
                     patch.object(acct, "load_index", return_value=idx), patch.object(acct, "save_index"),
                     patch.object(acct, "current_name", return_value=None),
                     patch.object(acct, "pool_call", side_effect=pool),
                     patch.object(acct, "_set_seats", side_effect=claude.set_seats),
                     patch.object(acct.sys, "stdout", io.StringIO()),
                     patch.object(acct, "_spawn_log_flush"),
                     switch=patch.object(acct, "cmd_switch")) as p:
            acct._mover_once(argparse.Namespace(force=False, manual=False))
        p["switch"].assert_not_called()
        return idx

    def test_premium_below_the_thresholds_is_published_once_with_the_accounts_with_a_seat(self):
        pool, claude = FakePool(), Claude({P: (40, 20), T1: (0, 30), T2: (0, 10)})
        self.run_once(pool, claude)
        self.assertEqual(len(pool.puts), 1)
        put = pool.puts[0]
        self.assertEqual((put["active"], put["premium"], put["message"]), (P, P, "Premium"))
        self.assertEqual(put["seated"], [P, T1, T2, X0, X1])
        self.assertEqual(put["fallbacks"][:2], [X0, X1])  # 0 %, then the list order; T2 10 %, T1 30 %
        self.assertEqual(claude.changes, [])
        self.assertEqual([a["email"] for a in pool.accounts], list(acct.POOL_EMAILS))
        self.assertEqual(pool.state["snapshots"][P]["five_hour"], 40)  # kept for when it has no seat
        self.run_once(pool, claude)
        self.assertEqual(len(pool.puts), 1)  # nothing changed: nothing published

    def test_premium_at_a_threshold_every_machine_goes_to_the_best_standard_account_and_back_after_its_reset(self):
        pool, claude = FakePool(active=P), Claude({P: (80, 20, 2), T1: (0, 30), T2: (0, 10), X0: (5, 40), X1: (0, 50)})
        self.run_once(pool, claude)
        self.assertEqual(pool.state["active"], T2)
        self.assertIn("Premium at a threshold until", pool.state["message"])
        claude.usage[T2] = (60, 12)  # still below: kept
        claude.usage[T1] = (0, 0)
        self.run_once(pool, claude)
        self.assertEqual(pool.state["active"], T2)
        claude.usage[P] = (0, 21)  # its 5-hour reset
        self.run_once(pool, claude)
        self.assertEqual((pool.state["active"], pool.state["message"]), (P, "Premium"))
        self.assertEqual(claude.changes, [])

    def near(self):
        """Premium at a threshold for 3 hours; the active account X0 near one; every other one with a seat too."""
        return Claude({P: (85, 40, 3), T1: (85, 30), T2: (75, 20), X0: (72, 50), X1: (82, 60)})

    def test_a_seat_moves_before_the_machines_need_it_and_only_once(self):
        pool, claude = FakePool(active=X0), self.near()
        self.run_once(pool, claude)
        self.assertEqual(claude.changes, [([(X1, "No seat"), (X2, "Standard")], ORG_B)])
        self.assertEqual(pool.state["active"], X0)  # it is still below the thresholds
        self.assertIn(X2, pool.state["seated"])
        self.assertNotIn(X1, pool.state["seated"])
        self.assertEqual(pool.state["fallbacks"], [X2, T2])  # T2 75 %: near, but still below the thresholds
        self.assertEqual(pool.pool["swaps_week"], 1)
        self.assertEqual(pool.state["snapshots"][X1]["five_hour"], 82)  # its last reading, kept without a seat
        self.run_once(pool, claude)
        self.assertEqual(len(claude.changes), 1)  # X2 has room now
        claude.usage[X0] = (80, 50)
        self.run_once(pool, claude)
        self.assertEqual(pool.state["active"], X2)  # it holds the seat already

    def test_a_weekly_limit_stops_seat_moves_until_the_date_claude_ai_gives(self):
        pool, claude = FakePool(active=X0), self.near()
        claude.refuse = LIMIT
        self.run_once(pool, claude)
        self.run_once(pool, claude)
        self.assertEqual(len(claude.changes), 1)
        ms = acct._mover_state()
        self.assertEqual(ms["swap_blocked_until"], LIMIT_DAY.timestamp())
        self.assertTrue(pool.pool["swap_limit"])
        self.assertIn("weekly limit", pool.pool["swap_blocked"])
        self.assertIn("but claude.ai refuses seat moves until %s" % LIMIT_DAY.strftime("%d/%m 00:00 UTC"),
                      pool.pool["swap_note"])
        self.assertEqual(pool.state["active"], X0)  # the machines go on

    def test_another_failure_waits_15_minutes(self):
        pool, claude = FakePool(active=X0), self.near()
        claude.refuse = "Seat change failed. The admin browser is not signed in to claude.ai."
        self.run_once(pool, claude)
        ms = acct._mover_state()
        self.assertAlmostEqual(ms["swap_blocked_until"], time.time() + acct.MOVE_BACKOFF, delta=5)
        self.assertFalse(ms["swap_limit"])
        ms["swap_blocked_until"] = time.time() - 1
        acct._save_mover_state(ms)
        claude.refuse = None
        self.run_once(pool, claude)
        self.assertEqual(len(claude.changes), 2)
        self.assertNotIn("swap_error", acct._mover_state())

    def test_no_seat_moves_when_premium_is_back_within_30_minutes(self):
        pool, claude = FakePool(active=X0), self.near()
        claude.usage[P] = (85, 40, 0.3)
        self.run_once(pool, claude)
        self.assertEqual(claude.changes, [])
        self.assertIn("Premium is back at", pool.pool["swap_note"])

    def test_the_dashboard_puts_every_machine_on_an_account_until_it_reaches_a_threshold(self):
        pool = FakePool(active=P, request={"id": "1", "kind": "use", "email": X1, "status": "pending"})
        claude = Claude({X1: (10, 10)})
        self.run_once(pool, claude)
        self.assertEqual((pool.state["active"], pool.state["message"]), (X1, "picked on the dashboard"))
        self.assertEqual(pool.results[-1][0], "ok")
        self.run_once(pool, claude)
        self.assertEqual(pool.state["active"], X1)
        claude.usage[X1] = (80, 10)
        self.run_once(pool, claude)
        self.assertEqual(pool.state["active"], P)
        self.assertNotIn("pin", acct._mover_state())

    def test_the_dashboard_gives_a_seat_to_an_account_without_one(self):
        pool = FakePool(active=P, request={"id": "2", "kind": "seat", "email": X3, "status": "pending"})
        claude = Claude({X0: (10, 10), X1: (50, 60)})
        self.run_once(pool, claude)
        self.assertEqual(claude.changes, [([(X1, "No seat"), (X3, "Standard")], ORG_B)])
        self.assertEqual(pool.results, [("running", None), ("ok", "%s now holds a Standard seat (taken from %s)" % (X3, X1))])
        self.assertIn(X3, pool.state["seated"])

    def test_a_dashboard_request_that_cannot_be_done_fails_without_moving(self):
        for kind, email, why in (("seat", X1, "holds a seat already"), ("seat", T1, "is not an account of xtgroup.com.vn"),
                                 ("use", X2, "has no seat"), ("use", "nobody@x.vn", "is not a pool account")):
            pool = FakePool(active=P, request={"id": "3", "kind": kind, "email": email, "status": "pending"})
            claude = Claude()
            self.run_once(pool, claude)
            self.assertEqual(claude.changes, [])
            self.assertEqual(pool.results[-1][0], "fail")
            self.assertIn(why, pool.results[-1][1])

    def test_the_dashboard_table_has_every_account_its_organization_and_the_next_one_to_get_a_seat(self):
        pool = FakePool(active=P)
        pool.state["snapshots"] = {X2: snap(10, 30, 1, 50), X3: snap(0, 5, 1, 50)}
        self.run_once(pool, Claude({P: (40, 20)}))
        by = {a["email"]: a for a in pool.accounts}
        self.assertEqual((by[P]["org"], by[P]["role"], by[P]["active"], by[P]["seat"]), ("xtgroup.vn", "premium", True, "Premium"))
        self.assertEqual((by[T1]["role"], by[X0]["org"], by[X0]["role"]), ("standard", "xtgroup.com.vn", "swap"))
        self.assertEqual((by[X2]["estimated"], by[X2]["five_hour"], by[X2]["seven_day"]), (True, 10, 30))
        self.assertTrue(by[X3]["next_seat"])
        self.assertFalse(by[X2]["next_seat"])
        self.assertEqual(pool.pool["thresholds"], {"five_hour": 80, "seven_day": 90, "margin": 10})

    def test_needs_the_mover_key(self):
        acct.save_pool_settings({"api_key": "k", "machine_id": "m"})
        with self.assertRaises(acct.AcctError):
            acct._mover_once(argparse.Namespace(force=False, manual=False))


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

    def test_the_seat_manager_gets_the_organization_and_time_for_a_rollback(self):
        ok = acct.subprocess.CompletedProcess([], 0, "Seats set: a -> Standard.", "")
        with patch.object(acct, "ensure_node_deps"), patch.object(acct, "seat_manager_env", return_value={}), \
             patch.object(acct.subprocess, "run", return_value=ok) as run, patch.object(acct.sys, "stdout", io.StringIO()):
            acct._set_seats([("a@x.vn", "No seat"), ("b@x.vn", "Standard")], {}, ORG_B)
        self.assertEqual(run.call_args.args[0][2:], ["--org", ORG_B, "a@x.vn=No seat", "b@x.vn=Standard"])
        self.assertEqual(run.call_args.kwargs["timeout"], acct.SEAT_CHANGE_TIMEOUT)
        self.assertLess(acct.SEAT_CHANGE_TIMEOUT + 120, 600)  # within the mover lock, with the readings around it

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



class JoinAndServiceTests(unittest.TestCase):
    def tearDown(self):
        acct.save_pool_settings({})

    def test_join_saves_the_key_and_turns_switching_on(self):
        with Patches(patch.object(acct, "pool_call", return_value={"phase": "STABLE", "premium": P, "active": X1}),
                     out=patch.object(acct.sys, "stdout", io.StringIO())) as _p:
            out = _p["out"]
            acct.cmd_auto_join(argparse.Namespace(key=" k1 ", url="http://10.0.0.1:5680/"))
        st = acct.pool_settings()
        self.assertEqual((st["api_key"], st["url"]), ("k1", "http://10.0.0.1:5680"))
        self.assertTrue(acct.load_rotation()["enabled"])
        self.assertIn(X1, out.getvalue())

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
        del idx["accounts"][X1]
        with Patches(patch.object(acct, "load_index", return_value=idx),
                     patch.object(acct, "read_stored_creds", return_value=CREDS),
                     patch.object(acct, "token_state", side_effect=lambda m, c: "relogin" if m["profile"]["emailAddress"] == T1 else "ok"),
                     patch.object(acct, "claude_bin", return_value="/bin/claude"),
                     out=patch.object(acct.sys, "stdout", io.StringIO()),
                     term=patch.object(acct, "open_terminal", return_value=True)) as _p:
            acct.cmd_login(argparse.Namespace(name=None, email=None))
        script = open(_p["term"].call_args.args[0], encoding="utf-8").read().replace("'", "")
        self.assertIn("--email %s" % X1, script)
        self.assertIn("--email %s" % T1, script)
        self.assertNotIn("--email %s" % P, script)
        self.assertIn("(1/2)", script)
        self.assertIn("while they hold one", _p["out"].getvalue())

    def test_nothing_to_do_when_every_pool_account_is_logged_in(self):
        with Patches(*stored(), patch.object(acct, "load_index", return_value=pool_index()),
                     term=patch.object(acct, "open_terminal"),
                     out=patch.object(acct.sys, "stdout", io.StringIO())) as _p:
            acct.cmd_login(argparse.Namespace(name=None, email=None))
        _p["term"].assert_not_called()
        self.assertIn("All 7 pool accounts", _p["out"].getvalue())


class RotationPreviewTests(unittest.TestCase):
    def preview(self, idx, state):
        with Patches(*stored(), patch.object(acct, "load_rotation", return_value={"enabled": True}),
                     out=patch.object(acct.sys, "stdout", io.StringIO())) as _p:
            acct._print_rotation_preview(idx, None, state, [])
            return _p["out"].getvalue()

    def test_names_the_active_account_the_next_ones_and_those_not_logged_in_here(self):
        idx = pool_index([e for e in acct.POOL_EMAILS if e != X2])
        text = self.preview(idx, {"active": X0, "message": "Premium at a threshold until 18/11 10:00 UTC",
                                  "fallbacks": [X2, T2], "seated": [P, T1, T2, X0, X2]})
        self.assertIn("Active account (every machine works on it): %s (Premium at a threshold" % X0, text)
        self.assertIn("Next, below the thresholds: %s, %s" % (X2, T2), text)
        self.assertIn("Not logged in on this machine: %s" % X2, text)

    def test_says_when_machine_2_has_not_published_yet(self):
        self.assertIn("not published yet", self.preview(pool_index(), {"phase": "STABLE", "premium": P}))


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
