#!/usr/bin/env bash
# SSH emergency exit for paid users (IMPROVEMENT_PLAN F, E4). Run as root on EXIT.
#
#   setup_ssh_exit.sh                 set up or re-run: image, sshd config, host key,
#                                     container, sync script, timer; prints SSH_HOST_KEY
#   setup_ssh_exit.sh --sync          what the timer runs: pull the users, apply them
#   setup_ssh_exit.sh --print-config  the sshd_config it writes (nothing else)
#   setup_ssh_exit.sh --remove        timer and container off (keys stay)
#
# /etc/ssh-exit.env (root-only):
#   BOT_URL=https://<dashboard host>:9443     the bot, reachable from exit
#   DPI_REPORT_TOKEN=<the same token as /etc/exit-dpi.env>
#   SSH_EXIT_PORT=22022                       the port users connect to
#
# The sshd only forwards: no shell, no tty, no agent/X11, group vpn only. The
# users and their password hashes come from GET $BOT_URL/api/ssh/users — SHA-512
# crypt hashes of the passwords the bot derives (bot/services/ssh_exit.py); no
# password or uuid ever reaches this host. Then, on entry, in .env:
#   SSH_EXIT_HOST=<exit ip>  SSH_EXIT_PORT=22022  SSH_HOST_KEY=<printed line>
#   SSH_USER_SECRET=<random 32+ chars, only in entry's .env>
# and recreate vpn-bot. Open the port in exit's firewall (ufw allow 22022/tcp).
set -euo pipefail

CONF_DIR=/etc/ssh-exit
ENV_FILE=/etc/ssh-exit.env
IMAGE=ssh-exit:local
NAME=ssh-exit
SYNC_BIN=/usr/local/sbin/ssh-exit-sync

sshd_config() {
  cat <<'CFG'
Port 22
HostKey /etc/ssh/keys/ssh_host_ed25519_key
PasswordAuthentication yes
KbdInteractiveAuthentication no
PubkeyAuthentication no
PermitRootLogin no
PermitEmptyPasswords no
AllowGroups vpn
AllowTcpForwarding local
GatewayPorts no
PermitTunnel no
X11Forwarding no
AllowAgentForwarding no
AllowStreamLocalForwarding no
PermitTTY no
ForceCommand /bin/false
MaxSessions 16
MaxStartups 30:50:100
LoginGraceTime 20
ClientAliveInterval 30
ClientAliveCountMax 4
UseDNS no
LogLevel ERROR
CFG
}

load_env() {
  [ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE (BOT_URL, DPI_REPORT_TOKEN, SSH_EXIT_PORT)" >&2; exit 2; }
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  : "${BOT_URL:?BOT_URL not set}" "${DPI_REPORT_TOKEN:?DPI_REPORT_TOKEN not set}"
  SSH_EXIT_PORT="${SSH_EXIT_PORT:-22022}"
}

sync_users() {
  load_env
  docker inspect "$NAME" >/dev/null 2>&1 || { echo "container $NAME not running — skip"; exit 0; }
  local json
  json="$(curl -fsS --max-time 20 -H "X-DPI-Token: ${DPI_REPORT_TOKEN}" "${BOT_URL%/}/api/ssh/users")" \
    || { echo "bot unreachable — users unchanged"; exit 0; }
  # The plan as shell lines: "user<TAB>hash", validated (names n + 12 hex, hashes $6$).
  local plan
  plan="$(printf '%s' "$json" | python3 -c '
import json, re, sys
d = json.load(sys.stdin)
if not d.get("ok"):
    sys.exit(3)
for u in d.get("users") or []:
    name, h = str(u.get("user", "")), str(u.get("hash", ""))
    if re.fullmatch(r"n[0-9a-f]{12}", name) and re.fullmatch(r"\$6\$[./A-Za-z0-9]{1,16}\$[./A-Za-z0-9]{86}", h):
        print(name + "\t" + h)
')" || { echo "bot answered no list — users unchanged"; exit 0; }
  local have
  have="$(docker exec "$NAME" sh -c "grep -cE '^n[0-9a-f]{12}:' /etc/passwd" || true)"
  if [ -z "$plan" ] && [ "${have:-0}" -gt 0 ]; then
    # A broken read is likelier than every payer lapsing at once.
    echo "the bot lists 0 users while $have exist — not applied (re-run by hand to force)"
    [ "${FORCE:-0}" = 1 ] || exit 0
  fi
  printf '%s\n' "$plan" | docker exec -i "$NAME" sh -c '
    set -e
    wanted=""
    while IFS="$(printf "\t")" read -r name hash; do
      [ -n "$name" ] || continue
      wanted="$wanted $name"
      id "$name" >/dev/null 2>&1 || adduser -D -H -s /bin/false -G vpn "$name"
      printf "%s:%s\n" "$name" "$hash" | chpasswd -e
    done
    for name in $(grep -oE "^n[0-9a-f]{12}" /etc/passwd); do
      case " $wanted " in *" $name "*) ;; *) deluser "$name" >/dev/null 2>&1 || true ;; esac
    done'
  echo "ssh-exit: users synced ($(printf '%s\n' "$plan" | grep -c . || true))"
}

setup() {
  load_env
  install -d -m 700 "$CONF_DIR" "$CONF_DIR/keys"
  sshd_config > "$CONF_DIR/sshd_config"
  docker build -q -t "$IMAGE" - <<'DOCKERFILE' >/dev/null
FROM alpine:3.20
RUN apk add --no-cache openssh-server shadow && addgroup vpn
CMD ["/usr/sbin/sshd", "-D", "-e", "-f", "/etc/ssh/sshd_config"]
DOCKERFILE
  if [ ! -s "$CONF_DIR/keys/ssh_host_ed25519_key" ]; then
    docker run --rm -v "$CONF_DIR/keys:/k" "$IMAGE" ssh-keygen -q -t ed25519 -N '' -f /k/ssh_host_ed25519_key
  fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped \
    -p "${SSH_EXIT_PORT}:22" \
    -v "$CONF_DIR/sshd_config:/etc/ssh/sshd_config:ro" \
    -v "$CONF_DIR/keys:/etc/ssh/keys:ro" \
    --memory 128m --pids-limit 256 "$IMAGE" >/dev/null
  install -m 755 -o root -g root "$0" "$SYNC_BIN"
  cat > /etc/systemd/system/ssh-exit-sync.service <<UNIT
[Unit]
Description=SSH emergency exit: sync users from the bot (IMPROVEMENT_PLAN F, E4)
After=docker.service network-online.target
[Service]
Type=oneshot
ExecStart=$SYNC_BIN --sync
UNIT
  cat > /etc/systemd/system/ssh-exit-sync.timer <<'UNIT'
[Unit]
Description=Sync the SSH emergency exit users every 2 minutes
[Timer]
OnBootSec=1min
OnUnitActiveSec=2min
[Install]
WantedBy=timers.target
UNIT
  systemctl daemon-reload
  systemctl enable --now ssh-exit-sync.timer >/dev/null
  "$SYNC_BIN" --sync || true
  echo "SSH_HOST_KEY=$(cut -d' ' -f1,2 "$CONF_DIR/keys/ssh_host_ed25519_key.pub")"
  echo "open the port: ufw allow ${SSH_EXIT_PORT}/tcp"
}

remove() {
  systemctl disable --now ssh-exit-sync.timer >/dev/null 2>&1 || true
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "ssh-exit stopped (keys kept in $CONF_DIR/keys)"
}

case "${1:-}" in
  --sync) sync_users ;;
  --print-config) sshd_config ;;
  --remove) remove ;;
  "") setup ;;
  *) echo "usage: $0 [--sync|--print-config|--remove]" >&2; exit 2 ;;
esac
