#!/usr/bin/env bash
# VLESS + XHTTP + Reality (`xhr`) on an xray of its own on EXIT
# (IMPROVEMENT_PLAN F, E3; bot/services/xhr_exit.py). Run as root on exit.
#
#   setup_xhr_exit.sh           set up, or re-run: dest, image and panel
#                               checks, Reality keys (once), the config, the
#                               `xhr-exit` container, the sync timer; prints
#                               the haproxy lines and .env values entry needs
#                               (re-run it after this script changes: the
#                               timer runs a root-owned copy, see below)
#   setup_xhr_exit.sh --sync    what the timer runs every 2 minutes: the
#                               traffic counters into the panel, the user
#                               list from the bot and the panel, a new
#                               container only when it changed
#   setup_xhr_exit.sh --print   the haproxy lines and .env values again
#   setup_xhr_exit.sh --remove  timer and container gone, their last bytes
#                               counted (keys stay)
#
# Why not a 3x-ui inbound: the relational panel gives every VLESS inbound
# of a client that client's Vision flow, and xray rejects a Vision-flow
# account whose client sends no flow with TLS inside. XHTTP carries no
# flow, so a panel inbound would reject every user (AGENTS.md §51). This
# script never touches the 3x-ui container or its compose file; of the
# panel's database it writes only the traffic counters (below).
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
# Traffic: xhr bytes count against the same quota as every panel protocol.
# xray counts each user's bytes (StatsService; its API listens on
# 127.0.0.1:$STATS_PORT in the container's own network namespace, and the
# routing refuses that port to users, whatever a name resolves to). Every
# run first reads and resets the counters (`xray api statsquery -reset`),
# before anything can recreate the container (a new one starts from zero),
# and once more right before a recreation; a sync that cannot read the
# counters of a running container does not recreate it. The bytes go into
# a root-only pending file at once ($DIR/traffic.pending), then into the
# panel's x-ui.db: the label n<12 hex of sha256(uuid)> → the panel client
# with that uuid → its email → client_traffics up/down += …, in one
# transaction, as hy2-traffic-collector adds hy2's (AGENTS.md §26). What
# the panel does not take (locked, no client for that uuid yet) stays
# pending for the next run; after 31 days without a client it is dropped,
# with a line. At least once: a run killed between the commit and the
# pending file's rewrite counts that window twice, never zero times. The
# last seconds of a container that dies on its own die with it.
#
# The panel has the last word on who uses xhr: a user the bot lists stays
# out of the config while the panel has switched it off (client_traffics
# enable = 0, up + down >= total > 0, or an expiry that passed) or has no
# client for its uuid, and comes back once the panel lets it in again.
# That read is read-only; one that fails, or that finds none of the listed
# users, leaves the users as they are and fails the run.
#
# The timer runs as root, so it runs /usr/local/sbin/xhr-exit-sync, a
# root-owned copy setup installs, never this file: /opt/vpn-bot is an
# rsync target, and a root timer must not execute a file another account
# can change.
#
# /etc/xhr-exit.env (root-only), read by setup; --sync reads only BOT_URL
# and the token from it (the server parameters setup checked are kept in
# /etc/xhr-exit/server.env, so an edit here takes effect via setup):
#   BOT_URL=https://<dashboard host>:9443   the bot, reachable from exit; or
#           https://<entry ip>:8448 with BOT_CA_FILE (the hop, AGENTS.md §53).
#           Plain http only to loopback: the token must not cross in clear.
#   BOT_CA_FILE=/etc/nekovpn-hop/entry-hop.pem   optional, the only CA for
#           BOT_URL (the hop's pinned certificate); empty = the system's CAs
#   DPI_REPORT_TOKEN=<the same token as /etc/exit-dpi.env>
#   XHR_SNI=<Reality dest of its own: TLS 1.3, h2, certificate record
#           <= 8000 B; not a name entry's haproxy routes elsewhere>
#   XHR_PATH=/xhr               optional
#   XHR_EXIT_PORT=8446          optional, the port haproxy forwards to
#   XHR_EXIT_PUBLIC_IP=<ip>     optional, for the printed haproxy line
#   XHR_EXIT_IMAGE=<image@sha256:...>  optional, digest-pinned
#   XHR_EXIT_MEMORY=128m        optional
#   XUI_DB_PATH=<path>          optional, the panel's x-ui.db (default: the
#                               3x-ui volume, as for hy2-traffic-collector)
set -euo pipefail
# Byte order for sort/comm.
export LC_ALL=C

# XHR_EXIT_DIR / _ENV / _UNIT_DIR / _BIN / _NAME / _SETTLE_S / _DB_TIMEOUT_S
# exist for the tests (tests/unit/test_setup_xhr_exit_sh.py); prod runs the
# defaults.
DIR="${XHR_EXIT_DIR:-/etc/xhr-exit}"
ENV_FILE="${XHR_EXIT_ENV:-/etc/xhr-exit.env}"
UNIT_DIR="${XHR_EXIT_UNIT_DIR:-/etc/systemd/system}"
BIN="${XHR_EXIT_BIN:-/usr/local/sbin/xhr-exit-sync}"
NAME="${XHR_EXIT_NAME:-xhr-exit}"
UNIT=xhr-exit-sync
SELF=$(readlink -f "$0")
# xray 26.6.22 (the exit's own version, plan F step 0), pinned by digest.
DEFAULT_IMAGE=ghcr.io/xtls/xray-core:26.6.22@sha256:7ec1ff2ea903a1e0999ada29557eadec1447588a59fd3afae64b46f9ae4b7953
# The first xray mihomo does not keep Reality compatible with.
XRAY_LIMIT=26.7.11
# The image's own unprivileged user; the config is readable by it alone.
XRAY_UID=65532
# xray's API (StatsService only), on the container's own loopback.
STATS_PORT=10085
# The panel's database on exit: the 3x-ui volume.
DEFAULT_XUI_DB=/var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db
# Bytes xray counted that the panel has not taken yet.
PENDING="$DIR/traffic.pending"

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
# interleave their config swaps or their reads of the counters.
mkdir -p "$DIR" && chmod 700 "$DIR"
exec 9>"$DIR/.lock"
if [ "$mode" = sync ]; then
  flock -n 9 || exit 0   # another run is on it
else
  flock -w 120 9 || die "another run holds $DIR/.lock"
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

ENV_KEYS='BOT_URL BOT_CA_FILE DPI_REPORT_TOKEN XHR_SNI XHR_PATH XHR_EXIT_PORT XHR_EXIT_PUBLIC_IP XHR_EXIT_IMAGE XHR_EXIT_MEMORY XUI_DB_PATH'

load_env() {  # the env file is the only source of these
  [ -r "$ENV_FILE" ] || die "missing $ENV_FILE (BOT_URL, DPI_REPORT_TOKEN, XHR_SNI)"
  # shellcheck disable=SC2086
  unset $ENV_KEYS
  # shellcheck disable=SC2086
  read_env "$ENV_FILE" $ENV_KEYS
  [ -n "${BOT_URL:-}" ] || die "BOT_URL not set in $ENV_FILE"
  [ -n "${DPI_REPORT_TOKEN:-}" ] || die "DPI_REPORT_TOKEN not set in $ENV_FILE"
  case "$DPI_REPORT_TOKEN" in *[!A-Za-z0-9._~+/=-]*) die "odd characters in DPI_REPORT_TOKEN" ;; esac
  check_bot_url
}

# The token, and the users' list it fetches, must not cross the internet in
# clear: plain http only to loopback. BOT_CA_FILE pins the hop's certificate
# (AGENTS.md §53), and curl then trusts that file alone: --capath /dev/null,
# because Debian's curl also loads /etc/ssl/certs next to --cacert.
BOT_TLS=()
check_bot_url() {
  local authority host
  case "$BOT_URL" in
    https://*) ;;
    http://*)
      authority=${BOT_URL#http://}
      authority=${authority%%[/?#]*}
      authority=${authority##*@}
      case "$authority" in
        \[*) host=${authority%%]*}; host=${host#[} ;;
        *) host=${authority%%:*} ;;
      esac
      if [ "$host" != localhost ] && [ "$host" != ::1 ] \
         && ! [[ $host =~ ^127\.[0-9]{1,3}\.[0-9]{1,3}\.[0-9]{1,3}$ ]]; then
        die "BOT_URL is plain http to another host: the token would cross the internet in clear (the hop's https listener, AGENTS.md §53)"
      fi ;;
    *) die "BOT_URL must be an http(s) URL" ;;
  esac
  BOT_TLS=()
  if [ -n "${BOT_CA_FILE:-}" ]; then
    [ -r "$BOT_CA_FILE" ] || die "BOT_CA_FILE $BOT_CA_FILE is not readable"
    BOT_TLS=(--cacert "$BOT_CA_FILE" --capath /dev/null)
  fi
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
  [ "$PORT" != "$STATS_PORT" ] || die "XHR_EXIT_PORT $STATS_PORT is the port of xray's own API"
  case "$SNI" in *.*) ;; *) die "XHR_SNI is not a host name" ;; esac
  case "$SNI" in *[!A-Za-z0-9.-]*) die "odd characters in XHR_SNI" ;; esac
  case "$XPATH" in /*) ;; *) die "XHR_PATH must start with /" ;; esac
  case "$XPATH" in *[!A-Za-z0-9._~/-]*) die "odd characters in XHR_PATH" ;; esac
  case "$MEMORY" in *[!0-9kmg]*|[!0-9]*) die "XHR_EXIT_MEMORY like 128m" ;; esac
  case "$IMAGE" in *[!A-Za-z0-9.:/_@-]*) die "odd characters in XHR_EXIT_IMAGE" ;; esac
  printf '%s' "$IMAGE" | grep -qE '@sha256:[0-9a-f]{64}$' \
    || die "XHR_EXIT_IMAGE must be pinned by digest (…@sha256:<64 hex>)"
  case "$XUI_DB" in /*) ;; *) die "XUI_DB_PATH must be an absolute path" ;; esac
  case "$XUI_DB" in *[!A-Za-z0-9._/-]*) die "odd characters in XUI_DB_PATH" ;; esac
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

running() {
  docker inspect -f '{{.State.Running}}' "$NAME" 2>/dev/null | grep -qx true
}

# The traffic counters and the panel (the header, "Traffic"):
#   read     the running container's counters, read and reset, into the
#            pending file (nothing to read when the live config counts no
#            traffic: an older render)
#   deliver  the pending file into the panel's client_traffics
#   gate     the uuids in $BASE → USERS= the ones the panel lets in, and an
#            OFF= line per user it keeps out (read-only)
#   check    x-ui.db is a panel database with clients (setup)
# 3 = it could not; the reason is on stderr.
meter() {
  XHR_EXIT_BASE="${BASE:-[]}" python3 - "$1" "$PENDING" "$XUI_DB" "$NAME" \
    "127.0.0.1:$STATS_PORT" "$DIR/config.json" "${XHR_EXIT_DB_TIMEOUT_S:-10}" <<'PY'
import hashlib, json, os, re, sqlite3, subprocess, sys, time
from urllib.parse import quote

mode, pending_path, db, name, addr, live_cfg, timeout = sys.argv[1:8]
timeout = float(timeout)
LABEL = re.compile(r'^n[0-9a-f]{12}$')
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
FIELDS = ('up', 'down', 'at', 'since')
# Bytes no panel client takes are kept this long, then dropped with a line.
KEEP_MS = 31 * 86400 * 1000


def now_ms():
    return int(time.time() * 1000)


def fail(msg):
    print('ERROR: ' + msg, file=sys.stderr)
    sys.exit(3)


def label(uuid):  # the name render gives a user (ssh_exit.username_for)
    return 'n' + hashlib.sha256(uuid.encode('utf-8')).hexdigest()[:12]


def load_pending():
    """{label: {up, down, at, since}}: bytes xray counted that the panel has
    not taken yet; ``at`` is the last read that added to them, ``since`` the
    first (ms)."""
    try:
        with open(pending_path) as f:
            d = json.load(f)
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        d = None
    if not (isinstance(d, dict) and all(
            LABEL.match(k) and isinstance(v, dict)
            and all(type(v.get(x)) is int and v[x] >= 0 for x in FIELDS)
            for k, v in d.items())):
        # Never overwritten: its bytes are nowhere else.
        fail('%s is damaged: move it aside and add its bytes by hand; until then'
             ' the counters stay in xray' % pending_path)
    return d


def save_pending(d):
    if not d:
        try:
            os.unlink(pending_path)
        except FileNotFoundError:
            pass
        return
    tmp = pending_path + '.tmp'
    with os.fdopen(os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as f:
        json.dump(d, f, sort_keys=True)
        f.write('\n')
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, pending_path)
    fd = os.open(os.path.dirname(pending_path), os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def parse(raw):
    """({label: [up, down]} with bytes, other names) from `statsquery`.
    xray leaves a zero value out; protobuf JSON may write int64 as a string."""
    d = json.loads(raw) if raw.strip() else {}
    stats = d.get('stat', []) if isinstance(d, dict) else None
    if not isinstance(stats, list):
        raise ValueError('not a statsquery answer')
    out, other = {}, set()
    for s in stats:
        stat = s.get('name') if isinstance(s, dict) else None
        parts = stat.split('>>>') if isinstance(stat, str) else []
        if len(parts) != 4 or parts[0] != 'user' or parts[2] != 'traffic' \
                or parts[3] not in ('uplink', 'downlink'):
            continue
        v = s.get('value', 0)
        if isinstance(v, str) and v.isdigit():
            v = int(v)
        if type(v) is not int or v < 0:
            raise ValueError('%s = %r' % (stat, v))
        if not LABEL.match(parts[1]):
            other.add(parts[1])
            continue
        pair = out.setdefault(parts[1], [0, 0])
        pair[0 if parts[3] == 'uplink' else 1] += v
    return {k: v for k, v in out.items() if v[0] or v[1]}, other


def cmd_read():
    # Only a config with xray's API counts: an older render has nothing to read.
    try:
        with open(live_cfg) as f:
            services = (json.load(f).get('api') or {}).get('services') or []
    except (OSError, ValueError, AttributeError):
        services = []
    if 'StatsService' not in services:
        return
    pending = load_pending()   # before the reset: what cannot be kept is not taken
    try:
        r = subprocess.run(['docker', 'exec', name, 'xray', 'api', 'statsquery',
                            '-s', addr, '-pattern', 'user>>>', '-reset'],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=60)
    except (OSError, subprocess.SubprocessError) as e:
        fail('could not read the xhr traffic counters (%s)' % e)
    if r.returncode != 0:
        why = r.stderr.decode('utf-8', 'replace').strip().splitlines()
        fail('could not read the xhr traffic counters (%s)'
             % (why[-1] if why else 'exit %d' % r.returncode))
    raw = r.stdout.decode('utf-8', 'replace')
    try:
        got, other = parse(raw)
    except ValueError as e:
        # The counters may be reset already: the journal keeps what xray said.
        fail('unreadable xhr traffic counters (%s); xray may have reset them,'
             ' its answer: %s' % (e, raw[:4000]))
    if other:
        print('xhr traffic of users this script does not name, not counted: %s'
              % ', '.join(sorted(other)), file=sys.stderr)
    if not got:
        return
    t = now_ms()
    for lab, (up, down) in sorted(got.items()):
        e = pending.setdefault(lab, {'up': 0, 'down': 0, 'at': t, 'since': t})
        e['up'] += up
        e['down'] += down
        e['at'] = t
    try:
        save_pending(pending)
    except OSError as e:
        fail('could not keep the xhr traffic counters (%s); not counted yet: %s'
             % (e, json.dumps(got, sort_keys=True)))


def connect(rw):
    # The path is quoted into the URI: a "?" in it must not become a mode.
    # mode=rw never creates the file.
    conn = sqlite3.connect('file:%s?mode=%s' % (quote(os.path.abspath(db)), 'rw' if rw else 'ro'),
                           uri=True, timeout=timeout, isolation_level=None)
    conn.execute('PRAGMA busy_timeout = %d' % int(timeout * 1000))
    if not rw:
        conn.execute('PRAGMA query_only = 1')
    return conn


def uuid_emails(conn):
    """{uuid: email} of the panel's clients: the relational table first, then
    every inbound's settings for the uuids it lacks."""
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}

    def columns(table):
        if table not in tables:
            return set()
        return {r[1] for r in conn.execute('PRAGMA table_info(%s)' % table)}

    pairs = []
    if {'uuid', 'email'} <= columns('clients'):
        pairs += conn.execute('SELECT uuid, email FROM clients').fetchall()
    if 'settings' in columns('inbounds'):
        for (settings,) in conn.execute('SELECT settings FROM inbounds').fetchall():
            try:
                s = json.loads(settings or '{}')
            except (TypeError, ValueError):
                continue
            for c in (s.get('clients') if isinstance(s, dict) else None) or []:
                if isinstance(c, dict):
                    pairs.append((c.get('id'), c.get('email')))
    out = {}
    for uuid, email in pairs:
        if isinstance(uuid, str) and isinstance(email, str) and email.strip():
            uuid = uuid.strip().lower()
            if UUID.match(uuid):
                out.setdefault(uuid, email)
    return out


def cmd_deliver():
    pending = load_pending()
    if not pending:
        return
    done = set()
    try:
        conn = connect(rw=True)
        try:
            by_label = {}
            for uuid, email in uuid_emails(conn).items():
                by_label.setdefault(label(uuid), set()).add(email)
            if not by_label:
                raise sqlite3.DatabaseError('the panel lists no clients')
            # A label two panel clients share (48 bits of a hash) stays pending.
            todo = sorted((lab, min(by_label[lab])) for lab in pending
                          if len(by_label.get(lab, ())) == 1)
            if todo:
                conn.execute('BEGIN IMMEDIATE')
                try:
                    for lab, email in todo:
                        e = pending[lab]
                        cur = conn.execute(
                            'UPDATE client_traffics SET up = up + ?, down = down + ?,'
                            ' last_online = MAX(COALESCE(last_online, 0), ?) WHERE email = ?',
                            (e['up'], e['down'], e['at'], email))
                        if cur.rowcount > 0:
                            done.add(lab)
                    conn.execute('COMMIT')
                except BaseException:
                    if conn.in_transaction:
                        conn.execute('ROLLBACK')
                    raise
        finally:
            conn.close()
    except sqlite3.Error as e:
        fail('could not add xhr traffic to the panel (%s); kept in %s' % (e, pending_path))
    t = now_ms()
    rest = {}
    for lab, e in sorted(pending.items()):
        if lab in done:
            continue
        if t - e['since'] > KEEP_MS:
            print('dropped %d byte(s) of xhr traffic of %s: no panel client took them in 31 days'
                  % (e['up'] + e['down'], lab))
            continue
        rest[lab] = e
    try:
        save_pending(rest)
    except OSError as e:
        fail('the panel has the xhr traffic, but %s could not be rewritten (%s):'
             ' empty it before the next run, or it is counted twice' % (pending_path, e))


def off_reason(row, t):
    """Why the panel keeps this client out, or None (hy2-traffic-collector's
    rule for kicking a live session)."""
    enable, up, down, total, expiry = row

    def num(v):
        try:
            return int(v or 0)
        except (TypeError, ValueError):
            return 0

    if enable in (0, '0', 'false', 'False'):
        return 'switched off'
    if num(total) > 0 and num(up) + num(down) >= num(total):
        return 'over quota'
    if 0 < num(expiry) < t:
        return 'expired'
    return None


def cmd_gate():
    base = json.loads(os.environ.get('XHR_EXIT_BASE') or '[]')
    if not (isinstance(base, list) and all(isinstance(u, str) and UUID.match(u) for u in base)):
        fail('the user list is not a list of uuids')
    try:
        conn = connect(rw=False)
        try:
            emails = uuid_emails(conn)
            rows = {r[0]: r[1:] for r in conn.execute(
                'SELECT email, enable, up, down, total, expiry_time FROM client_traffics')
                    if isinstance(r[0], str)}
        finally:
            conn.close()
    except sqlite3.Error as e:
        fail('could not read the panel %s (%s): users unchanged' % (db, e))
    t = now_ms()
    kept, off = [], []
    for uuid in base:
        row = rows.get(emails.get(uuid, ''))
        why = 'no client in the panel' if row is None else off_reason(row, t)
        if why:
            off.append('%s (%s)' % (label(uuid), why))
        else:
            kept.append(uuid)
    if base and len(off) == len(base) and all(o.endswith('(no client in the panel)') for o in off):
        fail('none of the %d listed user(s) has a client in the panel: users unchanged' % len(base))
    print('USERS=' + json.dumps(kept))
    for o in off:
        print('OFF=' + o)


def cmd_check():
    if not os.path.isfile(db):
        fail("%s not found: XUI_DB_PATH must be the panel's x-ui.db, xhr traffic is counted there" % db)
    try:
        conn = connect(rw=False)
        try:
            have = {r[1] for r in conn.execute('PRAGMA table_info(client_traffics)')}
            emails = uuid_emails(conn)
        finally:
            conn.close()
    except sqlite3.Error as e:
        fail('could not read %s (%s)' % (db, e))
    missing = [c for c in ('email', 'enable', 'up', 'down', 'total', 'expiry_time', 'last_online')
               if c not in have]
    if missing:
        fail('%s has no client_traffics(%s): not the panel database?' % (db, ', '.join(missing)))
    if not emails:
        fail('%s lists no clients (neither clients.uuid nor inbounds.settings)' % db)
    print('panel %s: %d client(s), xhr traffic is counted there' % (db, len(emails)))


{'read': cmd_read, 'deliver': cmd_deliver, 'gate': cmd_gate, 'check': cmd_check}[mode]()
PY
}

read_counters() {  # fails when a running container's counters could not be read
  running || return 0   # a stopped one has none left
  meter read
}

if [ "$mode" = remove ]; then
  XUI_DB=''
  if [ -s "$DIR/server.env" ]; then read_env "$DIR/server.env" XUI_DB; fi
  XUI_DB="${XUI_DB:-$DEFAULT_XUI_DB}"
  systemctl disable --now "$UNIT.timer" >/dev/null 2>&1 || true
  rm -f "$UNIT_DIR/$UNIT.service" "$UNIT_DIR/$UNIT.timer" "$BIN"
  systemctl daemon-reload
  # Its last bytes first: the counters go with the container.
  failed=0
  read_counters || failed=1
  docker rm -f "$NAME" >/dev/null 2>&1 || true
  meter deliver || failed=1
  echo "xhr-exit removed: container and sync timer (keys kept in $DIR)"
  echo "the bot hands xhr out while XHR_SNI is set: switch xhr off in the cascade," \
       "and drop the is_xhr_sni route from entry's haproxy"
  if [ -s "$PENDING" ]; then
    echo "xhr traffic the panel has not taken is kept in $PENDING (a setup run adds it)" >&2
  fi
  exit "$failed"
fi

if [ "$mode" = setup ]; then
  load_env
  PORT="${XHR_EXIT_PORT:-8446}"
  SNI="${XHR_SNI:-}"
  XPATH="${XHR_PATH:-/xhr}"
  IMAGE="${XHR_EXIT_IMAGE:-$DEFAULT_IMAGE}"
  MEMORY="${XHR_EXIT_MEMORY:-128m}"
  XUI_DB="${XUI_DB_PATH:-$DEFAULT_XUI_DB}"
  [ -n "$SNI" ] || die "XHR_SNI not set in $ENV_FILE (a Reality dest of its own)"
  check_params
  if ss -ltnH "( sport = :$PORT )" | grep -q . \
     && ! docker ps --format '{{.Names}}' | grep -qx "$NAME"; then
    die "port $PORT is already taken by something other than $NAME"
  fi
  check_dest
  check_image
  meter check || exit 1
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
  (umask 077 && printf 'PORT=%s\nSNI=%s\nXPATH=%s\nIMAGE=%s\nMEMORY=%s\nXUI_DB=%s\n' \
    "$PORT" "$SNI" "$XPATH" "$IMAGE" "$MEMORY" "$XUI_DB" > "$DIR/server.env")
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
  PORT='' SNI='' XPATH='' IMAGE='' MEMORY='' XUI_DB=''
  read_env "$DIR/server.env" PORT SNI XPATH IMAGE MEMORY XUI_DB
  # A server.env from before the traffic metering has no XUI_DB.
  XUI_DB="${XUI_DB:-$DEFAULT_XUI_DB}"
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
         | curl -sS --max-time 20 -H @- ${BOT_TLS[@]+"${BOT_TLS[@]}"} \
                -o "$ANSWER" -w '%{http_code}' \
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

gate_users() {  # $1 = the uuids → NEW (the ones the panel lets in) and OFF
  local out line
  out=$(BASE=$1 meter gate) || return 1
  OFF=''
  while IFS= read -r line; do
    case "$line" in
      USERS=*) NEW=${line#USERS=} ;;
      OFF=*)   OFF="${OFF:+$OFF, }${line#OFF=}" ;;
    esac
  done <<< "$out"
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
    python3 - "$2" "$PORT" "$SNI" "$XPATH" "$SHORT_ID" "$STATS_PORT" <<'PY'
import hashlib, json, os, re, sys
out, port, sni, path, sid, stats_port = sys.argv[1:]
UUID = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$')
uuids = json.loads(os.environ['XHR_EXIT_USERS'])
if not (isinstance(uuids, list) and all(isinstance(u, str) and UUID.match(u) for u in uuids)):
    sys.exit('the user list is not a list of uuids')
# The label is the SSH exit's name for the same user (ssh_exit.username_for):
# what xray logs and counts traffic under, no address or chat id. xray wants
# it unique; two users sharing 48 bits of a hash fail `xray run -test`, and
# the live config stays.
clients = [{'id': uuid, 'email': 'n' + hashlib.sha256(uuid.encode('utf-8')).hexdigest()[:12],
            'flow': ''} for uuid in sorted(set(uuids))]
cfg = {
    'log': {'loglevel': 'warning', 'access': 'none'},
    # Per-user byte counters: the sync reads and resets them and adds them
    # to the user's row in the panel (the script's header, "Traffic").
    'stats': {},
    'api': {'tag': 'api', 'listen': f'127.0.0.1:{stats_port}', 'services': ['StatsService']},
    'policy': {'levels': {'0': {'statsUserUplink': True, 'statsUserDownlink': True}}},
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
            # The API's port, by port: a name checked as public may point at
            # loopback when it is dialed, and a user who reached the API could
            # reset the counters.
            {'type': 'field', 'outboundTag': 'block', 'port': stats_port},
        ],
    },
}
with open(out, 'w') as f:
    json.dump(cfg, f, indent=2)
    f.write('\n')
PY
  ) && chown "$XRAY_UID:$XRAY_UID" "$2" && chmod 400 "$2"
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
  # The counters once more, right before the container that holds them goes
  # (config.json is still the one it runs).
  if ! read_counters; then
    FAILED=1
    echo "ERROR: recreating $NAME all the same: its traffic since the last read is not counted" >&2
  fi
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

FAILED=0
# Traffic first: the running container's counters, before anything here can
# recreate it, and into the panel.
counters=ok
read_counters || counters=failed FAILED=1
meter deliver || FAILED=1

fetch_users
old=$(live_users)
if [ "$fetch" = ok ]; then
  base=$USERS
else
  base=$old   # the bot did not answer with a list: the users stay as they are
fi
# The panel's word on each of them (read-only); a read that fails changes nothing.
NEW=$old OFF=''
gate_users "$base" || FAILED=1
new=$NEW
render "$new" "$DIR/config.json.new" || die "could not render the config"
new_count=$(count "$new")
old_count=$(count "$old")

changed=1 applied=0
if [ "$mode" = sync ] && cmp -s "$DIR/config.json.new" "$DIR/config.json" && running; then
  changed=0   # nothing to do — the usual case, and a quiet one
fi
if [ "$changed" = 1 ]; then
  if [ "$mode" = sync ] && [ "$fetch" = ok ] && [ "$(count "$USERS")" -eq 0 ] \
     && [ "$old_count" -gt 0 ]; then
    die "the bot lists 0 users (live: $old_count) — not applied; run $SELF by hand if that is real"
  fi
  if [ "$mode" = sync ] && [ "$counters" = failed ]; then
    # A new container starts from zero: the bytes it holds would be lost.
    rm -f "$DIR/config.json.new"
    echo "ERROR: $NAME not recreated: its traffic counters could not be read — users unchanged" >&2
  else
    apply_new
    applied=1
    added=$(python3 -c 'import json, sys
a, b = set(json.loads(sys.argv[1])), set(json.loads(sys.argv[2]))
print(f"+{len(b - a)} -{len(a - b)}")' "$old" "$new")
    echo "xhr-exit on :$PORT (sni $SNI): $new_count user(s) ($added)${OFF:+; off in the panel: $OFF}"
  fi
fi
if [ "$applied" = 1 ] && [ -s "$PENDING" ]; then
  meter deliver || FAILED=1   # what was read right before the recreation
fi

if [ "$mode" = setup ]; then
  # The root-owned copy the timer runs (see the header).
  if [ "$SELF" != "$(readlink -f "$BIN" 2>/dev/null || echo "$BIN")" ]; then
    cp "$SELF" "$BIN.new" && chown root:root "$BIN.new" && chmod 755 "$BIN.new" \
      && mv "$BIN.new" "$BIN" || { rm -f "$BIN.new"; die "could not install $BIN"; }
  fi
  cat > "$UNIT_DIR/$UNIT.service" <<EOF
[Unit]
Description=xhr (VLESS + XHTTP + Reality): users from the bot into the xhr-exit container, its traffic into the panel
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
[ "$FAILED" = 0 ] || exit 1
