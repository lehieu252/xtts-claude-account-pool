#!/usr/bin/env python3
"""Account Pool server: shared state of the Premium seat rotation, the machines of the pool, and their log.

Runs on the xtts server (port 5680) behind nginx at https://xtts.xtgroup.vn/pool/. Stdlib only.

  GET  /healthz                      no auth
  GET  /api/state                    X-Pool-Key (any machine) or X-Pool-Mover-Key; with last_used
                                     {email: when it last became Premium}, which orders the next Premium
  PUT  /api/state                    X-Pool-Mover-Key (machine 2 only): {phase, premium, target, message, backup};
                                     without "backup" the backup account stays as it was
  POST /api/machine                  X-Pool-Key: {machine_id, name, host, version, role, account, at_info};
                                     answers with the state, so a follower needs one request per check
  GET  /api/machines                 X-Pool-Key
  POST /api/log                      X-Pool-Key: {"events": [...]}; answers "ok"
  POST /api/accounts                 X-Pool-Mover-Key: usage of every pool account, read by machine 2
  POST /api/hold                     any key: {machine_id, name, account, backup, backup_tier, seconds}; a machine
                                     lends the Standard seat to `account` to log it in (409 while a seat moves or
                                     another machine holds it); machine 2 moves no seat until it is released
  DELETE /api/hold                   any key: release it (machine 2 also releases an expired one after restoring)
  GET  /                             dashboard (Basic auth POOL_VIEW_USER / POOL_VIEW_PASS)
  GET  /api/dashboard                Basic auth: state, machines, transitions, log (filters: machine, event)

The /pool prefix (POOL_BASE) is stripped before routing, so the same paths work direct and behind nginx.
"""
import base64
import hmac
import json
import os
import sqlite3
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

APP_DIR = os.path.dirname(os.path.abspath(__file__))
PHASES = ("STABLE", "PREPARING", "MOVING")
ACTIVE_SEC = 300  # a machine seen within this many seconds counts as working
MOVER_STALE_SEC = 120  # the dashboard flags machine 2 after this long without a report
MAX_BODY = 1024 * 1024
LOG_KEEP_SEC = 30 * 86400
ACCOUNT_FIELDS = ("email", "role", "rank", "last_used", "seat", "five_hour", "five_hour_reset", "seven_day", "seven_day_reset",
                  "status", "read_at")
LOG_FIELDS = ("ts", "machine", "host", "version", "hook", "event", "live", "premium", "target", "p5h", "p7d", "message")


class Config:
    def __init__(self, env=None):
        env = os.environ if env is None else env
        self.port = int(env.get("POOL_PORT", "5680"))
        self.data = env.get("POOL_DATA", "/mnt/data/account-pool/data")
        self.base = env.get("POOL_BASE", "/pool").rstrip("/")
        self.api_key = env.get("POOL_API_KEY", "")
        self.mover_key = env.get("POOL_MOVER_KEY", "")
        self.view_user = env.get("POOL_VIEW_USER", "xtts")
        self.view_pass = env.get("POOL_VIEW_PASS", "")


class Store:
    """SQLite behind one lock: the server handles a handful of machines, not concurrency."""

    def __init__(self, path):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.lock = threading.Lock()
        with self.lock, self.db:
            self.db.executescript("""
                CREATE TABLE IF NOT EXISTS state (id INTEGER PRIMARY KEY CHECK (id = 1), phase TEXT NOT NULL,
                    premium TEXT, target TEXT, message TEXT, updated_at REAL NOT NULL, by TEXT);
                CREATE TABLE IF NOT EXISTS transitions (id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL NOT NULL,
                    phase TEXT NOT NULL, premium TEXT, target TEXT, message TEXT, by TEXT);
                CREATE TABLE IF NOT EXISTS machines (machine_id TEXT PRIMARY KEY, name TEXT, host TEXT,
                    version TEXT, role TEXT, account TEXT, at_info INTEGER, last_seen REAL NOT NULL);
                CREATE TABLE IF NOT EXISTS log (id INTEGER PRIMARY KEY AUTOINCREMENT, received REAL NOT NULL,
                    ts TEXT, machine TEXT, host TEXT, version TEXT, hook TEXT, event TEXT, live TEXT,
                    premium TEXT, target TEXT, p5h REAL, p7d REAL, message TEXT);
                CREATE INDEX IF NOT EXISTS log_received ON log(received);
                CREATE TABLE IF NOT EXISTS accounts (email TEXT PRIMARY KEY, position INTEGER, data TEXT NOT NULL,
                    updated REAL NOT NULL);
            """)
            # 1.0.8: the backup account the machines wait on while the seat moves (machine 2 picks it)
            cols = [r["name"] for r in self.db.execute("PRAGMA table_info(state)")]
            if "backup" not in cols:
                self.db.execute("ALTER TABLE state ADD COLUMN backup TEXT")
            if "hold" not in cols:  # 1.0.12: the Standard seat lent to an account so a machine can log it in
                self.db.execute("ALTER TABLE state ADD COLUMN hold TEXT")
            if not self.db.execute("SELECT 1 FROM state").fetchone():
                self.db.execute("INSERT INTO state (id, phase, premium, target, message, updated_at, by) VALUES (1, 'STABLE', NULL, NULL, 'not set yet', ?, NULL)", (time.time(),))

    def state(self):
        with self.lock:
            st = dict(self.db.execute("SELECT phase, premium, target, backup, hold, message, updated_at, by FROM state").fetchone())
        st["hold"] = json.loads(st["hold"]) if st["hold"] else None
        return st

    def set_hold(self, hold):
        with self.lock, self.db:
            self.db.execute("UPDATE state SET hold=? WHERE id=1", (json.dumps(hold, ensure_ascii=False) if hold else None,))

    def set_state(self, phase, premium, target, message, by, backup=None):
        now = time.time()
        with self.lock, self.db:
            self.db.execute("UPDATE state SET phase=?, premium=?, target=?, message=?, updated_at=?, by=?, "
                            "backup=COALESCE(?, backup) WHERE id=1", (phase, premium, target, message, now, by, backup))
            self.db.execute("INSERT INTO transitions (at, phase, premium, target, message, by) VALUES (?,?,?,?,?,?)",
                            (now, phase, premium, target, message, by))

    def report(self, m):
        with self.lock, self.db:
            self.db.execute("""INSERT INTO machines (machine_id, name, host, version, role, account, at_info, last_seen)
                VALUES (:machine_id, :name, :host, :version, :role, :account, :at_info, :last_seen)
                ON CONFLICT(machine_id) DO UPDATE SET name=excluded.name, host=excluded.host,
                    version=excluded.version, role=excluded.role, account=excluded.account,
                    at_info=excluded.at_info, last_seen=excluded.last_seen""", m)

    def machines(self, now=None):
        now = now or time.time()
        with self.lock:
            rows = [dict(r) for r in self.db.execute("SELECT * FROM machines ORDER BY role DESC, name")]
        for r in rows:
            r["active"] = now - r["last_seen"] <= ACTIVE_SEC
            r["at_info"] = bool(r["at_info"])
        return rows

    def add_log(self, events):
        now = time.time()
        rows = [tuple([now] + [_cell(ev.get(f)) for f in LOG_FIELDS]) for ev in events]
        with self.lock, self.db:
            self.db.executemany("INSERT INTO log (received, %s) VALUES (?%s)" % (", ".join(LOG_FIELDS), ",?" * len(LOG_FIELDS)), rows)
            if rows and int(now) % 50 == 0:  # prune now and then
                self.db.execute("DELETE FROM log WHERE received < ?", (now - LOG_KEEP_SEC,))

    def log(self, limit=200, machine=None, event=None):
        sql, args = "SELECT * FROM log WHERE 1=1", []
        if machine:
            sql += " AND machine = ?"; args.append(machine)
        if event:
            sql += " AND event = ?"; args.append(event)
        sql += " ORDER BY id DESC LIMIT ?"; args.append(limit)
        with self.lock:
            return [dict(r) for r in self.db.execute(sql, args)]

    def set_accounts(self, accounts):
        """Replace the usage table machine 2 publishes (one row per pool account)."""
        now = time.time()
        with self.lock, self.db:
            self.db.execute("DELETE FROM accounts")
            self.db.executemany("INSERT INTO accounts (email, position, data, updated) VALUES (?,?,?,?)",
                                [(a["email"], i, json.dumps(a, ensure_ascii=False), now) for i, a in enumerate(accounts)])

    def accounts(self):
        with self.lock:
            rows = self.db.execute("SELECT data, updated FROM accounts ORDER BY position").fetchall()
        return [dict(json.loads(r["data"]), published=r["updated"]) for r in rows]

    def last_used(self):
        """{email: when it last became Premium}, from the phase history (moves by machine 2 and by hand alike)."""
        with self.lock:
            rows = self.db.execute("SELECT at, premium FROM transitions WHERE phase = 'STABLE' AND premium IS NOT NULL "
                                   "ORDER BY id").fetchall()
        out, prev = {}, None
        for r in rows:
            if r["premium"] != prev:
                out[r["premium"]] = r["at"]
                prev = r["premium"]
        return out

    def transitions(self, limit=30):
        with self.lock:
            return [dict(r) for r in self.db.execute("SELECT * FROM transitions ORDER BY id DESC LIMIT ?", (limit,))]


def _cell(v):
    if v is None or isinstance(v, (int, float)):
        return v
    return str(v)[:2000]


def _text(v, n=200):
    return None if v in (None, "") else str(v)[:n]


def make_handler(cfg, store):
    page_path = os.path.join(APP_DIR, "dashboard.html")

    class Handler(BaseHTTPRequestHandler):
        server_version = "AccountPool/1"

        def log_message(self, fmt, *args):  # quiet: systemd journal only needs errors
            pass

        # ---- helpers
        def _path(self):
            path = urlparse(self.path).path
            if cfg.base and (path == cfg.base or path.startswith(cfg.base + "/")):
                path = path[len(cfg.base):] or "/"
            return path

        def _send(self, code, body, ctype="application/json; charset=utf-8", extra=None):
            data = body if isinstance(body, bytes) else (
                body.encode("utf-8") if isinstance(body, str) else json.dumps(body, ensure_ascii=False).encode("utf-8"))
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(data)

        def _key_ok(self, header, expected):
            given = self.headers.get(header, "")
            return bool(expected) and hmac.compare_digest(given.encode(), expected.encode())

        def _is_mover(self):
            return self._key_ok("X-Pool-Mover-Key", cfg.mover_key)

        def _is_machine(self):
            return self._key_ok("X-Pool-Key", cfg.api_key) or self._is_mover()

        def _is_viewer(self):
            auth = self.headers.get("Authorization", "")
            if not cfg.view_pass or not auth.startswith("Basic "):
                return False
            try:
                user, _, pw = base64.b64decode(auth[6:]).decode("utf-8").partition(":")
            except Exception:
                return False
            return hmac.compare_digest(user.encode(), cfg.view_user.encode()) and \
                hmac.compare_digest(pw.encode(), cfg.view_pass.encode())

        def _deny_viewer(self):
            self._send(401, {"error": "login required"}, extra={"WWW-Authenticate": 'Basic realm="Account Pool"'})

        def _body(self):
            n = int(self.headers.get("Content-Length") or 0)
            if n <= 0 or n > MAX_BODY:
                raise ValueError("body missing or too large")
            data = json.loads(self.rfile.read(n).decode("utf-8"))
            if not isinstance(data, dict):
                raise ValueError("body must be a JSON object")
            return data

        def _state_payload(self):
            st = store.state()
            st["last_used"] = store.last_used()
            st["server_time"] = time.time()
            return st

        # ---- routes
        def do_GET(self):
            path, q = self._path(), parse_qs(urlparse(self.path).query)
            if path == "/healthz":
                return self._send(200, "ok", "text/plain; charset=utf-8")
            if path == "/api/state":
                return self._send(200, self._state_payload()) if self._is_machine() else self._send(403, {"error": "key required"})
            if path == "/api/machines":
                return self._send(200, store.machines()) if self._is_machine() else self._send(403, {"error": "key required"})
            if path in ("/", "/index.html"):
                if not self._is_viewer():
                    return self._deny_viewer()
                with open(page_path, "rb") as f:
                    return self._send(200, f.read(), "text/html; charset=utf-8")
            if path == "/api/dashboard":
                if not self._is_viewer():
                    return self._deny_viewer()
                first = lambda k: (q.get(k) or [None])[0] or None
                try:
                    limit = max(1, min(1000, int(first("limit") or 200)))
                except ValueError:
                    limit = 200
                return self._send(200, {
                    "state": self._state_payload(),
                    "machines": store.machines(),
                    "transitions": store.transitions(),
                    "accounts": store.accounts(),
                    "log": store.log(limit, first("machine"), first("event")),
                    "active_sec": ACTIVE_SEC, "mover_stale_sec": MOVER_STALE_SEC,
                })
            self._send(404, {"error": "not found"})

        def do_DELETE(self):
            if self._path() != "/api/hold":
                return self._send(404, {"error": "not found"})
            if not self._is_machine():
                return self._send(403, {"error": "key required"})
            store.set_hold(None)
            self._send(200, self._state_payload())

        def _take_hold(self, b):
            st = store.state()
            account, machine_id = _text(b.get("account")), _text(b.get("machine_id"), 100)
            if not account or not machine_id:
                return self._send(400, {"error": "account and machine_id required"})
            if st["phase"] != "STABLE":
                return self._send(409, {"error": "a seat move is running (%s): try again in a few minutes" % st["phase"]})
            held = st.get("hold")
            if held and held.get("until", 0) > time.time() and held.get("machine_id") != machine_id:
                return self._send(409, {"error": "%s is already logging in %s" % (held.get("name"), held.get("account"))})
            if held and held.get("until", 0) <= time.time():
                return self._send(409, {"error": "machine 2 is still giving back the seat lent to %s" % held.get("account")})
            seconds = min(max(int(b.get("seconds") or 600), 60), 900)
            store.set_hold({"machine_id": machine_id, "name": _text(b.get("name")), "account": account,
                            "backup": _text(b.get("backup")), "backup_tier": _text(b.get("backup_tier"), 20),
                            "since": time.time(), "until": time.time() + seconds})
            self._send(200, self._state_payload())

        def do_PUT(self):
            if self._path() != "/api/state":
                return self._send(404, {"error": "not found"})
            if not self._is_mover():
                return self._send(403, {"error": "mover key required"})
            try:
                b = self._body()
                phase = b.get("phase")
                if phase not in PHASES:
                    raise ValueError("phase must be one of %s" % ", ".join(PHASES))
                premium, target = _text(b.get("premium")), _text(b.get("target"))
                if phase == "STABLE" and not premium:
                    raise ValueError("STABLE needs the premium account")
                if phase in ("PREPARING", "MOVING") and not (premium and target):
                    raise ValueError("%s needs premium and target" % phase)
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            store.set_state(phase, premium, target, _text(b.get("message"), 500), _text(b.get("by")), _text(b.get("backup")))
            self._send(200, self._state_payload())

        def do_POST(self):
            path = self._path()
            if path not in ("/api/machine", "/api/log", "/api/accounts", "/api/hold"):
                return self._send(404, {"error": "not found"})
            if not self._is_machine():
                return self._send(403, {"error": "key required"})
            try:
                b = self._body()
            except (ValueError, json.JSONDecodeError) as e:
                return self._send(400, {"error": str(e)})
            if path == "/api/hold":
                return self._take_hold(b)
            if path == "/api/accounts":
                if not self._is_mover():
                    return self._send(403, {"error": "mover key required"})
                accounts = b.get("accounts")
                if not isinstance(accounts, list):
                    return self._send(400, {"error": "accounts must be a list"})
                store.set_accounts([{k: a.get(k) for k in ACCOUNT_FIELDS} for a in accounts[:50]
                                    if isinstance(a, dict) and _text(a.get("email"))])
                return self._send(200, "ok", "text/plain; charset=utf-8")
            if path == "/api/machine":
                if not _text(b.get("machine_id")):
                    return self._send(400, {"error": "machine_id required"})
                store.report({
                    "machine_id": _text(b.get("machine_id"), 100), "name": _text(b.get("name")),
                    "host": _text(b.get("host")), "version": _text(b.get("version"), 40),
                    "role": "mover" if self._is_mover() else "worker",
                    "account": _text(b.get("account")), "at_info": 1 if b.get("at_info") else 0,
                    "last_seen": time.time(),
                })
                return self._send(200, self._state_payload())
            events = b.get("events")
            if not isinstance(events, list):
                return self._send(400, {"error": "events must be a list"})
            store.add_log([e for e in events[:500] if isinstance(e, dict) and isinstance(e.get("event"), str)])
            self._send(200, "ok", "text/plain; charset=utf-8")

    return Handler


def serve(cfg):
    store = Store(os.path.join(cfg.data, "pool.sqlite"))
    httpd = ThreadingHTTPServer(("0.0.0.0", cfg.port), make_handler(cfg, store))
    print("account pool server on :%d (base %s)" % (cfg.port, cfg.base or "/"), flush=True)
    httpd.serve_forever()


if __name__ == "__main__":
    serve(Config())
