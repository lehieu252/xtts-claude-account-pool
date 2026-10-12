#!/usr/bin/env python3
"""claude-acct: manage multiple Claude Code accounts (claude.ai login) on one machine.

Commands:
  login  <name|email> [email]  Log in to a new account / log in again (opens a terminal), save as <name>
  save   <name>                Save the currently logged-in account as <name>
  list                         List saved accounts + 5-hour / 7-day usage
  switch <name>                Switch to account <name>
  remove <name>                Remove account <name> from the list (deletes its saved token)

Where Claude Code keeps the live login:
  macOS          Keychain, service "Claude Code-credentials", account = $USER
  Windows/Linux  <config_dir>/.credentials.json
  Identity       <home>/.claude.json, key "oauthAccount"

Where this script keeps saved accounts ($CLAUDE_ACCT_HOME, default ~/.claude-acct):
  accounts.json  list + non-secret metadata + usage cache
  macOS          tokens in Keychain, service "claude-acct", account = <name>
  Windows/Linux  tokens in secrets/<name>.json (mode 600)

Standard library only, Python 3.8+.
"""

import argparse
import datetime as dt
import getpass
import hashlib
import json
import os
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

# CLAUDE_ACCT_BACKEND=file forces the file backend instead of Keychain (to test the Windows/Linux path on macOS)
IS_MAC = sys.platform == "darwin" and os.environ.get("CLAUDE_ACCT_BACKEND") != "file"
LIVE_SERVICE = "Claude Code-credentials"
STORE_SERVICE = os.environ.get("CLAUDE_ACCT_KEYCHAIN_SERVICE", "claude-acct")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"  # unofficial
USAGE_CACHE_TTL = 120  # seconds, to avoid HTTP 429
EXPIRY_MARGIN_MS = 60_000
# Claude Code caches Keychain reads for 30 s (UOt=30000 in the 2.1.29x bundle). After that, a
# running session sees the new token and switches to it, so waiting this long after a swap
# means the current chat already uses the new account. Override with CLAUDE_ACCT_SWITCH_WAIT.
KEYCHAIN_CACHE_WAIT = float(os.environ.get("CLAUDE_ACCT_SWITCH_WAIT", "31"))
BACKUP_NAME = ".live-backup"  # leading dot: valid_name() rejects it, so no account can collide


class AcctError(Exception):
    pass


# ---------------------------------------------------------------- paths

def config_dir():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    return os.path.abspath(os.path.expanduser(env)) if env else os.path.expanduser("~/.claude")


def global_config_path():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if env:
        return os.path.join(config_dir(), ".claude.json")
    return os.path.expanduser("~/.claude.json")


def store_dir():
    return os.path.abspath(os.path.expanduser(os.environ.get("CLAUDE_ACCT_HOME", "~/.claude-acct")))


def index_path():
    return os.path.join(store_dir(), "accounts.json")


# ---------------------------------------------------------------- file helpers

def read_json(path, default=None):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default


def write_json_atomic(path, data, private=False):
    d = os.path.dirname(path)
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
        if private and not sys.platform.startswith("win"):
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------- macOS keychain

def _kc_find(service, account):
    r = subprocess.run(
        ["security", "find-generic-password", "-s", service, "-a", account, "-w"],
        capture_output=True, text=True,
    )
    return r.stdout.rstrip("\n") if r.returncode == 0 else None


def _kc_write(service, account, secret):
    # Write through `security -i` (stdin) with -X hex so the token never appears in argv / ps.
    hexdata = secret.encode("utf-8").hex()
    cmd = 'add-generic-password -U -s "%s" -a "%s" -X %s\n' % (service, account, hexdata)
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if r.returncode != 0 or "error" in (r.stderr or "").lower():
        raise AcctError("Keychain write failed (%s/%s): %s" % (service, account, r.stderr.strip()))


def _kc_delete(service, account):
    subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                   capture_output=True, text=True)


def live_keychain_service():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if not env:
        return LIVE_SERVICE
    # Community observation (anthropics/claude-code#20553): suffix = first 8 hex chars of sha256(path).
    return "%s-%s" % (LIVE_SERVICE, hashlib.sha256(config_dir().encode()).hexdigest()[:8])


# ---------------------------------------------------------------- live credentials

def live_creds_file():
    return os.path.join(config_dir(), ".credentials.json")


def read_live_creds():
    """Return (credentials dict, storage kind) of the account Claude Code is logged in with."""
    if IS_MAC:
        raw = _kc_find(live_keychain_service(), getpass.getuser())
        if raw:
            return json.loads(raw), "keychain"
    data = read_json(live_creds_file())
    if data:
        return data, "file"
    raise AcctError("No Claude Code login found. Run /login first.")


def write_live_creds(creds, where):
    if where == "keychain":
        _kc_write(live_keychain_service(), getpass.getuser(), json.dumps(creds))
    else:
        write_json_atomic(live_creds_file(), creds, private=True)


def read_live_profile():
    return (read_json(global_config_path(), {}) or {}).get("oauthAccount")


def write_live_profile(profile):
    path = global_config_path()
    data = read_json(path, {}) or {}
    data["oauthAccount"] = profile
    write_json_atomic(path, data)


# ---------------------------------------------------------------- stored accounts

def load_index():
    idx = read_json(index_path(), None) or {}
    idx.setdefault("accounts", {})
    return idx


def save_index(idx):
    write_json_atomic(index_path(), idx, private=True)


def read_stored_creds(name):
    if IS_MAC:
        raw = _kc_find(STORE_SERVICE, name)
        if raw is None:
            raise AcctError("Saved token for '%s' is missing from the Keychain." % name)
        return json.loads(raw)
    data = read_json(os.path.join(store_dir(), "secrets", name + ".json"))
    if data is None:
        raise AcctError("Saved token for '%s' is missing." % name)
    return data


def write_stored_creds(name, creds):
    if IS_MAC:
        _kc_write(STORE_SERVICE, name, json.dumps(creds))
    else:
        write_json_atomic(os.path.join(store_dir(), "secrets", name + ".json"), creds, private=True)


def delete_stored_creds(name):
    if IS_MAC:
        _kc_delete(STORE_SERVICE, name)
    else:
        try:
            os.unlink(os.path.join(store_dir(), "secrets", name + ".json"))
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- identity

def identity(profile):
    if not profile:
        return None
    return (profile.get("accountUuid"), profile.get("organizationUuid"))


def find_by_identity(idx, ident):
    if not ident or ident == (None, None):
        return None
    for name, meta in idx["accounts"].items():
        if identity(meta.get("profile")) == ident:
            return name
    return None


def current_name(idx):
    return find_by_identity(idx, identity(read_live_profile()))


def sync_current(idx):
    """Copy the active account's latest token into the store (Claude Code refreshes tokens over time)."""
    name = current_name(idx)
    if not name:
        return None
    creds, _ = read_live_creds()
    write_stored_creds(name, creds)
    meta = idx["accounts"][name]
    meta["profile"] = read_live_profile()
    meta["oauth"] = public_oauth(creds)
    return name


def public_oauth(creds):
    o = creds.get("claudeAiOauth", {})
    return {k: o.get(k) for k in ("expiresAt", "subscriptionType", "rateLimitTier")}


def valid_name(name):
    if not name or any(c in name for c in '/\\:"\' ') or name.startswith("."):
        raise AcctError("Invalid account name: '%s' (no spaces, / \\ : or quotes)." % name)


def resolve_name(idx, query, must_exist=True):
    """Find an account by name, email, or the part of the email before @ (case-insensitive)."""
    accts = idx["accounts"]
    if query in accts:
        return query
    q = query.lower()

    def email(n):
        return ((accts[n].get("profile") or {}).get("emailAddress") or "").lower()

    for test in (lambda n: n.lower() == q,
                 lambda n: email(n) == q,
                 lambda n: email(n).split("@")[0] == q or n.lower().split("@")[0] == q):
        hits = [n for n in accts if test(n)]
        if len(hits) == 1:
            return hits[0]
        if len(hits) > 1:
            raise AcctError("'%s' matches several accounts: %s. Type the full name." % (query, ", ".join(sorted(hits))))
    if must_exist:
        raise AcctError("No account '%s'. Saved: %s" % (query, ", ".join(sorted(accts)) or "(none)"))
    return None


# ---------------------------------------------------------------- usage

def fetch_usage(access_token):
    req = urllib.request.Request(USAGE_URL, headers={
        "Authorization": "Bearer " + access_token,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "claude-acct/0.1",
    })
    with urllib.request.urlopen(req, timeout=10) as r:
        return json.loads(r.read().decode("utf-8"))


def token_state(meta, creds):
    """ok | access_expired (Claude Code refreshes it on use) | relogin (must log in again)."""
    o = creds.get("claudeAiOauth", {})
    now_ms = time.time() * 1000
    if meta.get("revoked") or not o.get("refreshToken"):
        return "relogin"
    rexp = o.get("refreshTokenExpiresAt")
    if isinstance(rexp, (int, float)) and rexp and rexp < now_ms:
        return "relogin"
    if not o.get("accessToken") or (o.get("expiresAt") or 0) < now_ms + EXPIRY_MARGIN_MS:
        return "access_expired"
    return "ok"


def refresh_usage(meta, creds, force=False):
    """Update meta['usage'] if the access token is still valid. Return a status string."""
    cache = meta.get("usage")
    now = time.time()
    if cache and not force and now - cache.get("fetchedAt", 0) < USAGE_CACHE_TTL:
        return "cache"
    o = creds.get("claudeAiOauth", {})
    if not o.get("accessToken") or (o.get("expiresAt") or 0) < now * 1000 + EXPIRY_MARGIN_MS:
        return "expired"
    try:
        data = fetch_usage(o["accessToken"])
    except urllib.error.HTTPError as e:
        if e.code == 401:  # a non-expired token that is rejected has been revoked
            meta["revoked"] = True
            return "revoked"
        return "http %d" % e.code
    except Exception as e:  # network error, timeout, ...
        return "error: %s" % type(e).__name__
    meta["usage"] = {"fetchedAt": now, "data": data}
    meta.pop("revoked", None)
    return "ok"


def fmt_window(w):
    if not isinstance(w, dict) or w.get("utilization") is None:
        return "—"
    s = "%d%%" % round(w["utilization"])
    reset = w.get("resets_at")
    if reset:
        try:
            if isinstance(reset, (int, float)):
                t = dt.datetime.fromtimestamp(reset / 1000 if reset > 1e11 else reset).astimezone()
            else:
                t = dt.datetime.fromisoformat(str(reset).replace("Z", "+00:00")).astimezone()
            s += " (reset %s)" % t.strftime("%d/%m %H:%M")
        except (ValueError, OverflowError, OSError):
            pass
    return s


def fmt_age(ts):
    mins = int((time.time() - ts) // 60)
    if mins < 1:
        return "just now"
    if mins < 60:
        return "%d min ago" % mins
    if mins < 60 * 24:
        return "%d h ago" % (mins // 60)
    return "%d d ago" % (mins // 1440)


# ---------------------------------------------------------------- commands

def store_account(idx, name, creds, profile, source):
    """Save creds + profile as `name`. Same name + same account = update the token (re-login)."""
    ident = identity(profile)
    if not ident or ident == (None, None):
        raise AcctError("Could not read the account identity (%s)." % source)
    email = (profile or {}).get("emailAddress", "?")
    existing = find_by_identity(idx, ident)
    if existing and existing != name:
        raise AcctError("Account %s is already saved as '%s'. Use that name (e.g. /acct:login %s to log in again)."
                        % (email, existing, existing))
    if name in idx["accounts"] and identity(idx["accounts"][name].get("profile")) != ident:
        raise AcctError("Name '%s' belongs to another account (%s). Pick another name or run /acct:logout %s first."
                        % (name, idx["accounts"][name].get("profile", {}).get("emailAddress"), name))
    relogin = name in idx["accounts"]
    write_stored_creds(name, creds)
    idx["accounts"][name] = {
        "profile": profile,
        "oauth": public_oauth(creds),
        "savedAt": time.time(),
        "usage": idx["accounts"].get(name, {}).get("usage"),
    }
    save_index(idx)
    print("%s account **%s**: %s (%s)." % ("Refreshed login for" if relogin else "Saved",
          name, email, (profile or {}).get("organizationName", "?")))


def cmd_save(args):
    valid_name(args.name)
    idx = load_index()
    creds, _ = read_live_creds()
    store_account(idx, args.name, creds, read_live_profile(), "current login")


# ---------------------------------------------------------------- login (new login in a terminal window)

class _ConfigDir:
    """Temporarily point CLAUDE_CONFIG_DIR at another directory to read / delete the login there."""

    def __init__(self, path):
        self.path, self.old = path, None

    def __enter__(self):
        self.old = os.environ.get("CLAUDE_CONFIG_DIR")
        os.environ["CLAUDE_CONFIG_DIR"] = self.path

    def __exit__(self, *exc):
        if self.old is None:
            os.environ.pop("CLAUDE_CONFIG_DIR", None)
        else:
            os.environ["CLAUDE_CONFIG_DIR"] = self.old


def claude_bin():
    cand = os.environ.get("CLAUDE_CODE_EXECPATH")
    if cand and os.path.isfile(cand):
        return cand
    import shutil
    for n in ("claude", "claude.exe"):
        found = shutil.which(n)
        if found:
            return found
    raise AcctError("Could not find the claude executable. Run this inside Claude Code, or add claude to PATH.")


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def cmd_login(args):
    valid_name(args.name)
    idx = load_index()
    existing = resolve_name(idx, args.name, must_exist=False)
    if existing:  # logging in again to a saved account
        args.name = existing
        if not args.email:
            args.email = (idx["accounts"][existing].get("profile") or {}).get("emailAddress")
    if not args.email and "@" in args.name:
        args.email = args.name
    tmp = os.path.join(store_dir(), "login-" + args.name)
    if os.path.isdir(tmp):
        import shutil
        shutil.rmtree(tmp, ignore_errors=True)
    os.makedirs(tmp)
    if IS_MAC:
        # Back up the live login in case the Keychain is not keyed by CLAUDE_CONFIG_DIR (see cmd_import_login).
        main = _kc_find(LIVE_SERVICE, getpass.getuser())
        if main:
            _kc_write(STORE_SERVICE, BACKUP_NAME, main)
    login_cmd = [claude_bin(), "auth", "login"] + (["--email", args.email] if args.email else [])
    helper = os.path.join(tmp, "login.sh")
    with open(helper, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write("echo '=== Claude account login: %s ==='\n" % args.name)
        f.write("export CLAUDE_CONFIG_DIR=%s\n" % sh_quote(tmp))
        f.write(" ".join(sh_quote(a) for a in login_cmd) + "\n")
        f.write("unset CLAUDE_CONFIG_DIR\n")
        f.write("bash %s import-login %s %s %s\n" % (
            sh_quote(os.path.join(os.path.dirname(os.path.abspath(__file__)), "acct.sh")), sh_quote(tmp),
            sh_quote(args.name), sh_quote(args.email or "")))
        f.write("echo; echo 'Done. You can close this window and go back to Claude: /acct:list'\n")
    os.chmod(helper, 0o700)
    opened = open_terminal(helper)
    if opened:
        print("Opened a **terminal window** to log in to **%s**%s." % (
            args.name, " (" + args.email + ")" if args.email else ""))
        print("1. Sign in with the right account in the browser window that opened.")
        print("2. If the page shows a code, paste it into the terminal window and press Enter.")
        print("3. Then run /acct:list to check. Your current account is NOT changed.")
    else:
        print("Could not open a terminal window. Open a terminal and run:")
        print("")
        print("    bash %s" % sh_quote(helper))


def open_terminal(script):
    if os.environ.get("CLAUDE_ACCT_NO_TERMINAL"):  # used by tests
        return False
    try:
        if sys.platform == "darwin":
            r = subprocess.run(["osascript",
                                "-e", 'tell application "Terminal" to do script "bash " & quoted form of "%s"' % script,
                                "-e", 'tell application "Terminal" to activate'],
                               capture_output=True, text=True)
            return r.returncode == 0
        if sys.platform.startswith("win") or os.environ.get("MSYSTEM"):
            subprocess.Popen(["cmd.exe", "/c", "start", "", "bash", script])
            return True
        import shutil
        for term in (["x-terminal-emulator", "-e"], ["gnome-terminal", "--"], ["konsole", "-e"], ["xterm", "-e"]):
            if shutil.which(term[0]):
                subprocess.Popen(term + ["bash", script])
                return True
    except OSError:
        pass
    return False


def cmd_import_login(args):
    """Called by login.sh after `claude auth login`: take the login from the temp dir, save it, clean up."""
    import shutil
    tmp = os.path.abspath(args.dir)
    idx = load_index()
    try:
        with _ConfigDir(tmp):
            service = live_keychain_service()
            creds = None
            if IS_MAC:
                raw = _kc_find(service, getpass.getuser())
                creds = json.loads(raw) if raw else None
            if creds is None:
                creds = read_json(live_creds_file())
            profile = read_live_profile()
        if creds is None and IS_MAC:
            # In case the Keychain is NOT keyed by CLAUDE_CONFIG_DIR: the new login overwrote the
            # main item. Take the new token from it, then restore the backed-up live login.
            backup = _kc_find(STORE_SERVICE, BACKUP_NAME)
            main = _kc_find(LIVE_SERVICE, getpass.getuser())
            if backup and main and main != backup:
                creds = json.loads(main)
                _kc_write(LIVE_SERVICE, getpass.getuser(), backup)
                print("(Keychain item is shared, so your current login was restored.)")
        if creds is None:
            raise AcctError("Login did not finish (no new token found). Run /acct:login %s again." % args.name)
        got = ((profile or {}).get("emailAddress") or "").lower()
        if args.expect_email and got and got != args.expect_email.lower():
            raise AcctError("You signed in as %s, not %s, so nothing was saved. Run /acct:login %s again and pick the right account in the browser."
                            % (got, args.expect_email, args.name))
        store_account(idx, args.name, creds, profile, "new login")
    finally:
        if IS_MAC:
            _kc_delete(service, getpass.getuser())
            _kc_delete(STORE_SERVICE, BACKUP_NAME)
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_list(args):
    idx = load_index()
    if not idx["accounts"]:
        print("No saved accounts. Run /acct:login <email> to log in, or /acct:save <name> to save the current account.")
        return
    try:
        cur = sync_current(idx)
    except AcctError:
        cur = None
    live_profile = read_live_profile()
    rows, need_login = [], []
    for name in sorted(idx["accounts"]):
        meta = idx["accounts"][name]
        try:
            creds = read_stored_creds(name)
            status = refresh_usage(meta, creds, force=args.refresh)
            state = token_state(meta, creds)
        except AcctError as e:
            status, state = str(e), "relogin"
        usage = (meta.get("usage") or {}).get("data") or {}
        fetched = (meta.get("usage") or {}).get("fetchedAt")
        # Only flag accounts that need action; an expired access token is refreshed automatically.
        badge = ""
        if state == "relogin":
            badge = "🔴 Log in again: `/acct:login %s`" % name
            need_login.append(name)
        note = ("updated " + fmt_age(fetched)) if fetched else "no data yet"
        if status not in ("ok", "cache", "expired", "revoked") and state != "relogin":
            note += " (%s)" % status
        p = meta.get("profile") or {}
        rows.append([
            ("▶ " if name == cur else "") + name,
            p.get("emailAddress", "?"),
            p.get("organizationName", "?"),
            (meta.get("oauth") or {}).get("subscriptionType") or "?",
            badge,
            fmt_window(usage.get("five_hour")),
            fmt_window(usage.get("seven_day")),
            note,
        ])
    save_index(idx)
    head = ["Account", "Email", "Organization", "Plan", "Status", "5-hour", "7-day", "Usage"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        print("| " + " | ".join(r) + " |")
    if need_login:
        print("\n🔴 %d account(s) need a new login: %s" % (len(need_login), ", ".join(
            "`/acct:login %s`" % n for n in need_login)))
    if not cur:
        print("\nLogged in as %s, but this account is not saved. Run /acct:save <name> to save it."
              % ((live_profile or {}).get("emailAddress") or "(unknown)"))
    else:
        print("\n▶ = current account.")


def cmd_switch(args):
    idx = load_index()
    args.name = resolve_name(idx, args.name)
    cur = current_name(idx)
    if cur == args.name:
        print("Already using **%s**." % args.name)
        return
    target = read_stored_creds(args.name)
    if token_state(idx["accounts"][args.name], target) == "relogin":
        raise AcctError("The login for '%s' has expired or was revoked. Run /acct:login %s to log in again."
                        % (args.name, args.name))
    live_creds, where = read_live_creds()
    if cur is None and not args.force:
        raise AcctError("The current account (%s) is not saved, so switching away would lose this login. "
                        "Run /acct:save <name> first, or add --force to skip this check."
                        % ((read_live_profile() or {}).get("emailAddress") or "unknown"))
    if cur:
        write_stored_creds(cur, live_creds)  # keep the latest token of the account we leave
        idx["accounts"][cur]["oauth"] = public_oauth(live_creds)
    write_live_creds(target, where)
    write_live_profile(idx["accounts"][args.name].get("profile"))
    save_index(idx)
    p = idx["accounts"][args.name].get("profile") or {}
    # The file backend is re-read as soon as its mtime changes; only the Keychain read is cached.
    if where == "keychain" and not args.no_wait and os.environ.get("CLAUDECODE") and KEYCHAIN_CACHE_WAIT > 0:
        time.sleep(KEYCHAIN_CACHE_WAIT)
    print("Switched to **%s**: %s (%s)." % (args.name, p.get("emailAddress", "?"), p.get("organizationName", "?")))
    if where == "keychain" and args.no_wait:
        print("Your next message in this chat uses the new account after about 30 seconds.")
    else:
        print("You can continue in this chat: your next message uses the new account.")


def auto_config_path():
    return os.path.join(store_dir(), "rotation.json")


def load_rotation():
    return read_json(auto_config_path(), {}) or {}


def save_rotation(cfg):
    write_json_atomic(auto_config_path(), cfg, private=True)


def _rotation_thresholds(cfg):
    legacy = cfg.get("threshold", 90)
    return (cfg.get("dailyThreshold", legacy), cfg.get("weeklyThreshold", legacy))


def _usage_window_values(usage):
    result = {}
    for key in ("five_hour", "seven_day"):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            result[key] = window["utilization"]
    return result


def _usage_reached_thresholds(usage, daily_threshold, weekly_threshold):
    values = _usage_window_values(usage)
    return ("five_hour" in values and values["five_hour"] >= daily_threshold) or \
           ("seven_day" in values and values["seven_day"] >= weekly_threshold)


def cmd_auto_configure(args):
    idx = load_index()
    premium = resolve_name(idx, args.premium)
    order = [resolve_name(idx, item.strip()) for item in args.order.split(",") if item.strip()]
    if len(order) < 2 or len(set(order)) != len(order):
        raise AcctError("Rotation order must contain at least two different saved accounts.")
    if premium not in order:
        raise AcctError("The current Premium account must be included in the rotation order.")
    legacy_threshold = args.threshold if args.threshold is not None else 90
    daily_threshold = args.daily_threshold if args.daily_threshold is not None else legacy_threshold
    weekly_threshold = args.weekly_threshold if args.weekly_threshold is not None else legacy_threshold
    if not 1 <= daily_threshold <= 99 or not 1 <= weekly_threshold <= 99:
        raise AcctError("Daily and weekly thresholds must each be between 1 and 99.")
    cfg = {
        "enabled": False,
        "dailyThreshold": daily_threshold,
        "weeklyThreshold": weekly_threshold,
        "order": order,
        "premiumAccount": premium,
        "membersUrl": args.members_url,
        "cdpUrl": args.cdp_url,
        "browserProfile": os.path.abspath(os.path.expanduser(args.browser_profile)),
    }
    save_rotation(cfg)
    print("Rotation configured: %s (Premium) → %s. Daily (5-hour) threshold: %d%%; weekly (7-day) threshold: %d%%. Rotation is OFF; run `acct auto enable` after setup."
          % (premium, " → ".join(order[order.index(premium) + 1:] + order[:order.index(premium)]), daily_threshold, weekly_threshold))


def cmd_auto_enable(args):
    cfg = load_rotation()
    if not cfg.get("order"):
        raise AcctError("Configure rotation first: acct.sh auto configure --premium <account> --order name1,name2,...")
    cfg["enabled"] = True
    save_rotation(cfg)
    print("Automatic Premium rotation enabled. It checks usage before prompts and tool calls.")


def cmd_auto_disable(args):
    cfg = load_rotation()
    cfg["enabled"] = False
    save_rotation(cfg)
    print("Automatic Premium rotation disabled.")


def cmd_auto_status(args):
    cfg = load_rotation()
    if not cfg.get("order"):
        print("Automatic Premium rotation is not configured.")
        return
    print("Automatic Premium rotation: %s" % ("ON" if cfg.get("enabled") else "OFF"))
    daily_threshold, weekly_threshold = _rotation_thresholds(cfg)
    print("Daily (5-hour) threshold: %s%%" % daily_threshold)
    print("Weekly (7-day) threshold: %s%%" % weekly_threshold)
    print("Rotation order: %s" % " → ".join(cfg["order"]))
    print("Current Premium account: %s" % cfg.get("premiumAccount", "?"))
    print("Browser profile: %s" % cfg.get("browserProfile", "(CDP)"))


def cmd_auto_browser(args):
    cfg = load_rotation()
    if not cfg.get("order"):
        raise AcctError("Configure rotation first with `acct auto configure`.")
    node = os.environ.get("CLAUDE_ACCT_NODE", "node")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seat_manager.mjs")
    env = dict(os.environ)
    env["CLAUDE_ACCT_MEMBERS_URL"] = cfg.get("membersUrl", "https://claude.ai/admin-settings/members")
    env["CLAUDE_ACCT_BROWSER_PROFILE"] = cfg.get("browserProfile", os.path.join(store_dir(), "browser-profile"))
    try:
        subprocess.Popen([node, script, "--setup"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
    except OSError as e:
        raise AcctError("Could not open the persistent Chrome profile: %s" % e)
    print("Opened Chrome with the saved automation profile. Sign in as an organization owner/admin if needed. Leave this profile available for seat automation; the login is saved for future rotations.")


def _next_rotation_account(cfg):
    order = cfg.get("order", [])
    cur = cfg.get("premiumAccount")
    if cur not in order or len(order) < 2:
        raise AcctError("Rotation config is invalid. Run `acct.sh auto configure` again.")
    return order[(order.index(cur) + 1) % len(order)]


def _set_premium_seat(old_name, new_name, cfg, idx):
    old_email = ((idx["accounts"].get(old_name, {}).get("profile") or {}).get("emailAddress") or "").strip()
    new_email = ((idx["accounts"].get(new_name, {}).get("profile") or {}).get("emailAddress") or "").strip()
    if not old_email or not new_email:
        raise AcctError("Both accounts need a saved email address before changing seat tiers.")
    node = os.environ.get("CLAUDE_ACCT_NODE", "node")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seat_manager.mjs")
    env = dict(os.environ)
    env["CLAUDE_ACCT_MEMBERS_URL"] = cfg.get("membersUrl", "https://claude.ai/admin-settings/members")
    env["CLAUDE_ACCT_CDP_URL"] = cfg.get("cdpUrl", "http://127.0.0.1:9222")
    env["CLAUDE_ACCT_BROWSER_PROFILE"] = cfg.get("browserProfile", os.path.join(store_dir(), "browser-profile"))
    try:
        r = subprocess.run([node, script, old_email, new_email], capture_output=True, text=True, timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AcctError("Could not run seat automation: %s. Check Node.js, Playwright, and the Chrome CDP connection." % e)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "unknown error").strip()
        raise AcctError("Premium seat transfer failed; account switch was not performed. %s" % detail[-1200:])
    if r.stdout.strip():
        print(r.stdout.strip())


def _cmd_auto_check_locked(args):
    cfg = load_rotation()
    if not cfg.get("enabled"):
        return
    idx = load_index()
    premium = cfg.get("premiumAccount")
    if premium not in idx["accounts"]:
        raise AcctError("Current Premium account '%s' is not saved. Disable rotation or reconfigure it." % premium)
    # Rotate only while this Claude Code session is actually using the Premium account.
    try:
        cur = sync_current(idx)
    except AcctError:
        cur = current_name(idx)
    if cur != premium:
        return
    try:
        creds = read_stored_creds(premium)
    except AcctError as e:
        raise AcctError("Automatic rotation could not read the Premium account: %s" % e)
    meta = idx["accounts"][premium]
    status = refresh_usage(meta, creds, force=args.force)
    if status not in ("ok", "cache"):
        save_index(idx)
        if args.manual:
            raise AcctError("Cannot check Premium usage (%s); rotation did not run." % status)
        return
    usage = (meta.get("usage") or {}).get("data") or {}
    daily_threshold, weekly_threshold = _rotation_thresholds(cfg)
    values = _usage_window_values(usage)
    save_index(idx)
    if cfg.get("rotationHold") == premium:
        if not values or _usage_reached_thresholds(usage, daily_threshold, weekly_threshold):
            return
        cfg.pop("rotationHold", None)
        save_rotation(cfg)
    if not values or not _usage_reached_thresholds(usage, daily_threshold, weekly_threshold):
        return
    old_name = premium
    new_name = _next_rotation_account(cfg)
    if token_state(idx["accounts"].get(new_name, {}), read_stored_creds(new_name)) == "relogin":
        raise AcctError("Next account '%s' needs a new login; Premium seat was not moved." % new_name)
    if args.manual:
        reached = []
        if values.get("five_hour", -1) >= daily_threshold:
            reached.append("5-hour %d%% (threshold %d%%)" % (round(values["five_hour"]), daily_threshold))
        if values.get("seven_day", -1) >= weekly_threshold:
            reached.append("7-day %d%% (threshold %d%%)" % (round(values["seven_day"]), weekly_threshold))
        print("Usage threshold reached for %s. Transferring Premium from %s to %s..." % (" and ".join(reached), old_name, new_name))
    _set_premium_seat(old_name, new_name, cfg, idx)
    # Change credentials only after the Members page confirms the tier change.
    cmd_switch(argparse.Namespace(name=new_name, force=False, no_wait=False))
    cfg["premiumAccount"] = new_name
    next_usage = ((idx["accounts"].get(new_name, {}).get("usage") or {}).get("data") or {})
    if _usage_reached_thresholds(next_usage, daily_threshold, weekly_threshold):
        # Avoid immediately rotating through accounts whose standard-tier usage was already high.
        cfg["rotationHold"] = new_name
    else:
        cfg.pop("rotationHold", None)
    save_rotation(cfg)


def cmd_auto_check(args):
    if not load_rotation().get("enabled"):
        return
    lock_path = os.path.join(store_dir(), "rotation.lock")
    os.makedirs(store_dir(), exist_ok=True)
    try:
        fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except FileExistsError:
        try:
            if time.time() - os.path.getmtime(lock_path) > 300:
                os.unlink(lock_path)
                fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            else:
                return
        except (FileNotFoundError, FileExistsError):
            return
    try:
        os.close(fd)
        _cmd_auto_check_locked(args)
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


def cmd_remove(args):
    idx = load_index()
    args.name = resolve_name(idx, args.name)
    was_current = current_name(idx) == args.name
    delete_stored_creds(args.name)
    del idx["accounts"][args.name]
    save_index(idx)
    print("Removed **%s** from the list." % args.name)
    if was_current:
        print("This is the current account, and Claude Code is still logged in with it. "
              "To log out completely, run /logout, or /acct:switch to another account.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claude-acct", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="log in to a new account / log in again (opens a terminal)"); p.add_argument("name", nargs="?"); p.add_argument("email", nargs="?"); p.set_defaults(fn=cmd_login)
    p = sub.add_parser("import-login", help=argparse.SUPPRESS); p.add_argument("dir"); p.add_argument("name"); p.add_argument("expect_email", nargs="?"); p.set_defaults(fn=cmd_import_login)
    p = sub.add_parser("save", help="save the current account"); p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_save)
    p = sub.add_parser("list", help="list accounts + usage"); p.add_argument("--refresh", action="store_true"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("switch", help="switch account"); p.add_argument("name", nargs="?"); p.add_argument("--force", action="store_true"); p.add_argument("--no-wait", action="store_true"); p.set_defaults(fn=cmd_switch)
    p = sub.add_parser("remove", help="remove an account from the list"); p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_remove)
    p = sub.add_parser("auto", help="configure automatic Premium seat rotation")
    autos = p.add_subparsers(dest="auto_cmd", required=True)
    q = autos.add_parser("configure", help="set account rotation order and organization page")
    q.add_argument("--premium", required=True); q.add_argument("--order", required=True)
    q.add_argument("--daily-threshold", type=int, help="5-hour usage trigger (default: 90%%)")
    q.add_argument("--weekly-threshold", type=int, help="7-day usage trigger (default: 90%%)")
    q.add_argument("--threshold", type=int, help="legacy shorthand: set both thresholds to this value")
    q.add_argument("--members-url", default="https://claude.ai/admin-settings/members")
    q.add_argument("--cdp-url", default="http://127.0.0.1:9222")
    q.add_argument("--browser-profile", default=os.path.join(store_dir(), "browser-profile"))
    q.set_defaults(fn=cmd_auto_configure)
    q = autos.add_parser("enable", help="enable automatic rotation"); q.set_defaults(fn=cmd_auto_enable)
    q = autos.add_parser("disable", help="disable automatic rotation"); q.set_defaults(fn=cmd_auto_disable)
    q = autos.add_parser("status", help="show rotation settings"); q.set_defaults(fn=cmd_auto_status)
    q = autos.add_parser("browser", help="open the persistent admin browser profile for one-time sign-in"); q.set_defaults(fn=cmd_auto_browser)
    q = autos.add_parser("check", help="check usage and rotate if threshold is reached")
    q.add_argument("--force", action="store_true"); q.add_argument("--manual", action="store_true")
    q.set_defaults(fn=cmd_auto_check)
    args = ap.parse_args(argv)
    try:
        if hasattr(args, "name") and not args.name:
            hint = {"login": "/acct:login", "save": "/acct:save", "switch": "/acct:switch", "remove": "/acct:logout"}[args.cmd]
            raise AcctError("Missing account name. Example: %s work" % hint)
        args.fn(args)
    except AcctError as e:
        print("Error: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
