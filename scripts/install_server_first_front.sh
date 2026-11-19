#!/usr/bin/env bash
# Install the server-first front on ENTRY (IMPROVEMENT_PLAN G4; AGENTS.md §62).
# Run as root from a copy of the repo:
#
#     bash install_server_first_front.sh            # install or update, (re)start
#     bash install_server_first_front.sh --remove   # stop and remove; the config stays
#
# The unit runs a root-owned COPY of scripts/server_first_front.py in
# /usr/local/lib/nekovpn-sf-front, never a file another account can change
# (AGENTS.md §36). Re-run after the script changes: a restart drops the
# connections it carries, and the app reconnects within seconds.
#
# The config, /etc/nekovpn-sf-front.conf, is written from
# systemd/nekovpn-sf-front.conf.example when there is none and never
# overwritten. The installer never touches haproxy or shadow-tls: it says
# which target nothing listens on yet, and for a proxy-v2 route the bind line
# haproxy needs.
set -euo pipefail

LIB_DIR="${SFF_LIB_DIR:-/usr/local/lib/nekovpn-sf-front}"
UNIT_DIR="${SFF_UNIT_DIR:-/etc/systemd/system}"
CONF="${SFF_CONF:-/etc/nekovpn-sf-front.conf}"
HAPROXY_CFG="${SFF_HAPROXY_CFG:-/etc/haproxy/haproxy.cfg}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
SS="${SS:-ss}"
WAIT_S="${SFF_WAIT_S:-1}"     # tests: 0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=server-first-front.service
SCRIPT=server_first_front.py

if [ "$(id -u)" != "0" ] && [ "${SFF_ALLOW_NONROOT:-0}" != "1" ]; then
    echo "run as root" >&2
    exit 1
fi

if [ "${1:-}" = "--remove" ]; then
    "$SYSTEMCTL" disable --now "$UNIT" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/$UNIT"
    rm -rf "$LIB_DIR"
    "$SYSTEMCTL" daemon-reload
    echo "removed (the config $CONF stays)"
    exit 0
fi
if [ $# -gt 0 ]; then
    echo "usage: $0 [--remove]" >&2
    exit 2
fi

for f in "$SRC/scripts/$SCRIPT" "$SRC/systemd/$UNIT" "$SRC/systemd/nekovpn-sf-front.conf.example"; do
    [ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
# A copy that does not even compile would take every server-first user down.
python3 -I -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' \
    "$SRC/scripts/$SCRIPT"

if [ ! -e "$CONF" ]; then
    install -m 0644 -o root -g root "$SRC/systemd/nekovpn-sf-front.conf.example" "$CONF"
    echo "wrote $CONF from the example"
fi
if ! routes="$(python3 -I "$SRC/scripts/$SCRIPT" --config "$CONF" --check)"; then
    echo "$CONF does not parse: nothing installed" >&2
    exit 1
fi

install -d -m 0755 -o root -g root "$LIB_DIR"
install -m 0644 -o root -g root "$SRC/scripts/$SCRIPT" "$LIB_DIR/$SCRIPT.new"
mv -f "$LIB_DIR/$SCRIPT.new" "$LIB_DIR/$SCRIPT"
install -m 0644 -o root -g root "$SRC/systemd/$UNIT" "$UNIT_DIR/$UNIT"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable "$UNIT" >/dev/null
"$SYSTEMCTL" restart "$UNIT"

# Type=simple is "active" the moment it forks: a front that cannot listen
# exits a moment later and restarts every 2 s. It must still be the same
# process seconds later.
sleep "$WAIT_S"
pid="$("$SYSTEMCTL" show -p MainPID --value "$UNIT" 2>/dev/null || true)"
sleep "$((WAIT_S * 2))"
if [ "$("$SYSTEMCTL" is-active "$UNIT" 2>/dev/null || true)" != "active" ] || [ -z "$pid" ] \
        || [ "$pid" = 0 ] || [ "$("$SYSTEMCTL" show -p MainPID --value "$UNIT" 2>/dev/null || true)" != "$pid" ]; then
    echo "$UNIT does not stay up: journalctl -u $UNIT -n 30" >&2
    exit 1
fi
echo "installed: $LIB_DIR/$SCRIPT, $UNIT running"

# What each route reaches. "LISTEN->TARGET[ proxy-v2]", one per line.
listening() {   # host port: does something listen there (or on every address)?
    "$SS" -Hltn "( sport = :$2 )" 2>/dev/null \
        | awk -v h="$1" -v p="$2" '{ if ($4 == h":"p || $4 == "0.0.0.0:"p || $4 == "*:"p || $4 == "[::]:"p) f = 1 } END { exit !f }'
}
# A proxy-v2 route's target must be haproxy's accept-proxy bind, not merely a
# listener: the first draft pointed at 18443, where the decoy nginx listens.
haproxy_binds() {   # target: does haproxy's config bind it with accept-proxy?
    [ -r "$HAPROXY_CFG" ] || return 1
    awk -v t="$1" '$1 == "bind" && $2 == t { for (i = 3; i <= NF; i++) if ($i == "accept-proxy") f = 1 }
                   END { exit !f }' "$HAPROXY_CFG"
}
while IFS= read -r route; do
    [ -n "$route" ] || continue
    target="${route#*->}"
    target="${target%% *}"
    host="${target%:*}"
    port="${target##*:}"
    echo "route $route"
    case "$route" in
        *" proxy-v2")
            if ! haproxy_binds "$target"; then
                if listening "$host" "$port"; then
                    echo "  something else listens on $target: give haproxy's accept-proxy bind another port"
                fi
                echo "  haproxy: add 'bind $target accept-proxy' to frontend xray_in," \
                     "then haproxy -c -f $HAPROXY_CFG && systemctl reload haproxy"
            elif ! listening "$host" "$port"; then
                echo "  $HAPROXY_CFG binds $target, but nothing listens there yet: reload haproxy"
            fi ;;
        *)
            if ! listening "$host" "$port"; then
                echo "  nothing listens on $target yet"
            fi ;;
    esac
done <<<"$routes"
if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
    echo "ufw is active: allow each route's port (ufw allow <port>/tcp)"
fi
