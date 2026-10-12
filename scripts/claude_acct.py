#!/usr/bin/env python3
"""claude-acct — quản lý nhiều account Claude Code (claude.ai login) trên một máy.

Lệnh:
  login  <name> [email]  Đăng nhập account mới / đăng nhập lại (mở Terminal), lưu dưới tên <name>
  save   <name>   Lưu account đang đăng nhập dưới tên <name>
  list            Liệt kê account đã lưu + usage 5h / 7 ngày
  switch <name>   Đổi sang account <name> (có hiệu lực ở phiên/tab Claude mới)
  remove <name>   Xoá account <name> khỏi danh sách (xoá token đã lưu)

Nơi Claude Code giữ đăng nhập (live):
  macOS         Keychain, service "Claude Code-credentials", account = $USER
  Windows/Linux <config_dir>/.credentials.json
  Danh tính     <home>/.claude.json, khoá "oauthAccount"

Nơi script giữ các account đã lưu ($CLAUDE_ACCT_HOME, mặc định ~/.claude-acct):
  accounts.json         danh sách + thông tin không bí mật + usage cache
  macOS                 token ở Keychain, service "claude-acct", account = <name>
  Windows/Linux         token ở secrets/<name>.json (quyền 600)

Chỉ dùng thư viện chuẩn của Python 3.8+.
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

# CLAUDE_ACCT_BACKEND=file ép dùng file thay Keychain (để test luồng Windows/Linux trên macOS)
IS_MAC = sys.platform == "darwin" and os.environ.get("CLAUDE_ACCT_BACKEND") != "file"
LIVE_SERVICE = "Claude Code-credentials"
STORE_SERVICE = os.environ.get("CLAUDE_ACCT_KEYCHAIN_SERVICE", "claude-acct")
USAGE_URL = "https://api.anthropic.com/api/oauth/usage"  # không chính thức
USAGE_CACHE_TTL = 120  # giây — tránh bị 429
EXPIRY_MARGIN_MS = 60_000
BACKUP_NAME = ".live-backup"  # tên dấu chấm: valid_name() không cho account trùng


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
    # Ghi qua `security -i` (stdin) + -X hex để token không xuất hiện trên argv / ps.
    hexdata = secret.encode("utf-8").hex()
    cmd = 'add-generic-password -U -s "%s" -a "%s" -X %s\n' % (service, account, hexdata)
    r = subprocess.run(["security", "-i"], input=cmd, capture_output=True, text=True)
    if r.returncode != 0 or "error" in (r.stderr or "").lower():
        raise AcctError("Ghi Keychain thất bại (%s/%s): %s" % (service, account, r.stderr.strip()))


def _kc_delete(service, account):
    subprocess.run(["security", "delete-generic-password", "-s", service, "-a", account],
                   capture_output=True, text=True)


def live_keychain_service():
    env = os.environ.get("CLAUDE_CONFIG_DIR")
    if not env:
        return LIVE_SERVICE
    # Theo quan sát cộng đồng (anthropics/claude-code#20553): hậu tố = 8 ký tự hex sha256 của đường dẫn.
    return "%s-%s" % (LIVE_SERVICE, hashlib.sha256(config_dir().encode()).hexdigest()[:8])


# ---------------------------------------------------------------- live credentials

def live_creds_file():
    return os.path.join(config_dir(), ".credentials.json")


def read_live_creds():
    """Trả về (dict credentials, nơi lưu) của account Claude Code đang đăng nhập."""
    if IS_MAC:
        raw = _kc_find(live_keychain_service(), getpass.getuser())
        if raw:
            return json.loads(raw), "keychain"
    data = read_json(live_creds_file())
    if data:
        return data, "file"
    raise AcctError("Không thấy đăng nhập Claude Code nào. Hãy gõ /login trước.")


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
            raise AcctError("Thiếu token đã lưu của '%s' trong Keychain." % name)
        return json.loads(raw)
    data = read_json(os.path.join(store_dir(), "secrets", name + ".json"))
    if data is None:
        raise AcctError("Thiếu token đã lưu của '%s'." % name)
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
    """Ghi token mới nhất của account đang dùng vào kho (Claude Code tự làm mới token theo thời gian)."""
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
        raise AcctError("Tên account không hợp lệ: '%s' (không dùng khoảng trắng, / \\ : hoặc dấu nháy)." % name)


def resolve_name(idx, query, must_exist=True):
    """Tìm account theo tên, email, hoặc phần trước @ của email (không phân biệt hoa thường)."""
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
            raise AcctError("'%s' khớp nhiều account: %s. Gõ đầy đủ tên." % (query, ", ".join(sorted(hits))))
    if must_exist:
        raise AcctError("Không có account '%s'. Đã lưu: %s" % (query, ", ".join(sorted(accts)) or "(trống)"))
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


def refresh_usage(meta, creds, force=False):
    """Cập nhật meta['usage'] nếu token còn hạn. Trả về chuỗi trạng thái."""
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
        return "http %d" % e.code
    except Exception as e:  # mạng lỗi, timeout...
        return "lỗi: %s" % type(e).__name__
    meta["usage"] = {"fetchedAt": now, "data": data}
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
        return "vừa xong"
    if mins < 60:
        return "%d phút trước" % mins
    if mins < 60 * 24:
        return "%d giờ trước" % (mins // 60)
    return "%d ngày trước" % (mins // 1440)


# ---------------------------------------------------------------- commands

def store_account(idx, name, creds, profile, source):
    """Lưu creds+profile dưới tên name. Cùng tên + cùng account = cập nhật token (đăng nhập lại)."""
    ident = identity(profile)
    if not ident or ident == (None, None):
        raise AcctError("Không đọc được thông tin account (%s)." % source)
    email = (profile or {}).get("emailAddress", "?")
    existing = find_by_identity(idx, ident)
    if existing and existing != name:
        raise AcctError("Account %s đã được lưu với tên '%s'. Dùng tên đó (vd /acct:login %s để đăng nhập lại)."
                        % (email, existing, existing))
    if name in idx["accounts"] and identity(idx["accounts"][name].get("profile")) != ident:
        raise AcctError("Tên '%s' đang là của account khác (%s). Chọn tên khác hoặc /acct:logout %s trước."
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
    print("%s account **%s** — %s (%s)." % ("Đã cập nhật đăng nhập" if relogin else "Đã lưu",
          name, email, (profile or {}).get("organizationName", "?")))


def cmd_save(args):
    valid_name(args.name)
    idx = load_index()
    creds, _ = read_live_creds()
    store_account(idx, args.name, creds, read_live_profile(), "account đang đăng nhập")


# ---------------------------------------------------------------- login (đăng nhập mới trong Terminal)

class _ConfigDir:
    """Tạm trỏ CLAUDE_CONFIG_DIR sang thư mục khác để đọc/xoá đăng nhập trong đó."""

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
    raise AcctError("Không tìm thấy chương trình claude. Chạy lệnh này trong Claude Code, hoặc thêm claude vào PATH.")


def sh_quote(s):
    return "'" + s.replace("'", "'\\''") + "'"


def cmd_login(args):
    valid_name(args.name)
    idx = load_index()
    existing = resolve_name(idx, args.name, must_exist=False)
    if existing:  # đăng nhập lại account đã lưu
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
        # Sao lưu đăng nhập đang dùng, phòng Keychain không tách theo CLAUDE_CONFIG_DIR (xem cmd_import_login).
        main = _kc_find(LIVE_SERVICE, getpass.getuser())
        if main:
            _kc_write(STORE_SERVICE, BACKUP_NAME, main)
    login_cmd = [claude_bin(), "auth", "login"] + (["--email", args.email] if args.email else [])
    helper = os.path.join(tmp, "login.sh")
    with open(helper, "w", encoding="utf-8", newline="\n") as f:
        f.write("#!/usr/bin/env bash\n")
        f.write("echo '=== Đăng nhập account Claude: %s ==='\n" % args.name)
        f.write("export CLAUDE_CONFIG_DIR=%s\n" % sh_quote(tmp))
        f.write(" ".join(sh_quote(a) for a in login_cmd) + "\n")
        f.write("unset CLAUDE_CONFIG_DIR\n")
        f.write("bash %s import-login %s %s %s\n" % (
            sh_quote(os.path.join(os.path.dirname(os.path.abspath(__file__)), "acct.sh")), sh_quote(tmp),
            sh_quote(args.name), sh_quote(args.email or "")))
        f.write("echo; echo 'Xong. Có thể đóng cửa sổ này và quay lại Claude: /acct:list'\n")
    os.chmod(helper, 0o700)
    opened = open_terminal(helper)
    if opened:
        print("Đã mở cửa sổ **Terminal** để đăng nhập account **%s**%s." % (
            args.name, " (" + args.email + ")" if args.email else ""))
        print("1. Đăng nhập trên trình duyệt vừa mở (đúng account muốn lưu).")
        print("2. Nếu trang web hiện mã, dán mã vào cửa sổ Terminal rồi Enter.")
        print("3. Xong thì gõ /acct:list để kiểm tra. Account đang dùng KHÔNG bị thay đổi.")
    else:
        print("Không tự mở được cửa sổ terminal. Mở một terminal rồi chạy:")
        print("")
        print("    bash %s" % sh_quote(helper))


def open_terminal(script):
    if os.environ.get("CLAUDE_ACCT_NO_TERMINAL"):  # dùng khi test
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
    """Gọi từ login.sh sau khi `claude auth login` xong: lấy đăng nhập trong thư mục tạm, lưu, rồi dọn."""
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
            # Phòng trường hợp Keychain KHÔNG tách theo CLAUDE_CONFIG_DIR: đăng nhập mới đã ghi đè
            # mục chính. Lấy token mới từ mục chính rồi trả lại đăng nhập cũ đã sao lưu.
            backup = _kc_find(STORE_SERVICE, BACKUP_NAME)
            main = _kc_find(LIVE_SERVICE, getpass.getuser())
            if backup and main and main != backup:
                creds = json.loads(main)
                _kc_write(LIVE_SERVICE, getpass.getuser(), backup)
                print("(Keychain dùng chung mục chính — đã khôi phục đăng nhập đang dùng.)")
        if creds is None:
            raise AcctError("Chưa đăng nhập xong (không thấy token mới). Chạy lại /acct:login %s." % args.name)
        got = ((profile or {}).get("emailAddress") or "").lower()
        if args.expect_email and got and got != args.expect_email.lower():
            raise AcctError("Bạn vừa đăng nhập %s, không phải %s — KHÔNG lưu. Chạy lại /acct:login %s và chọn đúng account trên trình duyệt."
                            % (got, args.expect_email, args.name))
        store_account(idx, args.name, creds, profile, "đăng nhập mới")
    finally:
        if IS_MAC:
            _kc_delete(service, getpass.getuser())
            _kc_delete(STORE_SERVICE, BACKUP_NAME)
        shutil.rmtree(tmp, ignore_errors=True)


def cmd_list(args):
    idx = load_index()
    if not idx["accounts"]:
        print("Chưa lưu account nào. Gõ /acct:login <tên> [email] để đăng nhập, hoặc /acct:save <tên> để lưu account đang dùng.")
        return
    try:
        cur = sync_current(idx)
    except AcctError:
        cur = None
    live_profile = read_live_profile()
    rows = []
    for name in sorted(idx["accounts"]):
        meta = idx["accounts"][name]
        try:
            creds = read_stored_creds(name)
            status = refresh_usage(meta, creds, force=args.refresh)
        except AcctError as e:
            status = str(e)
        usage = (meta.get("usage") or {}).get("data") or {}
        fetched = (meta.get("usage") or {}).get("fetchedAt")
        if status in ("ok", "cache"):
            note = "cập nhật " + fmt_age(fetched)
        elif fetched:
            note = "số cũ, cập nhật %s (%s)" % (fmt_age(fetched), "token hết hạn" if status == "expired" else status)
        else:
            note = "chưa có số (%s)" % ("token hết hạn — switch sang để làm mới" if status == "expired" else status)
        p = meta.get("profile") or {}
        rows.append([
            ("▶ " if name == cur else "") + name,
            p.get("emailAddress", "?"),
            p.get("organizationName", "?"),
            (meta.get("oauth") or {}).get("subscriptionType") or "?",
            fmt_window(usage.get("five_hour")),
            fmt_window(usage.get("seven_day")),
            note,
        ])
    save_index(idx)
    head = ["Account", "Email", "Tổ chức", "Gói", "5 giờ", "7 ngày", "Ghi chú"]
    print("| " + " | ".join(head) + " |")
    print("|" + "---|" * len(head))
    for r in rows:
        print("| " + " | ".join(r) + " |")
    if not cur:
        print("\nĐang đăng nhập %s nhưng account này chưa được lưu — gõ /acct:save <tên> để lưu."
              % ((live_profile or {}).get("emailAddress") or "(không rõ)"))
    else:
        print("\n▶ = account đang dùng.")


def cmd_switch(args):
    idx = load_index()
    args.name = resolve_name(idx, args.name)
    cur = current_name(idx)
    if cur == args.name:
        print("Đang dùng **%s** rồi." % args.name)
        return
    live_creds, where = read_live_creds()
    if cur is None and not args.force:
        raise AcctError("Account đang đăng nhập (%s) chưa được lưu — đổi đi sẽ mất đăng nhập này. "
                        "Gõ /acct:save <tên> để lưu trước, hoặc thêm --force để bỏ qua."
                        % ((read_live_profile() or {}).get("emailAddress") or "không rõ"))
    if cur:
        write_stored_creds(cur, live_creds)  # giữ token mới nhất của account cũ
        idx["accounts"][cur]["oauth"] = public_oauth(live_creds)
    target = read_stored_creds(args.name)
    write_live_creds(target, where)
    write_live_profile(idx["accounts"][args.name].get("profile"))
    save_index(idx)
    p = idx["accounts"][args.name].get("profile") or {}
    print("Đã chuyển sang **%s** — %s (%s)." % (args.name, p.get("emailAddress", "?"), p.get("organizationName", "?")))
    print("Tab đang mở có thể tự dùng account mới sau khoảng 30–60 giây (chưa được Anthropic xác nhận). "
          "Chắc chắn nhất: mở tab Claude mới rồi /resume để tiếp tục cuộc hội thoại cũ.")


def cmd_remove(args):
    idx = load_index()
    args.name = resolve_name(idx, args.name)
    was_current = current_name(idx) == args.name
    delete_stored_creds(args.name)
    del idx["accounts"][args.name]
    save_index(idx)
    print("Đã xoá **%s** khỏi danh sách." % args.name)
    if was_current:
        print("Đây là account đang đăng nhập — Claude Code vẫn đăng nhập account này. "
              "Muốn đăng xuất hẳn thì gõ /logout, hoặc /acct:switch sang account khác.")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="claude-acct", description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("login", help="đăng nhập account mới/lại (mở Terminal)"); p.add_argument("name", nargs="?"); p.add_argument("email", nargs="?"); p.set_defaults(fn=cmd_login)
    p = sub.add_parser("import-login", help=argparse.SUPPRESS); p.add_argument("dir"); p.add_argument("name"); p.add_argument("expect_email", nargs="?"); p.set_defaults(fn=cmd_import_login)
    p = sub.add_parser("save", help="lưu account đang đăng nhập"); p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_save)
    p = sub.add_parser("list", help="liệt kê + usage"); p.add_argument("--refresh", action="store_true"); p.set_defaults(fn=cmd_list)
    p = sub.add_parser("switch", help="đổi account"); p.add_argument("name", nargs="?"); p.add_argument("--force", action="store_true"); p.set_defaults(fn=cmd_switch)
    p = sub.add_parser("remove", help="xoá account khỏi danh sách"); p.add_argument("name", nargs="?"); p.set_defaults(fn=cmd_remove)
    args = ap.parse_args(argv)
    try:
        if hasattr(args, "name") and not args.name:
            hint = {"login": "/acct:login", "save": "/acct:save", "switch": "/acct:switch", "remove": "/acct:logout"}[args.cmd]
            raise AcctError("Thiếu tên account. Ví dụ: %s work" % hint)
        args.fn(args)
    except AcctError as e:
        print("Lỗi: %s" % e)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
