#!/usr/bin/env bash
# Deploy the Hermes HOST-side helpers from the repo to entry: the API
# watchdog, the free-model billing guard and their four systemd units.
#
# These run on the entry host itself, not in the vpn-bot container: the
# guard rewrites /root/.hermes/config.yaml and restarts hermes-api, which
# only root on the host can do. The repo is the source of truth — edit
# here and redeploy; never hand-edit /usr/local/bin on entry (that is how
# the watchdog drifted before scripts/hermes_api_watchdog.sh existed).
#
# Usage (from the repo root, on the dev machine):
#   ./scripts/deploy_hermes_host.sh               # ssh alias 'entry'
#   ENTRY=other-alias ./scripts/deploy_hermes_host.sh
#   FORCE=1 ./scripts/deploy_hermes_host.sh       # overwrite a hand-edited remote copy
#
# Idempotent: rsync ships only changed bytes, daemon-reload is harmless,
# `enable --now` is a no-op for an already-enabled timer. Ends with a
# --dry-run of the guard on entry so the first plan is visible right
# away (reads only, changes nothing, sends nothing).
#
# Clobber guard: rsync -a preserves mtimes, so after a deploy the remote
# copy carries the repo file's mtime. A remote script whose content
# differs AND whose mtime is NEWER than the local file was edited on the
# host after the last deploy — the diff is shown and the deploy aborts
# unless FORCE=1 (port the change into the repo first).
set -euo pipefail

cd "$(dirname "$0")/.."
ENTRY="${ENTRY:-entry}"
FORCE="${FORCE:-}"

for f in scripts/hermes_api_watchdog.sh scripts/hermes_model_guard.py \
         systemd/hermes-api-watchdog.service systemd/hermes-api-watchdog.timer \
         systemd/hermes-model-guard.service systemd/hermes-model-guard.timer; do
    [ -f "$f" ] || { echo "ERROR: $f missing — run from the repo root" >&2; exit 1; }
done

python3 -m py_compile scripts/hermes_model_guard.py

# check_drift LOCAL REMOTE — abort when the remote copy is newer AND different.
check_drift() {
    local local_f="$1" remote_f="$2" remote_info local_mtime remote_mtime remote_sha local_sha
    remote_info=$(ssh "$ENTRY" "if [ -f '$remote_f' ]; then stat -c %Y '$remote_f'; sha256sum '$remote_f' | cut -d' ' -f1; fi")
    [ -n "$remote_info" ] || return 0            # nothing deployed yet
    remote_mtime=$(echo "$remote_info" | sed -n 1p)
    remote_sha=$(echo "$remote_info" | sed -n 2p)
    local_sha=$(sha256sum "$local_f" | cut -d' ' -f1)
    [ "$remote_sha" != "$local_sha" ] || return 0   # identical, nothing to do
    local_mtime=$(stat -c %Y "$local_f")
    if [ "$remote_mtime" -gt "$local_mtime" ]; then
        echo "!!  $ENTRY:$remote_f is NEWER than $local_f and differs (hand-edited on the host?)" >&2
        echo "    diff (remote -> repo):" >&2
        ssh "$ENTRY" "cat '$remote_f'" | diff -u --label "$ENTRY:$remote_f" --label "$local_f" - "$local_f" >&2 || true
        if [ -z "$FORCE" ]; then
            echo "    Refusing to clobber it. Port the change into the repo, or rerun with FORCE=1." >&2
            exit 1
        fi
        echo "    FORCE=1 set — overwriting." >&2
    fi
}

echo "==> Drift check against $ENTRY"
check_drift scripts/hermes_api_watchdog.sh /usr/local/bin/hermes-api-watchdog.sh
check_drift scripts/hermes_model_guard.py  /usr/local/bin/hermes_model_guard.py

echo "==> Syncing scripts to $ENTRY:/usr/local/bin"
rsync -a --chmod=F0755 scripts/hermes_api_watchdog.sh "$ENTRY:/usr/local/bin/hermes-api-watchdog.sh"
rsync -a --chmod=F0755 scripts/hermes_model_guard.py  "$ENTRY:/usr/local/bin/hermes_model_guard.py"

echo "==> Syncing systemd units to $ENTRY:/etc/systemd/system"
rsync -a --chmod=F0644 \
    systemd/hermes-api-watchdog.service systemd/hermes-api-watchdog.timer \
    systemd/hermes-model-guard.service  systemd/hermes-model-guard.timer \
    "$ENTRY:/etc/systemd/system/"

echo "==> daemon-reload + enable timers"
ssh "$ENTRY" 'systemctl daemon-reload \
    && systemctl enable --now hermes-api-watchdog.timer hermes-model-guard.timer \
    && systemctl list-timers --no-pager --all | grep hermes'

echo "==> Guard dry-run on $ENTRY (reads only, sends nothing; exit 1/2 = see plan above)"
ssh "$ENTRY" '/usr/local/bin/hermes_model_guard.py --dry-run' \
    || echo "    guard --dry-run exited $? (1 = critical condition, 2 = could not check)"

echo "==> Done. Logs: ssh $ENTRY journalctl -u hermes-model-guard --no-pager -n 50"
