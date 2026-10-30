#!/usr/bin/env python3
"""claude-acct: manage multiple Claude Code accounts (claude.ai login) on one machine.

Commands:
  login  [<name|email> [email]] Log in to an account (opens a terminal); without one, every pool account this machine lacks
  auto   join|mover|enable|disable|status|browser   The XTGroup pool: switching (every machine), seat moves (machine 2)
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
USAGE_RETRY_DEFAULT = 300  # seconds to wait after a 429 without a Retry-After header
# OAuth refresh as Claude Code 2.1.29x does it (unofficial); access tokens last about 8 hours.
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
PROFILE_URL = "https://api.anthropic.com/api/oauth/profile"  # unofficial; organization.seat_tier is the member's seat
SEAT_LABELS = {"team_standard": "Standard", "team_tier_1": "Premium"}
PREMIUM_TIER = "team_tier_1"
STANDARD_TIER = "team_standard"
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
    blocked = meta.get("usage_blocked_until") or 0
    if now < blocked:  # asking again before Retry-After only keeps the account blocked longer
        return "rate limited until %s" % time.strftime("%H:%M", time.localtime(blocked))
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
        if e.code == 429:  # the usage API limits each account: wait as long as it says
            try:
                wait = int(e.headers.get("Retry-After") or USAGE_RETRY_DEFAULT)
            except (TypeError, ValueError):
                wait = USAGE_RETRY_DEFAULT
            meta["usage_blocked_until"] = now + max(wait, 1)
            return "rate limited until %s" % time.strftime("%H:%M", time.localtime(now + max(wait, 1)))
        return "http %d" % e.code
    except Exception as e:  # network error, timeout, ...
        return "error: %s" % net_error(e)
    meta["usage"] = {"fetchedAt": now, "data": data}
    meta.pop("revoked", None)
    meta.pop("usage_blocked_until", None)
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
    for path in (os.path.expanduser("~/.local/bin/claude.exe"), os.path.expanduser("~/.local/bin/claude")):
        if os.path.isfile(path):  # the native installer's location, before a new session has it on PATH
            return path
    raise AcctError("Could not find the claude executable. Run this inside Claude Code, or add claude to PATH.")


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def _backup_live_login():
    if IS_MAC:
        # Back up the live login in case the Keychain is not keyed by CLAUDE_CONFIG_DIR (see cmd_import_login).
        main = _kc_find(LIVE_SERVICE, getpass.getuser())
        if main:
            _kc_write(STORE_SERVICE, BACKUP_NAME, main)


def cmd_login_backup(args):
    _backup_live_login()


def _login_all():
    """Log in, one after the other in one terminal window, to every pool account this machine lacks."""
    idx = load_index()
    todo = []
    for email in POOL_EMAILS:
        name = _saved_name(idx, email)
        if name:
            try:
                if token_state(idx["accounts"][name], read_stored_creds(name)) != "relogin":
                    continue
            except AcctError:
                pass
        todo.append((name or email, email))
    if not todo:
        print("All %d pool accounts are logged in on this machine." % len(POOL_EMAILS))
        return
    acct_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acct.sh")
    work = os.path.join(store_dir(), "login-all")
    shutil.rmtree(work, ignore_errors=True)
    os.makedirs(work)
    helper = os.path.join(work, "login-all.sh")
    with open(helper, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/usr/bin/env bash\n")
        for n, (name, email) in enumerate(todo, 1):
            tmp = os.path.join(store_dir(), "login-" + name)
            f.write("echo; echo '=== (%d/%d) %s ==='\n" % (n, len(todo), email))
            f.write("read -r -p 'Press Enter to log in %s, or type s and Enter to skip it: ' A; "
                    "if [ \"$A\" != s ]; then\n" % email)
            f.write("rm -rf %s; mkdir -p %s\n" % (sh_quote(tmp), sh_quote(tmp)))
            f.write("bash %s login-backup\n" % sh_quote(acct_sh))
            f.write("CLAUDE_CONFIG_DIR=%s %s auth login --email %s\n" % (sh_quote(tmp), sh_quote(claude_bin()), sh_quote(email)))
            f.write("bash %s import-login %s %s %s || echo 'Not saved: %s. Run /acct:login %s later.'\n" % (
                sh_quote(acct_sh), sh_quote(tmp), sh_quote(name), sh_quote(email), email, email))
            f.write("fi\n")
        f.write("echo; echo 'Done. You can close this window and go back to Claude: /acct:list'\n")
    os.chmod(helper, 0o700)
    if open_terminal(helper):
        print("Opened a **terminal window** to log in to %d account(s), one after the other: %s." % (
            len(todo), ", ".join(e for _, e in todo)))
        print("For each one: claude.ai refuses Claude Code to an account without a seat, so first give it a seat on the "
              "Members page by hand (and take it back after). Press Enter in the terminal, open the login link in a "
              "**private browser window**, sign in as that account, authorize, and paste the whole code into the terminal "
              "(Windows Git Bash: Shift+Insert). Your current account is NOT changed.")
    else:
        print("Could not open a terminal window. Open a terminal and run:\n\n    bash %s" % sh_quote(helper))


def cmd_login(args):
    if not args.name:
        return _login_all()
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
    _backup_live_login()
    login_cmd = [claude_bin(), "auth", "login"] + (["--email", args.email] if args.email else [])
    helper = os.path.join(tmp, "login.sh")
    with open(helper, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write("echo '=== Claude account login: %s ==='\n" % args.name)
        acct_sh = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acct.sh")
        f.write("export CLAUDE_CONFIG_DIR=%s\n" % sh_quote(tmp))
        f.write(" ".join(sh_quote(a) for a in login_cmd) + "\n")
        f.write("unset CLAUDE_CONFIG_DIR\n")
        f.write("bash %s import-login %s %s %s\n" % (sh_quote(acct_sh), sh_quote(tmp),
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
            bash = os.environ.get("CLAUDE_CODE_GIT_BASH_PATH") or shutil.which("bash") or next(
                (b for b in (r"C:\Program Files\Git\bin\bash.exe", r"C:\Program Files (x86)\Git\bin\bash.exe")
                 if os.path.isfile(b)), "bash")
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
    # By identity: a failed token sync must not make the live login look unsaved.
    cur = live = current_name(idx)  # never refresh the token Claude Code is using
    sync_error = None
    if cur:
        try:
            sync_current(idx)
        except AcctError as e:
            sync_error = str(e)
    live_profile = read_live_profile()
    pool, machines = _pool_view()
    # Pool accounts without a seat have no usage to read (claude.ai answers 429): read only the Premium account
    # and the backup account with the Standard seat, plus any account outside the pool.
    seated = {(pool.get("premium") or "").lower(), _backup_of(pool)} if pool else None
    rows, need_login = [], []
    for name in sorted(idx["accounts"]):
        meta = idx["accounts"][name]
        email = ((meta.get("profile") or {}).get("emailAddress") or "").lower()
        unseated = seated is not None and email in POOL_EMAILS and email not in seated
        try:
            creds = read_stored_creds(name)
            if unseated:
                status = "no seat"
            else:
                status = refresh_usage(meta, creds, force=args.refresh, refresh_name=None if name == live else name)
                refresh_seat(meta, creds, force=args.refresh)
            state = token_state(meta, creds)
        except AcctError as e:
            status, state = str(e), "relogin"
        usage = {} if unseated else (meta.get("usage") or {}).get("data") or {}
        fetched = (meta.get("usage") or {}).get("fetchedAt")
        # Only flag accounts that need action; an expired access token is refreshed automatically.
        badge = ""
        if state == "relogin":
            badge = "🔴 Log in again: `/acct:login %s`" % name
            need_login.append(name)
        note = ("updated " + fmt_age(fetched)) if fetched else "no data yet"
        if unseated:
            note = "no seat: not read"
        elif status not in ("ok", "cache", "expired", "revoked") and state != "relogin":
            note += " (%s)" % status
        p = meta.get("profile") or {}
        rows.append([
            ("▶ " if name == cur else "") + name,
            p.get("emailAddress", "?"),
            p.get("organizationName", "?"),
            (meta.get("oauth") or {}).get("subscriptionType") or "?",
            "No seat" if unseated else fmt_seat(meta),
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
        email = (live_profile or {}).get("emailAddress") or "(unknown)"
        same_email = [n for n in sorted(idx["accounts"])
                      if ((idx["accounts"][n].get("profile") or {}).get("emailAddress") or "").lower() == email.lower()]
        if same_email:
            print("\nLogged in as %s, which matches saved account %s by email but not by account/organization ID. "
                  "Run /acct:login %s to save this login again." % (email, same_email[0], same_email[0]))
        else:
            print("\nLogged in as %s, but this account is not saved. Run /acct:save <name> to save it." % email)
    else:
        print("\n▶ = current account.")
        if sync_error:
            print("Could not copy the latest token of %s into the store: %s" % (cur, sync_error))
    _print_rotation_preview(idx, live, pool, machines)
    print("acct plugin %s" % plugin_version())


def _pool_view():
    """(state, machines) from the pool server, or (None, []) when this machine has not joined or it is down."""
    st = read_json(pool_settings_path(), {}) or {}
    if not (st.get("api_key") or st.get("mover_key")):
        return None, []
    try:
        return (pool_call("GET", "/api/state", mover=not st.get("api_key")),
                pool_call("GET", "/api/machines", mover=not st.get("api_key")))
    except AcctError as e:
        print("Pool server: %s" % e)
        return None, []


def _print_rotation_preview(idx, live, state, machines):
    """The pool phase, the working machines, and where the Premium seat would go next. Changes nothing."""
    print("\nAccount switching %s · backup %s · seat moves at %s." % (
        "ON" if load_rotation().get("enabled") else "OFF", (state or {}).get("backup") or BACKUP_EMAILS[0], _trigger_text()))
    if state:
        print("Pool phase: %s, Premium: %s%s" % (state.get("phase"), state.get("premium") or "not published yet",
                                                ", target: %s" % state.get("target") if state.get("phase") != "STABLE" else ""))
    working = [m for m in machines if m.get("active")]
    if working:
        print("Machines seen in the last 5 minutes: %s" % ", ".join(
            "%s%s → %s" % (m.get("name") or m.get("host"), " (machine 2)" if m.get("role") == "mover" else "", m.get("account") or "?")
            for m in working))
    holder = _saved_name(idx, (state or {}).get("premium")) or _seat_holder(idx, live)
    if not holder:
        print("Premium account: not found among the saved rotation accounts.")
        return
    usage = ((idx["accounts"][holder].get("usage") or {}).get("data")) or {}
    values = _usage_window_values(usage)
    reached = ("reached" if _usage_reached_threshold(usage) else "not reached") if values else "unknown"
    print("Premium %s: 5-hour %s, 7-day %s; threshold %s." % (
        holder, "%d%%" % round(values["five_hour"]) if "five_hour" in values else "?",
        "%d%%" % round(values["seven_day"]) if "seven_day" in values else "?", reached))
    try:
        name, skipped = _next_premium(idx, holder, (state or {}).get("snapshots"), live=live,
                                      premium_spent=any(v >= 100 for v in values.values()))
    except Exception as e:
        print("Next Premium: could not be determined (%s)." % e)
        return
    if name:
        print("Next Premium: **%s** (order by the last usage read of each: %s)" % (
            name, " → ".join(_premium_order((state or {}).get("snapshots"), _email_of(idx, holder), idx))))
    else:
        print("Next Premium: none below the thresholds; %s keeps the seat until it uses a window up. Notes:" % holder)
    for reason in skipped:
        print("- %s" % reason)


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


# ---------------------------------------------------------------- the XTGroup pool (fixed)

# The plugin serves one organization, so its accounts are fixed here: no `configure`, no order.
# The organization has two seats: one Premium (the rotation account at work) and one Standard (the backup
# account in use). Every other account has no seat, so its usage cannot be read and is never needed.
# The backup accounts are never Premium: every machine waits on the one with the Standard seat while the
# Premium seat moves. Once it reaches the same thresholds as the Premium account, machine 2 hands the
# Standard seat to the next backup account in the list (after the last, the first), so the ones at rest can reset.
BACKUP_EMAILS = ("info@xtgroup.vn", "careers@xtgroup.vn", "support@xtgroup.vn")
MANUAL_BACKUP_GRACE = 180  # seconds a backup account given the Standard seat by hand keeps it, spent or not
MANUAL_PREMIUM_GRACE = 180  # the same for an account given the Premium seat by hand (e.g. to log it in)
FORMER_BACKUP_EMAILS = ("hieule252uet@gmail.com",)  # 1.0.4-1.0.7: a machine still on it goes back to the pool
ROTATION_EMAILS = (  # the accounts that take turns holding Premium (least recently first); the order breaks ties
    "xtts@xtgroup.vn", "sales@xtgroup.vn", "contact@xtgroup.vn",
    "hieunx@xtgroup.vn", "truongnv@xtgroup.vn", "thanglt@xtgroup.vn", "kienlt@xtgroup.vn",
    "xtg1@xtgroup.vn", "xtg2@xtgroup.vn", "xtg3@xtgroup.vn", "xtg4@xtgroup.vn", "xtg5@xtgroup.vn",
    "xtg6@xtgroup.vn", "xtg7@xtgroup.vn", "xtg8@xtgroup.vn", "xtg9@xtgroup.vn", "xtg10@xtgroup.vn", "xtg11@xtgroup.vn",
)
POOL_EMAILS = BACKUP_EMAILS + ROTATION_EMAILS

# Rotation: the Premium account reaches 5-hour >= DAILY_THRESHOLD or 7-day >= WEEKLY_THRESHOLD.
DAILY_THRESHOLD = 80
WEEKLY_THRESHOLD = 90
# Machine 2 reads the usage of the two accounts with a seat (Premium, and the backup account with the Standard
# seat) once per USAGE_READ_EVERY: that one reading serves the threshold check, the usage kept for ranking and the
# dashboard table (the usage API limits each account). Seats are read every SEAT_CHECK_EVERY to notice a seat
# moved by hand, and always again right before machine 2 changes one.
USAGE_READ_EVERY = 60
SEAT_CHECK_EVERY = 120

# Pool server (server/pool_server.py on the xtts server): the shared phase, the machines, the log.
POOL_URL = "https://xtts.xtgroup.vn/pool"
STATE_TTL = 5  # seconds a machine reuses the state it last fetched
PREPARE_TIMEOUT = 120  # seconds machine 2 waits for the working machines to switch to the backup account
MOVE_BACKOFF = 900  # seconds machine 2 waits after a failed seat move before it tries again
MOVER_LABEL = "vn.xtgroup.acct-mover"  # launchd label (macOS)
MOVER_TASK = "XTTS_Acct_Mover"  # Task Scheduler name (Windows)
MOVER_INTERVAL = 30  # seconds between two runs of the seat-moving service

LOG_MAX_BYTES = 5_000_000
HEARTBEAT_EVERY = 600  # seconds; at most one heartbeat log line per machine
QUIET_EVERY = 1800  # seconds; repeated states (no destination, unreadable usage) are logged at most this often
_hook = None  # the hook event that started this process, set by main()


def _email_of(idx, name):
    return (((idx["accounts"].get(name) or {}).get("profile") or {}).get("emailAddress") or "").lower() or None


def _saved_name(idx, email):
    """The saved account with this email, whatever it was named when saved."""
    email = (email or "").lower()
    for name in sorted(idx["accounts"]):
        if _email_of(idx, name) == email:
            return name
    return None


def _trigger_text():
    return "5-hour >= %d%% or 7-day >= %d%% (or any window at 100%%)" % (DAILY_THRESHOLD, WEEKLY_THRESHOLD)


# ---------------------------------------------------------------- pool server client

def pool_settings_path():
    return os.path.join(store_dir(), "pool.json")


def pool_settings():
    """url, api_key (every machine), mover_key (machine 2 only) and this machine's id. Never in git."""
    st = read_json(pool_settings_path(), {}) or {}
    if not st.get("machine_id"):
        st["machine_id"] = hashlib.sha1(("%s|%s|%f" % (platform.node(), getpass.getuser(), time.time())).encode()).hexdigest()[:16]
        os.makedirs(store_dir(), exist_ok=True)
        write_json_atomic(pool_settings_path(), st, private=True)
    return st


def save_pool_settings(st):
    os.makedirs(store_dir(), exist_ok=True)
    write_json_atomic(pool_settings_path(), st, private=True)


def _pool_url(st=None):
    st = st if st is not None else pool_settings()
    return (os.environ.get("CLAUDE_ACCT_POOL_URL") or st.get("url") or POOL_URL).rstrip("/")


def pool_call(method, path, body=None, mover=False, timeout=4):
    """One request to the pool server. Returns the decoded JSON (or text); raises AcctError on any failure."""
    st = pool_settings()
    key = st.get("mover_key") if mover else (st.get("api_key") or st.get("mover_key"))
    if not key:
        raise AcctError("This machine has not joined the pool: run /acct:auto join <key>.")
    headers = {"Content-Type": "application/json",
               ("X-Pool-Mover-Key" if mover or not st.get("api_key") else "X-Pool-Key"): key}
    data = None if body is None else json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(_pool_url(st) + path, data=data, headers=headers, method=method)
    try:
        with urlopen(req, timeout=timeout) as r:
            raw = r.read().decode("utf-8", "replace")
            return json.loads(raw) if r.headers.get_content_type() == "application/json" else raw
    except urllib.error.HTTPError as e:
        try:
            reason = json.loads(e.read().decode("utf-8", "replace")).get("error")
        except Exception:
            reason = None
        raise AcctError("pool server answered HTTP %d for %s%s" % (e.code, path, ": " + reason if reason else ""))
    except (OSError, ValueError) as e:
        raise AcctError("pool server unreachable: %s" % net_error(e))


def _machine_report(account, at_info, mover=False):
    return {"machine_id": pool_settings()["machine_id"], "name": machine_name(), "host": platform.node(),
            "version": plugin_version(), "account": account, "at_info": bool(at_info)}


def pool_state(account, force=False):
    """Report this machine and read the shared state in one request; reuse it for STATE_TTL seconds."""
    cache_path = os.path.join(store_dir(), "pool-cache.json")
    cache = read_json(cache_path, {}) or {}
    if not force and cache.get("account") == account and time.time() - cache.get("fetched", 0) < STATE_TTL:
        return cache["state"]
    state = pool_call("POST", "/api/machine", _machine_report(account, account in BACKUP_EMAILS))
    write_json_atomic(cache_path, {"fetched": time.time(), "account": account, "state": state})
    return state


# ---------------------------------------------------------------- rotation log

def plugin_version():
    return (read_json(os.path.join(PLUGIN_ROOT, ".claude-plugin", "plugin.json"), {}) or {}).get("version", "?")


def machine_name():
    """The machine's name on the dashboard and in the log: "name" in pool.json, else the git user name."""
    name = (read_json(os.path.join(store_dir(), "pool.json"), {}) or {}).get("name")
    if name:
        return name
    try:
        r = subprocess.run(["git", "config", "--global", "user.name"], capture_output=True, text=True, timeout=3)
        name = r.stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        name = ""
    return name or platform.node()


def _log_upload_enabled():
    st = read_json(pool_settings_path(), {}) or {}
    return bool(st.get("api_key") or st.get("mover_key"))


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
        if _log_upload_enabled():
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
    if not _log_upload_enabled():
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
        if events and str(pool_call("POST", "/api/log", {"events": events}, timeout=20)).strip() != "ok":
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


# ---------------------------------------------------------------- usage rules

def _usage_window_values(usage):
    result = {}
    for key in ("five_hour", "seven_day"):
        window = usage.get(key)
        if isinstance(window, dict) and isinstance(window.get("utilization"), (int, float)):
            result[key] = window["utilization"]
    return result


def _usage_reached_threshold(usage, daily_threshold=None, weekly_threshold=None):
    """Rotate the Premium account at the 5-hour or 7-day threshold, or when any window is used up.

    Readings of the account holding Premium are measured against Premium limits.
    """
    daily = DAILY_THRESHOLD if daily_threshold is None else daily_threshold
    weekly = WEEKLY_THRESHOLD if weekly_threshold is None else weekly_threshold
    values = _usage_window_values(usage)
    return (values.get("five_hour", -1) >= daily or values.get("seven_day", -1) >= weekly
            or any(v >= 100 for v in values.values()))


def _parse_reset(value):
    try:
        return dt.datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except (AttributeError, TypeError, ValueError):
        return None


def _snapshot(meta, tier):
    """The usage just read of an account holding a `tier` seat ("premium" or "standard"), as kept on the pool server."""
    usage = (meta.get("usage") or {}).get("data") or {}
    five, seven = usage.get("five_hour") or {}, usage.get("seven_day") or {}
    return {"tier": tier, "five_hour": five.get("utilization"), "five_hour_reset": five.get("resets_at"),
            "seven_day": seven.get("utilization"), "seven_day_reset": seven.get("resets_at"),
            "at": (meta.get("usage") or {}).get("fetchedAt")}


def _remember_usage(state, email, meta, tier):
    """Keep on the pool server the last usage of an account while it holds a seat; it ranks the account once it
    has none (its usage cannot be read then, and it only goes down when a window resets)."""
    snap = _snapshot(meta, tier)
    if snap["five_hour"] is None and snap["seven_day"] is None:
        return
    old = (state.get("snapshots") or {}).get(email) or {}
    if all(old.get(k) == snap[k] for k in snap if k != "at"):
        return
    try:
        pool_call("POST", "/api/snapshots", {"snapshots": {email: snap}}, mover=True)
    except AcctError as e:
        log_event("read_fail", "usage snapshot: %s" % e, every=QUIET_EVERY, key="snapshots")
        return
    state.setdefault("snapshots", {})[email] = snap


def _estimate(snap, tier, now=None):
    """(5-hour %, its reset, 7-day %, its reset) of an account now, from its last reading with a `tier` seat,
    or None if it was never read with that seat. A window past its reset time is 0 % with no reset."""
    now = time.time() if now is None else now
    if not snap or snap.get("tier") != tier:
        return None
    out = []
    for key in ("five_hour", "seven_day"):
        value, reset = float(snap.get(key) or 0), _parse_reset(snap.get(key + "_reset"))
        if reset is not None and reset <= now:
            value, reset = 0.0, None
        out += [value, reset]
    return tuple(out)


def _over(est):
    return est is not None and (est[0] >= DAILY_THRESHOLD or est[2] >= WEEKLY_THRESHOLD)


def _rank_key(est, pos, usable=True):
    """Measured and below the thresholds first, then measured at a threshold, then never measured (only taken
    with the dashboard button), then not usable by machine 2; within a group the lowest 7-day, the lowest
    5-hour, the earliest 5-hour reset, the earliest 7-day reset (a window not running has none: after one
    that has), then the list order."""
    inf = float("inf")
    if not usable:
        return (3, 0, 0, inf, inf, pos)
    if est is None:
        return (2, 0, 0, inf, inf, pos)
    return (1 if _over(est) else 0, est[2], est[0], est[1] if est[1] is not None else inf,
            est[3] if est[3] is not None else inf, pos)


def _ranked(emails, snapshots, tier, idx=None):
    """[(email, estimate or None)] best first. With idx, accounts machine 2 cannot use go last."""
    rows = [(e, _estimate((snapshots or {}).get(e), tier)) for e in emails]
    return sorted(rows, key=lambda r: _rank_key(r[1], emails.index(r[0]), idx is None or not _usable(idx, r[0])))


def _pick(ranked, holder_spent):
    """The best measured account, or None when it would reach a threshold at once: then the holder keeps its
    seat, unless it has used a window up, and the best one has not. Never-measured accounts are not taken."""
    ranked = [r for r in ranked if r[1] is not None]
    if not ranked:
        return None
    email, est = ranked[0]
    if not _over(est):
        return email
    if holder_spent and est[0] < 100 and est[2] < 100:
        return email
    return None


def _usable(idx, email):
    """None if machine 2 can switch a working machine to it, else why not."""
    name = _saved_name(idx, email)
    if not name:
        return "not logged in on this machine"
    try:
        creds = read_stored_creds(name)
    except AcctError:
        return "credentials unavailable"
    if token_state(idx["accounts"][name], creds) == "relogin":
        return "login expired/revoked"
    return None


def _next_premium(idx, premium, snapshots, live=None, premium_spent=False):
    """The best rotation account for the Premium seat, from the last usage read of each (see _rank_key).
    Returns (name, [notes]); None when every account would reach a threshold at once."""
    premium_email = _email_of(idx, premium) or premium
    skipped, emails = [], []
    for email in ROTATION_EMAILS:
        if email == premium_email:
            continue
        why = _usable(idx, email)
        if why:
            skipped.append("%s (%s)" % (email, why))
        else:
            emails.append(email)
    ranked = _ranked(emails, snapshots, "premium")
    email = _pick(ranked, premium_spent)
    measured = [r for r in ranked if r[1] is not None]
    if not email and measured:
        best, est = measured[0]
        skipped.append("every account is at a threshold (best: %s, 5-hour %d%%, 7-day %d%%)" % (best, est[0], est[2]))
    unmeasured = [e for e, est in ranked if est is None]
    if unmeasured:
        skipped.append("never measured, taken only with the dashboard button: %s" % ", ".join(unmeasured))
    return (_saved_name(idx, email) if email else None), skipped


def _premium_order(snapshots, premium=None, idx=None):
    """The rotation accounts other than Premium, in the order they would become Premium."""
    return [e for e, _ in _ranked([e for e in ROTATION_EMAILS if e != premium], snapshots, "premium", idx)]


def _seat_tier(idx, name, live, force=False, ttl=USAGE_CACHE_TTL):
    """Return (status, tier) of a saved account's seat. The live login's token is never refreshed here."""
    try:
        creds = read_stored_creds(name)
    except AcctError:
        return "credentials unavailable", None
    meta = idx["accounts"][name]
    status = refresh_seat(meta, creds, force=force, refresh_name=None if name == live else name, ttl=ttl)
    return status, (meta.get("seat") or {}).get("tier")


def _seat_holder(idx, live, first=None, force=False):
    """The rotation account that holds Premium on claude.ai, or None if no saved account confirms it."""
    names = [n for n in (_saved_name(idx, e) for e in ROTATION_EMAILS) if n]
    if first in names:
        names.remove(first)
        names.insert(0, first)
    for name in names:
        status, tier = _seat_tier(idx, name, live, force=force, ttl=SEAT_CHECK_EVERY)
        if status in ("ok", "cache") and tier == PREMIUM_TIER:
            return name
    return None


def _forget_seat_readings(idx, *names):
    """Drop cached seat and usage of accounts whose seat changed: a percentage read against Standard
    limits would be taken for a Premium one (5x too high) and trigger another rotation."""
    for name in names:
        meta = idx["accounts"].get(name) or {}
        meta.pop("seat", None)
        meta.pop("usage", None)


# ---------------------------------------------------------------- working machines: switch account

def _logged_switch(cur, target):
    try:
        cmd_switch(argparse.Namespace(name=target, force=False, no_wait=False))
    except Exception as e:
        log_event("switch_fail", str(e)[-500:], live=cur, target=target)
        e.logged = True
        raise
    log_event("switch_ok", "", live=cur, target=target)


def _wanted_account(idx, cur, args):
    """(email of the account this session should use, why). None: leave the session as it is."""
    cur_email = _email_of(idx, cur)
    try:
        state = pool_state(cur_email, force=args.force)
    except AcctError as e:
        # The pool server is down or this machine has not joined: follow the seat on claude.ai.
        if "not joined" not in str(e):
            log_event("read_fail", "pool state: %s" % e, every=QUIET_EVERY, live=cur)
        # Unconfirmed readings leave the session as it is.
        holder = _seat_holder(idx, cur, first=cur)
        return (_email_of(idx, holder), "claude.ai seat") if holder else (None, "unknown")
    if state.get("phase") in ("PREPARING", "MOVING"):
        return _backup_of(state), "seat moving %s → %s" % (state.get("premium"), state.get("target"))
    if state.get("premium"):
        return state["premium"].lower(), "Premium"
    holder = _seat_holder(idx, cur, first=cur)  # machine 2 has not published the Premium account yet
    return (_email_of(idx, holder), "claude.ai seat") if holder else (None, "unknown")


def _worker_check(args):
    """Switch this session to the account the pool says: the backup while the seat moves, else Premium."""
    idx = load_index()
    try:
        cur = sync_current(idx)
    except AcctError:
        cur = current_name(idx)
    if _email_of(idx, cur) not in POOL_EMAILS + FORMER_BACKUP_EMAILS:
        return  # a login outside the pool (a personal plan, an unsaved account) is left alone
    want, why = _wanted_account(idx, cur, args)
    save_index(idx)
    if not want or want == _email_of(idx, cur):
        return
    target = _saved_name(idx, want)
    if not target:
        log_event("switch_fail", "%s is not logged in on this machine: run /acct:login" % want, every=QUIET_EVERY,
                  live=cur, target=want)
        if args.manual:
            raise _logged_error("The pool wants %s (%s), but it is not logged in on this machine. Run /acct:login." % (want, why))
        return
    _logged_switch(cur, target)
    try:  # tell machine 2 at once: it may be waiting for this machine to reach the backup account
        pool_state(want, force=True)
    except AcctError:
        pass


# ---------------------------------------------------------------- machine 2: move the seat

def _mover_state():
    return read_json(os.path.join(store_dir(), "mover-state.json"), {}) or {}


def _save_mover_state(ms):
    write_json_atomic(os.path.join(store_dir(), "mover-state.json"), ms)


def _put_state(phase, premium, target=None, message=None, backup=None):
    body = {"phase": phase, "premium": premium, "target": target, "message": message, "by": machine_name()}
    if backup:  # left out, the server keeps the backup account it has
        body["backup"] = backup
    return pool_call("PUT", "/api/state", body, mover=True)


def _backup_of(state):
    backup = (state.get("backup") or "").lower()
    return backup if backup in BACKUP_EMAILS else BACKUP_EMAILS[0]


def _backup_spent(idx, email, live, force=False):
    """Why a backup account should rest (the Premium thresholds: 5-hour, 7-day, or a window used up), else None."""
    name = _saved_name(idx, email)
    if not name:
        return "not logged in on machine 2"
    meta = idx["accounts"][name]
    try:
        status = refresh_usage(meta, read_stored_creds(name), force=force, refresh_name=None if name == live else name,
                               ttl=USAGE_READ_EVERY)
    except AcctError as e:
        return str(e)
    if status not in ("ok", "cache"):
        return "usage unavailable (%s)" % status
    values = _usage_window_values((meta.get("usage") or {}).get("data") or {})
    if "five_hour" not in values:
        return "usage unavailable"
    if not _usage_reached_threshold((meta.get("usage") or {}).get("data") or {}):
        return None
    return "5-hour %d%%, 7-day %d%% (%s)" % (round(values["five_hour"]), round(values.get("seven_day", 0)), _trigger_text())


def _end_hold(hold):
    """The lent seat was not given back in time: give it back from here, then release the hold."""
    changes = [(hold["account"], "No seat")]
    if hold.get("backup") and hold.get("backup_tier") == "Standard":
        changes.append((hold["backup"], "Standard"))
    try:
        _set_seats(changes, load_rotation())
    except Exception as e:
        log_event("hold_fail", "could not give back the seat lent to %s: %s" % (hold.get("account"), str(e)[-500:]),
                  every=QUIET_EVERY)
        return
    pool_call("DELETE", "/api/hold", mover=True)
    log_event("hold_end", "gave back the seat lent to %s (not given back within the time)" % hold.get("account"))


def _request_done(request, status, message=None):
    try:
        pool_call("POST", "/api/request/result", {"id": request.get("id"), "status": status, "message": message},
                  mover=True)
    except AcctError as e:
        log_event("read_fail", "dashboard request: %s" % e, every=QUIET_EVERY, key="request")


def _run_request(idx, state, request, holder, live):
    """The "Dùng" button of the dashboard: give `email` the Premium seat (the full move: everyone waits on the
    backup account first) or the Standard seat (backup accounts). It is then kept like a seat given by hand."""
    kind, email = request.get("kind"), (request.get("email") or "").lower()
    group = ROTATION_EMAILS if kind == "premium" else BACKUP_EMAILS
    current = _email_of(idx, holder) if kind == "premium" else _backup_of(state)
    why = ("%s is not a %s account" % (email, "rotation" if kind == "premium" else "backup") if email not in group
           else "%s already holds the seat" % email if email == current else _usable(idx, email))
    if why:
        log_event("request_fail", "dashboard asked %s → %s: %s" % (kind, email, why))
        return _request_done(request, "fail", why)
    _request_done(request, "running")
    log_event("request", "dashboard asked the %s seat for %s" % (kind, email))
    ms = _mover_state()
    if kind == "premium":
        ok = _move_seat(idx, holder, _saved_name(idx, email), live, {"premium": current}, state,
                        reason="asked on the dashboard")
        if not ok:
            try:  # the reason machine 2 published with the phase it fell back to
                reason = pool_call("GET", "/api/state", mover=True).get("message") or "the seat move failed"
            except AcctError:
                reason = "the seat move failed"
            return _request_done(request, "fail", reason)
        ms["premium_manual"] = {"email": email, "at": time.time()}
    else:
        try:
            _set_seats([(current, "No seat"), (email, "Standard")], load_rotation())
        except Exception as e:
            log_event("request_fail", "dashboard asked standard → %s: %s" % (email, str(e)[-500:]))
            return _request_done(request, "fail", str(e)[-300:])
        _forget_seat_readings(idx, *[n for n in (_saved_name(idx, e) for e in BACKUP_EMAILS) if n])
        save_index(idx)
        _put_state("STABLE", state.get("premium"), message="backup account: %s (dashboard)" % email, backup=email)
        ms["backup_manual"] = {"email": email, "at": time.time()}
    _save_mover_state(ms)
    _request_done(request, "ok", "%s now holds the %s seat" % (email, "Premium" if kind == "premium" else "Standard"))


def _sync_backup(idx, state, live, force=False):
    """While nothing moves: if the backup account the pool names no longer holds the Standard seat on claude.ai
    (moved by hand), publish the backup account that does. Returns the state, updated or not."""
    current = _backup_of(state)
    name = _saved_name(idx, current)
    if state.get("phase") != "STABLE" or not name:
        return state
    status, tier = _seat_tier(idx, name, live, force=force, ttl=SEAT_CHECK_EVERY)
    if status not in ("ok", "cache") or tier == STANDARD_TIER:
        return state  # unreadable: keep what the pool says
    for email in BACKUP_EMAILS:
        other = _saved_name(idx, email)
        if email == current or not other:
            continue
        status, tier = _seat_tier(idx, other, live, force=force, ttl=SEAT_CHECK_EVERY)
        if status in ("ok", "cache") and tier == STANDARD_TIER:
            log_event("backup_follow", "%s no longer holds the Standard seat; published %s found on claude.ai" % (current, email))
            ms = _mover_state()
            ms["backup_manual"] = {"email": email, "at": time.time()}
            _save_mover_state(ms)
            return _put_state("STABLE", state.get("premium"), message="backup synced with claude.ai: %s" % email, backup=email)
    log_event("no_backup_holder", "no backup account holds the Standard seat on claude.ai (the pool still names %s)" % current,
              every=QUIET_EVERY)
    return state


def _rest_backup(idx, state, live):
    """While nothing moves: once the backup account with the Standard seat is spent, give the seat to the next one
    in BACKUP_EMAILS (its usage cannot be read without a seat, so it is not checked). Returns the state, updated or not."""
    current = _backup_of(state)
    why = _backup_spent(idx, current, live)
    if _saved_name(idx, current) and not (why or "").startswith(("not logged", "usage unavailable", "rate limited")):
        _remember_usage(state, current, idx["accounts"][_saved_name(idx, current)], "standard")
    # Only a reading at the limit hands the seat on; an unreadable usage or login is no reason to.
    if not why or not why.startswith("5-hour") or state.get("phase") != "STABLE":
        return state
    ms = _mover_state()
    if time.time() < ms.get("backup_backoff_until", 0):
        return state
    manual = ms.get("backup_manual") or {}
    if manual.get("email") == current and time.time() - manual.get("at", 0) < MANUAL_BACKUP_GRACE:
        log_event("backup_manual", "%s was given the Standard seat by hand: kept for %d min although %s" % (
            current, MANUAL_BACKUP_GRACE // 60, why), every=QUIET_EVERY)
        return state
    # Like a Premium move: read the seat on claude.ai again right before changing it. A seat moved by hand
    # since the last (cached) reading is followed instead of changed.
    status, tier = _seat_tier(idx, _saved_name(idx, current), live, force=True)
    if status in ("ok", "cache") and tier != STANDARD_TIER:
        return _sync_backup(idx, state, live, force=True)
    meta = idx["accounts"][_saved_name(idx, current)]
    spent = any(v >= 100 for v in _usage_window_values((meta.get("usage") or {}).get("data") or {}).values())
    others = [e for e in BACKUP_EMAILS if e != current and not _usable(idx, e)]
    other = _pick(_ranked(others, state.get("snapshots"), "standard"), spent)
    if not other:
        log_event("backup_spent", "%s is at a threshold (%s) and no other backup account is below one: it keeps "
                  "the Standard seat" % (current, why), every=QUIET_EVERY)
        return state
    log_event("backup_change", "%s rests (%s): the Standard seat goes to %s" % (current, why, other))
    try:
        _set_seats([(current, "No seat"), (other, "Standard")], load_rotation())
    except Exception as e:
        ms["backup_backoff_until"] = time.time() + MOVE_BACKOFF
        _save_mover_state(ms)
        log_event("backup_change_fail", str(e)[-800:])
        return state
    names = [n for n in (_saved_name(idx, e) for e in BACKUP_EMAILS) if n]
    _forget_seat_readings(idx, *names)
    save_index(idx)
    return _put_state("STABLE", state.get("premium"), message="backup account: %s" % other, backup=other)


def _wait_for_backup(prepared_at, backup, poll=5):
    """Wait until every working machine seen in the last 5 minutes reports the backup account."""
    deadline = time.time() + PREPARE_TIMEOUT
    waiting = []
    while True:
        machines = pool_call("GET", "/api/machines", mover=True)
        # The account itself, not at_info: a machine on an older version may wait on a former backup account.
        waiting = [m.get("name") or m.get("host") for m in machines
                   if m.get("role") == "worker" and m.get("active")
                   and not ((m.get("account") or "").lower() == backup and m.get("last_seen", 0) >= prepared_at)]
        if not waiting or time.time() >= deadline:
            return waiting
        time.sleep(poll)


def _move_seat(idx, premium, target, live, fields, state=None, reason="threshold reached"):
    """PREPARING → wait for the backup account → MOVING → seat move → STABLE. Returns True on success."""
    p_email, t_email = _email_of(idx, premium), _email_of(idx, target)
    backup = _backup_of(state or {})
    log_event("move_start", "%s; backup %s" % (reason, backup), target=t_email, **fields)
    _publish_accounts(idx, live, state or {}, force=True)  # the dashboard shows the reading behind this move
    try:
        return _move_seat_steps(idx, premium, target, live, fields, backup, reason, p_email, t_email)
    finally:
        try:  # and the seats as they are once it is over, without waiting for the next run
            _publish_accounts(idx, live, pool_call("GET", "/api/state", mover=True), force=True)
        except AcctError:
            pass


def _move_seat_steps(idx, premium, target, live, fields, backup, reason, p_email, t_email):
    prepared = _put_state("PREPARING", p_email, t_email, reason, backup=backup)
    waiting = _wait_for_backup(prepared["updated_at"], backup)
    if waiting:
        log_event("prepare_timeout", "still not on %s after %ds: %s" % (backup, PREPARE_TIMEOUT, ", ".join(waiting)),
                  target=t_email, **fields)
    _put_state("MOVING", p_email, t_email)
    status, tier = _seat_tier(idx, premium, live, force=True)
    if status == "ok" and tier != PREMIUM_TIER:  # moved by hand meanwhile: publish where it is
        holder = _seat_holder(idx, live, first=premium, force=True)
        _put_state("STABLE", _email_of(idx, holder) or p_email, message="seat moved outside the service")
        log_event("move_skipped", "seat moved outside the service", target=_email_of(idx, holder), **fields)
        return False
    started = time.time()
    try:
        # One Premium seat: free it first, then give it to the target.
        _set_seats([(p_email, "No seat"), (t_email, "Premium")], load_rotation())
    except Exception as e:
        holder = _seat_holder(idx, live, first=premium, force=True)  # the seat manager rolls back; trust only claude.ai
        _put_state("STABLE", _email_of(idx, holder) or p_email, message="seat move failed: %s" % str(e)[-300:])
        ms = _mover_state()
        ms["backoff_until"] = time.time() + MOVE_BACKOFF
        _save_mover_state(ms)
        log_event("move_fail", "%ds: %s" % (time.time() - started, str(e)[-800:]), target=t_email, **fields)
        return False
    _put_state("STABLE", t_email, message="moved from %s" % p_email)
    _forget_seat_readings(idx, premium, target)
    save_index(idx)
    log_event("move_ok", "%ds" % (time.time() - started), target=t_email, **fields)
    return True


def _mover_once(args):
    st = pool_settings()
    if not st.get("mover_key"):
        raise AcctError("This machine is not the seat-moving machine: run /acct:auto mover <mover key> on machine 2.")
    idx = load_index()
    live = current_name(idx)
    state = pool_call("POST", "/api/machine", _machine_report(_email_of(idx, live), False), mover=True)
    request = state.get("request") or {}
    asked = request.get("status") == "pending"
    # A seat asked for on the dashboard is not held back by the wait after a failed automatic move.
    if time.time() < _mover_state().get("backoff_until", 0) and not args.force and not asked:
        return
    hold = state.get("hold")
    if hold:  # a machine lent the Standard seat to log an account in: move nothing until it gives it back
        if time.time() < hold.get("until", 0):
            log_event("hold", "%s is logging in %s: seats stay as they are" % (hold.get("name"), hold.get("account")),
                      every=QUIET_EVERY)
            return
        _end_hold(hold)
        return
    missing = [e for e in POOL_EMAILS if not _saved_name(idx, e)]
    if missing:
        log_event("not_logged_in", "machine 2 is missing: %s (run /acct:login)" % ", ".join(missing), every=QUIET_EVERY)
    holder = _seat_holder(idx, live, first=_saved_name(idx, state.get("premium")))
    if not holder:
        log_event("no_holder", "no rotation account holds Premium on claude.ai", every=QUIET_EVERY)
        return
    h_email = _email_of(idx, holder)
    if state.get("phase") != "STABLE" or (state.get("premium") or "").lower() != h_email:
        # first run, a seat moved by hand, or a move cut short: publish where the seat really is
        state = _put_state("STABLE", h_email, message="synced with claude.ai")
        log_event("follow", "published the Premium account found on claude.ai", premium=h_email)
        ms = _mover_state()
        ms["premium_manual"] = {"email": h_email, "at": time.time()}
        _save_mover_state(ms)
    if not state.get("backup"):
        state = _put_state("STABLE", h_email, message="backup account: %s" % _backup_of(state), backup=_backup_of(state))
    state = _sync_backup(idx, state, live)
    if asked:
        _run_request(idx, state, request, holder, live)
        return
    state = _rest_backup(idx, state, live)
    test = st.get("test_move") or {}
    if test.get("target") and time.time() < test.get("until", 0):
        # One seat move asked by `auto test-move`, whatever the usage: the full flow, on demand.
        st.pop("test_move", None)
        save_pool_settings(st)
        target = _saved_name(idx, test["target"])
        if target and target != holder:
            log_event("test_move", "test move asked by hand", premium=h_email, target=test["target"])
            _move_seat(idx, holder, target, live, {"premium": h_email}, state)
        return
    meta = idx["accounts"][holder]
    status = refresh_usage(meta, read_stored_creds(holder), force=args.force,
                           refresh_name=None if holder == live else holder, ttl=USAGE_READ_EVERY)
    save_index(idx)
    # Right after the reading, so the dashboard shows the figures the threshold check uses.
    _publish_accounts(idx, live, state, force=status == "ok")
    if status not in ("ok", "cache"):
        log_event("read_fail", "Premium usage: %s" % status, every=QUIET_EVERY, premium=h_email)
        return
    usage = (meta.get("usage") or {}).get("data") or {}
    _remember_usage(state, h_email, meta, "premium")
    if not _usage_reached_threshold(usage):
        return
    manual = _mover_state().get("premium_manual") or {}
    if manual.get("email") == h_email and time.time() - manual.get("at", 0) < MANUAL_PREMIUM_GRACE:
        log_event("premium_manual", "%s was given the Premium seat by hand: kept for %d min although the threshold "
                  "is reached" % (h_email, MANUAL_PREMIUM_GRACE // 60), every=QUIET_EVERY, premium=h_email)
        return
    fields = dict(premium=h_email, **_usage_fields(meta))
    spent = any(v >= 100 for v in _usage_window_values(usage).values())
    target, skipped = _next_premium(idx, holder, state.get("snapshots"), live=live, premium_spent=spent)
    if not target:
        log_event("no_destination", "; ".join(skipped), every=QUIET_EVERY, **fields)
        return
    if args.manual:
        print("Threshold reached on %s: moving the Premium seat to %s..." % (holder, target))
    _move_seat(idx, holder, target, live, fields, state)


ACCOUNTS_EVERY = 60  # seconds between two usage tables machine 2 sends to the dashboard


def _account_rows(idx, live, state):
    """The pool accounts for the dashboard, in display order: the Premium account, the rotation accounts in the
    order they would become Premium (rank 1 = next), then the backup accounts. Usage is read only for the two
    accounts with a seat (Premium, and the backup account with the Standard seat): the others have none."""
    premium, backup, last_used = (state.get("premium") or "").lower(), _backup_of(state), state.get("last_used") or {}
    snapshots = state.get("snapshots") or {}
    order = _premium_order(snapshots, premium, idx)
    rank = {e: i for i, e in enumerate(order, 1)}
    others = [e for e in BACKUP_EMAILS if e != backup]
    backups = [backup] + [e for e, _ in _ranked(others, snapshots, "standard", idx)]
    rows = []
    for email in (premium,) + tuple(order) + tuple(backups):
        if not email:
            continue
        role = "premium" if email == premium else "rotation" if email in ROTATION_EMAILS else "backup"
        row = {"email": email, "role": role, "last_used": last_used.get(email), "rank": rank.get(email)}
        name = _saved_name(idx, email)
        if email not in (premium, backup):
            # No seat, so no usage to read: show the estimate from its last reading with the seat it would get.
            snap = snapshots.get(email) or {}
            est = _estimate(snap, "premium" if role == "rotation" else "standard")
            iso = lambda t: dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat() if t else None
            row.update(seat="No seat", status=None if name else "not logged in on machine 2", estimated=True,
                       measured_at=snap.get("at") if est else None)
            if est:  # never measured: no figure (it is not 0 %)
                row.update(eligible=not _over(est), five_hour=est[0], five_hour_reset=iso(est[1]),
                           seven_day=est[2], seven_day_reset=iso(est[3]))
            rows.append(row)
            continue
        if not name:
            rows.append(dict(row, status="not logged in on machine 2"))
            continue
        meta = idx["accounts"][name]
        try:
            creds = read_stored_creds(name)
            # the readings of this run (USAGE_READ_EVERY / SEAT_CHECK_EVERY): no extra request for the table
            status = refresh_usage(meta, creds, refresh_name=None if name == live else name, ttl=USAGE_READ_EVERY)
            refresh_seat(meta, creds, refresh_name=None if name == live else name, ttl=SEAT_CHECK_EVERY)
            token = token_state(meta, creds)
        except AcctError as e:
            status, token = str(e), "relogin"
        usage = (meta.get("usage") or {}).get("data") or {}
        tier = (meta.get("seat") or {}).get("tier")
        row.update(seat=SEAT_LABELS.get(tier, tier) if tier else None,
                   five_hour=(usage.get("five_hour") or {}).get("utilization"),
                   five_hour_reset=(usage.get("five_hour") or {}).get("resets_at"),
                   seven_day=(usage.get("seven_day") or {}).get("utilization"),
                   seven_day_reset=(usage.get("seven_day") or {}).get("resets_at"),
                   read_at=(meta.get("usage") or {}).get("fetchedAt"),
                   status="log in again" if token == "relogin" else (None if status in ("ok", "cache", "expired") else status))
        rows.append(row)
    return rows


def _publish_accounts(idx, live, state, force=False):
    ms = _mover_state()
    if not force and time.time() - ms.get("accounts_at", 0) < ACCOUNTS_EVERY:
        return
    try:
        rows = _account_rows(idx, live, state)
        save_index(idx)
        pool_call("POST", "/api/accounts", {"accounts": rows}, mover=True, timeout=10)
    except Exception as e:  # the usage table is a convenience: never stop a seat move for it
        log_event("read_fail", "usage table: %s" % e, every=QUIET_EVERY, key="accounts-table")
        return
    ms = _mover_state()
    ms["accounts_at"] = time.time()
    _save_mover_state(ms)


def cmd_auto_serve(args):
    """One run of the seat-moving service on machine 2 (launchd starts it every MOVER_INTERVAL seconds)."""
    lock_path = os.path.join(store_dir(), "mover.lock")
    os.makedirs(store_dir(), exist_ok=True)
    if not _acquire_lock(lock_path, stale=600):  # a seat move can take about 4.5 minutes
        return
    try:
        _mover_once(args)
    except Exception as e:
        if not getattr(e, "logged", False):
            log_event("error", ("%s: %s" % (type(e).__name__, e))[-800:], every=60, key="mover-error")
        raise
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
            pass


# ---------------------------------------------------------------- launchd service (machine 2, macOS)

def _launch_agent_path():
    return os.path.expanduser("~/Library/LaunchAgents/%s.plist" % MOVER_LABEL)


def _mover_launcher_path():
    return os.path.join(store_dir(), "bin", "acct-mover.sh")


def _write_mover_launcher():
    """A stable entry point: launchd keeps calling it while plugin updates move the scripts."""
    path = _mover_launcher_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), "acct.sh")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/bin/bash\n")
        f.write("# Seat-moving service of the acct plugin (machine 2). Runs the installed plugin version.\n")
        f.write("export PATH=\"/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$PATH\"\n")
        f.write("S=$(python3 - <<'PY'\nimport json, os\n"
                "p = os.path.expanduser('~/.claude/plugins/installed_plugins.json')\n"
                "try:\n    d = json.load(open(p))\nexcept Exception:\n    d = {}\n"
                "for k, v in (d.get('plugins') or {}).items():\n"
                "    if k.startswith('acct@') and v:\n"
                "        s = os.path.join(v[0].get('installPath', ''), 'scripts', 'acct.sh')\n"
                "        py = os.path.join(os.path.dirname(s), 'claude_acct.py')\n"
                "        if os.path.isfile(s) and os.path.isfile(py) and 'def cmd_auto_serve' in open(py).read():\n"
                "            print(s)\n            break\nPY\n)\n")
        f.write("[ -n \"$S\" ] || S=%s\n" % sh_quote(here))
        f.write("exec bash \"$S\" auto serve-once\n")
    os.chmod(path, 0o755)
    return path


def _is_windows():
    return sys.platform.startswith("win") or bool(os.environ.get("MSYSTEM"))


_WINDOWS_LAUNCHER = r'''"""Seat-moving service of the acct plugin (machine 2, Windows). Started by Task Scheduler at logon."""
import json, os, subprocess, sys, time

STORE = os.environ.get("CLAUDE_ACCT_HOME") or os.path.join(os.path.expanduser("~"), ".claude-acct")
FALLBACK = __FALLBACK__
INTERVAL = __INTERVAL__
PYTHON = os.path.join(os.path.dirname(sys.executable), "python.exe")
if not os.path.isfile(PYTHON):
    PYTHON = sys.executable


def script():
    """The installed plugin version, so plugin updates apply without reinstalling the task."""
    try:
        with open(os.path.join(os.path.expanduser("~"), ".claude", "plugins", "installed_plugins.json"), encoding="utf-8") as f:
            plugins = json.load(f).get("plugins") or {}
        for key, entries in plugins.items():
            if key.startswith("acct@") and entries:
                s = os.path.join(entries[0].get("installPath", ""), "scripts", "claude_acct.py")
                # An older installed version (before 1.0.0) has no serve-once: use the fallback copy then.
                if os.path.isfile(s) and "def cmd_auto_serve" in open(s, encoding="utf-8").read():
                    return s
    except Exception:
        pass
    return FALLBACK


log_path = os.path.join(STORE, "mover.out.log")
while True:
    try:
        if os.path.exists(log_path) and os.path.getsize(log_path) > 5000000:
            os.replace(log_path, log_path + ".1")
        with open(log_path, "a", encoding="utf-8") as log:
            subprocess.run([PYTHON, "-I", script(), "auto", "serve-once"], stdout=log, stderr=log,
                           stdin=subprocess.DEVNULL, timeout=600, creationflags=0x08000000)  # CREATE_NO_WINDOW
    except Exception as e:
        try:
            with open(log_path, "a", encoding="utf-8") as log:
                log.write("launcher: %s\n" % e)
        except Exception:
            pass
    time.sleep(INTERVAL)
'''


def _write_windows_mover_launcher():
    """A loop for Task Scheduler, which cannot repeat a task faster than once a minute."""
    path = os.path.join(store_dir(), "bin", "acct-mover.py")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    text = _WINDOWS_LAUNCHER.replace("__FALLBACK__", repr(os.path.abspath(__file__))).replace("__INTERVAL__", str(MOVER_INTERVAL))
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        f.write(text)
    return path


def _ps(script):
    return subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
                          capture_output=True, text=True, timeout=120)


def _install_windows_mover_service():
    launcher = _write_windows_mover_launcher()
    exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
    exe = exe if os.path.isfile(exe) else sys.executable
    q = lambda v: "'" + v.replace("'", "''") + "'"
    r = _ps("; ".join([
        # The account running this command, as Windows names it (USERDOMAIN can be WORKGROUP over ssh).
        "$u = [Security.Principal.WindowsIdentity]::GetCurrent().Name",
        "$a = New-ScheduledTaskAction -Execute %s -Argument %s" % (q(exe), q('"%s"' % launcher)),
        "$t = New-ScheduledTaskTrigger -AtLogOn -User $u",
        "$s = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -ExecutionTimeLimit ([TimeSpan]::Zero) "
        "-RestartCount 999 -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries",
        # Interactive: the task runs in the logged-on session, where the admin Chrome profile and the logins live.
        "$p = New-ScheduledTaskPrincipal -UserId $u -LogonType Interactive -RunLevel Limited",
        "Register-ScheduledTask -TaskName %s -Action $a -Trigger $t -Settings $s -Principal $p -Force | Out-Null" % q(MOVER_TASK),
        "Start-ScheduledTask -TaskName %s" % q(MOVER_TASK),
    ]))
    if r.returncode != 0:
        raise AcctError("Task Scheduler could not create %s: %s" % (MOVER_TASK, (r.stderr or r.stdout).strip()[-600:]))


def _install_mover_service():
    if _is_windows():
        return _install_windows_mover_service()
    if sys.platform != "darwin":
        raise AcctError("The seat-moving service is set up for macOS (launchd) and Windows (Task Scheduler) only.")
    import plistlib
    launcher = _write_mover_launcher()
    plist = {"Label": MOVER_LABEL, "ProgramArguments": ["/bin/bash", launcher], "StartInterval": MOVER_INTERVAL,
             "RunAtLoad": True, "StandardOutPath": os.path.join(store_dir(), "mover.out.log"),
             "StandardErrorPath": os.path.join(store_dir(), "mover.out.log"), "ProcessType": "Background"}
    path = _launch_agent_path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        plistlib.dump(plist, f)
    domain = "gui/%d" % os.getuid()
    subprocess.run(["launchctl", "bootout", domain, path], capture_output=True)
    r = subprocess.run(["launchctl", "bootstrap", domain, path], capture_output=True, text=True)
    if r.returncode != 0:
        raise AcctError("launchctl could not load %s: %s" % (path, (r.stderr or r.stdout).strip()))


def _uninstall_mover_service():
    if _is_windows():
        _ps("Stop-ScheduledTask -TaskName '%s' -ErrorAction SilentlyContinue; "
            "Unregister-ScheduledTask -TaskName '%s' -Confirm:$false -ErrorAction SilentlyContinue" % (MOVER_TASK, MOVER_TASK))
        return
    path = _launch_agent_path()
    if sys.platform == "darwin" and os.path.exists(path):
        subprocess.run(["launchctl", "bootout", "gui/%d" % os.getuid(), path], capture_output=True)
        os.unlink(path)


def _mover_service_loaded():
    if _is_windows():
        try:
            r = _ps("(Get-ScheduledTask -TaskName '%s' -ErrorAction Stop).State" % MOVER_TASK)
        except (OSError, subprocess.TimeoutExpired):
            return False
        return r.returncode == 0 and r.stdout.strip() in ("Running", "Ready")
    if sys.platform != "darwin":
        return False
    r = subprocess.run(["launchctl", "print", "gui/%d/%s" % (os.getuid(), MOVER_LABEL)], capture_output=True)
    return r.returncode == 0


# ---------------------------------------------------------------- /acct:auto commands

def cmd_auto_join(args):
    st = pool_settings()
    st["api_key"] = args.key.strip()
    if args.url:
        st["url"] = args.url.rstrip("/")
    save_pool_settings(st)
    try:
        state = pool_call("GET", "/api/state")
    except AcctError as e:
        raise AcctError("Saved the key, but the pool server did not accept it: %s" % e)
    cfg = load_rotation()
    cfg["enabled"] = True
    save_rotation(cfg)
    print("Joined the pool at %s. Rotation is ON: before each request this session switches to %s while the "
          "seat moves, then to the Premium account (now %s)." % (_pool_url(st), " or ".join(BACKUP_EMAILS), state.get("premium") or "not published yet"))


def cmd_auto_mover(args):
    st = pool_settings()
    if args.key == "off":
        st.pop("mover_key", None)
        save_pool_settings(st)
        _uninstall_mover_service()
        print("This machine no longer moves the Premium seat; the service was removed.")
        return
    st["mover_key"] = args.key.strip()
    if args.url:
        st["url"] = args.url.rstrip("/")
    save_pool_settings(st)
    pool_call("POST", "/api/machine", _machine_report(None, False), mover=True)  # proves the key works
    _install_mover_service()
    print("This machine now moves the Premium seat: %s checks every %ds. Keep it awake and the user logged in "
          "(on Windows Server: disconnect RDP, never sign out), with every pool account logged in (/acct:login) "
          "and the admin browser signed in (/acct:auto browser)."
          % ("the scheduled task " + MOVER_TASK if _is_windows() else "the launchd service " + MOVER_LABEL, MOVER_INTERVAL))


def cmd_auto_test_move(args):
    """Machine 2 only: on its next run, move the seat to <email> through the full flow, ignoring the thresholds."""
    st = pool_settings()
    if not st.get("mover_key"):
        raise AcctError("Only the seat-moving machine can run a test move.")
    email = args.email.strip().lower()
    if email not in ROTATION_EMAILS:
        raise AcctError("%s is not a rotation account (%s)." % (email, ", ".join(ROTATION_EMAILS)))
    st["test_move"] = {"target": email, "until": time.time() + 300}
    save_pool_settings(st)
    print("Test move queued: within %ds the service moves the Premium seat to %s (PREPARING → backup → MOVING → STABLE). "
          "Watch https://xtts.xtgroup.vn/pool/." % (MOVER_INTERVAL, email))


def cmd_auto_enable(args):
    cfg = load_rotation()
    cfg["enabled"] = True
    save_rotation(cfg)
    print("Automatic account switching enabled. It checks the pool before prompts and tool calls.")


def cmd_auto_disable(args):
    cfg = load_rotation()
    cfg["enabled"] = False
    save_rotation(cfg)
    print("Automatic account switching disabled on this machine.")


def cmd_auto_status(args):
    st = read_json(pool_settings_path(), {}) or {}
    print("acct plugin %s" % plugin_version())
    print("Account switching: %s" % ("ON" if load_rotation().get("enabled") else "OFF"))
    print("Rotates at: %s" % _trigger_text())
    print("Backup accounts (while the seat moves, one at a time until %s): %s" % (_trigger_text(), ", ".join(BACKUP_EMAILS)))
    print("Rotation: %s" % " → ".join(ROTATION_EMAILS))
    print("Pool server: %s (%s)" % (_pool_url(st), "joined" if st.get("api_key") else "not joined: /acct:auto join <key>"))
    if st.get("api_key") or st.get("mover_key"):
        try:
            s = pool_call("GET", "/api/state", mover=not st.get("api_key"))
            print("Pool phase: %s, Premium: %s%s" % (s.get("phase"), s.get("premium") or "?",
                                                    ", target: %s" % s.get("target") if s.get("phase") != "STABLE" else ""))
        except AcctError as e:
            print("Pool phase: unknown (%s)" % e)
    if st.get("mover_key"):
        print("This machine moves the seat: service %s" % ("loaded" if _mover_service_loaded() else "NOT loaded"))


def cmd_auto_browser(args):
    ensure_node_deps()
    node = os.environ.get("CLAUDE_ACCT_NODE", "node")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seat_manager.mjs")
    env = seat_manager_env(load_rotation())
    try:
        subprocess.Popen([node, script, "--setup"], stdin=subprocess.DEVNULL,
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         env=env, start_new_session=True)
    except OSError as e:
        raise AcctError("Could not open the persistent Chrome profile: %s" % e)
    print("Opened Chrome with the saved automation profile. Sign in as an organization owner/admin if needed. Leave this profile available for seat automation; the login is saved for future rotations.")


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

    st = read_json(pool_settings_path(), {}) or {}
    mover = bool(st.get("mover_key"))
    check("acct plugin", True, plugin_version())
    check("Python", True, "%d.%d" % sys.version_info[:2])
    if mover:  # only the seat-moving machine drives Chrome
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
        chrome = find_chrome()
        check("Google Chrome", bool(chrome), chrome or "not found", "Install Google Chrome (or set CLAUDE_ACCT_CHROME to its executable).")
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
    try:
        read_live_creds()
        live = read_live_profile() or {}
        check("Claude Code login", True, "%s (%s)" % (live.get("emailAddress", "?"), live.get("organizationName", "?")))
    except AcctError:
        check("Claude Code login", False, "not logged in", "Log in to Claude Code with /login.")
    idx = load_index()
    missing = [e for e in POOL_EMAILS if not _saved_name(idx, e)]
    check("Pool accounts logged in", not missing, "all %d" % len(POOL_EMAILS) if not missing else "missing: " + ", ".join(missing),
          "Log in to the missing accounts: /acct:login (it goes through them one by one).")
    if st.get("api_key") or mover:
        try:
            s = pool_call("GET", "/api/state", mover=not st.get("api_key"))
            check("Pool server", True, "%s · %s, Premium %s" % (_pool_url(st), s.get("phase"), s.get("premium") or "?"))
        except AcctError as e:
            check("Pool server", False, str(e), "Check the network to %s, or the key (/acct:auto join <key>)." % _pool_url(st))
    else:
        check("Pool server", False, "not joined", "Join the pool with the API key you were given: /acct:auto join <key>")
    check("Account switching", bool(load_rotation().get("enabled")), "ON" if load_rotation().get("enabled") else "OFF",
          "Turn it on: /acct:auto enable")
    if mover:
        check("Seat-moving service", _mover_service_loaded(), ("task %s" % MOVER_TASK) if _is_windows() else ("launchd %s" % MOVER_LABEL),
              "Reinstall it: /acct:auto mover <mover key>")
        if not os.path.isdir(load_rotation().get("browserProfile") or os.path.join(store_dir(), "browser-profile")):
            todo.append("Open the admin browser profile once and sign in as an organization owner/admin: /acct:auto browser")
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


def _set_seats(changes, cfg):
    """Change seat tiers on the claude.ai Members page, in order: [(email, "Premium"|"Standard"|"No seat")].
    The seat manager rolls back the steps it made if a later one fails."""
    ensure_node_deps()
    node = os.environ.get("CLAUDE_ACCT_NODE", "node")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seat_manager.mjs")
    env = seat_manager_env(cfg)
    try:
        r = subprocess.run([node, script] + ["%s=%s" % c for c in changes], capture_output=True, text=True,
                           timeout=120, env=env)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise AcctError("Could not run seat automation: %s. Check Node.js, Playwright, and the Chrome CDP connection." % e)
    if r.returncode != 0:
        detail = (r.stderr or r.stdout or "unknown error").strip()
        raise AcctError("Seat change failed. %s" % detail[-1200:])
    if r.stdout.strip():
        print(r.stdout.strip())


def _acquire_lock(lock_path, wait=0, stale=300):
    """Return True once this process holds the lock file, False if it stays busy for `wait` seconds.
    A lock older than `stale` seconds is taken over: its holder died."""
    deadline = time.time() + wait
    while True:
        try:
            os.close(os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
            return True
        except FileExistsError:
            pass
        try:
            if time.time() - os.path.getmtime(lock_path) > stale:
                os.unlink(lock_path)
                continue
        except FileNotFoundError:
            continue
        if time.time() >= deadline:
            return False
        time.sleep(2)


def _heartbeat():
    try:
        idx = load_index()
        log_event("heartbeat", "switching %s; %s" % ("ON" if load_rotation().get("enabled") else "OFF", _trigger_text()),
                  every=HEARTBEAT_EVERY, live=current_name(idx))
    except Exception:
        pass


def cmd_auto_check(args, lock_wait=0):
    _heartbeat()
    if not load_rotation().get("enabled"):
        return
    lock_path = os.path.join(store_dir(), "rotation.lock")
    os.makedirs(store_dir(), exist_ok=True)
    if not _acquire_lock(lock_path, lock_wait):
        return
    try:
        _worker_check(args)
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
                  "account %s, which has usage available. Continue the task you were "
                  "working on when the limit was reached; do not repeat work that is already complete.")


def cmd_auto_recover(args):
    """After Claude Code reports a quota rate limit: switch to the account the pool says, and wake the model.

    Runs as an `asyncRewake` StopFailure hook: in the background, so the Keychain cache wait does
    not block the UI. Exit 2 makes Claude Code continue the stopped task.
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
    p = sub.add_parser("auto", help="automatic account switching and Premium seat moves (XTGroup pool)")
    autos = p.add_subparsers(dest="auto_cmd", required=True)
    q = autos.add_parser("join", help="join the pool server with the API key (every machine; turns switching on)")
    q.add_argument("key"); q.add_argument("--url", help="pool server URL (default %s)" % POOL_URL)
    q.set_defaults(fn=cmd_auto_join)
    q = autos.add_parser("mover", help="make this machine the one that moves the seat (machine 2), or 'off'")
    q.add_argument("key"); q.add_argument("--url", help="pool server URL (default %s)" % POOL_URL)
    q.set_defaults(fn=cmd_auto_mover)
    q = autos.add_parser("test-move", help="machine 2: move the seat to <email> once, ignoring the thresholds (to test the flow)")
    q.add_argument("email"); q.set_defaults(fn=cmd_auto_test_move)
    q = autos.add_parser("serve-once", help=argparse.SUPPRESS)
    q.add_argument("--force", action="store_true"); q.add_argument("--manual", action="store_true")
    q.set_defaults(fn=cmd_auto_serve, hook="mover")
    q = autos.add_parser("enable", help="turn automatic account switching on"); q.set_defaults(fn=cmd_auto_enable)
    q = autos.add_parser("disable", help="turn automatic account switching off on this machine"); q.set_defaults(fn=cmd_auto_disable)
    q = autos.add_parser("status", help="show rotation settings"); q.set_defaults(fn=cmd_auto_status)
    q = autos.add_parser("browser", help="open the persistent admin browser profile for one-time sign-in"); q.set_defaults(fn=cmd_auto_browser)
    q = autos.add_parser("check", help="switch this session to the account the pool says")
    q.add_argument("--force", action="store_true"); q.add_argument("--manual", action="store_true")
    q.add_argument("--hook", help=argparse.SUPPRESS)
    q.set_defaults(fn=cmd_auto_check)
    q = autos.add_parser("recover", help=argparse.SUPPRESS)
    q.set_defaults(fn=cmd_auto_recover, hook="StopFailure")
    p = sub.add_parser("log-flush", help=argparse.SUPPRESS); p.set_defaults(fn=cmd_log_flush)
    p = sub.add_parser("login-backup", help=argparse.SUPPRESS); p.set_defaults(fn=cmd_login_backup)
    args = ap.parse_args(argv)
    global _hook
    _hook = getattr(args, "hook", None)
    try:
        if hasattr(args, "name") and not args.name and args.cmd != "login":
            hint = {"login": "/acct:login", "save": "/acct:save", "switch": "/acct:switch", "remove": "/acct:logout"}[args.cmd]
            raise AcctError("Missing account name. Example: %s work" % hint)
        args.fn(args)
    except AcctError as e:
        print("Error: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
