#!/usr/bin/env bash
# VLESS + XHTTP + Reality (`xhr`) on an xray of its own on EXIT
# (IMPROVEMENT_PLAN F, E3; bot/services/xhr_exit.py). Run as root on exit.
#
#   setup_xhr_exit.sh           set up, or re-run: dest and image checks,
#                               Reality keys (once), the config, the
#                               `xhr-exit` container, the sync timer; prints
#                               the haproxy lines and .env values entry needs
#                               (re-run it after this script changes: the
#                               timer runs a root-owned copy, see below)
#   setup_xhr_exit.sh --sync    what the timer runs every 2 minutes: the user
#                               list from the bot, a new container only when
#                               it changed
#   setup_xhr_exit.sh --print   the haproxy lines and .env values again
#   setup_xhr_exit.sh --remove  timer and container gone (keys stay)
#
# Why not a 3x-ui inbound: the relational panel gives every VLESS inbound
# of a client that client's Vision flow, and xray rejects a Vision-flow
# account whose client sends no flow with TLS inside. XHTTP carries no
# flow, so a panel inbound would reject every user (AGENTS.md §51). This
# script never touches the 3x-ui container or its compose file.
#
# The server: xray (digest-pinned, below 26.7.11: mihomo does not keep
# Reality compatible past it) in a container of its own, VLESS + XHTTP +
# Reality on XHR_EXIT_PORT, flow-less clients, Reality keys of its own
# (/etc/xhr-exit/keys.env, root-only, generated once). Clients reach it
# through entry's haproxy (the port of ENTRY_NODE_PORT), which routes the
# SNI XHR_SNI here with PROXY protocol v2: the inbound requires the header,
# so a direct connection gets nowhere and xray sees the client's address.
# Its own bridge network, so its loopback is not the host's (the panel,
# 3x-ui's API inbound); the egress refuses private, loopback, link-local
# and CGNAT destinations, BitTorrent and SMTP :25 (everything leaves from
# exit's address, and an abuse report against it costs every user).
#
# Users: GET $BOT_URL/api/xhr/users with the exit report's token, every 2
# minutes. The config is rendered deterministically, so --sync recreates
# the container only on a real change (that drops the live xhr
# connections for a second). Every new config passes `xray run -test`
# before the live one moves, and a container that does not come back is
# rolled back. --sync never empties a non-empty list: a bot that suddenly
# lists 0 users is likelier broken than every payer gone at once, so that
# one waits for a hand-run setup. When the bot hands out other keys than
# this server's (XHR_PUBLIC_KEY / XHR_SID / XHR_SNI / XHR_PATH in entry's
# .env), --sync applies the users and fails loudly with the line to fix.
#
# The timer runs as root, so it runs /usr/local/sbin/xhr-exit-sync, a
# root-owned copy setup installs, never this file: /opt/vpn-bot is an
# rsync target, and a root timer must not execute a file another account
# can change.
#
# /etc/xhr-exit.env (root-only), read by setup; --sync reads only BOT_URL
# and the token from it (the server parameters setup checked are kept in
# /etc/xhr-exit/server.env, so an edit here takes effect via setup):
#   BOT_URL=https://<dashboard host>:9443   the bot, reachable from exit
#   DPI_REPORT_TOKEN=<the same token as /etc/exit-dpi.env>
#   XHR_SNI=<Reality dest of its own: TLS 1.3, h2, certificate record
#           <= 8000 B; not a name entry's haproxy routes elsewhere>
#   XHR_PATH=/xhr               optional
#   XHR_EXIT_PORT=8446          optional, the port haproxy forwards to
#   XHR_EXIT_PUBLIC_IP=<ip>     optional, for the printed haproxy line
#   XHR_EXIT_IMAGE=<image@sha256:...>  optional, digest-pinned
#   XHR_EXIT_MEMORY=128m        optional
#
# Not done: traffic. This xray never touches the panel, so xhr bytes do not
# count against the quota (bot/services/xhr_exit.py).
set -euo pipefail
# Byte order for sort/comm.
export LC_ALL=C

# XHR_EXIT_DIR / _ENV / _UNIT_DIR / _BIN / _SETTLE_S exist for the tests
# (tests/unit/test_setup_xhr_exit_sh.py); prod runs the defaults.
DIR="${XHR_EXIT_DIR:-/etc/xhr-exit}"
ENV_FILE="${XHR_EXIT_ENV:-/etc/xhr-exit.env}"
UNIT_DIR="${XHR_EXIT_UNIT_DIR:-/etc/systemd/system}"
BIN="${XHR_EXIT_BIN:-/usr/local/sbin/xhr-exit-sync}"
NAME=xhr-exit
UNIT=xhr-exit-sync
SELF=$(readlink -f "$0")
# xray 26.6.22 (the exit's own version, plan F step 0), pinned by digest.
DEFAULT_IMAGE=ghcr.io/xtls/xray-core:26.6.22@sha256:7ec1ff2ea903a1e0999ada29557eadec1447588a59fd3afae64b46f9ae4b7953
# The first xray mihomo does not keep Reality compatible with.
XRAY_LIMIT=26.7.11
# The image's own unprivileged user; the config is readable by it alone.
XRAY_UID=65532

die() { echo "ERROR: $*" >&2; exit 1; }

[ "$(id -u)" = 0 ] || die "run as root"
case "${1:-}" in
  '')       mode=setup ;;
  --sync)   mode=sync ;;
  --print)  mode=print ;;
  --remove) mode=remove ;;
  *)        die "usage: $0 [--sync | --print | --remove]" ;;
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

if [ "$mode" = remove ]; then
  systemctl disable --now "$UNIT.timer" >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/$UNIT.service" "$UNIT_DIR/$UNIT.timer" "$BIN"
  systemctl daemon-reload
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  echo "xhr-exit removed: container and sync timer (keys kept in $DIR)"
  echo "the bot hands xhr out while XHR_SNI is set: switch xhr off in the cascade," \
       "and drop the is_xhr_sni route from entry's haproxy"
  exit 0
fi

# KEY=VALUE files are read as data, never sourced: whatever a value holds
# (`;`, `$(…)`, a backquote) stays text and never runs as root.
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

ENV_KEYS='BOT_URL DPI_REPORT_TOKEN XHR_SNI XHR_PATH XHR_EXIT_PORT XHR_EXIT_PUBLIC_IP XHR_EXIT_IMAGE XHR_EXIT_MEMORY'

load_env() {  # the env file is the only source of these
  [ -r "$ENV_FILE" ] || die "missing $ENV_FILE (BOT_URL, DPI_REPORT_TOKEN, XHR_SNI)"
  # shellcheck disable=SC2086
  unset $ENV_KEYS
  # shellcheck disable=SC2086
  read_env "$ENV_FILE" $ENV_KEYS
  [ -n "${BOT_URL:-}" ] || die "BOT_URL not set in $ENV_FILE"
  [ -n "${DPI_REPORT_TOKEN:-}" ] || die "DPI_REPORT_TOKEN not set in $ENV_FILE"
  case "$DPI_REPORT_TOKEN" in *[!A-Za-z0-9._~+/=-]*) die "odd characters in DPI_REPORT_TOKEN" ;; esac
}

version_lt() {  # $1 < $2, dotted versions
  [ "$1" != "$2" ] && [ "$(printf '%s\n%s\n' "$1" "$2" | sort -V | head -n1)" = "$1" ]
}

check_dest() {  # the Reality dest, measured from here: exit's xray dials it
  local out hex size
  out=$(timeout 10 openssl s_client -connect "$SNI:443" -servername "$SNI" \
          -tls1_3 -alpn h2 -msg </dev/null 2>/dev/null) || true
  hex=$(printf '%s\n' "$out" \
        | grep -m1 -oE 'Handshake \[length [0-9a-f]+\], Certificate$' \
        | grep -oE '[0-9a-f]+\]' | tr -d ']') || true
  [ -n "$hex" ] || die "no TLS 1.3 Certificate from $SNI:443 — pick another XHR_SNI"
  size=$((16#$hex))
  # xray/reality refuses a Certificate record over 8192 bytes (AGENTS.md §23)
  [ "$size" -le 8000 ] || die "$SNI certificate record is $size bytes (> 8000) — pick another XHR_SNI"
  printf '%s\n' "$out" | grep -q '^ALPN protocol: h2$' \
    || die "$SNI:443 does not negotiate h2 — pick another XHR_SNI"
  echo "dest $SNI: TLS 1.3, h2, certificate record $size bytes — ok"
}

check_params() {  # the server parameters, from the env file or server.env
  case "$PORT" in ''|*[!0-9]*) die "XHR_EXIT_PORT must be a number" ;; esac
  [ "$PORT" -ge 1 ] && [ "$PORT" -le 65535 ] || die "XHR_EXIT_PORT out of range"
  case "$SNI" in *.*) ;; *) die "XHR_SNI is not a host name" ;; esac
  case "$SNI" in *[!A-Za-z0-9.-]*) die "odd characters in XHR_SNI" ;; esac
  case "$XPATH" in /*) ;; *) die "XHR_PATH must start with /" ;; esac
  case "$XPATH" in *[!A-Za-z0-9._~/-]*) die "odd characters in XHR_PATH" ;; esac
  case "$MEMORY" in *[!0-9kmg]*|[!0-9]*) die "XHR_EXIT_MEMORY like 128m" ;; esac
  case "$IMAGE" in *[!A-Za-z0-9.:/_@-]*) die "odd characters in XHR_EXIT_IMAGE" ;; esac
  printf '%s' "$IMAGE" | grep -qE '@sha256:[0-9a-f]{64}$' \
    || die "XHR_EXIT_IMAGE must be pinned by digest (…@sha256:<64 hex>)"
}

check_image() {
  local ver
  ver=$(docker run --rm --user "$XRAY_UID:$XRAY_UID" "$IMAGE" version 2>/dev/null \
        | sed -n '1s/^Xray \([0-9][0-9.]*\).*/\1/p') || true
  [ -n "$ver" ] || die "could not read the xray version of $IMAGE"
  version_lt "$ver" "$XRAY_LIMIT" \
    || die "xray $ver in $IMAGE: mihomo does not keep Reality compatible with xray $XRAY_LIMIT and later"
  echo "image: xray $ver — ok"
}

if [ "$mode" = setup ]; then
  load_env
  PORT="${XHR_EXIT_PORT:-8446}"
  SNI="${XHR_SNI:-}"
  XPATH="${XHR_PATH:-/xhr}"
  IMAGE="${XHR_EXIT_IMAGE:-$DEFAULT_IMAGE}"
  MEMORY="${XHR_EXIT_MEMORY:-128m}"
  [ -n "$SNI" ] || die "XHR_SNI not set in $ENV_FILE (a Reality dest of its own)"
  check_params
  if ss -ltnH "( sport = :$PORT )" | grep -q . \
     && ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    die "port $PORT is already taken by something other than $NAME"
  fi
  check_dest
  check_image
  if [ ! -s "$DIR/keys.env" ]; then
    kp=$(docker run --rm --user "$XRAY_UID:$XRAY_UID" "$IMAGE" x25519) || die "key generation failed"
    priv=$(printf '%s\n' "$kp" | awk -F': *' '/^Private ?[Kk]ey/ {print $2; exit}')
    pub=$(printf '%s\n' "$kp" | awk -F': *' '/^(Password|Public ?[Kk]ey)/ {print $2; exit}')
    for k in "$priv" "$pub"; do
      printf '%s' "$k" | grep -qE '^[A-Za-z0-9_-]{43}$' || die "keypair generation failed"
    done
    (umask 077 && printf 'PRIVATE_KEY=%s\nPUBLIC_KEY=%s\nSHORT_ID=%s\n' \
      "$priv" "$pub" "$(openssl rand -hex 8)" > "$DIR/keys.env")
    echo "generated a new Reality keypair in $DIR/keys.env"
  fi
  (umask 077 && printf 'PORT=%s\nSNI=%s\nXPATH=%s\nIMAGE=%s\nMEMORY=%s\n' \
    "$PORT" "$SNI" "$XPATH" "$IMAGE" "$MEMORY" > "$DIR/server.env")
else
  [ -s "$DIR/keys.env" ] && [ -s "$DIR/server.env" ] \
    || die "xhr-exit is not set up — run $SELF first"
  if [ "$mode" = sync ]; then
    [ -s "$DIR/config.json" ] || die "xhr-exit is not set up — run $SELF first"
    load_env
  elif [ -r "$ENV_FILE" ]; then
    unset XHR_EXIT_PUBLIC_IP
    read_env "$ENV_FILE" XHR_EXIT_PUBLIC_IP   # for the printed haproxy line
  fi
  # What setup checked wins over later edits of the env file.
  PORT='' SNI='' XPATH='' IMAGE='' MEMORY=''
  read_env "$DIR/server.env" PORT SNI XPATH IMAGE MEMORY
  check_params
fi
PRIVATE_KEY='' PUBLIC_KEY='' SHORT_ID=''
read_env "$DIR/keys.env" PRIVATE_KEY PUBLIC_KEY SHORT_ID
for k in "$PRIVATE_KEY" "$PUBLIC_KEY"; do
  printf '%s' "$k" | grep -qE '^[A-Za-z0-9_-]{43}$' || die "$DIR/keys.env is damaged"
done
printf '%s' "$SHORT_ID" | grep -qE '^[0-9a-f]{1,16}$' || die "$DIR/keys.env is damaged"

print_entry() {
  local ip="${XHR_EXIT_PUBLIC_IP:-}"
  [ -n "$ip" ] || ip=$(ip -4 route get 1.1.1.1 2>/dev/null \
                       | sed -n 's/.* src \([0-9.]*\).*/\1/p' | head -n1) || true
  [ -n "$ip" ] || ip='<exit-ip>'
  cat <<EOF

=== entry: /etc/haproxy/haproxy.cfg ===
# In the frontend that routes is_reality_sni (the port clients use,
# ENTRY_NODE_PORT), before any default_backend or catch-all use_backend:
    acl is_xhr_sni req_ssl_sni -i $SNI
    use_backend xhr_exit if is_xhr_sni
# A backend of its own; this server requires the PROXY header:
backend xhr_exit
    mode tcp
    server xhr_exit $ip:$PORT send-proxy-v2
# Then: haproxy -c -f /etc/haproxy/haproxy.cfg && systemctl reload haproxy

=== entry: /opt/vpn-bot/.env ===
XHR_SNI=$SNI
XHR_PATH=$XPATH
XHR_PUBLIC_KEY=$PUBLIC_KEY
XHR_SID=$SHORT_ID
# XHR_PORT stays unset: clients connect to ENTRY_NODE_PORT, haproxy's port.
# Then recreate vpn-bot (scripts/deploy_entry_bot.sh, --no-deps), regenerate
# the probe sidecar (AGENTS.md §51, compose profile xhr), and switch xhr on
# in the cascade editor: this server takes the users within 2 minutes.

=== exit: firewall ===
# Only entry needs :$PORT. A published docker port bypasses ufw; to close it
# to everyone else, persisted the way this host keeps its rules:
#   iptables -I DOCKER-USER -p tcp -m conntrack --ctorigdstport $PORT \\
#     --ctdir ORIGINAL ! -s <entry-ip> -j DROP
EOF
}

if [ "$mode" = print ]; then
  print_entry
  exit 0
fi

ANSWER=''
cleanup() {
  [ -z "$ANSWER" ] || rm -f "$ANSWER"
  rm -f "$DIR/config.json.new" "$DIR/config.json.tmp" "$DIR/config.json.prev.tmp"
}
trap cleanup EXIT

# The bot's answer → fetch=ok|transient|error, plus USERS (a JSON list of
# uuids), IN_CASCADE and MISMATCH (lines to print) when ok.
fetch=error FETCH_MSG='' USERS='[]' IN_CASCADE=0 MISMATCH=''
fetch_users() {
  local code parsed line
  ANSWER=$(mktemp "$DIR/.answer.XXXXXX")
  # The token goes through stdin, not argv: /proc/<pid>/cmdline is
  # readable by every local user.
  code=$(printf 'X-DPI-Token: %s\n' "$DPI_REPORT_TOKEN" \
         | curl -sS --max-time 20 -H @- -o "$ANSWER" -w '%{http_code}' \
                "${BOT_URL%/}/api/xhr/users" 2>/dev/null) || code=000
  case "$code" in
    200) ;;
    000) fetch=transient FETCH_MSG="the bot is unreachable"; return ;;
    5*)  fetch=transient FETCH_MSG="the bot could not read its users (HTTP $code)"; return ;;
    403) FETCH_MSG="the bot refused DPI_REPORT_TOKEN (HTTP 403)"; return ;;
    404) FETCH_MSG="the bot hands out no xhr (HTTP 404): XHR_SNI unset in entry's .env, or a bot without /api/xhr/users"; return ;;
    *)   FETCH_MSG="the bot answered HTTP $code"; return ;;
  esac
  parsed=$(XHR_OWN="$SNI $PUBLIC_KEY $SHORT_ID $XPATH" python3 - "$ANSWER" <<'PY'
import json, os, re, sys
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
try:
    with open(sys.argv[1]) as f:
        d = json.load(f)
except (OSError, ValueError):
    sys.exit(3)
if not (isinstance(d, dict) and d.get('ok') is True and isinstance(d.get('users'), list)):
    sys.exit(3)
uuids = set()
for u in d['users']:
    v = u.get('uuid') if isinstance(u, dict) else None
    if isinstance(v, str) and UUID.match(v.strip().lower()):
        uuids.add(v.strip().lower())
print('USERS=' + json.dumps(sorted(uuids)))
print('IN_CASCADE=' + ('1' if d.get('in_cascade') is True else '0'))
own = dict(zip(('sni', 'pbk', 'sid', 'path'), os.environ['XHR_OWN'].split(' ')))
env = {'sni': 'XHR_SNI', 'pbk': 'XHR_PUBLIC_KEY', 'sid': 'XHR_SID', 'path': 'XHR_PATH'}
srv = d.get('server')
if not isinstance(srv, dict):
    print("MISMATCH=the bot's answer carries no server keys to compare"
          " (a bot older than this script?)")
    srv = None
for key, mine in (own.items() if srv is not None else ()):
    theirs = re.sub(r'[^\x21-\x7e]', '?', str(srv.get(key) or ''))[:100]
    if theirs != mine:
        print(f'MISMATCH=the bot hands out {key} "{theirs}", this server has "{mine}":'
              f' set {env[key]}={mine} in entry\'s .env and recreate vpn-bot')
PY
  ) || { FETCH_MSG="the bot's answer is not a user list"; return; }
  rm -f "$ANSWER"
  while IFS= read -r line; do
    case "$line" in
      USERS=*)      USERS=${line#USERS=} ;;
      IN_CASCADE=*) IN_CASCADE=${line#IN_CASCADE=} ;;
      MISMATCH=*)   MISMATCH="$MISMATCH${line#MISMATCH=}"$'\n' ;;
    esac
  done <<< "$parsed"
  fetch=ok
}

live_users() {  # the uuids of the live config, a JSON list
  [ -s "$DIR/config.json" ] || { echo '[]'; return; }
  python3 -c 'import json, sys
c = json.load(open(sys.argv[1]))
print(json.dumps(sorted(x["id"] for x in c["inbounds"][0]["settings"]["clients"])))' \
    "$DIR/config.json"
}

count() { python3 -c 'import json, sys; print(len(json.loads(sys.argv[1])))' "$1"; }

render() {  # $1 = the uuids (a JSON list), $2 = output file
  rm -f "$2"   # a run killed midway leaves it read-only
  # The private key and the uuids go through the environment, not argv.
  (umask 077 && XHR_EXIT_PRIV="$PRIVATE_KEY" XHR_EXIT_USERS="$1" \
    python3 - "$2" "$PORT" "$SNI" "$XPATH" "$SHORT_ID" <<'PY'
import hashlib, json, os, re, sys
out, port, sni, path, sid = sys.argv[1:]
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
uuids = json.loads(os.environ['XHR_EXIT_USERS'])
if not (isinstance(uuids, list) and all(isinstance(u, str) and UUID.match(u) for u in uuids)):
    sys.exit('the user list is not a list of uuids')
# The label is the SSH exit's name for the same user (ssh_exit.username_for):
# what xray logs, no address or chat id. xray wants it unique; two users
# sharing 48 bits of a hash fail `xray run -test`, and the live config stays.
clients = [{'id': uuid, 'email': 'n' + hashlib.sha256(uuid.encode('utf-8')).hexdigest()[:12],
            'flow': ''} for uuid in sorted(set(uuids))]
cfg = {
    'log': {'loglevel': 'warning', 'access': 'none'},
    'inbounds': [{
        'tag': 'xhr-in', 'listen': '0.0.0.0', 'port': int(port), 'protocol': 'vless',
        'settings': {'clients': clients, 'decryption': 'none'},
        'streamSettings': {
            'network': 'xhttp', 'security': 'reality',
            'xhttpSettings': {'path': path, 'mode': 'auto'},
            'realitySettings': {
                'show': False, 'dest': f'{sni}:443', 'serverNames': [sni],
                'privateKey': os.environ['XHR_EXIT_PRIV'], 'shortIds': [sid],
            },
            # entry's haproxy sends PROXY v2; without it nothing gets in
            'sockopt': {'acceptProxyProtocol': True},
        },
        # sniffed only to tell BitTorrent apart; destinations stay as sent
        'sniffing': {'enabled': True, 'destOverride': ['http', 'tls', 'quic'],
                     'routeOnly': True},
    }],
    'outbounds': [
        {'tag': 'direct', 'protocol': 'freedom', 'settings': {'domainStrategy': 'UseIPv4'}},
        {'tag': 'block', 'protocol': 'blackhole'},
    ],
    'routing': {
        # a name is resolved and its addresses checked too
        'domainStrategy': 'IPIfNonMatch',
        'rules': [
            {'type': 'field', 'outboundTag': 'block',
             'ip': ['0.0.0.0/8', '10.0.0.0/8', '100.64.0.0/10', '127.0.0.0/8',
                    '169.254.0.0/16', '172.16.0.0/12', '192.168.0.0/16',
                    '224.0.0.0/4', '240.0.0.0/4', '::1/128', 'fc00::/7', 'fe80::/10']},
            {'type': 'field', 'outboundTag': 'block', 'protocol': ['bittorrent']},
            {'type': 'field', 'outboundTag': 'block', 'port': '25'},
        ],
    },
}
with open(out, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
PY
  ) && chown "$XRAY_UID:$XRAY_UID" "$2" && chmod 400 "$2"
}

running() {
  docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -qx true
}

start_container() {
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  docker run -d --name "$NAME" --restart unless-stopped \
    --user "$XRAY_UID:$XRAY_UID" --read-only --cap-drop ALL \
    --security-opt no-new-privileges \
    --memory "$MEMORY" --pids-limit 256 \
    --log-driver json-file --log-opt max-size=10m --log-opt max-file=3 \
    --dns 1.1.1.1 --dns 8.8.8.8 \
    --sysctl net.ipv6.conf.all.disable_ipv6=1 \
    -p "$PORT:$PORT/tcp" \
    -v "$DIR/config.json:/etc/xray/config.json:ro" \
    "$IMAGE" run -c /etc/xray/config.json >/dev/null
}

container_up() {  # running, and xray did not die and come back meanwhile
  sleep "${XHR_EXIT_SETTLE_S:-3}"
  [ "$(docker inspect -f '{{.State.Running}} {{.RestartCount}}' "$NAME" 2>/dev/null)" = "true 0" ]
}

copy_over() {  # $1 → $2 by a rename: $2 is read-only (0400) once it exists
  rm -f "$2.tmp" && cp -p "$1" "$2.tmp" && mv -f "$2.tmp" "$2"
}

apply_new() {  # config.json.new → checked → live, container recreated
  local err
  err=$(docker run --rm --user "$XRAY_UID:$XRAY_UID" \
          -v "$DIR/config.json.new:/etc/xray/config.json:ro" \
          "$IMAGE" run -test -c /etc/xray/config.json 2>&1) \
    || die "xray -test failed — the live config is untouched: $(printf '%s' "$err" | tail -n 3)"
  if [ -s "$DIR/config.json" ]; then
    copy_over "$DIR/config.json" "$DIR/config.json.prev" || die "could not keep the previous config"
  fi
  mv -f "$DIR/config.json.new" "$DIR/config.json"
  if start_container && container_up; then
    return 0
  fi
  docker logs --tail 30 "$NAME" >&2 || true
  if [ -s "$DIR/config.json.prev" ]; then
    copy_over "$DIR/config.json.prev" "$DIR/config.json" || die "could not roll back the config"
    start_container && container_up \
      && die "$NAME did not come up with the new config — rolled back to the previous one"
  fi
  die "$NAME is not running"
}

fetch_users
old=$(live_users)
if [ "$fetch" = ok ]; then
  new=$USERS
else
  new=$old   # the bot did not answer with a list: the users stay as they are
fi
render "$new" "$DIR/config.json.new" || die "could not render the config"
new_count=$(count "$new")
old_count=$(count "$old")

changed=1
if [ "$mode" = sync ] && cmp -s "$DIR/config.json.new" "$DIR/config.json" && running; then
  changed=0   # nothing to do — the usual case, and a quiet one
fi
if [ "$changed" = 1 ]; then
  if [ "$mode" = sync ] && [ "$new_count" -eq 0 ] && [ "$old_count" -gt 0 ]; then
    die "the bot lists 0 users (live: $old_count) — not applied; run $SELF by hand if that is real"
  fi
  apply_new
  added=$(python3 -c 'import json, sys
a, b = set(json.loads(sys.argv[1])), set(json.loads(sys.argv[2]))
print(f"+{len(b - a)} -{len(a - b)}")' "$old" "$new")
  echo "xhr-exit on :$PORT (sni $SNI): $new_count user(s) ($added)"
fi

if [ "$mode" = setup ]; then
  # The root-owned copy the timer runs (see the header).
  if [ "$SELF" != "$(readlink -f "$BIN" 2>/dev/null || echo "$BIN")" ]; then
    cp "$SELF" "$BIN.new" && chown root:root "$BIN.new" && chmod 755 "$BIN.new" \
      && mv "$BIN.new" "$BIN" || { rm -f "$BIN.new"; die "could not install $BIN"; }
  fi
  cat > "$UNIT_DIR/$UNIT.service" <<EOF
[Unit]
Description=xhr (VLESS + XHTTP + Reality): users from the bot into the xhr-exit container
After=docker.service network-online.target

[Service]
Type=oneshot
ExecStart=$BIN --sync
EOF
  cat > "$UNIT_DIR/$UNIT.timer" <<EOF
[Unit]
Description=xhr: user sync every 2 minutes

[Timer]
OnBootSec=3min
OnUnitActiveSec=2min
AccuracySec=15s

[Install]
WantedBy=timers.target
EOF
  systemctl daemon-reload
  systemctl enable --now "$UNIT.timer" >/dev/null
  echo "$UNIT.timer runs '$BIN --sync' every 2 minutes"
  print_entry
fi

case "$fetch" in
  ok)
    if [ "$new_count" -eq 0 ] && [ "$IN_CASCADE" = 0 ] && [ "$changed" = 1 ]; then
      echo "the bot lists nobody: xhr is switched off in its cascade"
    fi ;;
  transient)
    echo "$FETCH_MSG — users unchanged" ;;
  error)
    # The first setup runs before entry's .env knows this server.
    if [ "$mode" = setup ] && [ "${FETCH_MSG#*HTTP 404}" != "$FETCH_MSG" ]; then
      echo "$FETCH_MSG — users unchanged until it does"
    else
      die "$FETCH_MSG — users unchanged"
    fi ;;
esac
if [ -n "$MISMATCH" ]; then
  printf '%s' "$MISMATCH" >&2
  die "the bot's xhr profiles do not match this server"
fi
