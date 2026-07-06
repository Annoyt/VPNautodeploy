#!/usr/bin/env bash
# vpn-bot installer.
#
# Modular: each section can be skipped via flag. Defaults to a full,
# interactive install. Designed to be safe to re-run on an existing
# host (idempotent — won't clobber .env, won't reset volumes).
#
# Usage:
#   sudo ./scripts/install.sh                          # interactive, all features
#   sudo ./scripts/install.sh --yes                    # non-interactive defaults
#   sudo ./scripts/install.sh --no-agent --no-caddy    # skip those parts
#
# Flags:
#   --yes              don't prompt, assume "yes" everywhere
#   --no-docker        skip docker engine install (already installed)
#   --no-deploy        skip the actual `docker compose up`
#   --no-caddy         skip Caddy + Let's Encrypt
#   --no-agent         skip the OpenCode agent server
#   --no-backup        skip the daily backup timer

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/vpn-bot}"
REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
BACKUP_DIR="/opt/backups"

ASSUME_YES="${ASSUME_YES:-0}"
DO_DOCKER=1
DO_DEPLOY=1
DO_CADDY=1
DO_AGENT=1
DO_BACKUP=1

for arg in "$@"; do
  case "$arg" in
    --yes|-y) ASSUME_YES=1 ;;
    --no-docker) DO_DOCKER=0 ;;
    --no-deploy) DO_DEPLOY=0 ;;
    --no-caddy)  DO_CADDY=0 ;;
    --no-agent)  DO_AGENT=0 ;;
    --no-backup) DO_BACKUP=0 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "Unknown flag: $arg" >&2; exit 2 ;;
  esac
done

if [[ "$EUID" -ne 0 ]]; then
  echo "This installer must run as root. Try: sudo $0" >&2
  exit 1
fi

# ---------- helpers ----------

log()  { printf '\033[1;36m==>\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[!]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31merror:\033[0m %s\n' "$*" >&2; exit 1; }

ask_yn() {
  local prompt="$1" default="${2:-y}"
  if [[ "$ASSUME_YES" -eq 1 ]]; then return 0; fi
  local hint="[Y/n]"; [[ "$default" == "n" ]] && hint="[y/N]"
  read -r -p "$(printf '%s %s ' "$prompt" "$hint")" reply || true
  reply="${reply:-$default}"
  [[ "$reply" =~ ^[Yy]$ ]]
}

ask_value() {
  local prompt="$1" default="${2:-}"
  if [[ "$ASSUME_YES" -eq 1 ]]; then printf '%s' "$default"; return; fi
  read -r -p "$prompt [$default]: " reply || true
  printf '%s' "${reply:-$default}"
}

# ---------- preflight ----------

log "Detected source repo: $REPO_DIR"
log "Target deploy dir:    $APP_DIR"

if [[ ! -f "$REPO_DIR/docker-compose.yml" ]]; then
  die "docker-compose.yml not found in $REPO_DIR — run this script from the repo root."
fi

# ---------- 1. base packages ----------

log "Installing base packages (curl, jq, sqlite3, openssl, tmux, rsync)..."
apt-get -qq update
DEBIAN_FRONTEND=noninteractive apt-get -qq install -y \
    curl jq sqlite3 openssl tmux python3 python3-pip ca-certificates rsync >/dev/null

# ---------- 2. docker ----------

if [[ "$DO_DOCKER" -eq 1 ]]; then
  if command -v docker >/dev/null 2>&1; then
    log "Docker already installed: $(docker --version)"
  else
    log "Installing Docker via official convenience script..."
    curl -fsSL https://get.docker.com | sh
    systemctl enable --now docker
  fi
fi

# ---------- 3. deploy app code ----------

log "Syncing repo to $APP_DIR..."
mkdir -p "$APP_DIR"
rsync -a --delete \
    --exclude='.git/' \
    --exclude='.pytest_cache/' \
    --exclude='__pycache__/' \
    --exclude='venv/' \
    --exclude='.prod-hotfixes-capture/' \
    "$REPO_DIR/" "$APP_DIR/"

# Preserve existing .env if present
if [[ ! -f "$APP_DIR/.env" ]]; then
  cp "$APP_DIR/.env.example" "$APP_DIR/.env"
  warn "Created $APP_DIR/.env from .env.example — fill in BOT_TOKEN, REALITY_PUBLIC_KEY, etc., before starting the bot."
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# ---------- 4. caddy + lets encrypt ----------

if [[ "$DO_CADDY" -eq 1 ]] && ask_yn "Install Caddy + Let's Encrypt for the admin dashboard?"; then
  domain=$(ask_value "Dashboard domain (e.g. yourdomain.example.com)" "")
  if [[ -z "$domain" ]]; then
    warn "No domain given, skipping Caddy."
  else
    port=$(ask_value "Caddy listen port (443 conflicts with Xray; default 9443)" "9443")

    log "Installing Caddy from official apt repo..."
    apt-get -qq install -y debian-keyring debian-archive-keyring apt-transport-https gnupg >/dev/null
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
        | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
    curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
        | tee /etc/apt/sources.list.d/caddy-stable.list >/dev/null
    apt-get -qq update
    apt-get -qq install -y caddy >/dev/null

    log "Writing Caddyfile for $domain:$port → 127.0.0.1:8080 ..."
    [[ -f /etc/caddy/Caddyfile ]] && cp /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak
    cat > /etc/caddy/Caddyfile <<CADDY
# vpn-bot admin dashboard (auto-managed Let's Encrypt cert)
${domain}:${port} {
    encode gzip
    reverse_proxy 127.0.0.1:8080
}
CADDY
    systemctl reload caddy
    log "Caddy reloaded. Cert will be issued on first request to https://${domain}:${port}/."

    # Make WEBAPP_URL match what we just set up.
    if ! grep -q '^WEBAPP_URL=' "$APP_DIR/.env"; then
      echo "WEBAPP_URL=https://${domain}:${port}/" >> "$APP_DIR/.env"
    else
      sed -i "s#^WEBAPP_URL=.*#WEBAPP_URL=https://${domain}:${port}/#" "$APP_DIR/.env"
    fi
  fi
fi

# ---------- 5. OpenCode agent server ----------

if [[ "$DO_AGENT" -eq 1 ]] && ask_yn "Install OpenCode server (admin AI agent in Telegram)?"; then
  if command -v opencode >/dev/null 2>&1; then
    log "opencode already installed: $(command -v opencode)"
  else
    log "Downloading opencode installer..."
    curl -fsSL https://opencode.ai/install | bash
  fi

  log "Installing opencode.json + systemd unit..."
  install -m 0644 "$REPO_DIR/scripts/opencode.json" "$APP_DIR/opencode.json"
  install -m 0644 "$REPO_DIR/scripts/opencode.service" /etc/systemd/system/opencode.service
  mkdir -p /tmp/agent_out

  if [[ ! -f /etc/opencode.env ]]; then
    PW=$(openssl rand -hex 24)
    cat > /etc/opencode.env <<ENV
OPENCODE_SERVER_PASSWORD=${PW}
# Add the provider key matching opencode.json "model", e.g.:
# MOONSHOT_API_KEY=...
# ANTHROPIC_API_KEY=...
ENV
    chmod 600 /etc/opencode.env
    log "Generated /etc/opencode.env with a fresh basic-auth password."
    warn "Add a provider API key to /etc/opencode.env (see opencode.json 'model')."
  fi

  systemctl daemon-reload
  systemctl enable --now opencode.service

  PW=$(grep '^OPENCODE_SERVER_PASSWORD=' /etc/opencode.env | cut -d= -f2-)
  sleep 3
  if curl -fsS --max-time 5 -u "opencode:${PW}" http://127.0.0.1:4096/global/health >/dev/null; then
    log "opencode server healthy on :4096."
  else
    warn "opencode not responding on :4096 — check 'journalctl -u opencode'."
  fi

  # Wire it into the bot's .env
  for var in "OPENCODE_URL=http://host.docker.internal:4096" \
             "OPENCODE_USERNAME=opencode" \
             "OPENCODE_SERVER_PASSWORD=${PW}"; do
    key="${var%%=*}"
    if grep -q "^${key}=" "$APP_DIR/.env"; then
      sed -i "s#^${key}=.*#${var}#" "$APP_DIR/.env"
    else
      echo "$var" >> "$APP_DIR/.env"
    fi
  done

  cat <<MSG

  ⚠ Before /ai works: pick the provider/model in $APP_DIR/opencode.json and
    put the matching API key in /etc/opencode.env, then:
        systemctl restart opencode
    (Some providers need 'opencode auth login' instead of an env key.)

MSG
fi

# ---------- 6. backup timer ----------

if [[ "$DO_BACKUP" -eq 1 ]] && ask_yn "Install daily-backup systemd timer?"; then
  log "Installing backup script + timer..."
  if [[ ! -f "$REPO_DIR/scripts/backup.sh" ]]; then
    warn "scripts/backup.sh missing — skipping."
  else
    install -m 0755 "$REPO_DIR/scripts/backup.sh" /usr/local/bin/vpn-bot-backup.sh

    cat > /etc/systemd/system/vpn-bot-backup.service <<UNIT
[Unit]
Description=vpn-bot daily backup (code + db + volumes)
[Service]
Type=oneshot
ExecStart=/usr/local/bin/vpn-bot-backup.sh
UNIT

    cat > /etc/systemd/system/vpn-bot-backup.timer <<TIMER
[Unit]
Description=Run vpn-bot backup every day at 03:30
[Timer]
OnCalendar=*-*-* 03:30:00
Persistent=true
[Install]
WantedBy=timers.target
TIMER

    systemctl daemon-reload
    systemctl enable --now vpn-bot-backup.timer
    log "Backup timer enabled."
  fi
fi

# ---------- 7. deploy compose ----------

if [[ "$DO_DEPLOY" -eq 1 ]] && ask_yn "Bring up docker compose now?"; then
  log "docker compose up -d --build ..."
  (cd "$APP_DIR" && docker compose up -d --build) 2>&1 | tail -20
fi

# ---------- summary ----------

log "Done."
cat <<SUMMARY

Next steps:
  1. Edit $APP_DIR/.env — verify BOT_TOKEN, SUPER_ADMIN_ID, REALITY_PUBLIC_KEY,
     ENTRY_NODE_IP, SID_VALUE, SNI_VALUE, FORUM_GROUP_ID.
  2. Restart the bot if you changed env after deploy:
        cd $APP_DIR && docker compose up -d --force-recreate vpn-bot
  3. If you enabled kimi, finish the /login dance described above and set
     TOPIC_AI=<thread_id of the AI topic> in $APP_DIR/.env.
  4. On first start the bot will try to create any missing forum topics
     it needs (Requests, Statistics, Payments, Solved Issues, Support, AI)
     — it must be a group admin with "Manage Topics" for that to work.

Useful commands:
  docker compose -f $APP_DIR/docker-compose.yml ps
  docker logs -f vpn-bot
  systemctl status kimi-bridge
  systemctl list-timers vpn-bot-backup
  /usr/local/bin/vpn-bot-backup.sh   # run a backup on demand
SUMMARY
