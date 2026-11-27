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
import bisect
import datetime as dt
import getpass
import glob
import hashlib
import json
import os
import platform
import random
import re
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
USAGE_CACHE_TTL = 180  # seconds, to avoid HTTP 429
# After a 429 the endpoint is not asked again for USAGE_RETRY_DEFAULT seconds when Retry-After is 0 or missing
# (what /api/oauth/usage answers while it limits; Claude Code waits 5 minutes then too). Each further 429 in a row
# doubles the wait, up to USAGE_RETRY_MAX; a longer Retry-After is always kept.
USAGE_RETRY_DEFAULT = 300
USAGE_RETRY_MAX = 1800
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


# A saved login lasts about 28-30 days from the login (refreshing does not extend it): /acct:list and the
# dashboard warn this long before it ends, so it can be renewed before it stops a seat move.
RELOGIN_WARN_DAYS = 7


def login_expires_at(creds):
    """Epoch seconds the saved login (its refresh token) ends at, or None when unknown."""
    rexp = (creds or {}).get("claudeAiOauth", {}).get("refreshTokenExpiresAt")
    return rexp / 1000 if isinstance(rexp, (int, float)) and rexp else None


def login_ends_soon(creds, now=None):
    """Whole days left (0 = less than one) when the login ends within RELOGIN_WARN_DAYS, else None."""
    end = login_expires_at(creds)
    left = None if end is None else end - (now or time.time())
    return int(left // 86400) if left is not None and 0 <= left < RELOGIN_WARN_DAYS * 86400 else None


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


def _rate_limited(meta, key, now):
    """Why `key` ("usage" or "seat") must not be asked yet, or None. Asking before the wait is over only keeps
    the account limited longer."""
    until = meta.get(key + "_blocked_until") or 0
    return "rate limited until %s" % time.strftime("%H:%M", time.localtime(until)) if now < until else None


def _note_429(meta, key, e, now):
    """Record a 429 on `key` and return the status; see USAGE_RETRY_DEFAULT for the wait."""
    try:
        after = int(e.headers.get("Retry-After") or 0)
    except (TypeError, ValueError, AttributeError):
        after = 0
    streak = meta.get(key + "_429_streak", 0) + 1
    meta[key + "_429_streak"] = streak
    meta[key + "_blocked_until"] = now + max(after, min(USAGE_RETRY_DEFAULT * 2 ** (streak - 1), USAGE_RETRY_MAX))
    return _rate_limited(meta, key, now)


def _clear_429(meta, key):
    meta.pop(key + "_blocked_until", None)
    meta.pop(key + "_429_streak", None)


def refresh_usage(meta, creds, force=False, refresh_name=None, ttl=USAGE_CACHE_TTL):
    """Update meta['usage'] if the access token is still valid. Return a status string.

    With `refresh_name` (a saved account that is not the live login), an expired access token is
    refreshed first, so idle accounts still report their usage.
    """
    cache = meta.get("usage")
    now = time.time()
    if cache and not force and now - cache.get("fetchedAt", 0) < ttl:
        return "cache"
    limited = _rate_limited(meta, "usage", now)
    if limited:
        return limited
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
        if e.code == 429:
            return _note_429(meta, "usage", e, now)
        return "http %d" % e.code
    except Exception as e:  # network error, timeout, ...
        return "error: %s" % net_error(e)
    meta["usage"] = {"fetchedAt": now, "data": data}
    meta.pop("revoked", None)
    _clear_429(meta, "usage")
    return "ok"


def refresh_seat(meta, creds, force=False, refresh_name=None, ttl=USAGE_CACHE_TTL):
    """Update meta['seat'] with the organization seat tier, under the same rules as refresh_usage.

    Return "ok", "cache", or why the seat could not be read (the last known seat is kept).
    """
    cache = meta.get("seat")
    now = time.time()
    if cache and not force and now - cache.get("fetchedAt", 0) < ttl:
        return "cache"
    limited = _rate_limited(meta, "seat", now)
    if limited:
        return limited
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
    except urllib.error.HTTPError as e:
        if e.code == 429:
            return _note_429(meta, "seat", e, now)
        return "http %d" % e.code
    except Exception as e:  # the usage column already reports token problems
        return "error: %s" % net_error(e)
    meta["seat"] = {"fetchedAt": now, "tier": org.get("seat_tier")}
    _clear_429(meta, "seat")
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


def _required_emails(state=None):
    """The pool accounts a machine must be logged in to: the accounts with a seat (machine 2 publishes them) and
    the fixed ones of xtgroup.vn. The others (xtg1@, xtg2@xtgroup.com.vn while they have no seat) are not needed."""
    seated = {e.lower() for e in (state or {}).get("seated") or []}
    return tuple(e for e in POOL_EMAILS if e in seated or e in ALWAYS_EMAILS)


def _pool_state_quiet():
    """The pool state, or None when this machine has not joined or the pool server cannot be reached."""
    st = read_json(pool_settings_path(), {}) or {}
    if not (st.get("api_key") or st.get("mover_key")):
        return None
    try:
        return pool_call("GET", "/api/state", mover=not st.get("api_key"))
    except AcctError:
        return None


def _login_all():
    """Log in, one after the other in one terminal window, to every pool account this machine lacks that it needs
    (see _required_emails)."""
    idx = load_index()
    todo = []
    required = _required_emails(_pool_state_quiet())
    for email in required:
        name = _saved_name(idx, email)
        if name:
            try:
                if token_state(idx["accounts"][name], read_stored_creds(name)) != "relogin":
                    continue
            except AcctError:
                pass
        todo.append((name or email, email))
    if not todo:
        print("All %d pool accounts this machine needs are logged in (%s)." % (len(required), ", ".join(required)))
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
        print("For each one: claude.ai refuses Claude Code to an account without a seat, so log in the accounts of %s "
              "while they hold one (giving a seat by hand counts against claude.ai's weekly limit). Press Enter in the "
              "terminal, open the login link in a **private browser window**, sign in as that account, authorize, and "
              "paste the whole code into the terminal (Windows Git Bash: Shift+Insert). Your current account is NOT "
              "changed." % SWAP_ORG)
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
    # Pool accounts without a seat have no usage to read (claude.ai answers 429): read only the ones machine 2 says
    # hold a seat, plus any account outside the pool.
    seated = {e.lower() for e in pool.get("seated") or []} if pool and pool.get("seated") else None
    rows, need_login, soon = [], [], []
    for name in sorted(idx["accounts"]):
        meta = idx["accounts"][name]
        email = ((meta.get("profile") or {}).get("emailAddress") or "").lower()
        unseated = seated is not None and email in POOL_EMAILS and email not in seated
        creds = None
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
        elif login_ends_soon(creds) is not None:
            days = login_ends_soon(creds)
            badge = "🟠 Login ends %s: `/acct:login %s`" % ("in %d day(s)" % days if days else "today", name)
            soon.append(name)
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
    if soon:
        print("\n🟠 %d login(s) end within %d days (a login lasts about 4 weeks; using it does not extend it): %s" % (
            len(soon), RELOGIN_WARN_DAYS, ", ".join("`/acct:login %s`" % n for n in soon)))
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
    """The account the pool works on, the ones next, the accounts with a seat and the working machines."""
    print("\nAccount switching %s · an account is left at %s." % (
        "ON" if load_rotation().get("enabled") else "OFF", _trigger_text()))
    if not state:
        return
    print("Active account (every machine works on it): %s%s" % (
        state.get("active") or "not published yet", " (%s)" % state["message"] if state.get("message") else ""))
    if state.get("fallbacks"):
        print("Next, below the thresholds: %s" % ", ".join(state["fallbacks"]))
    if state.get("seated"):
        print("Accounts with a seat: %s" % ", ".join(state["seated"]))
    missing = [e for e in [state.get("active")] + list(state.get("fallbacks") or []) if e and not _saved_name(idx, e)]
    if missing:
        print("Not logged in on this machine: %s (run /acct:login)" % ", ".join(missing))
    working = [m for m in machines if m.get("active")]
    if working:
        print("Machines seen in the last 5 minutes: %s" % ", ".join(
            "%s%s → %s" % (m.get("name") or m.get("host"), " (machine 2)" if m.get("role") == "mover" else "", m.get("account") or "?")
            for m in working))


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
    _note_live_account((p.get("emailAddress") or "").lower() or None)  # the token statistics count calls by account
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

# claude.ai limits, per organization and seat type, how many people may use a seat type in a week and how often a seat
# may pass from one person to another ("This organization has reached its weekly limit ..."). On 17-18/11 both were
# used up in xtgroup.vn: an open seat then only went back to its last few holders. So since 1.0.33 the pool spans two
# claude.ai Teams, and machines switch between accounts that already hold a seat instead of moving seats:
#   xtgroup.vn      1 Premium + 2 Standard seats, held for good: machine 2 never changes a seat there.
#   xtgroup.com.vn  2 Standard seats for 4 accounts (SWAP_EMAILS): machine 2 moves one, and only when it is needed.
# Every machine works on the account machine 2 publishes as "active": Premium while it is below the thresholds, else
# a Standard account with a seat (kept until it reaches them), and Premium again as soon as its window resets.
PREMIUM_EMAIL = "sales@xtgroup.vn"
FIXED_STANDARD_EMAILS = ("truongnv@xtgroup.vn", "xtg2@xtgroup.vn")
SWAP_EMAILS = ("xtg@xtgroup.com.vn", "xtg1@xtgroup.com.vn", "xtg2@xtgroup.com.vn", "xtg3@xtgroup.com.vn")
SWAP_ORG = "xtgroup.com.vn"
SWAP_SEATS = 2  # the Standard seats of xtgroup.com.vn
STANDARD_EMAILS = FIXED_STANDARD_EMAILS + SWAP_EMAILS  # the order breaks ties
ALL_EMAILS = (PREMIUM_EMAIL,) + STANDARD_EMAILS  # the seven accounts of the two Teams
# The Teams the pool works in. Since 23/11 (1.0.37) only xtgroup.com.vn: its four accounts each hold a Standard seat, there is
# no Premium account, and a machine switches from one Standard account to the next. Every switch between two
# organizations makes Claude Code rewrite the whole conversation into the prompt cache and drop its earlier thinking
# ("Claude can't use thinking from another organization"), so staying in one Team also saves usage. Put "xtgroup.vn" back in
# to use the three accounts of xtgroup.vn too (the account with Premium is then preferred while it is below the thresholds).
POOL_ORGS = ("xtgroup.com.vn",)
# The last resort (1.0.38): a personal Claude Max plan, with no organization seat. The machines work on it only while no
# Standard account of the pool is below the thresholds, and go back to a Standard account as soon as one is (a window
# of an account only goes down when it resets, so this does not flip back and forth).
RESERVE_EMAILS = ("xuanpt2@gmail.com",)
POOL_EMAILS = tuple(e for e in ALL_EMAILS if e.rsplit("@", 1)[1] in POOL_ORGS) + RESERVE_EMAILS
# The token statistics count only the calls made with these accounts (both Teams and the reserve, whichever the pool
# uses that day); a call made with any other account (a personal plan, ...) is left out.
STATS_EMAILS = ALL_EMAILS + RESERVE_EMAILS
# Shown on the dashboard and asked for at login whatever their seat. Since 19/11 (1.0.36) every pool account: xtgroup.com.vn
# has 4 Standard seats, one per account. An account that is not logged in on machine 2 cannot show its seat, so it would never
# be listed or asked for if it had to hold one first. Narrow this to the accounts that hold a seat for good to show and ask
# for those only.
ALWAYS_EMAILS = POOL_EMAILS
# Accounts of former pools (before 1.0.33, and the accounts of xtgroup.vn since 1.0.37): a machine still on one of them goes to
# the active account.
FORMER_POOL_EMAILS = (
    "hieule252uet@gmail.com", "xtts@xtgroup.vn", "contact@xtgroup.vn", "xtg1@xtgroup.vn", "xtg6@xtgroup.vn",
    "info@xtgroup.vn", "careers@xtgroup.vn", "support@xtgroup.vn", "hieunx@xtgroup.vn", "thanglt@xtgroup.vn",
    "kienlt@xtgroup.vn", "xtg3@xtgroup.vn", "xtg4@xtgroup.vn", "xtg5@xtgroup.vn", "xtg7@xtgroup.vn",
    "xtg8@xtgroup.vn", "xtg9@xtgroup.vn", "xtg10@xtgroup.vn", "xtg11@xtgroup.vn",
) + tuple(e for e in ALL_EMAILS if e not in POOL_EMAILS)


def _premium_in_pool():
    """The Premium account, or None when the pool does not use it (POOL_ORGS)."""
    return PREMIUM_EMAIL if PREMIUM_EMAIL in POOL_EMAILS else None

# An account is at a threshold at 5-hour >= DAILY_THRESHOLD or 7-day >= WEEKLY_THRESHOLD (or a window at 100 %), each
# read against the limits of its own seat. The machines leave it then for another account with a seat.
DAILY_THRESHOLD = 80
WEEKLY_THRESHOLD = 90
# Since 26/11 (1.0.39) a Standard account is used further: up to 5-hour 90 % and 7-day 95 %. Premium and the reserve
# (the Claude Max account) keep DAILY_THRESHOLD / WEEKLY_THRESHOLD.
STANDARD_DAILY_THRESHOLD = 90
STANDARD_WEEKLY_THRESHOLD = 95
# PREPARE_MARGIN points below the thresholds the active account is "near" them. If no other account with a seat is
# below that too, machine 2 gives a seat of xtgroup.com.vn to an account without one that has room, so it holds the
# seat before the machines need it: a machine only ever switches to an account that holds a seat already.
PREPARE_MARGIN = 10
# ... unless Premium is back (its window resets) within PREMIUM_SOON seconds: seat moves are scarce.
PREMIUM_SOON = 1800
# Since 18/11 (1.0.34) machine 2 moves no seat at all: how many moves claude.ai allows a week is unknown. The machines
# only switch between the accounts that hold a seat (a seat given by hand on the Members page is used at once).
# "seatMoves": true in machine 2's rotation.json turns the moves above back on.
SEAT_MOVES = False


def _seat_moves_on():
    return bool(load_rotation().get("seatMoves", SEAT_MOVES))
# Machine 2 reads the seat of every pool account once per SEAT_CHECK_EVERY and the usage of every account with a seat
# once per USAGE_READ_EVERY (the usage API limits each account); an account without a seat keeps its last reading.
USAGE_READ_EVERY = 180
SEAT_CHECK_EVERY = 180

# Pool server (server/pool_server.py on the xtts server): the shared state, the machines, the log.
POOL_URL = "https://xtts.xtgroup.vn/pool"
STATE_TTL = 5  # seconds a machine reuses the state it last fetched
MOVE_BACKOFF = 900  # seconds machine 2 waits after a failed seat move (other than a claude.ai limit) before the next
SEAT_CHANGE_TIMEOUT = 150  # seconds one run of seat_manager.mjs may take (a few API calls and their rollback)
MOVER_LABEL = "vn.xtgroup.acct-mover"  # launchd label (macOS)
MOVER_TASK = "XTTS_Acct_Mover"  # Task Scheduler name (Windows)
MOVER_INTERVAL = 30  # seconds between two runs of the seat-moving service

LOG_MAX_BYTES = 5_000_000
HEARTBEAT_EVERY = 600  # seconds; at most one heartbeat log line per machine
QUIET_EVERY = 1800  # seconds; repeated states (no seat to give, unreadable usage) are logged at most this often
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
    return ("Standard 5-hour >= %d%% or 7-day >= %d%%, Claude Max 5-hour >= %d%% or 7-day >= %d%% (or any window at 100%%)"
            % (STANDARD_DAILY_THRESHOLD, STANDARD_WEEKLY_THRESHOLD, DAILY_THRESHOLD, WEEKLY_THRESHOLD))


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
    state = pool_call("POST", "/api/machine", _machine_report(account, False))
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


NEVER_READ = (0.0, None, 0.0, None)  # an account never read: nothing used


def _estimate(snap, now=None):
    """(5-hour %, its reset, 7-day %, its reset) of an account now, from a reading (_snapshot), or None without one.
    A window past its reset time is 0 % with no reset."""
    now = time.time() if now is None else now
    if not snap or (snap.get("five_hour") is None and snap.get("seven_day") is None):
        return None
    out = []
    for key in ("five_hour", "seven_day"):
        value, reset = min(100.0, float(snap.get(key) or 0)), _parse_reset(snap.get(key + "_reset"))
        if reset is not None and reset <= now:
            value, reset = 0.0, None
        out += [value, reset]
    return tuple(out)


def _over(est, five=None, seven=None):
    """At a threshold: 5-hour >= `five` or 7-day >= `seven` (DAILY_THRESHOLD / WEEKLY_THRESHOLD), or a window at 100 %."""
    five = DAILY_THRESHOLD if five is None else five
    seven = WEEKLY_THRESHOLD if seven is None else seven
    return est is not None and (est[0] >= five or est[2] >= seven or est[0] >= 100 or est[2] >= 100)


def _near(est):
    """Near a threshold: PREPARE_MARGIN points below it, or above."""
    return _over(est, DAILY_THRESHOLD - PREPARE_MARGIN, WEEKLY_THRESHOLD - PREPARE_MARGIN)


def _used_up(est):
    return est is not None and (est[0] >= 100 or est[2] >= 100)


def _back_at(est, five=None, seven=None):
    """When every window of `est` at its level (the thresholds by default) has reset: 0 when none is at it, None when
    one of them has no reset time."""
    five = DAILY_THRESHOLD if five is None else five
    seven = WEEKLY_THRESHOLD if seven is None else seven
    at = 0
    for value, reset, level in ((est[0], est[1], five), (est[2], est[3], seven)):
        if value >= level or value >= 100:
            if reset is None:
                return None
            at = max(at, reset)
    return at


def _limits(email):
    """(5-hour %, 7-day %) at which this account is at a threshold: STANDARD_* for a Standard account, else (Premium,
    the Claude Max reserve) DAILY_THRESHOLD / WEEKLY_THRESHOLD."""
    if email == PREMIUM_EMAIL or email in RESERVE_EMAILS:
        return DAILY_THRESHOLD, WEEKLY_THRESHOLD
    return STANDARD_DAILY_THRESHOLD, STANDARD_WEEKLY_THRESHOLD


def _row_over(row):
    """The account of a pool row is at its thresholds (_limits)."""
    return _over(row["est"], *_limits(row["email"]))


def _row_near(row):
    """PREPARE_MARGIN points below its thresholds, or above."""
    five, seven = _limits(row["email"])
    return _over(row["est"], five - PREPARE_MARGIN, seven - PREPARE_MARGIN)


def _rank_key(est, pos, over=None):
    """Below the thresholds first, then at one; within each the lowest 7-day, the lowest 5-hour, the earliest 5-hour
    reset, the earliest 7-day reset (a window not running has none: after one that has), then the list order."""
    inf = float("inf")
    return (1 if (_over(est) if over is None else over) else 0, est[2], est[0], est[1] if est[1] is not None else inf,
            est[3] if est[3] is not None else inf, pos)


def _row_rank(row):
    est = row["est"] if row["est"] is not None else (100.0, None, 100.0, None)
    return _rank_key(est, row["pos"], _over(est, *_limits(row["email"])))


def _hhmm(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%d/%m %H:%M UTC")


def _iso(t):
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).isoformat() if t else None


# ---------------------------------------------------------------- machine 2: read the pool

SEAT_NAMES = {PREMIUM_TIER: "Premium", STANDARD_TIER: "Standard"}


def _seated(row):
    return row["seat"] in ("Premium", "Standard", "Max")


def _ready(row):
    """The machines can work on it: it holds its seat (Premium for the Premium account, Standard for the others, a Max
    plan for the reserve), its login on machine 2 is valid, and its usage is known."""
    seat = ("Premium" if row["email"] == PREMIUM_EMAIL else "Max" if row["email"] in RESERVE_EMAILS else "Standard")
    return row["seat"] == seat and not row["why"] and row["est"] is not None


def _read_pool(idx, live, state, force=False, now=None):
    """One row per pool account, in POOL_EMAILS order: email, pos, name (saved on machine 2), seat ("Premium" |
    "Standard" | "No seat" | None: unknown), est (_estimate), estimated (no seat: from its last reading kept on the
    pool server, 0 % if never read) with measured_at, read_at, fresh (a new reading, to keep on the pool server),
    why (the machines cannot use it), status (why its usage could not be read), login_expires."""
    now = time.time() if now is None else now
    snaps = state.get("snapshots") or {}
    rows = []
    for pos, email in enumerate(POOL_EMAILS):
        row = {"email": email, "pos": pos, "name": _saved_name(idx, email), "seat": None, "est": None, "why": None,
               "status": None, "fresh": False, "estimated": False, "measured_at": None, "read_at": None,
               "login_expires": None}
        rows.append(row)
        if row["name"]:
            _read_account(idx, live, row, force)
        else:
            row["why"] = "not logged in on machine 2"
        if row["est"] is None and not _seated(row):
            snap = snaps.get(email) or {}
            row.update(est=_estimate(snap, now) or NEVER_READ, estimated=True, measured_at=snap.get("at"))
        # Seat moves off: only the accounts with a seat count (and the fixed ones, and one whose seat cannot be read
        # because its login ended, once it held one); the others are not shown, not asked for a login.
        row["shown"] = (_seat_moves_on() or _seated(row) or email in ALWAYS_EMAILS
                        or (row["seat"] is None and email in snaps))
    return rows


def _shown(row):
    return row.get("shown", True)


def _read_account(idx, live, row, force):
    name = row["name"]
    meta = idx["accounts"][name]
    refresh_name = None if name == live else name  # never refresh the token Claude Code is using
    try:
        creds = read_stored_creds(name)
    except AcctError:
        row["why"] = "credentials unavailable"
        return
    row["login_expires"] = login_expires_at(creds)
    if token_state(meta, creds) == "relogin":
        row["why"] = "log in again"
        return
    if row["email"] in RESERVE_EMAILS:
        row["seat"] = "Max"  # a personal Claude Max plan: there is no organization seat to read
    else:
        refresh_seat(meta, creds, force=force, refresh_name=refresh_name, ttl=SEAT_CHECK_EVERY)
        if meta.get("seat"):  # the last seat read; unknown until one is
            row["seat"] = SEAT_NAMES.get(meta["seat"].get("tier"), "No seat")
    if not _seated(row):
        return
    status = refresh_usage(meta, creds, force=force, refresh_name=refresh_name, ttl=USAGE_READ_EVERY)
    if status == "revoked":
        row["why"] = "log in again"
    row["status"] = None if status in ("ok", "cache") else status
    row["fresh"] = status == "ok"
    row["est"] = _estimate(_snapshot(meta, row["seat"].lower()))  # a failed read keeps the last one
    row["read_at"] = (meta.get("usage") or {}).get("fetchedAt")


def _keep_readings(idx, state, rows):
    for row in rows:
        if row["fresh"]:
            _remember_usage(state, row["email"], idx["accounts"][row["name"]], row["seat"].lower())


def _forget_seat_readings(idx, *names):
    """Drop the cached seat and usage of accounts whose seat changed: read them again at once."""
    for name in names:
        meta = idx["accounts"].get(name) or {}
        meta.pop("seat", None)
        meta.pop("usage", None)


# ---------------------------------------------------------------- machine 2: the active account

def _premium_note(prem):
    """Why the machines are not on Premium."""
    if prem["why"]:
        return "Premium %s: %s" % (prem["email"], prem["why"])
    if prem["seat"] != "Premium":
        return "Premium %s: %s" % (prem["email"], "seat unknown" if prem["seat"] is None else "it has no Premium seat")
    if prem["est"] is None:
        return "Premium %s: usage unknown (%s)" % (prem["email"], prem["status"] or "not read yet")
    back = _back_at(prem["est"])
    return "Premium at a threshold" + (" until %s" % _hhmm(back) if back else "")


def _choose_active(rows, current=None, pinned=None):
    """(email, why) of the account every machine works on; (None, why) leaves them where they are. In order:
    1. the account picked on the dashboard (`pinned`), while it is below the thresholds;
    2. Premium (when the pool has it, POOL_ORGS), while it is below the thresholds;
    3. the current Standard account, while it is below them (no switching back and forth);
    4. the best ranked Standard account with a seat below them (_rank_key);
    5. none below them: the reserve (the Claude Max account, RESERVE_EMAILS), while it is below them;
    6. none below them: the current one, else the best ranked one, unless it has used a window up.
    Only Standard accounts count for 3 and 4, so the machines leave the reserve for a Standard account the moment one is
    below the thresholds. Without a Premium account `why` is "kept until it reaches a threshold" (3) or "best account
    below the thresholds" (4)."""
    by = {r["email"]: r for r in rows}
    pin = by.get(pinned)
    if pin is not None and _ready(pin) and not _row_over(pin):
        return pinned, "picked on the dashboard"
    prem = by.get(PREMIUM_EMAIL)
    if prem is not None and _ready(prem) and not _row_over(prem):
        return PREMIUM_EMAIL, "Premium"
    note = _premium_note(prem) if prem is not None else None
    ready = [r for r in rows if _ready(r)]
    cur = by.get(current)
    standards = [r for r in ready if r["email"] != PREMIUM_EMAIL and r["email"] not in RESERVE_EMAILS]
    if cur is not None and cur in standards and not _row_over(cur):
        return current, note or "kept until it reaches a threshold"
    below = [r for r in standards if not _row_over(r)]
    if below:
        return min(below, key=_row_rank)["email"], note or "best account below the thresholds"
    reserve = [r for r in ready if r["email"] in RESERVE_EMAILS and not _row_over(r)]
    if reserve:
        pick = current if cur is not None and cur in reserve else min(reserve, key=_row_rank)["email"]
        return pick, "no Standard account below the thresholds: the Claude Max account"
    all_over = (note + "; " if note else "") + "every account with a seat is at a threshold"
    left = [r for r in ready if not _used_up(r["est"])]
    if cur is not None and cur in left:
        return current, all_over
    if left:
        return min(left, key=_row_rank)["email"], all_over
    return None, (note + "; " if note else "") + "every account with a seat has used a window up"


def _free_order(rows):
    """The accounts of xtgroup.com.vn without a seat that could take one (logged in on machine 2, below the near
    level once their windows reset; never read: 0 %), best ranked first."""
    return sorted((r for r in rows if r["email"] in SWAP_EMAILS and r["seat"] == "No seat" and not r["why"]
                   and not _row_near(r)), key=_row_rank)


def _seat_to_free(rows, active):
    """(the account of xtgroup.com.vn whose seat goes, None for a free seat, why not) for a seat move now: the one with
    a seat that has the least left, never the active one."""
    swap = [r for r in rows if r["email"] in SWAP_EMAILS]
    seated = [r for r in swap if _seated(r)]
    if len(seated) < SWAP_SEATS:
        unknown = [r["email"] for r in swap if r["seat"] is None]
        if unknown:  # it may hold the seat that looks free
            return None, "the seat of %s is unknown" % ", ".join(unknown)
        return None, None
    giving = [r for r in seated if r["email"] != active]
    if not giving:
        return None, "every seat of %s is in use by the active account" % SWAP_ORG
    return max(giving, key=_row_rank)["email"], None


def _plan_swap(rows, active, now=None):
    """(from, to, why): the seat of xtgroup.com.vn to move now, from `from` (None: a free seat) to `to`; or (None,
    None, why not or None). Only when the active account is near a threshold (_near), no other account with a seat
    is below that level, and Premium is not back within PREMIUM_SOON: then the account without a seat with the most
    room takes it, so it holds the seat before the machines switch to it."""
    now = time.time() if now is None else now
    by = {r["email"]: r for r in rows}
    act = by.get(active)
    if act is None or not _ready(act) or not _row_near(act):
        return None, None, None
    if any(r is not act and _ready(r) and not _row_near(r) for r in rows if r["email"] not in RESERVE_EMAILS):
        return None, None, None
    prem = by.get(PREMIUM_EMAIL)
    if prem is not None and _ready(prem):
        back = (_back_at(prem["est"], DAILY_THRESHOLD - PREPARE_MARGIN, WEEKLY_THRESHOLD - PREPARE_MARGIN)
                if act is prem else _back_at(prem["est"]))
        if back is not None and back - now <= PREMIUM_SOON:
            return None, None, "Premium is back %s: no seat moves before" % ("at " + _hhmm(back) if back else "now")
    free = _free_order(rows)
    if not free:
        return None, None, "%s is near a threshold, but no account of %s without a seat has room" % (active, SWAP_ORG)
    frm, why_not = _seat_to_free(rows, active)
    if why_not:
        return None, None, why_not
    return frm, free[0]["email"], "%s is near a threshold and no other account with a seat is below it" % active


# ---------------------------------------------------------------- machine 2: move a seat of xtgroup.com.vn

LIMIT_DATE = re.compile(r"try again after (\d{4}-\d{2}-\d{2})", re.I)


def _week_start(now):
    """Monday 00:00 UTC of the week of `now`: claude.ai counts its weekly seat limits from there."""
    d = dt.datetime.fromtimestamp(now, dt.timezone.utc)
    return (d - dt.timedelta(days=d.weekday())).replace(hour=0, minute=0, second=0, microsecond=0).timestamp()


def _limit_until(message, now=None):
    """When claude.ai lets seats move again after it refused one for a weekly limit; None for any other error."""
    if "weekly limit" not in message and "weekly_seat_limit" not in message:
        return None
    m = LIMIT_DATE.search(message)
    if m:
        return dt.datetime.strptime(m.group(1), "%Y-%m-%d").replace(tzinfo=dt.timezone.utc).timestamp()
    return _week_start(time.time() if now is None else now) + 7 * 86400


def _swap_org(idx):
    """The organization id of xtgroup.com.vn, from the login of one of its accounts saved on machine 2."""
    org = load_rotation().get("swapOrg")
    for email in SWAP_EMAILS:
        if org:
            break
        name = _saved_name(idx, email)
        org = ((idx["accounts"].get(name) or {}).get("profile") or {}).get("organizationUuid") if name else None
    if not org:
        raise AcctError("No account of %s is logged in on machine 2, so its organization is unknown: run /acct:login."
                        % SWAP_ORG)
    return org


def _swaps_this_week(ms, now):
    return [t for t in ms.get("swaps") or [] if t >= _week_start(now)]


def _swap_seat(idx, frm, to, why, now=None):
    """Give the Standard seat of `frm` (None: a free one) to `to`, in xtgroup.com.vn. Returns None, or the error.
    A refusal for a claude.ai weekly limit stops seat moves until the date claude.ai gives; another failure for
    MOVE_BACKOFF."""
    now = time.time() if now is None else now
    log_event("seat_swap", "%s: a Standard seat of %s goes from %s to %s" % (why, SWAP_ORG, frm or "nobody (free)", to),
              target=to)
    try:
        _set_seats(([(frm, "No seat")] if frm else []) + [(to, "Standard")], load_rotation(), _swap_org(idx))
    except AcctError as e:
        msg = str(e)
        until = _limit_until(msg, now)
        ms = _mover_state()
        ms.update(swap_blocked_until=until or now + MOVE_BACKOFF, swap_limit=bool(until), swap_error=msg[-400:])
        _save_mover_state(ms)
        log_event("seat_swap_fail", ("claude.ai weekly limit: no seat moves until %s. " % _hhmm(until) if until else "")
                  + msg[-800:], target=to)
        return msg
    ms = _mover_state()
    ms["swaps"] = _swaps_this_week(ms, now) + [now]
    for k in ("swap_error", "swap_blocked_until", "swap_limit"):
        ms.pop(k, None)
    _save_mover_state(ms)
    _forget_seat_readings(idx, *[n for n in (_saved_name(idx, e) for e in (frm, to)) if n])
    save_index(idx)
    log_event("seat_swap_ok", "%s → %s (%d this week)" % (frm or "free seat", to, len(ms["swaps"])), target=to)
    return None


def _swap_blocked(ms, now):
    """Why no seat may move now, or None."""
    until = ms.get("swap_blocked_until") or 0
    if now >= until:
        return None
    return ("claude.ai refuses seat moves until %s (weekly limit)" % _hhmm(until) if ms.get("swap_limit")
            else "the last seat move failed: next try after %s" % _hhmm(until))


# ---------------------------------------------------------------- working machines: switch account

def _logged_switch(cur, target):
    try:
        cmd_switch(argparse.Namespace(name=target, force=False, no_wait=False))
    except Exception as e:
        log_event("switch_fail", str(e)[-500:], live=cur, target=target)
        e.logged = True
        raise
    log_event("switch_ok", "", live=cur, target=target)


def _wanted_accounts(idx, cur, args):
    """([emails], why): the account this session should use (the active one), then the ones it may use when that one
    is not logged in on this machine. [] leaves the session as it is."""
    try:
        state = pool_state(_email_of(idx, cur), force=args.force)
    except AcctError as e:
        if "not joined" not in str(e):
            log_event("read_fail", "pool state: %s" % e, every=QUIET_EVERY, live=cur)
        return [], "pool server unknown"
    active = (state.get("active") or "").lower()
    if not active:
        return [], "machine 2 has not published the active account yet"
    return [active] + [e.lower() for e in state.get("fallbacks") or [] if e.lower() != active], state.get("message") or ""


def _worker_check(args):
    """Switch this session to the account the pool says (the active one), or the next one logged in here."""
    idx = load_index()
    try:
        cur = sync_current(idx)
    except AcctError:
        cur = current_name(idx)
    cur_email = _email_of(idx, cur)
    if cur_email not in POOL_EMAILS + FORMER_POOL_EMAILS:
        return  # a login outside the pool (a personal plan, an unsaved account) is left alone
    wanted, why = _wanted_accounts(idx, cur, args)
    save_index(idx)
    if not wanted or wanted[0] == cur_email:
        return
    if not _saved_name(idx, wanted[0]):
        log_event("switch_fail", "%s is not logged in on this machine: run /acct:login" % wanted[0], every=QUIET_EVERY,
                  live=cur, target=wanted[0])
        if args.manual:
            print("The pool works on %s (%s), but it is not logged in on this machine: run /acct:login." % (wanted[0], why))
    target = next((n for n in (_saved_name(idx, e) for e in wanted) if n), None)
    if not target or target == cur:
        return
    _logged_switch(cur, target)
    try:  # report the new account at once
        pool_state(_email_of(idx, target), force=True)
    except AcctError:
        pass


# ---------------------------------------------------------------- machine 2: the service

def _mover_state():
    return read_json(os.path.join(store_dir(), "mover-state.json"), {}) or {}


def _save_mover_state(ms):
    write_json_atomic(os.path.join(store_dir(), "mover-state.json"), ms)


def _put_state(active, message, fallbacks, seated):
    return pool_call("PUT", "/api/state", {"phase": "STABLE", "premium": _premium_in_pool(), "active": active,
                                           "fallbacks": fallbacks, "seated": seated, "message": message,
                                           "by": machine_name()}, mover=True)


def _publish_state(state, rows, active, why):
    """Publish the active account, the ones a machine may use instead (below the thresholds, best first) and the
    accounts with a seat, when one of them changed. Returns the state."""
    if not active:
        return state
    # best first, the reserve (Claude Max) after every Standard account
    fallbacks = [r["email"] for r in sorted((r for r in rows if r["email"] != active and _ready(r) and not _row_over(r)),
                                            key=lambda r: (r["email"] in RESERVE_EMAILS, _row_rank(r)))]
    seated = [r["email"] for r in rows if _seated(r)]
    if (state.get("active"), state.get("message"), state.get("fallbacks"), state.get("seated")) == (
            active, why, fallbacks, seated):
        return state
    if active != state.get("active"):
        log_event("active", "every machine now works on %s (%s)" % (active, why), live=state.get("active"), target=active)
    return _put_state(active, why, fallbacks, seated)


def _request_done(request, status, message=None):
    try:
        pool_call("POST", "/api/request/result", {"id": request.get("id"), "status": status, "message": message},
                  mover=True)
    except AcctError as e:
        log_event("read_fail", "dashboard request: %s" % e, every=QUIET_EVERY, key="request")


def _run_request(idx, state, request, rows, now):
    """The dashboard buttons: "use" puts every machine on an account with a seat (kept while it is below the
    thresholds); "seat" gives a Standard seat of xtgroup.com.vn to an account without one."""
    kind, email = request.get("kind"), (request.get("email") or "").lower()
    row = {r["email"]: r for r in rows}.get(email)
    if row is None:
        why = "%s is not a pool account" % email
    elif kind == "use":
        why = (row["why"] or (None if _ready(row) else "%s has no seat" % email if not _seated(row)
                              else "the usage of %s is unknown" % email)
               or ("%s is at a threshold" % email if _row_over(row) else None))
    elif kind == "seat":
        frm, why = _seat_to_free(rows, (state.get("active") or "").lower())
        why = ("seat moves are turned off: give the seat by hand on the Members page" if not _seat_moves_on()
               else "%s is not an account of %s" % (email, SWAP_ORG) if email not in SWAP_EMAILS
               else "%s holds a seat already" % email if _seated(row) else row["why"] or why
               or _swap_blocked(_mover_state(), now))
    else:
        why = "unknown request %s" % kind
    if why:
        log_event("request_fail", "dashboard asked %s %s: %s" % (kind, email, why))
        return _request_done(request, "fail", why)
    _request_done(request, "running")
    log_event("request", "dashboard asked %s %s" % (kind, email))
    if kind == "use":
        ms = _mover_state()
        ms["pin"] = {"email": email, "at": now}
        _save_mover_state(ms)
        return _request_done(request, "ok", "every machine switches to %s" % email)
    err = _swap_seat(idx, frm, email, "asked on the dashboard", now)
    if err:
        return _request_done(request, "fail", err[-300:])
    _request_done(request, "ok", "%s now holds a Standard seat%s" % (email, " (taken from %s)" % frm if frm else ""))


def _mover_once(args):
    st = pool_settings()
    if not st.get("mover_key"):
        raise AcctError("This machine is not the seat-moving machine: run /acct:auto mover <mover key> on machine 2.")
    idx = load_index()
    live = current_name(idx)
    state = pool_call("POST", "/api/machine", _machine_report(_email_of(idx, live), False), mover=True)
    rows = _read_pool(idx, live, state, force=args.force)
    missing = [r["email"] for r in rows if _shown(r) and r["why"] == "not logged in on machine 2"]
    if missing:
        log_event("not_logged_in", "machine 2 is missing: %s (run /acct:login)" % ", ".join(missing), every=QUIET_EVERY)
    _keep_readings(idx, state, rows)
    save_index(idx)
    fresh = any(r["fresh"] for r in rows)
    now = time.time()
    request = state.get("request") or {}
    if request.get("status") == "pending":
        _run_request(idx, state, request, rows, now)
        rows = _read_pool(idx, live, state)  # seats changed: their readings were dropped
        _keep_readings(idx, state, rows)
        save_index(idx)
        fresh = True
    ms = _mover_state()
    pinned = (ms.get("pin") or {}).get("email")
    current = (state.get("active") or "").lower() or None
    active, why = _choose_active(rows, current, pinned)
    if pinned and active != pinned:
        ms.pop("pin", None)
        _save_mover_state(ms)
        log_event("pin_end", "%s, picked on the dashboard, is no longer usable or reached a threshold" % pinned)
    frm, to, note = _plan_swap(rows, active, now) if _seat_moves_on() else (None, None, None)
    if to:
        blocked = _swap_blocked(_mover_state(), now)
        if blocked:
            note = "a seat should go from %s to %s, but %s" % (frm or "a free seat", to, blocked)
            log_event("swap_blocked", note, every=QUIET_EVERY)
        elif not _swap_seat(idx, frm, to, note, now):
            note = None
            rows = _read_pool(idx, live, state)
            _keep_readings(idx, state, rows)
            save_index(idx)
            active, why = _choose_active(rows, current, pinned)
            fresh = True
        else:
            note = "the seat move failed: see the log"
    elif note:
        log_event("no_swap", note, every=QUIET_EVERY)
    if not active:
        log_event("no_account", why, every=QUIET_EVERY)
    ms = _mover_state()
    if ms.get("swap_note") != note:
        ms["swap_note"] = note
        _save_mover_state(ms)
    new_state = _publish_state(state, rows, active, why)
    _publish_accounts(rows, new_state, force=fresh or new_state is not state or args.force)


ACCOUNTS_EVERY = 60  # seconds between two usage tables machine 2 sends to the dashboard


def _account_rows(rows, active):
    """The pool accounts for the dashboard, in POOL_EMAILS order. An account without a seat shows its last reading
    (estimated), 0 % if never read."""
    free = _free_order(rows) if _seat_moves_on() else []
    out = []
    for r in rows:
        if not _shown(r):
            continue
        est = r["est"]
        row = {"email": r["email"], "org": r["email"].rsplit("@", 1)[-1],
               "role": ("premium" if r["email"] == PREMIUM_EMAIL else "reserve" if r["email"] in RESERVE_EMAILS
                        else "swap" if r["email"] in SWAP_EMAILS else "standard"),
               "seat": r["seat"], "active": r["email"] == active, "estimated": r["estimated"],
               "measured_at": r["measured_at"], "read_at": r["read_at"], "status": r["why"] or r["status"],
               "login_expires": r["login_expires"], "eligible": est is not None and not _row_over(r),
               "near": est is not None and _row_near(r), "next_seat": bool(free) and r is free[0],
               "limit_5h": _limits(r["email"])[0], "limit_7d": _limits(r["email"])[1]}
        if est is not None:
            row.update(five_hour=est[0], five_hour_reset=_iso(est[1]), seven_day=est[2], seven_day_reset=_iso(est[3]))
        out.append(row)
    return out


def _pool_info(rows, state, now):
    ms = _mover_state()
    prem = next((r for r in rows if r["email"] == PREMIUM_EMAIL), None)  # none when the pool does not use Premium
    back = _back_at(prem["est"]) if prem is not None and prem["est"] is not None and _over(prem["est"]) else None
    return {"active": state.get("active"), "reason": state.get("message"), "premium_back_at": back or None,
            "has_premium": prem is not None,
            "swaps_week": len(_swaps_this_week(ms, now)), "week_start": _week_start(now),
            "swap_blocked": _swap_blocked(ms, now), "swap_blocked_until": ms.get("swap_blocked_until") if _swap_blocked(ms, now) else None,
            "swap_limit": bool(ms.get("swap_limit")) and bool(_swap_blocked(ms, now)),
            "swap_error": ms.get("swap_error"), "swap_note": ms.get("swap_note"),
            "pin": (ms.get("pin") or {}).get("email"), "swap_org": SWAP_ORG, "seat_moves": _seat_moves_on(),
            "thresholds": {"five_hour": DAILY_THRESHOLD, "seven_day": WEEKLY_THRESHOLD, "margin": PREPARE_MARGIN,
                           "standard_five_hour": STANDARD_DAILY_THRESHOLD, "standard_seven_day": STANDARD_WEEKLY_THRESHOLD}}


def _publish_accounts(rows, state, force=False):
    ms = _mover_state()
    now = time.time()
    if not force and now - ms.get("accounts_at", 0) < ACCOUNTS_EVERY:
        return
    try:
        pool_call("POST", "/api/accounts", {"accounts": _account_rows(rows, state.get("active")),
                                            "pool": _pool_info(rows, state, now)}, mover=True, timeout=10)
    except Exception as e:  # the usage table is a convenience: never stop the service for it
        log_event("read_fail", "usage table: %s" % e, every=QUIET_EVERY, key="accounts-table")
        return
    ms = _mover_state()
    ms["accounts_at"] = now
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
    print("Joined the pool at %s. Switching is ON: before each request this session switches to the account every "
          "machine works on (now %s). Log in to every pool account with /acct:login." % (
              _pool_url(st), state.get("active") or "not published yet"))


def cmd_auto_mover(args):
    st = pool_settings()
    if args.key == "off":
        st.pop("mover_key", None)
        save_pool_settings(st)
        _uninstall_mover_service()
        print("This machine no longer runs the pool service; the service was removed.")
        return
    st["mover_key"] = args.key.strip()
    if args.url:
        st["url"] = args.url.rstrip("/")
    save_pool_settings(st)
    pool_call("POST", "/api/machine", _machine_report(None, False), mover=True)  # proves the key works
    _install_mover_service()
    print("This machine now picks the account every machine works on and moves the Standard seats of %s: %s checks "
          "every %ds. Keep it awake and the user logged in (on Windows Server: disconnect RDP, never sign out), with "
          "every pool account logged in (/acct:login) and the admin browser signed in as an admin of %s "
          "(/acct:auto browser)." % (SWAP_ORG, "the scheduled task " + MOVER_TASK if _is_windows()
                                      else "the launchd service " + MOVER_LABEL, MOVER_INTERVAL, SWAP_ORG))


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
    print("An account is left at: %s" % _trigger_text())
    print("Pool (%s): %s" % (", ".join(POOL_ORGS), ", ".join(e for e in POOL_EMAILS if e not in RESERVE_EMAILS)))
    print("Reserve (Claude Max, used only while no Standard account is below the thresholds): %s" % ", ".join(RESERVE_EMAILS))
    if _premium_in_pool():
        print("Order: Premium below the thresholds, else the Standard account with a seat in use until it reaches them, "
              "else the best ranked one with a seat")
    else:
        print("Order: the Standard account in use until it reaches the thresholds, then the best ranked one with a seat "
              "below them (no Premium account, no switch between organizations)")
    print("Seat moves: %s" % ("ON (a seat of %s moves when the active account is %d points below a threshold and no other "
                              "account with a seat is)" % (SWAP_ORG, PREPARE_MARGIN) if _seat_moves_on()
                              else "OFF: machine 2 moves no seat; give one by hand on the Members page"))
    print("Pool server: %s (%s)" % (_pool_url(st), "joined" if st.get("api_key") else "not joined: /acct:auto join <key>"))
    if st.get("api_key") or st.get("mover_key"):
        try:
            s = pool_call("GET", "/api/state", mover=not st.get("api_key"))
            print("Active account: %s%s" % (s.get("active") or "not published yet",
                                             " (%s)" % s["message"] if s.get("message") else ""))
        except AcctError as e:
            print("Active account: unknown (%s)" % e)
    if st.get("mover_key"):
        print("This machine runs the pool service: %s" % ("loaded" if _mover_service_loaded() else "NOT loaded"))


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
    required = _required_emails(_pool_state_quiet())  # the accounts with a seat, which machine 2 publishes
    missing = [e for e in required if not _saved_name(idx, e)]
    check("Pool accounts logged in", not missing, "all %d needed (%s)" % (len(required), ", ".join(required)) if not missing
          else "missing: " + ", ".join(missing),
          "Log in to the missing accounts: /acct:login (it goes through them one by one).")
    if st.get("api_key") or mover:
        try:
            s = pool_call("GET", "/api/state", mover=not st.get("api_key"))
            check("Pool server", True, "%s · active account %s" % (_pool_url(st), s.get("active") or "not published yet"))
        except AcctError as e:
            check("Pool server", False, str(e), "Check the network to %s, or the key (/acct:auto join <key>)." % _pool_url(st))
    else:
        check("Pool server", False, "not joined", "Join the pool with the API key you were given: /acct:auto join <key>")
    check("Account switching", bool(load_rotation().get("enabled")), "ON" if load_rotation().get("enabled") else "OFF",
          "Turn it on: /acct:auto enable")
    if mover:
        check("Seat-moving service", _mover_service_loaded(), ("task %s" % MOVER_TASK) if _is_windows() else ("launchd %s" % MOVER_LABEL),
              "Reinstall it: /acct:auto mover <mover key>")
        try:
            check("Organization %s" % SWAP_ORG, True, _swap_org(idx))
        except AcctError as e:
            check("Organization %s" % SWAP_ORG, False, str(e), "Log in to an account of %s: /acct:login" % SWAP_ORG)
        if not os.path.isdir(load_rotation().get("browserProfile") or os.path.join(store_dir(), "browser-profile")):
            todo.append("Open the admin browser profile once and sign in as an owner/admin of %s: /acct:auto browser"
                        % SWAP_ORG)
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


def _set_seats(changes, cfg, org):
    """Change seat tiers in organization `org` (its id), in order: [(email, "Premium"|"Standard"|"No seat")].
    The seat manager rolls back the steps it made if a later one fails; its error carries claude.ai's own message
    (e.g. a weekly limit, with the date seats may move again)."""
    ensure_node_deps()
    node = os.environ.get("CLAUDE_ACCT_NODE", "node")
    script = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seat_manager.mjs")
    env = seat_manager_env(cfg)
    try:
        # A timeout kills it half way, with no rollback at all: SEAT_CHANGE_TIMEOUT leaves room for slow answers.
        # Node writes UTF-8; Windows would decode it as cp1252, fail, and lose the error message ("unknown error").
        r = subprocess.run([node, script, "--org", org] + ["%s=%s" % c for c in changes], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", timeout=SEAT_CHANGE_TIMEOUT, env=env)
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
    _note_live_account()
    _maybe_token_stats()


# ---------------------------------------------------------------- which account made each call

# A transcript does not say which account made a call. Every hook (before each prompt and tool call) writes the live
# account to account-timeline.jsonl when it changed (1.0.42), and so does every switch: a call is then counted for the
# account live when it was made. Before that file exists, the rotation log stands in: its heartbeats (at most every
# 10 minutes, with the live account) and its switches.
def _timeline_path():
    return os.path.join(store_dir(), "account-timeline.jsonl")


def _note_live_account(email=False, now=None):
    """Write the live account (`email`, else read from Claude Code's profile) to the timeline when it changed. Never
    raises."""
    try:
        now = time.time() if now is None else now
        if email is False:
            email = ((read_live_profile() or {}).get("emailAddress") or "").lower() or None
        state_path = os.path.join(store_dir(), "account-now.json")
        last = read_json(state_path, {}) or {}
        if "email" in last and last["email"] == email:
            return
        os.makedirs(store_dir(), exist_ok=True)
        with open(_timeline_path(), "a", encoding="utf-8") as f:
            f.write(json.dumps({"at": now, "email": email}) + "\n")
        write_json_atomic(state_path, {"email": email, "at": now})
    except Exception:
        pass


def _account_timeline(idx=None):
    """[(epoch, email or None)] by time: the live account of this machine from that moment on (None: an account that is
    not saved here). From account-timeline.jsonl, and the rotation log's heartbeats and switches."""
    points = []
    try:
        with open(_timeline_path(), encoding="utf-8") as f:
            for line in f:
                try:
                    e = json.loads(line)
                    points.append((float(e["at"]), (e.get("email") or None)))
                except (ValueError, KeyError, TypeError):
                    pass
    except OSError:
        pass
    try:
        idx = idx or load_index()
    except Exception:
        idx = {"accounts": {}}
    for name in ("rotation.log.1", "rotation.log"):
        try:
            f = open(os.path.join(store_dir(), name), encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"heartbeat"' not in line and '"switch_ok"' not in line:
                    continue
                try:
                    e = json.loads(line)
                    at = time.mktime(time.strptime(e["ts"], "%Y-%m-%d %H:%M:%S"))  # written in this machine's local time
                except (ValueError, KeyError, TypeError, OverflowError):
                    continue
                who = e.get("live") if e.get("event") == "heartbeat" else e.get("target") if e.get("event") == "switch_ok" else 0
                if who == 0:
                    continue
                points.append((at, (_email_of(idx, who) or (who.lower() if "@" in who else None)) if who else None))
    points.sort(key=lambda p: p[0])
    return points


# ---------------------------------------------------------------- token statistics (dashboard)

# Every machine that joined the pool sends, at most every TOKEN_STATS_EVERY, the tokens its Claude Code used on each of
# the last TOKEN_DAYS days (Vietnam time), read from the transcripts Claude Code keeps on the machine
# (<config dir>/projects/**/*.jsonl, subagents included). Nothing is asked of Claude or of the usage API: it costs no
# token. A message is written 1-5 times in a transcript with the same usage, so each one is counted once by its id.
TOKEN_STATS_EVERY = 600
TOKEN_DAYS = 8  # today and the 7 days before it
TOKEN_TZ = dt.timezone(dt.timedelta(hours=7))


# What the tokens would cost on the Claude API (USD per 1M tokens: input, output, cache read), to compare machines by what
# they really use (a cache read costs 0.025-0.1x an input token, an output token 5x). A cache write costs 1.25x input
# with the 5-minute TTL and 2x with the 1-hour one, which Claude Code uses for almost everything (usage.cache_creation
# splits them). Fast mode costs 2x. claude.ai plans do not bill per token: this is a yardstick, not a bill.
# A model id is matched by its longest prefix here (ids may carry a suffix); an unknown model is counted as unpriced.
MODEL_PRICES = {
    "claude-fable-5-2": (8.0, 40.0, 0.20), "claude-mythos-5-2": (8.0, 40.0, 0.20),
    "claude-fable-5-1": (10.0, 50.0, 0.25), "claude-mythos-5-1": (10.0, 50.0, 0.25),
    "claude-fable-5": (10.0, 50.0, 1.0), "claude-mythos-5": (10.0, 50.0, 1.0),
    "claude-opus-5-5": (4.0, 20.0, 0.20), "claude-opus-5": (5.0, 25.0, 0.50),
    "claude-opus-4-8": (5.0, 25.0, 0.50), "claude-opus-4-7": (5.0, 25.0, 0.50), "claude-opus-4-6": (5.0, 25.0, 0.50),
    "claude-sonnet-5-5": (2.0, 10.0, 0.10), "claude-sonnet-5": (2.0, 10.0, 0.20), "claude-sonnet-4-6": (3.0, 15.0, 0.30),
    "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1.0, 5.0, 0.10),
}
# Claude Haiku 5.5 costs more once the prompt is above 100K tokens.
LONG_PROMPT_PRICES = {"claude-haiku-5-5": (100_000, (0.50, 2.50, 0.05))}


def _model_price(model):
    """(input, output, cache read) USD per 1M tokens of `model`, or None when it is unknown."""
    model = (model or "").lower()
    best = max((k for k in MODEL_PRICES if model.startswith(k)), key=len, default=None)
    return (best, MODEL_PRICES[best]) if best else (None, None)


def _token_cost(model, usage):
    """USD the request would cost on the Claude API, or None for an unknown model."""
    key, price = _model_price(model)
    if price is None:
        return None
    n = lambda v: v if isinstance(v, (int, float)) and v > 0 else 0
    i, o = n(usage.get("input_tokens")), n(usage.get("output_tokens"))
    r, w = n(usage.get("cache_read_input_tokens")), n(usage.get("cache_creation_input_tokens"))
    cc = usage.get("cache_creation") if isinstance(usage.get("cache_creation"), dict) else {}
    w1h = min(n(cc.get("ephemeral_1h_input_tokens")), w)
    w5m = w - w1h
    if key in LONG_PROMPT_PRICES and i + r + w > LONG_PROMPT_PRICES[key][0]:
        price = LONG_PROMPT_PRICES[key][1]
    inp, out, read = price
    usd = (i * inp + w5m * inp * 1.25 + w1h * inp * 2.0 + r * read + o * out) / 1e6
    return usd * (2.0 if usage.get("speed") == "fast" else 1.0)


def _token_days(projects_dir, now=None, days=TOKEN_DAYS, timeline=None):
    """{"YYYY-MM-DD": {"calls", "input", "output", "cache_write", "cache_read", "usd", "unpriced"}} for the last `days`
    days (TOKEN_TZ); usd: what they would cost on the Claude API (_token_cost); unpriced: calls of an unknown model.
    With a `timeline` (_account_timeline), only the calls made with an account of STATS_EMAILS count: a call made with
    another account (a personal plan, ...), or before the timeline knows any, is left out. Without one, every call counts."""
    now = time.time() if now is None else now
    times = [p[0] for p in timeline or []]

    def ours(at):
        if timeline is None:
            return True
        i = bisect.bisect_right(times, at) - 1
        return i >= 0 and timeline[i][1] in STATS_EMAILS
    first = dt.datetime.fromtimestamp(now, TOKEN_TZ).date() - dt.timedelta(days=days - 1)
    since = dt.datetime.combine(first, dt.time(), TOKEN_TZ).timestamp()
    out, seen = {}, set()
    for path in glob.glob(os.path.join(projects_dir, "**", "*.jsonl"), recursive=True):
        try:
            if os.path.getmtime(path) < since:
                continue  # last written before the first day: none of its messages counts
            f = open(path, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with f:
            for line in f:
                if '"assistant"' not in line or '"usage"' not in line:  # most lines: skip them unparsed
                    continue
                try:
                    e = json.loads(line)
                except ValueError:
                    continue
                m = e.get("message") if isinstance(e, dict) else None
                u = (m or {}).get("usage") if isinstance(m, dict) else None
                if e.get("type") != "assistant" or not isinstance(u, dict) or m.get("model") == "<synthetic>":
                    continue
                key = (m.get("id"), e.get("requestId")) if m.get("id") or e.get("requestId") else e.get("uuid")
                if key in seen:
                    continue
                seen.add(key)
                at = _parse_reset(e.get("timestamp"))
                day = dt.datetime.fromtimestamp(at, TOKEN_TZ).date() if at is not None else None
                if day is None or day < first:
                    continue
                if not ours(at):
                    continue  # made with an account outside the pool: not counted
                d = out.setdefault(day.isoformat(), {"calls": 0, "input": 0, "output": 0, "cache_write": 0, "cache_read": 0,
                                                     "usd": 0.0, "unpriced": 0})
                n = lambda field: int(u[field]) if isinstance(u.get(field), (int, float)) and u[field] > 0 else 0
                cost = _token_cost(m.get("model"), u)
                d["calls"] += 1
                for name, field in (("input", "input_tokens"), ("output", "output_tokens"),
                                    ("cache_write", "cache_creation_input_tokens"), ("cache_read", "cache_read_input_tokens")):
                    d[name] += n(field)
                if cost is None:
                    d["unpriced"] += 1
                else:
                    d["usd"] += cost
    for d in out.values():
        d["usd"] = round(d["usd"], 4)
    return out


def _maybe_token_stats():
    """From a hook: start a token count in the background once TOKEN_STATS_EVERY has passed. Never raises."""
    try:
        if not _log_upload_enabled():
            return
        path = os.path.join(store_dir(), "token-stats.json")
        st = read_json(path, {}) or {}
        if time.time() - st.get("spawned", 0) < TOKEN_STATS_EVERY:
            return
        st["spawned"] = time.time()
        write_json_atomic(path, st)
        subprocess.Popen([sys.executable, "-I", os.path.abspath(__file__), "token-stats"],
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    except Exception:
        pass


def cmd_token_stats(args):
    """Count the tokens of the last TOKEN_DAYS days and send them to the pool server (the dashboard's Token panel)."""
    if not _log_upload_enabled():
        return
    lock_path = os.path.join(store_dir(), "token-stats.lock")
    if not _acquire_lock(lock_path, stale=600):
        return
    try:
        now = time.time()
        days = _token_days(os.path.join(config_dir(), "projects"), now=now, timeline=_account_timeline())
        # since: the first day counted; the server replaces every day of this machine from it on, so a day left with no
        # call of a pool account (or counted by an older version with every account) is cleared, not kept as it was
        since = (dt.datetime.fromtimestamp(now, TOKEN_TZ).date() - dt.timedelta(days=TOKEN_DAYS - 1)).isoformat()
        pool_call("POST", "/api/tokens", {"machine_id": pool_settings()["machine_id"], "name": machine_name(),
                                          "host": platform.node(), "version": plugin_version(), "since": since,
                                          "days": days}, timeout=20)
        path = os.path.join(store_dir(), "token-stats.json")
        st = read_json(path, {}) or {}
        st["sent"] = time.time()
        write_json_atomic(path, st)
    except Exception as e:
        log_event("read_fail", "token statistics: %s" % e, every=QUIET_EVERY, key="token-stats")
    finally:
        try:
            os.unlink(lock_path)
        except FileNotFoundError:
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
    p = sub.add_parser("token-stats", help=argparse.SUPPRESS); p.set_defaults(fn=cmd_token_stats)
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
