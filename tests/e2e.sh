#!/bin/bash
# Test end-to-end bằng account GIẢ trong thư mục tạm. Dùng: bash tests/e2e.sh <thư-mục-tạm> scripts/claude_acct.py
S="$1"; SCRIPT="$2"; rm -rf "$S"; mkdir -p "$S/cfg"
run(){ echo "\$ acct $*"; CLAUDE_CONFIG_DIR="$S/cfg" CLAUDE_ACCT_HOME="$S/store" CLAUDE_ACCT_KEYCHAIN_SERVICE=claude-acct-TEST python3 -I "$SCRIPT" "$@"; echo "[exit $?]"; }
login(){ python3 -I - "$S/cfg" "$1" "$2" <<'EOF'
import json,sys,time
d,who,tok=sys.argv[1:]
json.dump({"claudeAiOauth":{"accessToken":tok,"refreshToken":"r-"+tok,"expiresAt":int(time.time()*1000)-1,"subscriptionType":"max"}},open(d+"/.credentials.json","w"))
try: g=json.load(open(d+"/.claude.json"))
except Exception: g={"other":"keep-me"}
g["oauthAccount"]={"accountUuid":"u-"+who,"organizationUuid":"o1","emailAddress":who+"@x.com","organizationName":"Org"}
json.dump(g,open(d+"/.claude.json","w"))
EOF
}
live(){ python3 -I -c "import json;c=json.load(open('$S/cfg/.credentials.json'));g=json.load(open('$S/cfg/.claude.json'));print('LIVE =',c['claudeAiOauth']['accessToken'],g['oauthAccount']['emailAddress'],'other=',g.get('other'))"; }
for BACKEND in keychain file; do
 echo "=========== store backend: $BACKEND"; rm -rf "$S/store" "$S/cfg"; mkdir -p "$S/cfg"
 if [ $BACKEND = file ]; then export CLAUDE_ACCT_BACKEND=file; fi
 run list
 login A tokA1; run save a
 run save a2
 login B tokB1; run switch a
 run save b
 run save a
 login B tokB2; run switch a; live
 run list
 run switch b; live
 run switch b
 run switch zzz
 run remove a; run list
 run remove b
 unset CLAUDE_ACCT_BACKEND
done
echo "== keychain item test còn sót: $(security dump-keychain 2>/dev/null | grep -c claude-acct-TEST)"

echo "=========== login mới (file backend, không mở Terminal)"
export CLAUDE_ACCT_BACKEND=file CLAUDE_ACCT_NO_TERMINAL=1
rm -rf "$S/store" "$S/cfg"; mkdir -p "$S/cfg"
login A tokA1; run save a
run login c c@x.com
H="$S/store/login-c/login.sh"; [ -f "$H" ] && echo "helper OK:" && grep -E 'auth|import-login' "$H" | sed 's#/[^ ]*/##g'
# giả lập claude auth login ghi vào thư mục tạm
CLAUDE_CONFIG_DIR_SAVE="$S/cfg"; mkdir -p "$S/store/login-c"
( cd "$S/store/login-c" && python3 -I - "$S/store/login-c" C tokC1 <<'EOF'
import json,sys,time
d,who,tok=sys.argv[1:]
json.dump({"claudeAiOauth":{"accessToken":tok,"refreshToken":"r","expiresAt":1,"subscriptionType":"pro"}},open(d+"/.credentials.json","w"))
json.dump({"oauthAccount":{"accountUuid":"u-"+who,"organizationUuid":"o1","emailAddress":who+"@x.com","organizationName":"Org"}},open(d+"/.claude.json","w"))
EOF
)
run import-login "$S/store/login-c" c; [ -d "$S/store/login-c" ] && echo "TMP CÒN SÓT" || echo "tmp đã dọn"; live
# đăng nhập lại c (cùng account) -> cập nhật; đăng nhập tên a nhưng account C -> lỗi
for NAME in c a; do mkdir -p "$S/store/login-$NAME"; cp "$S/store/secrets/c.json" "$S/store/login-$NAME/.credentials.json"
 python3 -I -c "import json;json.dump({'oauthAccount':{'accountUuid':'u-C','organizationUuid':'o1','emailAddress':'C@x.com'}},open('$S/store/login-$NAME/.claude.json','w'))"
 run import-login "$S/store/login-$NAME" $NAME; done
run list

echo "=========== login chỉ bằng email + tìm theo email/phần trước @"
fake(){ mkdir -p "$1"; python3 -I -c "import json;json.dump({'claudeAiOauth':{'accessToken':'t-$2','refreshToken':'r','expiresAt':1}},open('$1/.credentials.json','w'));json.dump({'oauthAccount':{'accountUuid':'u-$2','organizationUuid':'o','emailAddress':'$2'}},open('$1/.claude.json','w'))"; }
run login alice@example.com
grep -o "'--email' '[^']*'\|import-login .*" "$S/store/login-alice@example.com/login.sh" | sed "s#'/[^']*'#<path>#g"
fake "$S/store/login-alice@example.com" khac@example.com
run import-login "$S/store/login-alice@example.com" alice@example.com alice@example.com   # nhầm account -> lỗi
fake "$S/store/login-alice@example.com" Alice@example.com
run import-login "$S/store/login-alice@example.com" alice@example.com alice@example.com   # đúng (khác hoa thường)
run login alice; grep -o "'--email' '[^']*'" "$S/store/login-alice@example.com/login.sh"   # đăng nhập lại theo phần trước @
fake "$S/x2" alice@other.com; run import-login "$S/x2" alice@other.com alice@other.com
run switch alice              # nhập nhằng
run switch alice@example.com
run switch ALICE@OTHER.COM
run remove alice@other.com
run switch alice; run list
