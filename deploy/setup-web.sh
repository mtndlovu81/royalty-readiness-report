#!/usr/bin/env bash
#
# Web server setup — Phases 2 and 3 of DEPLOY.md.
# Idempotent: safe to re-run. Doubles as the redeploy script.
#
# Installs the unit files from deploy/ rather than generating them, so the
# committed copies stay authoritative.
#
#   sudo ./deploy/setup-web.sh                      # Web02, no worker
#   RUN_WORKER=true sudo -E ./deploy/setup-web.sh   # Web01
#
# Does NOT create the database, apply the schema, or seed. Those run once,
# from Web01, by hand.

set -euo pipefail

APP_DIR=/srv/r3
APP_USER=r3
REPO="${REPO:-https://github.com/mtndlovu81/royalty-readiness-report.git}"
PYTHON=/usr/local/bin/python3.12
DB_HOST=10.227.9.215
RUN_WORKER="${RUN_WORKER:-false}"

log() { printf '\n\033[1;34m==>\033[0m %s\n' "$1"; }
die() { printf '\n\033[1;31mERROR:\033[0m %s\n' "$1" >&2; exit 1; }

[[ $EUID -eq 0 ]] || die "run with sudo"
[[ -x $PYTHON ]] || die "$PYTHON not found — Phase 0 (Python build) incomplete"

# --- service user -----------------------------------------------------------
# The units run as r3, not ubuntu. System account, no login shell.
log "Service user"
if id "$APP_USER" &>/dev/null; then
    echo "  $APP_USER exists"
else
    useradd --system --home-dir "$APP_DIR" --shell /usr/sbin/nologin "$APP_USER"
    echo "  created $APP_USER"
fi

# --- code -------------------------------------------------------------------
log "Code"
if [[ -d $APP_DIR/.git ]]; then
    git -C "$APP_DIR" pull --ff-only
else
    mkdir -p "$APP_DIR"
    git clone "$REPO" "$APP_DIR"
fi
chown -R "$APP_USER:$APP_USER" "$APP_DIR"

# --- virtualenv -------------------------------------------------------------
log "Virtualenv"
if [[ ! -x $APP_DIR/.venv/bin/python ]]; then
    "$PYTHON" -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/requirements.txt"
chown -R "$APP_USER:$APP_USER" "$APP_DIR/.venv"

# --- environment ------------------------------------------------------------
# Never overwrite an existing .env — it holds the database password.
if [[ -f $APP_DIR/.env ]]; then
    log "Environment (existing .env kept)"
    sed -i "s/^RUN_WORKER=.*/RUN_WORKER=${RUN_WORKER}/" "$APP_DIR/.env"
else
    log "Environment (creating .env)"
    read -rsp "Postgres password for user r3: " DB_PASSWORD
    echo
    [[ -n $DB_PASSWORD ]] || die "password cannot be empty"
    read -rp "MusicBrainz contact (email or project URL): " MB_CONTACT
    [[ -n $MB_CONTACT ]] || die "contact cannot be empty"

    cat > "$APP_DIR/.env" <<EOF
DATABASE_URL=postgresql://r3:${DB_PASSWORD}@${DB_HOST}:5432/r3
MB_USER_AGENT=RoyaltyReadinessReport/1.0 ( ${MB_CONTACT} )
MB_RATE_LIMIT_SECONDS=1.0
STALE_AFTER_DAYS=60
RUN_WORKER=${RUN_WORKER}
TRUST_PROXY_HEADERS=true
LOG_LEVEL=info
EOF
fi
chown "$APP_USER:$APP_USER" "$APP_DIR/.env"
chmod 600 "$APP_DIR/.env"

# --- preflight --------------------------------------------------------------
log "Preflight"

# Catches a venv built with the wrong interpreter — otherwise this surfaces
# as a confusing TypeError when the service starts.
(cd "$APP_DIR" && sudo -u "$APP_USER" "$APP_DIR/.venv/bin/python" \
    -c "import r3.main, r3.diagnostics") 2>/dev/null \
    || die "imports failed — was the venv built with $PYTHON?"
echo "  imports ok"

DB_URL=$(grep '^DATABASE_URL=' "$APP_DIR/.env" | cut -d= -f2-)
psql "$DB_URL" -c 'SELECT 1;' >/dev/null 2>&1 \
    || die "cannot reach Postgres at $DB_HOST — check Phase 1 and pg_hba.conf"
echo "  database reachable"

# --- systemd ----------------------------------------------------------------
log "systemd"

install -m 644 "$APP_DIR/deploy/r3-web.service" /etc/systemd/system/

if [[ $RUN_WORKER == true ]]; then
    install -m 644 "$APP_DIR/deploy/r3-worker.service" /etc/systemd/system/
    install -m 644 "$APP_DIR/deploy/r3-worker.timer"   /etc/systemd/system/
fi

systemctl daemon-reload
systemctl enable r3-web >/dev/null
systemctl restart r3-web

if [[ $RUN_WORKER == true ]]; then
    systemctl enable --now r3-worker.timer >/dev/null
    echo "  worker timer enabled — this must be the ONLY server running it"
else
    # Defensive, not merely "don't enable": a worker on the second server
    # doubles the outbound rate to MusicBrainz and gets the app blocked.
    systemctl disable --now r3-worker.timer 2>/dev/null || true
    rm -f /etc/systemd/system/r3-worker.service /etc/systemd/system/r3-worker.timer
    systemctl daemon-reload
    echo "  worker units absent (correct for the non-worker server)"
fi

# --- verify -----------------------------------------------------------------
log "Verify"
sleep 2

curl -fsS localhost:8000/health >/dev/null \
    || { journalctl -u r3-web -n 30 --no-pager; die "health check failed on localhost"; }
echo "  health ok on localhost"

# The bind address matters: HAProxy runs on Lb01, a different machine. Binding
# to 127.0.0.1 makes the backend unreachable and fails every health check.
MY_IP=$(hostname -I | awk '{print $1}')
curl -fsS "http://${MY_IP}:8000/health" >/dev/null \
    || die "not reachable on ${MY_IP}:8000 — check --host 0.0.0.0 in r3-web.service"
echo "  health ok on ${MY_IP} (HAProxy can reach this)"

log "Done — RUN_WORKER=${RUN_WORKER}"
