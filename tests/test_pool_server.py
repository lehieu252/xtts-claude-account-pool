import base64
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SPEC = importlib.util.spec_from_file_location("pool_server", os.path.join(ROOT, "server", "pool_server.py"))
pool = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(pool)

API, MOVER, PASS = "api-key", "mover-key", "view-pass"


class PoolServerTests(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        cfg = pool.Config({"POOL_DATA": self.dir, "POOL_API_KEY": API, "POOL_MOVER_KEY": MOVER,
                           "POOL_VIEW_PASS": PASS, "POOL_BASE": "/pool"})
        self.store = pool.Store(os.path.join(self.dir, "pool.sqlite"))
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), pool.make_handler(cfg, self.store))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.base = "http://127.0.0.1:%d" % self.httpd.server_address[1]

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()
        shutil.rmtree(self.dir)

    def call(self, method, path, body=None, key=None, mover=None, viewer=None, headers=None):
        req = urllib.request.Request(self.base + path, method=method,
                                     data=None if body is None else json.dumps(body).encode())
        if body is not None:
            req.add_header("Content-Type", "application/json")
        if key:
            req.add_header("X-Pool-Key", key)
        if mover:
            req.add_header("X-Pool-Mover-Key", mover)
        if viewer:
            req.add_header("Authorization", "Basic " + base64.b64encode(viewer.encode()).decode())
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                raw = r.read().decode()
                return r.status, (json.loads(raw) if r.headers.get_content_type() == "application/json" else raw)
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read().decode() or "{}")

    def test_health_needs_no_key_and_works_with_or_without_the_pool_prefix(self):
        self.assertEqual(self.call("GET", "/healthz"), (200, "ok"))
        self.assertEqual(self.call("GET", "/pool/healthz"), (200, "ok"))

    def test_state_needs_a_key(self):
        self.assertEqual(self.call("GET", "/api/state")[0], 403)
        self.assertEqual(self.call("GET", "/api/state", key="wrong")[0], 403)
        code, st = self.call("GET", "/pool/api/state", key=API)
        self.assertEqual((code, st["phase"]), (200, "STABLE"))

    def test_only_the_mover_key_writes_the_state(self):
        body = {"phase": "PREPARING", "premium": "a@x.vn", "target": "b@x.vn"}
        self.assertEqual(self.call("PUT", "/api/state", body, key=API)[0], 403)
        code, st = self.call("PUT", "/api/state", body, mover=MOVER)
        self.assertEqual((code, st["phase"], st["target"]), (200, "PREPARING", "b@x.vn"))
        self.assertEqual(self.call("GET", "/api/state", key=API)[1]["phase"], "PREPARING")

    def test_the_backup_account_is_kept_until_machine_2_changes_it(self):
        self.call("PUT", "/api/state", {"phase": "STABLE", "premium": "a@x.vn", "backup": "info@x.vn"}, mover=MOVER)
        st = self.call("PUT", "/api/state", {"phase": "PREPARING", "premium": "a@x.vn", "target": "b@x.vn"}, mover=MOVER)[1]
        self.assertEqual(st["backup"], "info@x.vn")
        st = self.call("PUT", "/api/state", {"phase": "STABLE", "premium": "b@x.vn", "backup": "careers@x.vn"}, mover=MOVER)[1]
        self.assertEqual(self.call("GET", "/api/state", key=API)[1]["backup"], "careers@x.vn")

    def test_last_used_is_when_each_account_last_became_premium(self):
        for body in ({"phase": "STABLE", "premium": "a@x.vn"}, {"phase": "STABLE", "premium": "a@x.vn", "message": "same"},
                     {"phase": "PREPARING", "premium": "a@x.vn", "target": "b@x.vn"},
                     {"phase": "STABLE", "premium": "b@x.vn"}, {"phase": "STABLE", "premium": "a@x.vn"}):
            self.call("PUT", "/api/state", body, mover=MOVER)
        rows = self.store.db.execute("SELECT at, premium FROM transitions WHERE phase='STABLE' ORDER BY id").fetchall()
        used = self.call("GET", "/api/state", key=API)[1]["last_used"]
        self.assertEqual(used, {"b@x.vn": rows[2]["at"], "a@x.vn": rows[3]["at"]})

    def test_a_machine_holds_the_seats_to_log_in_an_account(self):
        body = {"machine_id": "m1", "name": "Hiếu", "account": "xtg1@x.vn", "backup": "info@x.vn", "backup_tier": "Standard"}
        self.assertEqual(self.call("POST", "/api/hold", body)[0], 403)
        code, st = self.call("POST", "/api/hold", body, key=API)
        self.assertEqual((code, st["hold"]["account"], st["hold"]["backup"]), (200, "xtg1@x.vn", "info@x.vn"))
        self.assertAlmostEqual(st["hold"]["until"] - st["hold"]["since"], 600, delta=1)
        code, err = self.call("POST", "/api/hold", dict(body, machine_id="m2", account="xtg2@x.vn"), key=API)
        self.assertEqual(code, 409)
        self.assertIn("Hiếu", err["error"])
        self.assertEqual(self.call("DELETE", "/api/hold", key=API)[1]["hold"], None)

    def test_no_hold_while_a_seat_moves_and_an_expired_one_waits_for_machine_2(self):
        self.call("PUT", "/api/state", {"phase": "PREPARING", "premium": "a@x.vn", "target": "b@x.vn"}, mover=MOVER)
        self.assertEqual(self.call("POST", "/api/hold", {"machine_id": "m1", "account": "c@x.vn"}, key=API)[0], 409)
        self.call("PUT", "/api/state", {"phase": "STABLE", "premium": "b@x.vn"}, mover=MOVER)
        self.store.set_hold({"machine_id": "m1", "account": "c@x.vn", "until": 1})
        self.assertEqual(self.call("POST", "/api/hold", {"machine_id": "m1", "account": "c@x.vn"}, key=API)[0], 409)
        self.assertEqual(self.call("GET", "/api/state", key=API)[1]["hold"]["account"], "c@x.vn")

    def test_machine_2_keeps_the_last_usage_of_each_account(self):
        snap = {"tier": "premium", "five_hour": 82.0, "five_hour_reset": "2026-10-27T05:00:00+00:00",
                "seven_day": 47.0, "seven_day_reset": "2026-10-30T00:00:00+00:00", "at": 100.0, "secret": "x"}
        self.assertEqual(self.call("POST", "/api/snapshots", {"snapshots": {"a@x.vn": snap}}, key=API)[0], 403)
        self.assertEqual(self.call("POST", "/api/snapshots", {"snapshots": {"a@x.vn": snap}}, mover=MOVER), (200, "ok"))
        self.call("POST", "/api/snapshots", {"snapshots": {"b@x.vn": dict(snap, five_hour=1.0)}}, mover=MOVER)
        got = self.call("GET", "/api/state", key=API)[1]["snapshots"]
        self.assertEqual((got["a@x.vn"]["five_hour"], got["b@x.vn"]["five_hour"]), (82.0, 1.0))
        self.assertNotIn("secret", got["a@x.vn"])

    def test_the_dashboard_asks_for_a_seat_and_machine_2_reports_back(self):
        self.call("PUT", "/api/state", {"phase": "STABLE", "premium": "a@x.vn", "backup": "info@x.vn"}, mover=MOVER)
        body, dash = {"kind": "premium", "email": "B@x.vn"}, {"X-Pool-Dashboard": "1"}
        self.assertEqual(self.call("POST", "/api/request", body, headers=dash)[0], 401)
        self.assertEqual(self.call("POST", "/api/request", body, viewer="xtts:" + PASS)[0], 403)  # not from the page
        self.assertEqual(self.call("POST", "/api/request", body, key=API, headers=dash)[0], 401)
        code, st = self.call("POST", "/api/request", body, viewer="xtts:" + PASS, headers=dash)
        self.assertEqual((code, st["request"]["email"], st["request"]["status"]), (200, "b@x.vn", "pending"))
        self.assertEqual(self.call("POST", "/api/request", {"kind": "standard", "email": "c@x.vn"},
                                   viewer="xtts:" + PASS, headers=dash)[0], 409)  # one at a time
        rid = st["request"]["id"]
        self.assertEqual(self.call("POST", "/api/request/result", {"id": rid, "status": "ok"}, key=API)[0], 403)
        self.assertEqual(self.call("POST", "/api/request/result", {"id": rid, "status": "ok", "message": "done"}, mover=MOVER)[0], 200)
        self.assertEqual(self.call("GET", "/api/state", key=API)[1]["request"]["status"], "ok")
        self.assertEqual(self.call("POST", "/api/request", {"kind": "standard", "email": "info@x.vn"},
                                   viewer="xtts:" + PASS, headers=dash)[0], 409)  # already holds it

    def test_state_is_validated(self):
        self.assertEqual(self.call("PUT", "/api/state", {"phase": "DONE"}, mover=MOVER)[0], 400)
        self.assertEqual(self.call("PUT", "/api/state", {"phase": "STABLE"}, mover=MOVER)[0], 400)
        self.assertEqual(self.call("PUT", "/api/state", {"phase": "MOVING", "premium": "a@x.vn"}, mover=MOVER)[0], 400)

    def test_machine_report_returns_the_state_and_marks_the_mover(self):
        self.call("PUT", "/api/state", {"phase": "STABLE", "premium": "a@x.vn"}, mover=MOVER)
        code, st = self.call("POST", "/api/machine", {"machine_id": "m1", "name": "Hiếu", "account": "info@x.vn",
                                                      "at_info": True, "version": "1.0.0"}, key=API)
        self.assertEqual((code, st["premium"]), (200, "a@x.vn"))
        self.call("POST", "/api/machine", {"machine_id": "m2", "name": "Máy 2"}, mover=MOVER)
        machines = {m["machine_id"]: m for m in self.call("GET", "/api/machines", key=API)[1]}
        self.assertEqual((machines["m1"]["role"], machines["m1"]["at_info"], machines["m1"]["active"]), ("worker", True, True))
        self.assertEqual(machines["m2"]["role"], "mover")
        self.assertEqual(self.call("POST", "/api/machine", {"name": "x"}, key=API)[0], 400)

    def test_log_upload_answers_ok_and_is_filterable_on_the_dashboard(self):
        events = [{"ts": "2026-10-20 10:00:00", "machine": "Hiếu", "event": "follow", "p5h": 12.0},
                  {"ts": "2026-10-20 10:00:05", "machine": "Thắng", "event": "move_fail", "message": "timeout"},
                  {"not": "an event"}]
        self.assertEqual(self.call("POST", "/api/log", {"events": events}, key=API), (200, "ok"))
        code, d = self.call("GET", "/api/dashboard?machine=Th%E1%BA%AFng", viewer="xtts:" + PASS)
        self.assertEqual(code, 200)
        self.assertEqual([r["event"] for r in d["log"]], ["move_fail"])
        self.assertEqual(len(self.call("GET", "/api/dashboard", viewer="xtts:" + PASS)[1]["log"]), 2)

    def test_dashboard_needs_the_view_password(self):
        self.assertEqual(self.call("GET", "/pool/")[0], 401)
        self.assertEqual(self.call("GET", "/api/dashboard", viewer="xtts:wrong")[0], 401)
        code, page = self.call("GET", "/pool/", viewer="xtts:" + PASS)
        self.assertEqual(code, 200)
        self.assertIn("Account Pool", page)

    def test_only_machine_2_publishes_the_account_usage_table(self):
        rows = [{"email": "info@x.vn", "role": "backup", "seat": "Standard", "five_hour": 0, "seven_day": 100},
                {"email": "a@x.vn", "role": "rotation", "seat": "Premium", "five_hour": 40.0, "seven_day": 12.0, "secret": "x"}]
        self.assertEqual(self.call("POST", "/api/accounts", {"accounts": rows}, key=API)[0], 403)
        self.assertEqual(self.call("POST", "/api/accounts", {"accounts": rows}, mover=MOVER), (200, "ok"))
        got = self.call("GET", "/api/dashboard", viewer="xtts:" + PASS)[1]["accounts"]
        self.assertEqual([a["email"] for a in got], ["info@x.vn", "a@x.vn"])
        self.assertEqual(got[1]["seat"], "Premium")
        self.assertNotIn("secret", got[1])

    def test_transitions_are_recorded(self):
        for body in ({"phase": "PREPARING", "premium": "a", "target": "b"}, {"phase": "MOVING", "premium": "a", "target": "b"},
                     {"phase": "STABLE", "premium": "b"}):
            self.call("PUT", "/api/state", body, mover=MOVER)
        d = self.call("GET", "/api/dashboard", viewer="xtts:" + PASS)[1]
        self.assertEqual([t["phase"] for t in d["transitions"]], ["STABLE", "MOVING", "PREPARING"])

    def test_bad_bodies_are_rejected(self):
        req = urllib.request.Request(self.base + "/api/log", method="POST", data=b"not json",
                                     headers={"X-Pool-Key": API, "Content-Type": "application/json"})
        with self.assertRaises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req, timeout=5)
        self.assertEqual(e.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
