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
            f.write("echo 'In the browser, be signed in to claude.ai as %s (sign out of any other account first).'\n" % email)
            f.write("read -r -p 'Press Enter to open the login page for %s... '\n" % email)
            f.write("rm -rf %s; mkdir -p %s\n" % (sh_quote(tmp), sh_quote(tmp)))
            f.write("bash %s login-backup\n" % sh_quote(acct_sh))
            f.write("CLAUDE_CONFIG_DIR=%s %s auth login --email %s\n" % (sh_quote(tmp), sh_quote(claude_bin()), sh_quote(email)))
            f.write("bash %s import-login %s %s %s || echo 'Not saved: %s. Run /acct:login %s later.'\n" % (
                sh_quote(acct_sh), sh_quote(tmp), sh_quote(name), sh_quote(email), email, email))
        f.write("echo; echo 'Done. You can close this window and go back to Claude: /acct:list'\n")
    os.chmod(helper, 0o700)
    if open_terminal(helper):
        print("Opened a **terminal window** to log in to %d account(s), one after the other: %s." % (
            len(todo), ", ".join(e for _, e in todo)))
        print("For each one: be signed in to claude.ai as that account in the browser, press Enter in the terminal, "
              "finish the login (paste the code into the terminal if the page shows one). Your current account is NOT changed.")
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
    _print_rotation_preview(idx, live)
    print("acct plugin %s" % plugin_version())


def _print_rotation_preview(idx, live):
    """The pool phase, the working machines, and where the Premium seat would go next. Changes nothing."""
    st = read_json(pool_settings_path(), {}) or {}
    state, machines = None, []
    if st.get("api_key") or st.get("mover_key"):
        try:
            state = pool_call("GET", "/api/state", mover=not st.get("api_key"))
            machines = pool_call("GET", "/api/machines", mover=not st.get("api_key"))
        except AcctError as e:
            print("\nPool server: %s" % e)
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
        name, skipped = _next_eligible_rotation_account(idx, holder, live=live)
    except Exception as e:
        print("Next Premium: could not be determined (%s)." % e)
        return
    if name:
        print("Next Premium: **%s**" % name)
    else:
        print("Next Premium: none eligible; %s keeps the seat. Why each account is skipped:" % holder)
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
# The backup accounts are never Premium: every machine waits on one of them while the seat moves. Machine 2
# keeps using one until it reaches BACKUP_5H_LIMIT (or any window is used up), then moves on to the other,
# so the 5-hour window of the one at rest can reset. The first one is used until machine 2 says otherwise.
BACKUP_EMAILS = ("info@xtgroup.vn", "careers@xtgroup.vn")
BACKUP_5H_LIMIT = 85
FORMER_BACKUP_EMAILS = ("hieule252uet@gmail.com",)  # 1.0.4-1.0.7: a machine still on it goes back to the pool
ROTATION_EMAILS = (  # the accounts that take turns holding Premium; the order breaks the last tie
    "xtts@xtgroup.vn", "sales@xtgroup.vn", "contact@xtgroup.vn",
    "hieunx@xtgroup.vn", "truongnv@xtgroup.vn", "thanglt@xtgroup.vn", "kienlt@xtgroup.vn",
)
POOL_EMAILS = BACKUP_EMAILS + ROTATION_EMAILS

# Rotation: the Premium account reaches 5-hour >= DAILY_THRESHOLD or 7-day >= WEEKLY_THRESHOLD.
DAILY_THRESHOLD = 80
WEEKLY_THRESHOLD = 90
# A Premium seat has 5x the limits of a Standard seat (6.25x vs 1.25x Pro, per the Team plan help page),
# so a percentage read on one seat is about PREMIUM_FACTOR times the same usage read on the other.
PREMIUM_FACTOR = 5
PREMIUM_CHECK_TTL = 30  # seconds; how stale the Premium account's seat and usage may be

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
        raise AcctError("pool server answered HTTP %d for %s" % (e.code, path))
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


def _rotation_rank(usage, offset):
    """(sort key, None) of a Standard account as the next Premium, lower first, or (None, reason) when it is skipped."""
    values = _usage_window_values(usage)
    over = ["%s %d%% ≥ %d%%" % (label, round(values.get(key, 0)), limit * PREMIUM_FACTOR)
            for key, label, limit in (("five_hour", "5-hour", DAILY_THRESHOLD), ("seven_day", "7-day", WEEKLY_THRESHOLD))
            if values.get(key, 0) / PREMIUM_FACTOR >= limit]
    if over:
        return None, ", ".join(over)
    reset = _parse_reset((usage.get("seven_day") or {}).get("resets_at")) or float("inf")
    return (values.get("seven_day", 100), values["five_hour"], reset, offset), None


def _next_eligible_rotation_account(idx, premium, live=None):
    """Pick the least-used Standard account of the rotation after checking the live status and usage of each.

    Standard members share the same limits, so their 7-day percentages compare actual use:
    lowest 7-day first, then lowest 5-hour, then the earliest 7-day reset, then the fixed order
    after the current Premium. An account whose usage would already be at a threshold once read
    against Premium limits (Standard reading / PREMIUM_FACTOR) is skipped: it would hand the seat on at once.
    """
    order = list(ROTATION_EMAILS)
    premium_email = _email_of(idx, premium) or premium
    start = order.index(premium_email) if premium_email in order else -1
    skipped, candidates = [], []
    for offset in range(1, len(order) + 1):
        email = order[(start + offset) % len(order)]
        if email == premium_email:
            continue
        name = _saved_name(idx, email)
        if not name:
            skipped.append("%s (not logged in on this machine)" % email)
            continue
        meta = idx["accounts"][name]
        try:
            creds = read_stored_creds(name)
        except AcctError:
            skipped.append("%s (credentials unavailable)" % name)
            continue
        if token_state(meta, creds) == "relogin":
            skipped.append("%s (login expired/revoked)" % name)
            continue
        # Never refresh the token of the live login: Claude Code owns it.
        status = refresh_usage(meta, creds, refresh_name=None if name == live else name)
        usage = (meta.get("usage") or {}).get("data") or {}
        values = _usage_window_values(usage)
        # Fail closed: do not move Premium onto an account without a fresh 5-hour reading.
        if status not in ("ok", "cache") or "five_hour" not in values:
            skipped.append("%s (usage unavailable)" % name)
            continue
        key, over = _rotation_rank(usage, offset)
        if over:
            skipped.append("%s (already at threshold: %s)" % (name, over))
            continue
        candidates.append(key + (name,))
    if not candidates:
        return None, skipped
    return min(candidates)[-1], skipped


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
        status, tier = _seat_tier(idx, name, live, force=force, ttl=PREMIUM_CHECK_TTL)
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
    """Why a backup account should rest (5-hour >= BACKUP_5H_LIMIT or a window used up), else None."""
    name = _saved_name(idx, email)
    if not name:
        return "not logged in on machine 2"
    meta = idx["accounts"][name]
    try:
        status = refresh_usage(meta, read_stored_creds(name), force=force, refresh_name=None if name == live else name)
    except AcctError as e:
        return str(e)
    if status not in ("ok", "cache"):
        return "usage unavailable (%s)" % status
    values = _usage_window_values((meta.get("usage") or {}).get("data") or {})
    if "five_hour" not in values:
        return "usage unavailable"
    if values["five_hour"] >= BACKUP_5H_LIMIT:
        return "5-hour %d%% >= %d%%" % (round(values["five_hour"]), BACKUP_5H_LIMIT)
    if any(v >= 100 for v in values.values()):
        return "7-day 100%"
    return None


def _choose_backup(idx, state, live, force=False):
    """Keep the backup account in use until it is spent, then the other one if it is not."""
    current = _backup_of(state)
    why = _backup_spent(idx, current, live, force=force)
    if not why:
        return current
    for other in BACKUP_EMAILS:
        if other != current and not _backup_spent(idx, other, live, force=force):
            log_event("backup_change", "%s rests (%s): the backup account is now %s" % (current, why, other))
            return other
    log_event("backup_spent", "every backup account is spent; keeping %s (%s)" % (current, why), every=QUIET_EVERY)
    return current


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


def _move_seat(idx, premium, target, live, fields, state=None):
    """PREPARING → wait for the backup account → MOVING → seat move → STABLE. Returns True on success."""
    p_email, t_email = _email_of(idx, premium), _email_of(idx, target)
    backup = _choose_backup(idx, state or {}, live, force=True)
    log_event("move_start", "threshold reached; backup %s" % backup, target=t_email, **fields)
    prepared = _put_state("PREPARING", p_email, t_email, "threshold reached", backup=backup)
    waiting = _wait_for_backup(prepared["updated_at"], backup)
    if waiting:
        log_event("prepare_timeout", "still not on %s after %ds: %s" % (backup, PREPARE_TIMEOUT, ", ".join(waiting)),
                  target=t_email, **fields)
    _put_state("MOVING", p_email, t_email)
    status, tier = _seat_tier(idx, premium, live, force=True)
    if status == "ok" and tier != PREMIUM_TIER:  # moved by hand meanwhile: publish where it is
        holder = _seat_holder(idx, live, force=True)
        _put_state("STABLE", _email_of(idx, holder) or p_email, message="seat moved outside the service")
        log_event("move_skipped", "seat moved outside the service", target=_email_of(idx, holder), **fields)
        return False
    started = time.time()
    try:
        _set_premium_seat(premium, target, load_rotation(), idx)
    except Exception as e:
        holder = _seat_holder(idx, live, force=True)  # the seat manager rolls back; trust only claude.ai
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
    if time.time() < _mover_state().get("backoff_until", 0) and not args.force:
        return
    _publish_accounts(idx, live, state.get("premium"))
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
        _put_state("STABLE", h_email, message="synced with claude.ai")
        log_event("follow", "published the Premium account found on claude.ai", premium=h_email)
    backup = _choose_backup(idx, state, live)
    if backup != _backup_of(state) or not state.get("backup"):
        state = _put_state("STABLE", h_email, message="backup account: %s" % backup, backup=backup)
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
                           refresh_name=None if holder == live else holder, ttl=PREMIUM_CHECK_TTL)
    save_index(idx)
    if status not in ("ok", "cache"):
        log_event("read_fail", "Premium usage: %s" % status, every=QUIET_EVERY, premium=h_email)
        return
    usage = (meta.get("usage") or {}).get("data") or {}
    if not _usage_reached_threshold(usage):
        return
    fields = dict(premium=h_email, **_usage_fields(meta))
    target, skipped = _next_eligible_rotation_account(idx, holder, live=live)
    save_index(idx)
    if not target:
        log_event("no_destination", "; ".join(skipped), every=QUIET_EVERY, **fields)
        return
    if args.manual:
        print("Threshold reached on %s: moving the Premium seat to %s..." % (holder, target))
    _move_seat(idx, holder, target, live, fields, state)


ACCOUNTS_EVERY = 60  # seconds between two usage tables machine 2 sends to the dashboard


def _account_rows(idx, live, premium=None):
    """The usage of every pool account for the dashboard, in display order: the Premium account, the
    rotation accounts in the order the next Premium is chosen (rank 1 = next), then the backup accounts."""
    order = list(ROTATION_EMAILS)
    premium = (premium or "").lower()
    start = order.index(premium) if premium in order else -1
    rows = []
    for email in POOL_EMAILS:
        name = _saved_name(idx, email)
        role = "rotation" if email in ROTATION_EMAILS else "backup"
        row = {"email": email, "role": "premium" if email == premium else role}
        if not name:
            rows.append(dict(row, status="not logged in on machine 2"))
            continue
        meta = idx["accounts"][name]
        try:
            creds = read_stored_creds(name)
            status = refresh_usage(meta, creds, refresh_name=None if name == live else name)
            refresh_seat(meta, creds, refresh_name=None if name == live else name)
            state = token_state(meta, creds)
        except AcctError as e:
            status, state = str(e), "relogin"
        usage = (meta.get("usage") or {}).get("data") or {}
        tier = (meta.get("seat") or {}).get("tier")
        row.update(seat=SEAT_LABELS.get(tier, tier) if tier else None,
                   five_hour=(usage.get("five_hour") or {}).get("utilization"),
                   five_hour_reset=(usage.get("five_hour") or {}).get("resets_at"),
                   seven_day=(usage.get("seven_day") or {}).get("utilization"),
                   seven_day_reset=(usage.get("seven_day") or {}).get("resets_at"),
                   read_at=(meta.get("usage") or {}).get("fetchedAt"),
                   status="log in again" if state == "relogin" else (None if status in ("ok", "cache", "expired") else status))
        if row["role"] == "rotation" and not row["status"] and "five_hour" in _usage_window_values(usage):
            offset = (order.index(email) - start) % len(order)
            row["_key"], over = _rotation_rank(usage, offset)
            if over:
                row["status"] = "already at threshold: " + over
        rows.append(row)
    ranked = sorted((r for r in rows if r.get("_key")), key=lambda r: r["_key"])
    for rank, r in enumerate(ranked, 1):
        r["rank"] = rank
    group = {"premium": 0, "rotation": 1, "backup": 2}
    rows.sort(key=lambda r: (group[r["role"]], r.get("rank") or 99))
    for r in rows:
        r.pop("_key", None)
    return rows


def _publish_accounts(idx, live, premium=None, force=False):
    ms = _mover_state()
    if not force and time.time() - ms.get("accounts_at", 0) < ACCOUNTS_EVERY:
        return
    try:
        rows = _account_rows(idx, live, premium)
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
    print("Backup accounts (while the seat moves, one at a time until 5-hour >= %d%%): %s" % (BACKUP_5H_LIMIT, ", ".join(BACKUP_EMAILS)))
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
        raise AcctError("Premium seat transfer failed. %s" % detail[-1200:])
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
