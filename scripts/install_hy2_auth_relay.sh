#!/usr/bin/env bash
# Install the hy2 auth relay on EXIT (IMPROVEMENT_PLAN F, A6; AGENTS.md §56;
# docs/runbooks/hy2_auth_relay.md). Run as root from a copy of the repo:
#
#     bash install_hy2_auth_relay.sh            # install or update, (re)start
#     bash install_hy2_auth_relay.sh --remove   # stop and remove
#
# The unit runs a root-owned COPY of scripts/hy2_auth_relay.py in
# /usr/local/lib/nekovpn-hy2-relay, never a file another account can change
# (AGENTS.md §36). Re-run after the script changes, while the bot answers: a
# restart empties the relay's memory, and the bot's key list refills it.
#
# It never touches hysteria. Pointing hysteria's auth URL at the relay is a
# separate step (the runbook), and --remove refuses while a hysteria config
# still points at the relay: hysteria would refuse every new hy2 connection.
set -euo pipefail

LIB_DIR="${RELAY_LIB_DIR:-/usr/local/lib/nekovpn-hy2-relay}"
UNIT_DIR="${RELAY_UNIT_DIR:-/etc/systemd/system}"
HYSTERIA_DIR="${RELAY_HYSTERIA_DIR:-/etc/hysteria}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
WAIT_S="${RELAY_WAIT_S:-1}"     # tests: 0
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
UNIT=hy2-auth-relay.service

if [ "$(id -u)" != "0" ] && [ "${RELAY_ALLOW_NONROOT:-0}" != "1" ]; then
    echo "run as root" >&2
    exit 1
fi

# The hysteria configs whose auth URL is a loopback http one: the relay's.
pointing() {
    grep -lE '^[^#]*http://(127\.[0-9.]+|\[::1\]|localhost)(:[0-9]+)?/api/hy2t?/auth' \
        "$HYSTERIA_DIR"/*.yaml 2>/dev/null || true
}

if [ "${1:-}" = "--remove" ]; then
    users="$(pointing)"
    if [ -n "$users" ]; then
        echo "hysteria still asks the relay ($(tr '\n' ' ' <<<"$users")): point it back at the bot first" >&2
        echo "(docs/runbooks/hy2_auth_relay.md, rollback), then --remove" >&2
        exit 1
    fi
    "$SYSTEMCTL" disable --now "$UNIT" >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/$UNIT"
    rm -rf "$LIB_DIR"
    "$SYSTEMCTL" daemon-reload
    echo "removed"
    exit 0
fi
if [ $# -gt 0 ]; then
    echo "usage: $0 [--remove]" >&2
    exit 2
fi

for f in "$SRC/scripts/hy2_auth_relay.py" "$SRC/systemd/$UNIT"; do
    [ -f "$f" ] || { echo "missing: $f" >&2; exit 1; }
done
# A copy that does not even compile would leave hysteria without answers.
python3 -I -c 'import sys; compile(open(sys.argv[1], "rb").read(), sys.argv[1], "exec")' \
    "$SRC/scripts/hy2_auth_relay.py"

install -d -m 0755 -o root -g root "$LIB_DIR"
install -m 0644 -o root -g root "$SRC/scripts/hy2_auth_relay.py" "$LIB_DIR/hy2_auth_relay.py.new"
mv -f "$LIB_DIR/hy2_auth_relay.py.new" "$LIB_DIR/hy2_auth_relay.py"
install -m 0644 -o root -g root "$SRC/systemd/$UNIT" "$UNIT_DIR/$UNIT"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable "$UNIT" >/dev/null
"$SYSTEMCTL" restart "$UNIT"

# Type=simple is "active" the moment it forks: a relay that refuses its
# config exits a moment later and restarts every 2 s. It must still be the
# same process seconds later.
sleep "$WAIT_S"
pid="$("$SYSTEMCTL" show -p MainPID --value "$UNIT" 2>/dev/null || true)"
sleep "$((WAIT_S * 2))"
if [ "$("$SYSTEMCTL" is-active "$UNIT" 2>/dev/null || true)" != "active" ] || [ -z "$pid" ] \
        || [ "$pid" = 0 ] || [ "$("$SYSTEMCTL" show -p MainPID --value "$UNIT" 2>/dev/null || true)" != "$pid" ]; then
    echo "$UNIT does not stay up: journalctl -u $UNIT -n 30" >&2
    exit 1
fi
echo "installed: $LIB_DIR/hy2_auth_relay.py, $UNIT running"
users="$(pointing)"
if [ -n "$users" ]; then
    echo "hysteria asks the relay: $(tr '\n' ' ' <<<"$users")"
else
    echo "hysteria does not ask the relay yet (docs/runbooks/hy2_auth_relay.md)"
fi
