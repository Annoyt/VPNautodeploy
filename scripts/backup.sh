#!/usr/bin/env bash
# vpn-bot daily backup.
#
# Snapshots into /opt/backups/:
#   - /opt/vpn-bot/ as a tarball (excludes venv + __pycache__)
#   - .env and nodes.json copied separately (easy restore for config)
#   - every named Docker volume tagged vpn-bot_*
#   - x-ui certificate directory if present
#
# Keeps the last KEEP=7 backups per kind, prunes older.
#
# Run manually:
#   /usr/local/bin/vpn-bot-backup.sh
# or via the systemd timer installed by scripts/install.sh.

set -euo pipefail

APP_DIR="${APP_DIR:-/opt/vpn-bot}"
BACKUP_DIR="${BACKUP_DIR:-/opt/backups}"
KEEP="${KEEP_BACKUPS:-7}"
TS=$(date +%Y%m%d-%H%M%S)

log() { printf '[backup %s] %s\n' "$(date +%H:%M:%S)" "$*"; }

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

# ---------- 1. /opt/vpn-bot tarball ----------

if [[ -d "$APP_DIR" ]]; then
  out="$BACKUP_DIR/vpn-bot-files-${TS}.tar.gz"
  log "Tarballing $APP_DIR → $out"
  tar --exclude="$APP_DIR/venv" \
      --exclude="*/__pycache__" \
      --exclude="*.pyc" \
      -czf "$out" "$APP_DIR" 2>/dev/null
  log "  $(du -h "$out" | cut -f1)"
fi

# ---------- 2. config copies ----------

for f in .env nodes.json; do
  if [[ -f "$APP_DIR/$f" ]]; then
    cp -a "$APP_DIR/$f" "$BACKUP_DIR/${f}.${TS}"
    log "Copied $f"
  fi
done

# ---------- 3. docker volumes ----------

if command -v docker >/dev/null 2>&1; then
  vols=$(docker volume ls --format '{{.Name}}' 2>/dev/null | grep '^vpn-bot_' || true)
  for v in $vols; do
    out="$BACKUP_DIR/volume-${v}-${TS}.tar.gz"
    log "Dumping volume $v → $(basename "$out")"
    if docker run --rm \
        -v "${v}:/data:ro" \
        -v "$BACKUP_DIR:/backup" \
        alpine \
        sh -c "tar czf /backup/$(basename "$out") -C /data ." >/dev/null 2>&1; then
      log "  $(du -h "$out" 2>/dev/null | cut -f1)"
    else
      log "  failed to back up $v (ignored, continuing)"
    fi
  done
fi

# ---------- 4. x-ui certs ----------

if [[ -d "$APP_DIR/3x-ui/cert" ]]; then
  out="$BACKUP_DIR/3xui-certs-${TS}.tar.gz"
  log "Saving 3x-ui certs → $(basename "$out")"
  tar -czf "$out" -C "$APP_DIR/3x-ui" cert 2>/dev/null
fi

# ---------- 5. prune ----------

prune_kind() {
  local pattern="$1"
  shopt -s nullglob
  local files=( "$BACKUP_DIR"/$pattern )
  shopt -u nullglob
  local count="${#files[@]}"
  if (( count > KEEP )); then
    log "Pruning $((count - KEEP)) old '$pattern' backups"
    ls -1t "$BACKUP_DIR"/$pattern | tail -n +$((KEEP + 1)) | xargs -r rm -f
  fi
}

prune_kind 'vpn-bot-files-*.tar.gz'
prune_kind 'volume-vpn-bot_*-*.tar.gz'
prune_kind '3xui-certs-*.tar.gz'
prune_kind '.env.*'
prune_kind 'nodes.json.*'

log "Done. 10 most recent backups:"
ls -1t "$BACKUP_DIR" | head -10 | sed 's/^/  /'
