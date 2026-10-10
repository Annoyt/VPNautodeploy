#!/usr/bin/env bash
# RU-zone egress on entry — Russian sites from abroad for paid users
# (bot/services/ru_exit.py).
#
# Runs ON entry as root, after ./scripts/deploy_to_entry.sh synced it:
#   setup_ru_exit.sh           set up, or re-run: Reality keys (once), the
#                              config, the `ru-exit` container,
#                              app_settings.ru_exit and the sync timer
#   setup_ru_exit.sh --sync    what the timer runs every 2 minutes: the
#                              user list from the bot, a new container only
#                              when it changed
#   setup_ru_exit.sh --remove  container, timer and app_settings.ru_exit
#                              gone (the keys stay in /etc/ru-exit)
#
# A standalone sing-box container `ru-exit` with a VLESS-Reality inbound
# on RU_EXIT_PORT that egresses straight from entry's RU address. It does
# NOT touch haproxy, shadow-tls or the 3x-ui container (the 2026-07-19
# lesson: never recreate 3x-ui as a side effect).
#
# Users = every paid user (ru_exit.is_eligible), read from the bot DB by
# `python3 -P bot/services/ru_exit.py users` inside vpn-bot. The config is
# rendered deterministically, so --sync compares it with the live one and
# recreates the container only on a real change (that drops the live
# RU-zone connections for a second or two). Every new config passes
# `sing-box check` before the running container is touched; a container
# that does not come back is rolled back to the previous config. --sync
# never empties a non-empty list: a list that suddenly reads 0 users is
# far likelier a broken read than every payer lapsing at once, so that
# one waits for a hand-run setup. A stopped container is brought back by
# the next --sync; to switch the egress off use --remove.
#
# The egress refuses private addresses (docker bridges, hermes :4097, the
# panel), BitTorrent and outbound SMTP :25 — all of it leaves from the
# address the whole service enters by, and an abuse report against that
# address costs every user, not one.
#
# Knobs (env, read by setup only; remembered in /etc/ru-exit/server.env
# for --sync): RU_EXIT_PORT (8445), RU_EXIT_SNI (www.google.com),
# RU_EXIT_IMAGE (the sing-box image probe-proxy already pins).
set -euo pipefail
# Byte order for sort/comm: the user names are compared as Python sorts them.
export LC_ALL=C

# RU_EXIT_DIR / RU_EXIT_UNIT_DIR / RU_EXIT_SETTLE_S exist for the tests
# (tests/unit/test_setup_ru_exit_sh.py); prod runs the defaults.
DIR="${RU_EXIT_DIR:-/etc/ru-exit}"
UNIT_DIR="${RU_EXIT_UNIT_DIR:-/etc/systemd/system}"
NAME=ru-exit
BOT=vpn-bot
UNIT=ru-exit-sync
SELF=$(readlink -f "$0")

die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
case "${1:-}" in
  '')       mode=setup ;;
  --sync)   mode=sync ;;
  --remove) mode=remove ;;
  *)        die "usage: $0 [--sync | --remove]" ;;
esac

# One run at a time: the timer's --sync and a hand-run setup must not
# interleave their config swaps.
mkdir -p "$DIR" && chmod 700 "$DIR"
exec 9>"$DIR/.lock"
if [ "$mode" = sync ]; then
  flock -n 9 || exit 0   # another run is on it
else
  flock -w 120 9 || die "another run holds $DIR/.lock"
fi

bot_running() {
  docker inspect -f '{{.State.Running}}' "$BOT" 2>/dev/null | grep -q true
}

set_bot_setting() {  # $1 = JSON value ('' clears)
  docker exec -i "$BOT" python3 - "$1" <<'PY'
import sys
from bot.config import Settings
from bot.core.database import Database
ok = Database(Settings().DB_PATH).set_setting('ru_exit', sys.argv[1])
sys.exit(0 if ok else 1)
PY
}

if [ "$mode" = remove ]; then
  bot_running || die "container $BOT is not running"
  # The bot stops offering the RU-zone first, then the server goes.
  set_bot_setting '' || die "could not clear app_settings.ru_exit"
  systemctl disable --now "$UNIT.timer" >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/$UNIT.service" "$UNIT_DIR/$UNIT.timer"
  systemctl daemon-reload
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "ru-exit removed: container, sync timer, app_settings.ru_exit (keys kept in $DIR)"
  exit 0
fi

if [ "$mode" = setup ]; then
  bot_running || die "container $BOT is not running"
  PORT="${RU_EXIT_PORT:-8445}"
  SNI="${RU_EXIT_SNI:-www.google.com}"
  IMAGE="${RU_EXIT_IMAGE:-ghcr.io/sagernet/sing-box:v1.11.15}"
  case "$PORT" in ''|*[!0-9]*) die "RU_EXIT_PORT must be a number" ;; esac
  case "$SNI$IMAGE" in *[!A-Za-z0-9.:/_@-]*) die "odd characters in RU_EXIT_SNI / RU_EXIT_IMAGE" ;; esac
  if ss -ltnH "( sport = :$PORT )" | grep -q . \
     && ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    die "port $PORT is already taken by something other than $NAME"
  fi

  # Reality dest must pass the cert-record size check (AGENTS.md §23:
  # xray/reality refuses a Certificate record over 8192 bytes).
  hex=$(timeout 10 openssl s_client -connect "$SNI:443" -servername "$SNI" \
          -tls1_3 -msg </dev/null 2>/dev/null \
        | grep -m1 -oE 'Handshake \[length [0-9a-f]+\], Certificate$' \
        | grep -oE '[0-9a-f]+\]' | tr -d ']') || true
  [ -n "$hex" ] || die "no TLS 1.3 Certificate from $SNI:443 — pick another RU_EXIT_SNI"
  size=$((16#$hex))
  [ "$size" -le 8000 ] || die "$SNI certificate record is $size bytes (> 8000)"
  echo "dest $SNI: certificate record $size bytes — ok"

  if [ ! -s "$DIR/keys.env" ]; then
    kp=$(docker run --rm "$IMAGE" generate reality-keypair)
    priv=$(echo "$kp" | awk '/PrivateKey/ {print $2}')
    pub=$(echo "$kp" | awk '/PublicKey/ {print $2}')
    [ -n "$priv" ] && [ -n "$pub" ] || die "keypair generation failed"
    (umask 077 && printf 'PRIVATE_KEY=%s\nPUBLIC_KEY=%s\nSHORT_ID=%s\n' \
      "$priv" "$pub" "$(openssl rand -hex 8)" > "$DIR/keys.env")
    echo "generated a new Reality keypair in $DIR/keys.env"
  fi
  (umask 077 && printf 'PORT=%s\nSNI=%s\nIMAGE=%s\n' "$PORT" "$SNI" "$IMAGE" \
    > "$DIR/server.env")
else
  [ -s "$DIR/keys.env" ] && [ -s "$DIR/server.env" ] && [ -s "$DIR/config.json" ] \
    || die "ru-exit is not set up — run $SELF first"
  # shellcheck disable=SC1091
  . "$DIR/server.env"
  # A bot being redeployed is not a failure: the next run catches up.
  bot_running || { echo "$BOT is not running — sync skipped"; exit 0; }
fi
# shellcheck disable=SC1091
. "$DIR/keys.env"

users_json=$(docker exec -w /app "$BOT" python3 -P bot/services/ru_exit.py users) || {
  if [ "$mode" = sync ] && ! bot_running; then
    echo "$BOT went away mid-read — sync skipped"; exit 0
  fi
  die "could not read the user list from $BOT"
}

render() {  # $1 = output file; the config from the variables above
  # The private key and the uuids go through the environment, not argv:
  # /proc/<pid>/cmdline is readable by every local user, environ is not.
  (umask 077 && RU_EXIT_PRIV="$PRIVATE_KEY" RU_EXIT_USERS="$users_json" \
    python3 - "$1" "$PORT" "$SNI" "$SHORT_ID" <<'PY'
import json, os, sys
path, port, sni, sid = sys.argv[1:]
priv = os.environ['RU_EXIT_PRIV']
users = json.loads(os.environ['RU_EXIT_USERS'])
if not (isinstance(users, list)
        and all(isinstance(u, dict) and u.get('name') and u.get('uuid') for u in users)):
    sys.exit('the user list is not a list of {name, uuid}')
cfg = {
    'log': {'level': 'warn', 'timestamp': True},
    # Resolve on entry (RU resolver → RU CDN nodes), IPv4 only: the
    # docker bridge has no v6 route.
    'dns': {'servers': [{'tag': 'local', 'address': 'local'}],
            'strategy': 'ipv4_only'},
    'inbounds': [{
        'type': 'vless', 'tag': 'ru-zone-in',
        'listen': '0.0.0.0', 'listen_port': int(port),
        'users': users,
        'tls': {
            'enabled': True, 'server_name': sni,
            'reality': {
                'enabled': True,
                'handshake': {'server': sni, 'server_port': 443},
                'private_key': priv, 'short_id': [sid],
            },
        },
    }],
    'outbounds': [{'type': 'direct', 'tag': 'direct'}],
    'route': {
        'rules': [
            # Sniff so the protocol rule below can see BitTorrent; :25 is
            # spam's port (mail clients submit on 465/587). Both leave
            # from the address everyone enters by.
            {'action': 'sniff'},
            {'protocol': 'bittorrent', 'action': 'reject'},
            {'port': 25, 'action': 'reject'},
            # Resolve first so a name pointing at 127/8, 10/8, 172.16/12
            # (docker bridges, hermes :4097, the panel) is caught too.
            {'action': 'resolve', 'strategy': 'ipv4_only'},
            {'ip_is_private': True, 'action': 'reject'},
        ],
        'final': 'direct',
    },
}
with open(path, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
PY
  )
}

user_names() {  # $1 = config file → its user names, one per line, sorted
  python3 -c 'import json, sys
c = json.load(open(sys.argv[1]))
print("\n".join(sorted(u["name"] for u in c["inbounds"][0]["users"])))' "$1"
}

container_up() {  # running, and the port answers
  sleep "${RU_EXIT_SETTLE_S:-3}"
  docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -q true \
    && ss -ltnH "( sport = :$PORT )" | grep -q .
}

start_container() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped --memory 128m \
    -p "$PORT:$PORT/tcp" -v "$DIR:/etc/sing-box:ro" \
    "$IMAGE" run -c /etc/sing-box/config.json >/dev/null
}

render "$DIR/config.json.new" || { rm -f "$DIR/config.json.new"; die "could not render the config"; }
new_names=$(user_names "$DIR/config.json.new")
new_count=$(printf '%s' "$new_names" | grep -c . || true)
old_names=''
[ -s "$DIR/config.json" ] && old_names=$(user_names "$DIR/config.json" 2>/dev/null || true)
old_count=$(printf '%s' "$old_names" | grep -c . || true)

if [ "$mode" = sync ]; then
  if cmp -s "$DIR/config.json.new" "$DIR/config.json" \
     && docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -q true; then
    rm -f "$DIR/config.json.new"
    exit 0   # nothing changed — the usual case, and a quiet one
  fi
  if [ "$new_count" -eq 0 ] && [ "$old_count" -gt 0 ]; then
    rm -f "$DIR/config.json.new"
    die "the user list reads 0 users (live: $old_count) — not applied; run $SELF by hand if that is real"
  fi
fi

docker run --rm -v "$DIR:/etc/sing-box:ro" "$IMAGE" \
  check -c /etc/sing-box/config.json.new >/dev/null \
  || { rm -f "$DIR/config.json.new"; die "sing-box check failed — the live config is untouched"; }
[ -s "$DIR/config.json" ] && cp -p "$DIR/config.json" "$DIR/config.json.prev"
mv "$DIR/config.json.new" "$DIR/config.json"
start_container
if ! container_up; then
  docker logs --tail 30 "$NAME" >&2 || true
  if [ -s "$DIR/config.json.prev" ]; then
    cp -p "$DIR/config.json.prev" "$DIR/config.json"
    start_container
    container_up && die "$NAME did not come up with the new config — rolled back to the previous one"
  fi
  die "$NAME is not running"
fi

added=$(comm -13 <(printf '%s\n' "$old_names" | grep . || true) \
                 <(printf '%s\n' "$new_names" | grep . || true) | grep -c . || true)
removed=$(comm -23 <(printf '%s\n' "$old_names" | grep . || true) \
                   <(printf '%s\n' "$new_names" | grep . || true) | grep -c . || true)
echo "ru-exit on :$PORT (sni $SNI): $new_count user(s) (+$added -$removed)"

if [ "$mode" = setup ]; then
  setting=$(python3 -c 'import json, sys; print(json.dumps({
    "port": int(sys.argv[1]), "sni": sys.argv[2], "pbk": sys.argv[3],
    "sid": sys.argv[4]}))' "$PORT" "$SNI" "$PUBLIC_KEY" "$SHORT_ID")
  set_bot_setting "$setting" || die "could not write app_settings.ru_exit"

  cat > "$UNIT_DIR/$UNIT.service" <<EOF
[Unit]
Description=RU-zone egress: paid users into the ru-exit container
After=docker.service

[Service]
Type=oneshot
ExecStart=$SELF --sync
EOF
  cat > "$UNIT_DIR/$UNIT.timer" <<EOF
[Unit]
Description=RU-zone egress: user sync every 2 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
AccuracySec=15s

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$UNIT.timer" >/dev/null
  echo "app_settings.ru_exit written; $UNIT.timer runs '$SELF --sync' every 2 minutes"
  echo "client profile: the user's /sub link + '?mode=abroad&format=clash' (the bot's 🇷🇺 button)"
fi
