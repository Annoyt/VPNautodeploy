#!/usr/bin/env bash
# The SSH emergency path through entry (AGENTS.md §51, E4): a TCP relay on
# entry in front of ssh-exit on exit. Run as root on ENTRY.
#
#   setup_ssh_entry_relay.sh                 set up or re-run: config, `ssh-relay` container
#   setup_ssh_entry_relay.sh --print-config  the haproxy.cfg it writes (nothing else)
#   setup_ssh_entry_relay.sh --remove        container off
#
# Why: ssh-exit is reached at exit's foreign address. Through this relay the
# client speaks the same SSH to entry's Russian address instead: no TLS
# name, no UDP, no foreign hosting on the client's side. The session still
# ends in ssh-exit on exit, so the users, passwords and host key are the
# same: the relay copies bytes, and the client pins exit's host key through
# it. The bot puts this server LAST in the emergency profiles, after
# ssh-exit itself (bot/services/ssh_exit.py, entry_outbound). It does not
# help under a mobile white list: that list is by address, and entry's
# address is not on it.
#
# What it reads: SSH_EXIT_HOST, SSH_EXIT_PORT and SSH_ENTRY_PORT (and
# ENTRY_NODE_IP, for the line it prints for exit) from the bot's own
# /opt/vpn-bot/.env, read as data, those keys only, never sourced (the file
# also holds the bot token). One source: the relay
# forwards to the exit the bot hands out, on the port the bot hands out.
# Set SSH_ENTRY_PORT there first; the bot hands the path out once vpn-bot
# is recreated with it.
#
# The container: haproxy, pinned by digest, on the host network. A front
# like haproxy and shadow-tls: it listens on entry's own address, under the
# host's firewall (ufw), with no docker NAT in the path. A port docker
# publishes would bypass ufw. The probe sidecar reaches it through entry's
# public address as users do.
# Read-only, no capabilities, no new privileges, the image's own user,
# 128 MB; no `log` lines, so it records no user's address. It does not
# touch the Reality haproxy, shadow-tls, iptables or ufw. Every new config
# passes `haproxy -c` before the running container is touched; a container
# that does not come back is rolled back to the previous config.
#
# On exit, every user of this path arrives from entry's address. sshd from
# OpenSSH 9.8 on refuses an address after a few connections that never
# authenticate (PerSourcePenalties), so one scanner on this port would lock
# out everyone behind the relay. setup_ssh_exit.sh exempts SSH_RELAY_IPS
# (entry's address, in /etc/ssh-exit.env). Today's ssh-exit image runs
# OpenSSH 9.7, which has no such option yet.
set -euo pipefail

# SSH_RELAY_DIR / _ENV / _SETTLE_S exist for the tests
# (tests/unit/test_setup_ssh_entry_relay_sh.py); prod runs the defaults.
DIR="${SSH_RELAY_DIR:-/etc/ssh-relay}"
BOT_ENV="${SSH_RELAY_ENV:-/opt/vpn-bot/.env}"
NAME=ssh-relay
# haproxy 2.8 LTS (2.8.31), the image the xhr-exit stand pins.
IMAGE='haproxy:2.8-alpine@sha256:fce63f5dde742cd1f2b883ab39f7d4f9f88788827dd54fcfa864f63b9eec3bc8'
CFG=/usr/local/etc/haproxy/haproxy.cfg

die() { echo "ERROR: $*" >&2; exit 1; }

# KEY=VALUE files are read as data, never sourced: whatever a value holds
# (`;`, `$(…)`, a backquote) stays text and never runs as root. The same
# reader as setup_ssh_exit.sh and setup_xhr_exit.sh.
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

port_in() {  # $1 = value, $2 = lowest allowed
  [[ $1 =~ ^[0-9]{1,5}$ ]] && [ "$((10#$1))" -ge "$2" ] && [ "$((10#$1))" -le 65535 ]
}

load_env() {  # the bot's .env is the only source of these
  [ -r "$BOT_ENV" ] || die "missing $BOT_ENV"
  unset SSH_EXIT_HOST SSH_EXIT_PORT SSH_ENTRY_PORT ENTRY_NODE_IP
  read_env "$BOT_ENV" SSH_EXIT_HOST SSH_EXIT_PORT SSH_ENTRY_PORT ENTRY_NODE_IP
  SSH_EXIT_HOST="${SSH_EXIT_HOST:-}"
  SSH_EXIT_PORT="${SSH_EXIT_PORT:-22022}"
  SSH_ENTRY_PORT="${SSH_ENTRY_PORT:-}"
  [ -n "$SSH_ENTRY_PORT" ] \
    || die "SSH_ENTRY_PORT is not set in $BOT_ENV (the port users reach the relay on, e.g. 22022)"
  # The image's own user binds it: no capability, so nothing below 1024.
  port_in "$SSH_ENTRY_PORT" 1024 || die "SSH_ENTRY_PORT must be a port number 1024-65535"
  port_in "$SSH_EXIT_PORT" 1 || die "SSH_EXIT_PORT must be a port number 1-65535"
  [ -n "$SSH_EXIT_HOST" ] || die "SSH_EXIT_HOST is not set in $BOT_ENV"
  # An IPv4 address or a host name: it goes into haproxy's config as is.
  [[ $SSH_EXIT_HOST =~ ^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$ ]] \
    || die "SSH_EXIT_HOST must be an IPv4 address or a host name"
  SSH_ENTRY_PORT=$((10#$SSH_ENTRY_PORT))
  SSH_EXIT_PORT=$((10#$SSH_EXIT_PORT))
}

render() {
  cat <<CFG
# ssh-relay: entry :${SSH_ENTRY_PORT} -> ssh-exit at ${SSH_EXIT_HOST}:${SSH_EXIT_PORT}
# (AGENTS.md §51, the SSH path through entry). Written by
# scripts/setup_ssh_entry_relay.sh: edits here are overwritten.
global
    maxconn 1000

defaults
    mode tcp
    timeout connect 10s
    # sshd on exit sends a keepalive every 30 s (ClientAliveInterval), so
    # a live session is never idle this long.
    timeout client 1h
    timeout server 1h
    timeout tunnel 1h
    timeout client-fin 30s
    timeout server-fin 30s

frontend ssh_entry
    bind 0.0.0.0:${SSH_ENTRY_PORT}
    default_backend ssh_exit

backend ssh_exit
    server exit ${SSH_EXIT_HOST}:${SSH_EXIT_PORT}
CFG
}

relay_running() {
  [ "$(docker inspect -f '{{.State.Running}} {{.State.Restarting}}' "$NAME" 2>/dev/null)" = "true false" ]
}

listening() {
  ss -ltnH "( sport = :$SSH_ENTRY_PORT )" 2>/dev/null | grep -q .
}

container_up() {  # running, not in a restart loop, and the port answers
  sleep "${SSH_RELAY_SETTLE_S:-3}"
  relay_running && listening
}

start_container() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped --network host \
    --read-only --cap-drop ALL --security-opt no-new-privileges \
    --memory 128m --pids-limit 64 \
    --log-opt max-size=1m --log-opt max-file=2 \
    -v "$DIR/haproxy.cfg:$CFG:ro" "$IMAGE" >/dev/null
}

check_config() {  # $1 = the file to check
  docker run --rm --network host -v "$1:$CFG:ro" "$IMAGE" haproxy -c -q -f "$CFG"
}

banner() {  # the first line ssh-exit sends, read through the relay
  timeout 8 bash -c 'exec 3<>"/dev/tcp/127.0.0.1/$1"; IFS= read -r line <&3; printf "%s" "$line"' \
    _ "$SSH_ENTRY_PORT" 2>/dev/null || true
}

ufw_hint() {
  command -v ufw >/dev/null 2>&1 || return 0
  local status
  status=$(ufw status 2>/dev/null) || return 0
  case "$status" in *"Status: active"*) ;; *) return 0 ;; esac
  if ! printf '%s\n' "$status" | grep -qE "^${SSH_ENTRY_PORT}(/tcp)?[[:space:]].*ALLOW"; then
    echo "ufw is active and does not allow ${SSH_ENTRY_PORT}/tcp: users cannot reach the relay until"
    echo "  ufw allow ${SSH_ENTRY_PORT}/tcp"
  fi
}

case "${1:-}" in
  --print-config) load_env; render; exit 0 ;;
  --remove) mode=remove ;;
  '') mode=setup ;;
  *) echo "usage: $0 [--print-config|--remove]" >&2; exit 2 ;;
esac

[ "$(id -u)" = 0 ] || die "run as root"

if [ "$mode" = remove ]; then
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "ssh-relay removed"
  if [ -r "$BOT_ENV" ]; then
    unset SSH_ENTRY_PORT
    read_env "$BOT_ENV" SSH_ENTRY_PORT
    if [ -n "${SSH_ENTRY_PORT:-}" ]; then
      echo "SSH_ENTRY_PORT is still set in $BOT_ENV: the bot hands the path out until it is"
      echo "removed there and vpn-bot is recreated"
    fi
  fi
  exit 0
fi

load_env
mkdir -p "$DIR" && chmod 700 "$DIR"
if listening && ! relay_running; then
  die "port $SSH_ENTRY_PORT is already taken by something other than $NAME"
fi

render > "$DIR/haproxy.cfg.new"
# haproxy reads it as the image's own user, whatever root's umask; it
# holds no secret (exit's address and two ports).
chmod 644 "$DIR/haproxy.cfg.new"
if cmp -s "$DIR/haproxy.cfg.new" "$DIR/haproxy.cfg" 2>/dev/null && relay_running; then
  rm -f "$DIR/haproxy.cfg.new"
  echo "ssh-relay unchanged: :$SSH_ENTRY_PORT -> $SSH_EXIT_HOST:$SSH_EXIT_PORT"
else
  check_config "$DIR/haproxy.cfg.new" \
    || { rm -f "$DIR/haproxy.cfg.new"; die "haproxy -c failed: the live config is untouched"; }
  [ -s "$DIR/haproxy.cfg" ] && cp -p "$DIR/haproxy.cfg" "$DIR/haproxy.cfg.prev"
  mv -f "$DIR/haproxy.cfg.new" "$DIR/haproxy.cfg"
  start_container
  if ! container_up; then
    docker logs --tail 30 "$NAME" >&2 || true
    if [ -s "$DIR/haproxy.cfg.prev" ]; then
      cp -p "$DIR/haproxy.cfg.prev" "$DIR/haproxy.cfg"
      start_container
      container_up && die "$NAME did not come up with the new config: rolled back to the previous one"
    fi
    die "$NAME is not running"
  fi
  echo "ssh-relay on :$SSH_ENTRY_PORT -> $SSH_EXIT_HOST:$SSH_EXIT_PORT"
fi

line=$(banner)
case "$line" in
  SSH-2.0-*) echo "through the relay: ${line%$'\r'}" ;;
  *) echo "WARNING: no SSH banner through the relay: is ssh-exit up on $SSH_EXIT_HOST:$SSH_EXIT_PORT?" >&2 ;;
esac
ufw_hint
relay_ip="${ENTRY_NODE_IP:-}"
[[ $relay_ip =~ ^[0-9.]+$ ]] || relay_ip="<entry's address>"
echo "on exit, in /etc/ssh-exit.env: SSH_RELAY_IPS=$relay_ip (setup_ssh_exit.sh exempts it from sshd's per-address penalties)"
echo "the bot hands the path out once vpn-bot runs with SSH_ENTRY_PORT=$SSH_ENTRY_PORT"
