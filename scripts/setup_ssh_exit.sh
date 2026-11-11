#!/usr/bin/env bash
# SSH emergency exit for paid users (IMPROVEMENT_PLAN F, E4). Run as root on EXIT.
#
#   setup_ssh_exit.sh                 set up or re-run: image, sshd config, host key,
#                                     container, sync script, timer; prints SSH_HOST_KEY
#   setup_ssh_exit.sh --sync          what the timer runs: pull the users, apply them
#   setup_ssh_exit.sh --print-config  the sshd_config it writes (nothing else)
#   setup_ssh_exit.sh --print-entrypoint  the container's entrypoint (egress rules, users)
#   setup_ssh_exit.sh --remove        timer and container off (keys stay)
#
# /etc/ssh-exit.env (root-only), KEY=VALUE lines read as data, never sourced
# (quotes and an `export ` prefix are fine; nothing in a value ever runs):
#   BOT_URL=https://<dashboard host>:9443     the bot, reachable from exit
#   DPI_REPORT_TOKEN=<the same token as /etc/exit-dpi.env>
#   SSH_EXIT_PORT=22022                       the port users connect to
#
# The sshd only forwards: no shell, no tty, no agent/X11, group vpn only, and only
# to the ports clients need (PermitOpen). The container firewalls its own egress
# at start (its netns only, NET_ADMIN): no private, link-local, CGNAT or loopback
# destination (the host, other containers, a cloud metadata service), no SMTP.
# Nothing meters this path, so that is the abuse floor (ru-exit has the same).
# The users and their password hashes come from GET $BOT_URL/api/ssh/users —
# SHA-512 crypt hashes of the passwords the bot derives (bot/services/ssh_exit.py);
# no password or uuid ever reaches this host. A user the bot no longer lists is
# removed and their live sessions killed. The last list applied is kept
# (/etc/ssh-exit/users.plan, root-only) and applied when the container starts,
# so re-running setup does not lock everyone out until the next sync.
# Then, on entry, in .env:
#   SSH_EXIT_HOST=<exit ip>  SSH_EXIT_PORT=22022  SSH_HOST_KEY=<printed line>
#   SSH_USER_SECRET=<random 32+ chars, only in entry's .env>
# and recreate vpn-bot. Open the port in exit's firewall (ufw allow 22022/tcp).
set -euo pipefail

# SSH_EXIT_DIR / _ENV / _UNIT_DIR / _BIN exist for the tests
# (tests/unit/test_setup_ssh_exit_sh.py); prod runs the defaults. The env
# file cannot set them: it is read for its three keys only (load_env).
CONF_DIR="${SSH_EXIT_DIR:-/etc/ssh-exit}"
ENV_FILE="${SSH_EXIT_ENV:-/etc/ssh-exit.env}"
UNIT_DIR="${SSH_EXIT_UNIT_DIR:-/etc/systemd/system}"
IMAGE=ssh-exit:local
NAME=ssh-exit
SYNC_BIN="${SSH_EXIT_BIN:-/usr/local/sbin/ssh-exit-sync}"
PLAN_FILE=$CONF_DIR/users.plan
# alpine 3.20, pinned (the repo pins its images, AGENTS.md §23)
BASE_IMAGE=alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc
# What forwarding may reach: web, DNS over TLS, mail clients (not SMTP 25),
# XMPP/Apple push, the usual alternate web ports.
PERMIT_OPEN='*:80 *:443 *:853 *:993 *:995 *:465 *:587 *:5222 *:5223 *:8080 *:8443'

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
PermitOpen __PERMIT_OPEN__
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

sshd_config_rendered() {
  sshd_config | sed "s/__PERMIT_OPEN__/${PERMIT_OPEN}/"
}

# The container's entrypoint: its own egress rules first (fail closed: no
# rules, no sshd), then the last user list, then sshd.
entrypoint() {
  cat <<'EP'
#!/bin/sh
set -eu
iptables -A OUTPUT -o lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
for net in 0.0.0.0/8 10.0.0.0/8 100.64.0.0/10 127.0.0.0/8 169.254.0.0/16 \
           172.16.0.0/12 192.168.0.0/16 224.0.0.0/4 240.0.0.0/4; do
  iptables -A OUTPUT -d "$net" -j REJECT
done
iptables -A OUTPUT -p tcp --dport 25 -j REJECT
if [ -r /etc/ssh/users.plan ]; then
  while IFS="$(printf '\t')" read -r name hash; do
    [ -n "$name" ] || continue
    id "$name" >/dev/null 2>&1 || adduser -D -H -s /bin/false -G vpn "$name"
    printf '%s:%s\n' "$name" "$hash" | chpasswd -e
  done < /etc/ssh/users.plan
fi
exec /usr/sbin/sshd -D -e -f /etc/ssh/sshd_config
EP
}

# KEY=VALUE files are read as data, never sourced: whatever a value holds
# (`;`, `$(…)`, a backquote) stays text and never runs as root. The same
# reader as setup_xhr_exit.sh.
read_env() {  # $1 = file; then the keys to take from it
  local file=$1 line key value want
  shift
  while IFS= read -r line || [ -n "$line" ]; do
    line=${line%$'\r'}
    case "$line" in
      'export '*=*) line=${line#export } ;;
      [A-Za-z_]*=*) ;;
      *) continue ;;
    esac
    key=${line%%=*}
    value=${line#*=}
    case "$value" in
      \"*\") value=${value#\"}; value=${value%\"} ;;
      \'*\') value=${value#\'}; value=${value%\'} ;;
    esac
    for want in "$@"; do
      if [ "$key" = "$want" ]; then printf -v "$want" '%s' "$value"; fi
    done
  done < "$file"
}

ENV_KEYS='BOT_URL DPI_REPORT_TOKEN SSH_EXIT_PORT'

load_env() {  # the env file is the only source of these
  [ -r "$ENV_FILE" ] || { echo "missing $ENV_FILE (BOT_URL, DPI_REPORT_TOKEN, SSH_EXIT_PORT)" >&2; exit 2; }
  # shellcheck disable=SC2086
  unset $ENV_KEYS
  # shellcheck disable=SC2086
  read_env "$ENV_FILE" $ENV_KEYS
  : "${BOT_URL:?BOT_URL not set}" "${DPI_REPORT_TOKEN:?DPI_REPORT_TOKEN not set}"
  SSH_EXIT_PORT="${SSH_EXIT_PORT:-22022}"
}

sync_users() {
  load_env
  [ "$(docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null)" = "true" ] \
    || { echo "container $NAME not running — skip"; exit 0; }
  local json
  # The token goes through stdin, not argv: /proc/<pid>/cmdline is
  # readable by every local user (printf is a builtin, so not even there).
  json="$(printf 'X-DPI-Token: %s\n' "$DPI_REPORT_TOKEN" \
          | curl -fsS --max-time 20 -H @- "${BOT_URL%/}/api/ssh/users")" \
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
    echo "the bot lists 0 users while $have exist — not applied (FORCE=1 $0 --sync to force)"
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
      case " $wanted " in *" $name "*) ;; *)
        # a revoked key must not keep its tunnel: one ssh connection
        # carries every stream, and deluser does not end it
        pkill -KILL -u "$name" >/dev/null 2>&1 || true
        deluser "$name" >/dev/null 2>&1 || true ;;
      esac
    done'
  umask 077
  printf '%s\n' "$plan" > "$PLAN_FILE.tmp" && mv -f "$PLAN_FILE.tmp" "$PLAN_FILE"
  echo "ssh-exit: users synced ($(printf '%s\n' "$plan" | grep -c . || true))"
}

setup() {
  load_env
  install -d -m 700 "$CONF_DIR" "$CONF_DIR/keys"
  sshd_config_rendered > "$CONF_DIR/sshd_config"
  entrypoint > "$CONF_DIR/entrypoint.sh"
  chmod 755 "$CONF_DIR/entrypoint.sh"
  [ -e "$PLAN_FILE" ] || install -m 600 /dev/null "$PLAN_FILE"
  docker build -q -t "$IMAGE" - <<DOCKERFILE >/dev/null
FROM ${BASE_IMAGE}
RUN apk add --no-cache openssh-server shadow iptables procps && addgroup vpn
CMD ["/entrypoint.sh"]
DOCKERFILE
  if [ ! -s "$CONF_DIR/keys/ssh_host_ed25519_key" ]; then
    docker run --rm -v "$CONF_DIR/keys:/k" "$IMAGE" ssh-keygen -q -t ed25519 -N '' -f /k/ssh_host_ed25519_key
  fi
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped \
    -p "${SSH_EXIT_PORT}:22" \
    --cap-add NET_ADMIN --dns 1.1.1.1 --dns 8.8.8.8 \
    --sysctl net.ipv6.conf.all.disable_ipv6=1 \
    -v "$CONF_DIR/sshd_config:/etc/ssh/sshd_config:ro" \
    -v "$CONF_DIR/keys:/etc/ssh/keys:ro" \
    -v "$CONF_DIR/entrypoint.sh:/entrypoint.sh:ro" \
    -v "$PLAN_FILE:/etc/ssh/users.plan:ro" \
    --memory 128m --pids-limit 256 "$IMAGE" >/dev/null
  # The timer runs a root-owned copy; running setup FROM that copy is fine.
  if [ "$(readlink -f "$0")" != "$SYNC_BIN" ]; then
    install -m 755 -o root -g root "$0" "$SYNC_BIN"
  fi
  cat > "$UNIT_DIR/ssh-exit-sync.service" <<UNIT
[Unit]
Description=SSH emergency exit: sync users from the bot (IMPROVEMENT_PLAN F, E4)
After=docker.service network-online.target
[Service]
Type=oneshot
ExecStart=$SYNC_BIN --sync
UNIT
  cat > "$UNIT_DIR/ssh-exit-sync.timer" <<'UNIT'
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
  --print-config) sshd_config_rendered ;;
  --print-entrypoint) entrypoint ;;
  --remove) remove ;;
  "") setup ;;
  *) echo "usage: $0 [--sync|--print-config|--print-entrypoint|--remove]" >&2; exit 2 ;;
esac
