#!/bin/bash
# End-to-end test with FAKE accounts in a temp directory. Usage: bash tests/e2e.sh <temp-dir> scripts/claude_acct.py
export CLAUDE_ACCT_SWITCH_WAIT=0  # no post-switch sleep in tests
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
echo "== leftover test keychain items: $(security dump-keychain 2>/dev/null | grep -c claude-acct-TEST)"

echo "=========== new login (file backend, no terminal window)"
export CLAUDE_ACCT_BACKEND=file CLAUDE_ACCT_NO_TERMINAL=1
rm -rf "$S/store" "$S/cfg"; mkdir -p "$S/cfg"
login A tokA1; run save a
run login c c@x.com
H="$S/store/login-c/login.sh"; [ -f "$H" ] && echo "helper OK:" && grep -E 'auth|import-login' "$H" | sed 's#/[^ ]*/##g'
# simulate claude auth login writing into the temp dir
CLAUDE_CONFIG_DIR_SAVE="$S/cfg"; mkdir -p "$S/store/login-c"
( cd "$S/store/login-c" && python3 -I - "$S/store/login-c" C tokC1 <<'EOF'
import json,sys,time
d,who,tok=sys.argv[1:]
json.dump({"claudeAiOauth":{"accessToken":tok,"refreshToken":"r","expiresAt":1,"subscriptionType":"pro"}},open(d+"/.credentials.json","w"))
json.dump({"oauthAccount":{"accountUuid":"u-"+who,"organizationUuid":"o1","emailAddress":who+"@x.com","organizationName":"Org"}},open(d+"/.claude.json","w"))
EOF
)
run import-login "$S/store/login-c" c; [ -d "$S/store/login-c" ] && echo "TEMP DIR LEFT BEHIND" || echo "temp dir cleaned"; live
# log in again to c (same account) -> refresh; name a but account C -> error
for NAME in c a; do mkdir -p "$S/store/login-$NAME"; cp "$S/store/secrets/c.json" "$S/store/login-$NAME/.credentials.json"
 python3 -I -c "import json;json.dump({'oauthAccount':{'accountUuid':'u-C','organizationUuid':'o1','emailAddress':'C@x.com'}},open('$S/store/login-$NAME/.claude.json','w'))"
 run import-login "$S/store/login-$NAME" $NAME; done
run list

echo "=========== email-only login + lookup by email / part before @"
fake(){ mkdir -p "$1"; python3 -I -c "import json;json.dump({'claudeAiOauth':{'accessToken':'t-$2','refreshToken':'r','expiresAt':1}},open('$1/.credentials.json','w'));json.dump({'oauthAccount':{'accountUuid':'u-$2','organizationUuid':'o','emailAddress':'$2'}},open('$1/.claude.json','w'))"; }
run login alice@example.com
grep -o "'--email' '[^']*'\|import-login .*" "$S/store/login-alice@example.com/login.sh" | sed "s#'/[^']*'#<path>#g"
fake "$S/store/login-alice@example.com" bob@example.com
run import-login "$S/store/login-alice@example.com" alice@example.com alice@example.com   # wrong account -> error
fake "$S/store/login-alice@example.com" Alice@example.com
run import-login "$S/store/login-alice@example.com" alice@example.com alice@example.com   # right account (different case)
run login alice; grep -o "'--email' '[^']*'" "$S/store/login-alice@example.com/login.sh"   # log in again by the part before @
fake "$S/x2" alice@other.com; run import-login "$S/x2" alice@other.com alice@other.com
run switch alice              # ambiguous
run switch alice@example.com
run switch ALICE@OTHER.COM
run remove alice@other.com
run switch alice; run list

echo "=========== token state"
rm -rf "$S/store" "$S/cfg"; mkdir -p "$S/cfg"
login A tokA1; run save a          # current account, access token expired -> not flagged
login B tokB1; run save b          # access expired, refresh valid -> not flagged
login D tokD1; run save d
python3 -I - "$S/store" <<'EOF'
import json,sys,time
d=sys.argv[1]
c=json.load(open(d+"/secrets/b.json")); c["claudeAiOauth"]["refreshTokenExpiresAt"]=int(time.time()*1000)-1000
json.dump(c,open(d+"/secrets/b.json","w"))                       # refresh expired -> 🔴
i=json.load(open(d+"/accounts.json")); i["accounts"]["a"]["revoked"]=True; json.dump(i,open(d+"/accounts.json","w"))  # a revoked -> 🔴
EOF
run switch a; run list; run switch b
