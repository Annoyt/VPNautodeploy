#!/usr/bin/env bash
# Install the daily encrypted service backup on ENTRY (AGENTS.md §58,
# docs/runbooks/backup_restore.md). Run as root from the rsync'd checkout:
#
#     sudo /opt/vpn-bot/scripts/install_service_backup.sh --recipient age1…   # the first time
#     sudo /opt/vpn-bot/scripts/install_service_backup.sh                     # after the script changes
#     sudo /opt/vpn-bot/scripts/install_service_backup.sh --remove
#
# The timer runs as root, so it runs a root-owned COPY of scripts/service_backup.py
# in /usr/local/lib/nekovpn-backup, never the file in /opt/vpn-bot: that tree
# belongs to the deploy account (AGENTS.md §36). Re-run after the script changes.
#
# --recipient takes the PUBLIC age key (age1…; on the owner's machine:
# ~/.config/nekovpn-signing/nekovpn-backup-age.pub). A private key
# (AGE-SECRET-KEY-…) is refused and never echoed: it must not be on a server.
# Replacing an installed key needs --force: archives made with the old key open
# only with the old private key.
#
# Needs age and zstd on entry (apt-get install age zstd) and a bot image with
# scripts/backup_send.py (deploy the bot first). Tells the bot the timer is
# installed, so the alert backup:stale starts watching; --remove tells it to stop.
set -euo pipefail

LIB_DIR="${BACKUP_LIB_DIR:-/usr/local/lib/nekovpn-backup}"
UNIT_DIR="${BACKUP_UNIT_DIR:-/etc/systemd/system}"
CONF_DIR="${BACKUP_CONF_DIR:-/etc/nekovpn-backup}"
SYSTEMCTL="${SYSTEMCTL:-systemctl}"
BOT_CONTAINER="${BACKUP_BOT_CONTAINER:-vpn-bot}"
SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SENDER=/app/scripts/backup_send.py
RCPT_FILE="$CONF_DIR/recipients.txt"
KEY_RE='^age1[02-9ac-hj-np-z]{58}$'

die() { echo "ERROR: $*" >&2; exit 1; }

if [ "$(id -u)" != "0" ] && [ "${BACKUP_ALLOW_NONROOT:-0}" != "1" ]; then
    die "run as root"
fi

RECIPIENT=""
FORCE=0
REMOVE=0
while [ $# -gt 0 ]; do
    case "$1" in
        --recipient)
            [ $# -ge 2 ] || die "--recipient needs the public key (age1…)"
            RECIPIENT="$2"
            shift 2 ;;
        --force) FORCE=1; shift ;;
        --remove) REMOVE=1; shift ;;
        # The argument itself is not echoed: it may be a pasted secret key.
        *) die "unknown argument (see the header of this script)" ;;
    esac
done

if [ "$REMOVE" = "1" ]; then
    "$SYSTEMCTL" disable --now nekovpn-backup.timer >/dev/null 2>&1 || true
    rm -f "$UNIT_DIR/nekovpn-backup.service" "$UNIT_DIR/nekovpn-backup.timer"
    rm -rf "$LIB_DIR"
    "$SYSTEMCTL" daemon-reload
    if docker exec "$BOT_CONTAINER" python3 "$SENDER" forget >/dev/null 2>&1; then
        echo "the bot forgot the backup state: the alert backup:stale is off"
    else
        echo "WARN: could not tell the bot; backup:stale pages until:" \
             "docker exec $BOT_CONTAINER python3 $SENDER forget" >&2
    fi
    echo "removed (kept: $CONF_DIR with the public key, /var/lib/nekovpn-backup/archives)"
    exit 0
fi

for f in "$SRC/scripts/service_backup.py" "$SRC/systemd/nekovpn-backup.service" \
         "$SRC/systemd/nekovpn-backup.timer"; do
    [ -f "$f" ] || die "missing: $f"
done
for tool in age zstd python3; do
    command -v "$tool" >/dev/null 2>&1 || die "$tool is not installed: apt-get install age zstd"
done

if [ -n "$RECIPIENT" ]; then
    case "$RECIPIENT" in
        *[Aa][Gg][Ee]-[Ss][Ee][Cc][Rr][Ee][Tt]-[Kk][Ee][Yy]-*)
            die "that is a SECRET age key: it must never be on a server. It is now in this" \
                "shell's history: make a new key pair on the owner's machine" \
                "(docs/runbooks/backup_restore.md) and remove the line from ~/.bash_history" ;;
    esac
    [[ "$RECIPIENT" =~ $KEY_RE ]] || die "--recipient is not an age public key (age1…, 62 characters)"
    if [ -f "$RCPT_FILE" ] && ! grep -qxF "$RECIPIENT" "$RCPT_FILE" && [ "$FORCE" != "1" ]; then
        die "$RCPT_FILE holds another key. Replacing it needs --force: archives made" \
            "with the old key open only with the old private key"
    fi
    install -d -m 0755 -o root -g root "$CONF_DIR"
    tmp="$(mktemp "$CONF_DIR/.recipients.XXXXXX")"
    {
        echo "# age public key of the NekoVPN service backup (AGENTS.md §58)."
        echo "# The private key is on the owner's machine only."
        echo "$RECIPIENT"
    } > "$tmp"
    chmod 0644 "$tmp"
    mv -f "$tmp" "$RCPT_FILE"
fi

# shellcheck disable=SC2088  # the hint names a path on the owner's machine
[ -f "$RCPT_FILE" ] || die "no $RCPT_FILE: pass --recipient age1… (the public key," \
    "~/.config/nekovpn-signing/nekovpn-backup-age.pub on the owner's machine)"
if grep -qi 'AGE-SECRET-KEY-' "$RCPT_FILE"; then
    die "$RCPT_FILE holds a SECRET age key: delete the file, only the public key belongs here"
fi
grep -Eq "$KEY_RE" "$RCPT_FILE" || die "$RCPT_FILE has no age public key"
if grep -Ev '^[[:space:]]*(#|$)' "$RCPT_FILE" | grep -Evq "$KEY_RE"; then
    die "$RCPT_FILE has a line that is not an age public key"
fi

docker exec "$BOT_CONTAINER" test -f "$SENDER" \
    || die "the bot image has no $SENDER: deploy the bot first (./scripts/deploy_to_entry.sh)"

install -d -m 0755 -o root -g root "$LIB_DIR"
install -m 0755 -o root -g root "$SRC/scripts/service_backup.py" "$LIB_DIR/service_backup.py"
install -m 0644 -o root -g root "$SRC/systemd/nekovpn-backup.service" "$UNIT_DIR/nekovpn-backup.service"
install -m 0644 -o root -g root "$SRC/systemd/nekovpn-backup.timer" "$UNIT_DIR/nekovpn-backup.timer"
"$SYSTEMCTL" daemon-reload
"$SYSTEMCTL" enable --now nekovpn-backup.timer
if ! docker exec "$BOT_CONTAINER" python3 "$SENDER" installed >/dev/null 2>&1; then
    echo "WARN: could not tell the bot; until a backup reports, backup:stale stays quiet" >&2
fi
echo "installed: $LIB_DIR/service_backup.py, timer nekovpn-backup.timer (daily 01:40 UTC)"
echo "list what goes in:  /usr/bin/python3 $LIB_DIR/service_backup.py --dry-run"
echo "the first backup:   systemctl start nekovpn-backup.service; journalctl -u nekovpn-backup -n 40"
