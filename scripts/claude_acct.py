#!/usr/bin/env python3
"""claude-acct: manage multiple Claude Code accounts (claude.ai login) on one machine.

Commands:
  login  <name|email> [email]  Log in to a new account / log in again (opens a terminal), save as <name>
  save   <name>                Save the currently logged-in account as <name>
  list                         List saved accounts + 5-hour / 7-day usage
  switch <name>                Switch to account <name>
  remove <name>                Remove account <name> from the list (deletes its saved token)
  doctor                       Check requirements, install the Node dependency, list what is left to set up

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
import platform
import random
import shutil
import ssl
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
# OAuth refresh as Claude Code 2.1.29x does it (unofficial); access tokens last about 8 hours.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"  # unofficial; organization.seat_tier is the member's seat
SEAT_LABELS = {"team_standard": "Standard", "team_tier_1": "Premium"}
PREMIUM_TIER = "team_tier_1"
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

_ssl_context = None  # None: Python's default CA store; replaced once if that store cannot verify


def _fallback_ssl_context():
    """CA certificates for a Python that has none.

    python.org builds on macOS ship without a CA store until "Install Certificates.command" is
    run; Claude Code still works there because it brings its own. Use certifi when installed,
    otherwise the root certificates of the macOS system keychain.
    """
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except Exception:
        pass
    if sys.platform == "darwin":
        try:
            pem = subprocess.run(["security", "find-certificate", "-a", "-p",
                                  "/System/Library/Keychains/SystemRootCertificates.keychain"],
                                 capture_output=True, text=True, timeout=30).stdout
            if "BEGIN CERTIFICATE" in pem:
                return ssl.create_default_context(cadata=pem)
        except (OSError, subprocess.TimeoutExpired, ssl.SSLError):
            pass
    return None


def urlopen(req, timeout):
    global _ssl_context
    try:
        return urllib.request.urlopen(req, timeout=timeout, context=_ssl_context)
    except urllib.error.URLError as e:
        if _ssl_context is not None or not isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError):
            raise
        ctx = _fallback_ssl_context()
        if ctx is None:
            raise
        _ssl_context = ctx
        return urllib.request.urlopen(req, timeout=timeout, context=ctx)


def net_error(e):
    """Short reason for a failed request, e.g. 'SSL certificate not trusted' instead of 'URLError'."""
    reason = getattr(e, "reason", None)
    if isinstance(reason, ssl.SSLCertVerificationError):
        return "SSL certificate not trusted by this Python; see /acct:doctor"
    if reason is not None:
        return ("%s" % reason)[:80]
    return type(e).__name__


def fetch_usage(access_token, url=USAGE_URL):
    req = urllib.request.Request(url, headers={
        "Authorization": "Bearer " + access_token,
        "anthropic-beta": "oauth-2025-04-20",
        "Content-Type": "application/json",
        "User-Agent": "claude-acct/0.1",
    })
    with urlopen(req, timeout=10) as r:
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


def _post_token_refresh(oauth):
    body = {"grant_type": "refresh_token", "refresh_token": oauth["refreshToken"], "client_id": OAUTH_CLIENT_ID}
    if oauth.get("scopes"):
        body["scope"] = " ".join(oauth["scopes"])
    req = urllib.request.Request(TOKEN_URL, data=json.dumps(body).encode("utf-8"), method="POST", headers={
        "Content-Type": "application/json", "User-Agent": "claude-acct/0.1"})
    with urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode("utf-8"))


def refresh_stored_token(name, meta, creds):
    """Get a new access token for a saved account that is NOT the live Claude Code login.

    The store holds the only copy of such a login, so the script may use its refresh token. The
    server can rotate the refresh token, so the new pair is saved before anything else. Never call
    this for the live account: Claude Code owns that refresh token. Updates `creds` in place and
    returns "ok", "revoked" (log in again) or a short error.
    """
    lock_path = os.path.join(store_dir(), "refresh-%s.lock" % hashlib.sha1(name.encode("utf-8")).hexdigest()[:12])
    os.makedirs(store_dir(), exist_ok=True)
    if not _acquire_lock(lock_path, wait=20):
        return "refresh busy"
    try:
        latest = read_stored_creds(name)  # another process may have refreshed it while we waited
        o = latest.get("claudeAiOauth", {})
        if o.get("accessToken") and (o.get("expiresAt") or 0) >= time.time() * 1000 + EXPIRY_MARGIN_MS:
            creds.clear(); creds.update(latest)
            return "ok"
        if not o.get("refreshToken"):
            return "revoked"
        try:
            data = _post_token_refresh(o)
        except urllib.error.HTTPError as e:
            try:
                err = json.loads(e.read().decode("utf-8")).get("error")
            except Exception:
                err = None
            if err == "invalid_grant":
                meta["revoked"] = True
                return "revoked"
            return "refresh http %d" % e.code
        except Exception as e:
            return "refresh error: %s" % net_error(e)
        if not data.get("access_token") or not isinstance(data.get("expires_in"), (int, float)):
            return "refresh error: unexpected response"
        now_ms = int(time.time() * 1000)
        o = dict(o, accessToken=data["access_token"], refreshToken=data.get("refresh_token") or o["refreshToken"],
                 expiresAt=now_ms + int(data["expires_in"] * 1000))
        if isinstance(data.get("refresh_token_expires_in"), (int, float)):
            o["refreshTokenExpiresAt"] = now_ms + int(data["refresh_token_expires_in"] * 1000)
        if isinstance(data.get("scope"), str) and data["scope"]:
            o["scopes"] = data["scope"].split()
        latest["claudeAiOauth"] = o
        write_stored_creds(name, latest)
        meta["oauth"] = public_oauth(latest)
        meta.pop("revoked", None)
        creds.clear(); creds.update(latest)
        return "ok"
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


def refresh_usage(meta, creds, force=False, refresh_name=None, ttl=USAGE_CACHE_TTL):
    """Update meta['usage'] if the access token is still valid. Return a status string.

    With `refresh_name` (a saved account that is not the live login), an expired access token is
    refreshed first, so idle accounts still report their usage.
    """
    cache = meta.get("usage")
    now = time.time()
    if cache and not force and now - cache.get("fetchedAt", 0) < ttl:
        return "cache"
    o = creds.get("claudeAiOauth", {})
    if not o.get("accessToken") or (o.get("expiresAt") or 0) < now * 1000 + EXPIRY_MARGIN_MS:
        if not refresh_name:
            return "expired"
        status = refresh_stored_token(refresh_name, meta, creds)
        if status != "ok":
            return status
        o = creds.get("claudeAiOauth", {})
    try:
        data = fetch_usage(o["accessToken"])
    except urllib.error.HTTPError as e:
        if e.code == 401:  # a non-expired token that is rejected has been revoked
            meta["revoked"] = True
            return "revoked"
        return "http %d" % e.code
    except Exception as e:  # network error, timeout, ...
        return "error: %s" % net_error(e)
    meta["usage"] = {"fetchedAt": now, "data": data}
    meta.pop("revoked", None)
    return "ok"


def refresh_seat(meta, creds, force=False, refresh_name=None, ttl=USAGE_CACHE_TTL):
    """Update meta['seat'] with the organization seat tier, under the same rules as refresh_usage.

    Return "ok", "cache", or why the seat could not be read (the last known seat is kept).
    """
    cache = meta.get("seat")
    now = time.time()
    if cache and not force and now - cache.get("fetchedAt", 0) < ttl:
        return "cache"
    o = creds.get("claudeAiOauth", {})
    if not o.get("accessToken") or (o.get("expiresAt") or 0) < now * 1000 + EXPIRY_MARGIN_MS:
        if not refresh_name:
            return "expired"
        status = refresh_stored_token(refresh_name, meta, creds)
        if status != "ok":
            return status
        o = creds.get("claudeAiOauth", {})
    try:
        org = fetch_usage(o["accessToken"], PROFILE_URL).get("organization") or {}
    except Exception as e:  # the usage column already reports token problems
        return "error: %s" % net_error(e)
    meta["seat"] = {"fetchedAt": now, "tier": org.get("seat_tier")}
    return "ok"


def fmt_seat(meta):
    seat = meta.get("seat")
    if not seat:
        return "?"
    tier = seat.get("tier")
    if not tier:  # personal plans (Pro, Max) have no organization seat at all
        plan = (meta.get("oauth") or {}).get("subscriptionType")
        return "No seat" if plan in ("team", "enterprise") else "—"
    return SEAT_LABELS.get(tier, tier)


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
            # Git for Windows puts only git.exe on the Windows PATH; use the bash Claude Code runs with.
            bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or shutil.which("bash") or "bash"
            subprocess.Popen(["cmd.exe", "/c", "start", "", bash, script])
            return True
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
    live = cur or current_name(idx)  # by identity: never refresh the token Claude Code is using
    live_profile = read_live_profile()
    rows, need_login = [], []
    for name in sorted(idx["accounts"]):
        meta = idx["accounts"][name]
        try:
            creds = read_stored_creds(name)
            status = refresh_usage(meta, creds, force=args.refresh, refresh_name=None if name == live else name)
            refresh_seat(meta, creds, force=args.refresh)
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
            fmt_seat(meta),
            badge,
            fmt_window(usage.get("five_hour")),
            fmt_window(usage.get("seven_day")),
            note,
        ])
    save_index(idx)
    head = ["Account", "Email", "Organization", "Plan", "Seat", "Status", "5-hour", "7-day", "Usage"]
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


# Rotation log: one JSON line per decision or failure in ~/.claude-acct/rotation.log, mirrored to
# a shared Google Sheet (an Apps Script web app) so the machines of the pool can be compared.
LOG_URL = "https://script.google.com/macros/s/AKfycbzYV_mDP8TZfoxRNThhyiqdzHpIiBdINEizW1RNVp1C1pBRkShhaoEai1w0RfAihJ_9TA/exec"  # Apps Script web app (scripts/log_sheet.gs); empty keeps the log local
LOG_MAX_BYTES = 5_000_000
HEARTBEAT_EVERY = 600  # seconds; at most one heartbeat per machine
QUIET_EVERY = 1800  # seconds; repeated states (no destination, unreadable usage) are logged at most this often
_hook = None  # the hook event that started this process, set by main()


def _log_url():
    return os.environ.get("CLAUDE_ACCT_LOG_URL") or load_rotation().get("logUrl") or LOG_URL


def plugin_version():
    return (read_json(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), {}) or {}).get("version", "?")


def machine_name():
    try:
        r = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True, text=True, timeout=3)
        name = r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        name = ""
    return name or platform.node()


def log_event(event, message="", every=None, key=None, **fields):
    """Record one rotation event. `every` (seconds) throttles events sharing `key`. Never raises."""
    try:
        os.makedirs(store_dir(), exist_ok=True)
        now = time.time()
        if every:
            state_path = os.path.join(store_dir(), "log-state.json")
            state = read_json(state_path, {}) or {}
            key = key or event
            if now - state.get(key, 0) < every:
                return
            state[key] = now
            write_json_atomic(state_path, state)
        rec = {"ts": dt.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S"), "machine": machine_name(),
               "host": platform.node(), "version": plugin_version(), "hook": _hook or "manual", "event": event}
        rec.update({k: v for k, v in fields.items() if v is not None})
        rec["message"] = message
        line = json.dumps(rec, ensure_ascii=False) + "\n"
        path = os.path.join(store_dir(), "rotation.log")
        if os.path.exists(path) and os.path.getsize(path) > LOG_MAX_BYTES:
            os.replace(path, path + ".1")
        with open(path, "a", encoding="utf-8") as f:
            f.write(line)
        if _log_url():
            with open(os.path.join(store_dir(), "log-outbox.jsonl"), "a", encoding="utf-8") as f:
                f.write(line)
            _spawn_log_flush()
    except Exception:  # the log must never break a hook
        pass


def _spawn_log_flush():
    """Upload the outbox in a detached process, so a slow network never delays a prompt or tool call."""
    subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "log-flush"],
                     stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     start_new_session=True)


def cmd_log_flush(args):
    url = _log_url()
    if not url:
        return
    lock_path = os.path.join(store_dir(), "log-flush.lock")
    if not _acquire_lock(lock_path):
        return  # the running flush, or the next one, sends these events too
    try:
        outbox = os.path.join(store_dir(), "log-outbox.jsonl")
        sending = outbox + ".sending"  # left over when a previous upload failed: send it first
        if not os.path.exists(sending):
            if not os.path.exists(outbox):
                return
            os.replace(outbox, sending)
        events = []
        with open(sending, encoding="utf-8") as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except ValueError:
                    pass
        if events:
            body = json.dumps({"events": events}, ensure_ascii=False).encode("utf-8")
            req = urllib.request.Request(url, data=body, headers={"Content-Type": "application/json"}, method="POST")
            with urlopen(req, timeout=20) as r:
                if r.read(16).decode("utf-8", "replace").strip() != "ok":
                    return
        os.unlink(sending)
    except Exception:
        pass
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


def _logged_error(message):
    """An AcctError for a state that was just logged under its own event, so it is not logged again as an error."""
    e = AcctError(message)
    e.logged = True
    return e


def _usage_fields(meta):
    values = _usage_window_values(((meta or {}).get("usage") or {}).get("data") or {})
    return {"p5h": values.get("five_hour"), "p7d": values.get("seven_day")}


# Rotation: the Premium account reaches 5-hour >= DAILY_THRESHOLD or 7-day >= WEEKLY_THRESHOLD.
DAILY_THRESHOLD = 5
WEEKLY_THRESHOLD = 10
LEGACY_DAILY_DEFAULT = 90  # written by `configure` before 0.9.1 when no --threshold was given
# A Premium seat has 5x the limits of a Standard seat (6.25x vs 1.25x Pro, per the Team plan help page),
# so a percentage read on one seat is about PREMIUM_FACTOR times the same usage read on the other.
PREMIUM_FACTOR = 5
PREMIUM_CHECK_TTL = 30  # seconds; how stale the Premium account's seat and usage may be in a check
ROTATION_JITTER = 5  # seconds; machines that reach the threshold together do not move the seat at once


def _rotation_threshold(cfg):
    value = cfg.get("dailyThreshold", cfg.get("threshold"))
    # Configs saved with the old default follow the current default without re-running `configure`.
    return DAILY_THRESHOLD if value in (None, LEGACY_DAILY_DEFAULT) else value


def _weekly_threshold(cfg):
    # Not set by `configure`, so configs written by earlier versions get it without re-running anything.
    return cfg.get("weeklyThreshold", WEEKLY_THRESHOLD)


def _usage_window_values(usage):
    result = {}
    for key in ("five_hour", "seven_day"):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            result[key] = window["utilization"]
    return result


def _usage_reached_threshold(usage, daily_threshold, weekly_threshold=100):
    """Rotate the Premium account at the 5-hour or 7-day threshold, or when any window is used up.

    Readings of the account holding Premium are measured against Premium limits.
    """
    values = _usage_window_values(usage)
    return (values.get("five_hour", -1) >= daily_threshold or values.get("seven_day", -1) >= weekly_threshold
            or any(v >= 100 for v in values.values()))


def cmd_auto_configure(args):
    idx = load_index()
    premium = resolve_name(idx, args.premium)
    order = [resolve_name(idx, item.strip()) for item in args.order.split(",") if item.strip()]
    if len(order) < 2 or len(set(order)) != len(order):
        raise AcctError("Rotation order must contain at least two different saved accounts.")
    if premium not in order:
        raise AcctError("The current Premium account must be included in the rotation order.")
    daily_threshold = next((t for t in (args.daily_threshold, args.threshold) if t is not None), None)
    if daily_threshold is not None and not 1 <= daily_threshold <= 99:
        raise AcctError("The daily (5-hour) threshold must be between 1 and 99.")
    cfg = {
        "enabled": False,
        "order": order,
        "premiumAccount": premium,
        "membersUrl": args.members_url,
        "cdpUrl": args.cdp_url,
        "browserProfile": os.path.abspath(os.path.expanduser(args.browser_profile)),
    }
    if daily_threshold is not None:  # left out, the default applies and follows plugin updates
        cfg["dailyThreshold"] = daily_threshold
    save_rotation(cfg)
    print("Rotation configured: Premium %s; pool %s. Rotates at %s. The next Premium is the pool account with the lowest 7-day, then 5-hour usage. Rotation is OFF; run `acct auto enable` after setup."
          % (premium, ", ".join(order), _trigger_text(cfg)))


def _trigger_text(cfg):
    return "5-hour >= %s%% or 7-day >= %s%% (or any window at 100%%)" % (
        _rotation_threshold(cfg), _weekly_threshold(cfg))


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
    print("Rotates at: %s" % _trigger_text(cfg))
    print("Next Premium: the least-used account (lowest 7-day, then 5-hour usage); ties follow the order")
    print("Rotation order: %s" % " → ".join(cfg["order"]))
    print("Current Premium account: %s" % cfg.get("premiumAccount", "?"))
    print("Browser profile: %s" % cfg.get("browserProfile", "(CDP)"))


def cmd_auto_browser(args):
    cfg = load_rotation()
    if not cfg.get("order"):
        raise AcctError("Configure rotation first with `acct auto configure`.")
    ensure_node_deps()
    node = os.environ.get("CLAUDE_ACCT_NODE", "node")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seat_manager.mjs")
    env = seat_manager_env(cfg)
    try:
        subprocess.Popen([node, script, "--setup"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
    except OSError as e:
        raise AcctError("Could not open the persistent Chrome profile: %s" % e)
    print("Opened Chrome with the saved automation profile. Sign in as an organization owner/admin if needed. Leave this profile available for seat automation; the login is saved for future rotations.")


def _parse_reset(value):
    try:
        return dt.datetime.fromisoformat(value).timestamp()
    except (TypeError, ValueError):
        return None


def _mark_used_up(cfg, name, usage, weekly_threshold=100):
    """Remember until when a Premium account left with a used-up (or 7-day threshold) window cannot take the seat back."""
    limits = {"five_hour": 100, "seven_day": min(weekly_threshold, 100)}
    resets = [_parse_reset((usage.get(k) or {}).get("resets_at"))
              for k, v in _usage_window_values(usage).items() if v >= limits[k]]
    resets = [r for r in resets if r]
    if resets:
        cfg.setdefault("usedUpUntil", {})[name] = max(resets)


def _next_eligible_rotation_account(cfg, idx, daily_threshold, weekly_threshold=100):
    """Pick the least-used Standard account after checking the live status and usage of each.

    Standard members share the same limits, so their 7-day percentages compare actual use:
    lowest 7-day first, then lowest 5-hour, then the configured order after the current Premium.
    An account whose usage would already be at a threshold once read against Premium limits
    (Standard reading / PREMIUM_FACTOR) is skipped: it would hand the seat on at once.
    """
    order = cfg.get("order", [])
    current = cfg.get("premiumAccount")
    start = order.index(current)
    used_up = {n: t for n, t in (cfg.get("usedUpUntil") or {}).items() if t > time.time()}
    cfg["usedUpUntil"] = used_up
    skipped, candidates = [], []
    for offset in range(1, len(order)):
        name = order[(start + offset) % len(order)]
        meta = idx["accounts"].get(name)
        if not meta:
            skipped.append("%s (not saved)" % name)
            continue
        if name in used_up:
            # It ran out as Premium; taking the seat back before the reset would bounce straight off again.
            skipped.append("%s (used up as Premium until %s)" % (
                name, dt.datetime.fromtimestamp(used_up[name]).strftime("%d/%m %H:%M")))
            continue
        try:
            creds = read_stored_creds(name)
        except AcctError:
            skipped.append("%s (credentials unavailable)" % name)
            continue
        if token_state(meta, creds) == "relogin":
            skipped.append("%s (login expired/revoked)" % name)
            continue
        # Destinations are never the live login (rotation only runs while that is the Premium account).
        status = refresh_usage(meta, creds, refresh_name=name)
        usage = (meta.get("usage") or {}).get("data") or {}
        values = _usage_window_values(usage)
        # Fail closed: do not move Premium onto an account without a fresh 5-hour
        # reading. refresh_usage uses its short cache to avoid 429s.
        if status not in ("ok", "cache") or "five_hour" not in values:
            skipped.append("%s (usage unavailable)" % name)
            continue
        if (values["five_hour"] / PREMIUM_FACTOR >= daily_threshold
                or values.get("seven_day", 0) / PREMIUM_FACTOR >= weekly_threshold):
            skipped.append("%s (already at threshold)" % name)
            continue
        candidates.append((values.get("seven_day", 100), values["five_hour"], offset, name))
    if not candidates:
        return None, skipped
    return min(candidates)[3], skipped


PLUGIN_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def ensure_node_deps():
    """Install playwright-core next to the plugin the first time seat automation needs it.

    Every plugin version gets its own cache directory, so this also covers updates.
    """
    if os.path.isfile(os.path.join(PLUGIN_ROOT, "node_modules", "playwright-core", "package.json")):
        return
    npm = shutil.which("npm")
    if not npm:
        raise AcctError("npm was not found. Install Node.js 20+ (it includes npm), then run /acct:doctor.")
    r = subprocess.run([npm, "ci", "--omit=dev", "--no-audit", "--no-fund"], cwd=PLUGIN_ROOT,
                       capture_output=True, text=True, timeout=300)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "unknown error").strip()
        raise AcctError("Could not install playwright-core with npm in %s: %s" % (PLUGIN_ROOT, detail[-600:]))


def find_chrome():
    """Return the Chrome executable seat_manager.mjs will start, or None."""
    if os.environ.get("CLAUDE_ACCT_CHROME"):
        return shutil.which(os.environ["CLAUDE_ACCT_CHROME"]) or (
            os.environ["CLAUDE_ACCT_CHROME"] if os.path.isfile(os.environ["CLAUDE_ACCT_CHROME"]) else None)
    if sys.platform == "darwin":
        app = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        return app if os.path.isfile(app) else None
    if sys.platform == "win32":
        for base in (os.environ.get("PROGRAMFILES"), os.environ.get("PROGRAMFILES(X86)"), os.environ.get("LOCALAPPDATA")):
            exe = os.path.join(base or "", "Google", "Chrome", "Application", "chrome.exe")
            if base and os.path.isfile(exe):
                return exe
        return shutil.which("chrome.exe")
    for name in ("google-chrome", "google-chrome-stable", "chromium", "chromium-browser"):
        if shutil.which(name):
            return shutil.which(name)
    return None


def seat_manager_env(cfg):
    env = dict(os.environ)
    env["CLAUDE_ACCT_MEMBERS_URL"] = cfg.get("membersUrl", "https://claude.ai/admin-settings/members")
    env["CLAUDE_ACCT_CDP_URL"] = cfg.get("cdpUrl", "http://127.0.0.1:9222")
    env["CLAUDE_ACCT_BROWSER_PROFILE"] = cfg.get("browserProfile", os.path.join(store_dir(), "browser-profile"))
    chrome = find_chrome()
    if chrome and sys.platform != "darwin":  # macOS starts Chrome with `open -a`; elsewhere seat_manager.mjs needs the path
        env["CLAUDE_ACCT_CHROME"] = chrome
    return env


def cmd_doctor(args):
    """Check everything the plugin needs, install the Node dependency, and say what is left to do."""
    rows, todo = [], []

    def check(name, ok, detail, fix=None):
        rows.append(("✅" if ok else "❌", name, detail))
        if not ok and fix:
            todo.append(fix)

    check("Python", True, "%d.%d" % sys.version_info[:2])
    node = shutil.which(os.environ.get("CLAUDE_ACCT_NODE", "node"))
    node_version = None
    if node:
        try:
            node_version = subprocess.run([node, "--version"], capture_output=True, text=True, timeout=10).stdout.strip()
        except (OSError, subprocess.TimeoutExpired):
            pass
    try:
        node_ok = int((node_version or "v0").lstrip("v").split(".")[0]) >= 20
    except ValueError:
        node_ok = False
    check("Node.js 20+", node_ok, node_version or "not found", "Install Node.js 20 or later (https://nodejs.org), then run /acct:doctor again.")
    if node_ok:
        try:
            ensure_node_deps()
            check("playwright-core", True, "installed in %s" % PLUGIN_ROOT)
        except (AcctError, OSError, subprocess.TimeoutExpired) as e:
            check("playwright-core", False, str(e), "Fix the npm error above, then run /acct:doctor again.")
    else:
        check("playwright-core", False, "needs Node.js first")
    try:
        fetch_usage("doctor-check")  # no valid token: a 401 proves HTTPS works
        check("HTTPS to api.anthropic.com", True, "ok")
    except urllib.error.HTTPError:
        check("HTTPS to api.anthropic.com", True, "ok" + (" (macOS system certificates)" if _ssl_context else ""))
    except Exception as e:
        cert = isinstance(getattr(e, "reason", None), ssl.SSLCertVerificationError)
        check("HTTPS to api.anthropic.com", False, net_error(e),
              ("This Python (%s) has no CA certificates. python.org on macOS: run \"/Applications/Python %d.%d/Install Certificates.command\", "
               "or `%s -m pip install certifi`." % (sys.executable, sys.version_info[0], sys.version_info[1], sys.executable)) if cert
              else "Check the network, proxy or firewall for api.anthropic.com.")
    chrome = find_chrome()
    check("Google Chrome", bool(chrome), chrome or "not found",
          "Install Google Chrome (or set CLAUDE_ACCT_CHROME to its executable).")
    try:
        read_live_creds()
        live = read_live_profile() or {}
        check("Claude Code login", True, "%s (%s)" % (live.get("emailAddress", "?"), live.get("organizationName", "?")))
    except AcctError:
        check("Claude Code login", False, "not logged in", "Log in to Claude Code with /login.")
    idx = load_index()
    names = sorted(idx["accounts"])
    cur = current_name(idx)
    check("Saved accounts", len(names) >= 2, ", ".join(names) or "none",
          "Save every account of the pool: /acct:save <name> for the current one, /acct:login <name> <email> for each other one.")
    if names and not cur:
        todo.append("The current Claude Code login is not saved: run /acct:save <name>.")
    cfg = load_rotation()
    if not cfg.get("order"):
        check("Rotation", False, "not configured",
              "Configure rotation: /acct:auto configure --premium <current Premium account> --order <premium>,<next>,...")
    else:
        check("Rotation", True, "%s, Premium: %s, rotates at %s, next Premium: least used of %s" % (
            "ON" if cfg.get("enabled") else "OFF", cfg.get("premiumAccount"), _trigger_text(cfg), ", ".join(cfg["order"])))
        if not os.path.isdir(cfg.get("browserProfile") or ""):
            todo.append("Open the admin browser profile once and sign in as an organization owner/admin: /acct:auto browser")
        if not cfg.get("enabled"):
            todo.append("When the steps above are done: /acct:auto enable")
    print("| | Check | Detail |")
    print("|---|---|---|")
    for r in rows:
        print("| %s | %s | %s |" % r)
    if todo:
        print("\nTo do:")
        for i, t in enumerate(todo, 1):
            print("%d. %s" % (i, t))
    else:
        print("\nEverything is ready.")


FOLLOW_GRACE = 300  # seconds after this machine moved the seat; see _follow_premium_seat


def _seat_tier(idx, name, live, force=False, ttl=USAGE_CACHE_TTL):
    """Return (status, tier) of a saved account's seat. The live login's token is never refreshed here."""
    try:
        creds = read_stored_creds(name)
    except AcctError:
        return "credentials unavailable", None
    meta = idx["accounts"][name]
    status = refresh_seat(meta, creds, force=force, refresh_name=None if name == live else name, ttl=ttl)
    return status, (meta.get("seat") or {}).get("tier")


def _find_premium_holder(cfg, idx, live, force=False, exclude=None):
    """Return the pool account that holds the Premium seat, or None if no pool account confirms it.

    The seat can move without this machine: by hand on the Members page, or by another machine
    that shares the pool. Unconfirmed readings keep the configured account; `exclude` is never
    taken as the holder.
    """
    premium = cfg.get("premiumAccount")
    status, tier = _seat_tier(idx, premium, live, force, ttl=PREMIUM_CHECK_TTL)
    if status not in ("ok", "cache") or tier == PREMIUM_TIER:
        return premium
    for name in cfg.get("order", []):
        if name in (premium, exclude) or name not in idx["accounts"]:
            continue
        # Fresh readings: a cached Premium may be the seat this machine saw before it moved.
        status, tier = _seat_tier(idx, name, live, force=True)
        if status == "ok" and tier == PREMIUM_TIER:
            return name
    return None


def _forget_seat_readings(idx, *names):
    """Drop cached seat and usage of accounts whose seat changed: a percentage read against Standard
    limits would be taken for a Premium one (5x too high) and trigger another rotation."""
    for name in names:
        meta = idx["accounts"].get(name) or {}
        meta.pop("seat", None)
        meta.pop("usage", None)


def _follow_premium_seat(cfg, idx, cur, args):
    """Point the config and this session at the account holding Premium. Return True if done for this check."""
    premium = cfg.get("premiumAccount")
    # Right after this machine moved the seat, the profile API may still show the account it left as
    # Premium: never follow the seat back there. A move to any other account is followed at once.
    recent = time.time() - (cfg.get("rotatedAt") or 0) < FOLLOW_GRACE
    holder = _find_premium_holder(cfg, idx, cur or current_name(idx), force=args.force,
                                  exclude=cfg.get("rotatedFrom") if recent else None)
    save_index(idx)
    if holder is None:
        # Rotating would ask the Members page to demote an account that is not Premium.
        log_event("no_holder", "%s is not Premium and no saved pool account holds the seat" % premium,
                  every=QUIET_EVERY, live=cur, premium=premium)
        if args.manual:
            raise _logged_error("%s no longer holds the Premium seat, and no saved account in the rotation order does. "
                            "No seat or account was changed." % premium)
        return True
    if holder != premium:
        cfg["premiumAccount"] = holder
        cfg.pop("rotationHold", None)
        save_rotation(cfg)
        holder_seat = idx["accounts"][holder].get("seat")
        _forget_seat_readings(idx, premium, holder)
        idx["accounts"][holder]["seat"] = holder_seat  # just read fresh: it is Premium
        save_index(idx)
        print("The Premium seat moved from %s to %s outside this machine." % (premium, holder))
        log_event("follow", "seat moved outside this machine", live=cur, premium=premium, target=holder)
    if cur == holder:
        return False
    _logged_switch(cur, holder)
    return True


def _logged_switch(cur, target):
    try:
        cmd_switch(argparse.Namespace(name=target, force=False, no_wait=False))
    except Exception as e:
        log_event("switch_fail", str(e)[-500:], live=cur, target=target)
        e.logged = True
        raise
    log_event("switch_ok", "", live=cur, target=target)


def _set_premium_seat(old_name, new_name, cfg, idx):
    old_email = ((idx["accounts"].get(old_name, {}).get("profile") or {}).get("emailAddress") or "").strip()
    new_email = ((idx["accounts"].get(new_name, {}).get("profile") or {}).get("emailAddress") or "").strip()
    if not old_email or not new_email:
        raise AcctError("Both accounts need a saved email address before changing seat tiers.")
    ensure_node_deps()
    node = os.environ.get("CLAUDE_ACCT_NODE", "node")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seat_manager.mjs")
    env = seat_manager_env(cfg)
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
    try:
        cur = sync_current(idx)
    except AcctError:
        cur = current_name(idx)
    # A login outside the pool (a personal plan, an unsaved account) is left alone.
    if cur != premium and cur not in cfg.get("order", []):
        return
    if _follow_premium_seat(cfg, idx, cur, args):
        return
    premium = cfg["premiumAccount"]
    try:
        creds = read_stored_creds(premium)
    except AcctError as e:
        raise AcctError("Automatic rotation could not read the Premium account: %s" % e)
    meta = idx["accounts"][premium]
    status = refresh_usage(meta, creds, force=args.force, ttl=PREMIUM_CHECK_TTL)
    if status not in ("ok", "cache"):
        log_event("read_fail", "Premium usage: %s" % status, every=QUIET_EVERY, live=cur, premium=premium)
        save_index(idx)
        if args.manual:
            raise _logged_error("Cannot check Premium usage (%s); rotation did not run." % status)
        return
    usage = (meta.get("usage") or {}).get("data") or {}
    daily_threshold = _rotation_threshold(cfg)
    weekly_threshold = _weekly_threshold(cfg)
    values = _usage_window_values(usage)
    save_index(idx)
    if cfg.get("rotationHold") == premium:
        if not values or _usage_reached_threshold(usage, daily_threshold, weekly_threshold):
            return
        cfg.pop("rotationHold", None)
        save_rotation(cfg)
    if not values or not _usage_reached_threshold(usage, daily_threshold, weekly_threshold):
        return
    old_name = premium
    _mark_used_up(cfg, premium, usage, weekly_threshold)
    new_name, skipped = _next_eligible_rotation_account(cfg, idx, daily_threshold, weekly_threshold)
    save_rotation(cfg)
    save_index(idx)
    fields = dict(live=cur, premium=premium, **_usage_fields(meta))
    if not new_name:
        log_event("no_destination", "; ".join(skipped), every=QUIET_EVERY, **fields)
        if args.manual:
            raise _logged_error("No eligible account for Premium: every other account is at the usage threshold or has an expired login / unavailable usage. "
                            "No seat or account was changed. Checked: %s" % (", ".join(skipped) or "none"))
        return
    if args.manual:
        reached = []
        if values.get("five_hour", -1) >= daily_threshold:
            reached.append("5-hour %d%% (threshold %d%%)" % (round(values["five_hour"]), daily_threshold))
        if values.get("seven_day", -1) >= weekly_threshold:
            reached.append("7-day %d%% (threshold %d%%)" % (round(values["seven_day"]), weekly_threshold))
        elif values.get("seven_day", -1) >= 100:
            reached.append("7-day %d%% (used up)" % round(values["seven_day"]))
        print("Usage threshold reached for %s. Transferring Premium from %s to %s..." % (" and ".join(reached), old_name, new_name))
    # Other machines may reach the threshold at the same moment. Wait a random moment, then confirm
    # with a fresh reading that the seat has not moved meanwhile; if it has, follow it instead.
    time.sleep(random.uniform(0, ROTATION_JITTER))
    status, tier = _seat_tier(idx, premium, cur, force=True)
    if status == "ok" and tier != PREMIUM_TIER:
        log_event("move_skipped", "seat moved by another machine during the wait", target=new_name, **fields)
        save_index(idx)
        _follow_premium_seat(cfg, idx, cur, argparse.Namespace(force=True, manual=args.manual))
        return
    log_event("move_start", "threshold reached", target=new_name, **fields)
    started = time.time()
    try:
        _set_premium_seat(old_name, new_name, cfg, idx)
    except Exception as e:
        log_event("move_fail", "%ds: %s" % (time.time() - started, str(e)[-800:]), target=new_name, **fields)
        e.logged = True
        raise
    log_event("move_ok", "%ds" % (time.time() - started), target=new_name, **fields)
    # Change credentials only after the Members page confirms the tier change.
    _logged_switch(cur, new_name)
    cfg["premiumAccount"] = new_name
    cfg["rotatedAt"] = time.time()
    cfg["rotatedFrom"] = old_name
    cfg.pop("rotationHold", None)
    save_rotation(cfg)
    idx = load_index()  # cmd_switch saved its own copy
    _forget_seat_readings(idx, old_name, new_name)
    save_index(idx)


def _acquire_lock(lock_path, wait=0):
    """Return True once this process holds the lock file, False if it stays busy for `wait` seconds."""
    deadline = time.time() + wait
    while True:
        try:
            os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            pass
        try:
            if time.time() - os.path.getmtime(lock_path) > 300:
                os.unlink(lock_path)
                continue
        except FileNotFoundError:
            continue
        if time.time() >= deadline:
            return False
        time.sleep(2)


def _heartbeat(cfg):
    try:
        idx = load_index()
        premium = cfg.get("premiumAccount")
        log_event("heartbeat", "rotation %s; %s" % ("ON" if cfg.get("enabled") else "OFF", _trigger_text(cfg)),
                  every=HEARTBEAT_EVERY, live=current_name(idx), premium=premium,
                  **_usage_fields(idx["accounts"].get(premium)))
    except Exception:
        pass


def cmd_auto_check(args, lock_wait=0):
    cfg = load_rotation()
    if cfg.get("order"):
        _heartbeat(cfg)
    if not cfg.get("enabled"):
        return
    lock_path = os.path.join(store_dir(), "rotation.lock")
    os.makedirs(store_dir(), exist_ok=True)
    if not _acquire_lock(lock_path, lock_wait):
        return
    try:
        _cmd_auto_check_locked(args)
    except Exception as e:
        if not getattr(e, "logged", False):
            log_event("error", ("%s: %s" % (type(e).__name__, e))[-800:])
        raise
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


# Exit code 2 from an `asyncRewake` hook wakes the model with this text (stderr) in the same session.
REWAKE_EXIT = 2
REWAKE_MESSAGE = ("The claude.ai usage limit of account %s was reached, so the acct plugin switched this session to "
                  "account %s, which holds the organization's Premium seat and has usage available. Continue the task you were "
                  "working on when the limit was reached; do not repeat work that is already complete.")


def cmd_auto_recover(args):
    """Seat rotation after Claude Code reports a quota rate limit; wakes the model when the account changed.

    Runs as an `asyncRewake` StopFailure hook: in the background, so the seat transfer and the
    Keychain cache wait do not block the UI. Exit 2 makes Claude Code continue the stopped task.
    """
    try:
        event = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    if event.get("error") != "rate_limit":
        return
    if not load_rotation().get("enabled"):
        return
    before = current_name(load_index())
    # StopFailure's rate_limit category also covers ordinary API throttling.
    # Reuse normal checks: live usage must confirm the configured threshold, and
    # the destination must have fresh usage below both thresholds. Wait for a
    # check that is already rotating instead of skipping it.
    try:
        cmd_auto_check(argparse.Namespace(force=True, manual=False), lock_wait=240)
    finally:
        after = current_name(load_index())
        log_event("recover", "usage-limit stop; %s" % ("switched, task continues" if before and after and after != before
                                                        else "no switch, session stays stopped"),
                  live=before, target=after)
    if before and after and after != before:
        sys.stderr.write(REWAKE_MESSAGE % (before, after))
        sys.exit(REWAKE_EXIT)


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
    # acct.sh runs Python with -I, which ignores PYTHONIOENCODING. On Windows, pipes (how hooks and
    # skills read the output) default to the ANSI code page, which cannot encode ▶ / 🔴 / ✅.
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError):
            pass
    ap = argparse.ArgumentParser(prog="claude-acct", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="log in to a new account / log in again (opens a terminal)"); p.add_argument("name", nargs="?"); p.add_argument("email", nargs="?"); p.set_defaults(fn=cmd_login)
    p = sub.add_parser("import-login", help=argparse.SUPPRESS); p.add_argument("dir"); p.add_argument("name"); p.add_argument("expect_email", nargs="?"); p.set_defaults(fn=cmd_import_login)
    p = sub.add_parser("save", help="save the current account"); p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_save)
    p = sub.add_parser("list", help="list accounts + usage"); p.add_argument("--refresh", action="store_true"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("switch", help="switch account"); p.add_argument("name", nargs="?"); p.add_argument("--force", action="store_true"); p.add_argument("--no-wait", action="store_true"); p.set_defaults(fn=cmd_switch)
    p = sub.add_parser("remove", help="remove an account from the list"); p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_remove)
    p = sub.add_parser("doctor", help="check requirements, install the Node dependency, list remaining setup steps"); p.set_defaults(fn=cmd_doctor)
    p = sub.add_parser("auto", help="configure automatic Premium seat rotation")
    autos = p.add_subparsers(dest="auto_cmd", required=True)
    q = autos.add_parser("configure", help="set account rotation order and organization page")
    q.add_argument("--premium", required=True); q.add_argument("--order", required=True)
    q.add_argument("--daily-threshold", type=int, help="5-hour usage trigger (default: 5%%)")
    q.add_argument("--threshold", type=int, help="same as --daily-threshold")
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
    q.add_argument("--hook", help=argparse.SUPPRESS)
    q.set_defaults(fn=cmd_auto_check)
    q = autos.add_parser("recover", help=argparse.SUPPRESS)
    q.set_defaults(fn=cmd_auto_recover, hook="StopFailure")
    p = sub.add_parser("log-flush", help=argparse.SUPPRESS); p.set_defaults(fn=cmd_log_flush)
    args = ap.parse_args(argv)
    global _hook
    _hook = getattr(args, "hook", None)
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
