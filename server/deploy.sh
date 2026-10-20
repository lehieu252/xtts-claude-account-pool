#!/usr/bin/env bash
# Deploy the Account Pool server to the xtts server (run from this repo):
#   bash server/deploy.sh [ssh-host]
# Idempotent: the first run creates the API key, the mover key and the dashboard password in
# /etc/account-pool.env (never in git); later runs only update the code and restart.
set -euo pipefail
HOST="${1:-xtts}"
HERE="$(cd "$(dirname "$0")" && pwd)"
BASE=/mnt/data/account-pool

echo "==> copy code -> $HOST:$BASE/app"
ssh "$HOST" "mkdir -p $BASE/app $BASE/data"
rsync -a --delete --exclude __pycache__/ --exclude deploy.sh "$HERE/" "$HOST:$BASE/app/"

echo "==> configure"
ssh "$HOST" "bash -s" <<'REMOTE'
set -euo pipefail
BASE=/mnt/data/account-pool
ENV=/etc/account-pool.env
if [ ! -f "$ENV" ]; then
  umask 077
  gen() { python3 -c 'import secrets;print(secrets.token_urlsafe(24))'; }
  printf 'POOL_API_KEY=%s\nPOOL_MOVER_KEY=%s\nPOOL_VIEW_USER=xtts\nPOOL_VIEW_PASS=%s\n' "$(gen)" "$(gen)" "$(python3 -c 'import secrets;print(secrets.token_urlsafe(12))')" > "$ENV"
  echo "   created keys and dashboard password in $ENV"
fi
grep -v '^POOL_DATA=\|^POOL_PORT=\|^POOL_BASE=' "$ENV" > "$ENV.tmp" || true
printf 'POOL_DATA=%s/data\nPOOL_PORT=5680\nPOOL_BASE=/pool\n' "$BASE" >> "$ENV.tmp"
install -m 600 "$ENV.tmp" "$ENV"; rm -f "$ENV.tmp"

install -m 644 $BASE/app/account-pool.service /etc/systemd/system/account-pool.service
systemctl daemon-reload
systemctl enable -q account-pool
systemctl restart account-pool
for i in $(seq 1 20); do curl -fsS http://127.0.0.1:5680/healthz && break; sleep 1; done; echo
systemctl is-active account-pool
REMOTE
echo "==> done: http://42.96.41.209:5680/  (https://xtts.xtgroup.vn/pool/ once nginx has the /pool/ block)"
echo "   keys and dashboard password: ssh $HOST cat /etc/account-pool.env"
