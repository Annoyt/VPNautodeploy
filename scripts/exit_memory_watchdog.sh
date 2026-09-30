#!/bin/bash
# Exit-node memory watchdog. Installed on the EXIT host as
# /usr/local/bin/memory_watchdog.sh and run by /etc/cron.d/memory-watchdog
# (scripts/exit_memory_watchdog.cron) every 5 minutes.
#
# Why it looks like this (rewritten 2026-09-30)
# ---------------------------------------------
# The previous version force-rebooted the host whenever MemAvailable stayed
# under 100 MB for two checks. It did that on 2026-09-13, -17 and -25, and
# every reboot dropped every VPN connection and every call at once. It was
# wrong three ways:
#   * it ignored swap: the host has 1.5 GB of it, >1 GB free at the time.
#     MemAvailable sitting at 140-190 MB is this host's normal (x-ui ~200 MB,
#     xray ~100 MB of 930 MB RAM), not an emergency;
#   * a strike only reset above 150 MB, so with memory hovering 100-150 MB one
#     old strike lingered for days and the next dip rebooted — "two in a row"
#     was never in a row;
#   * `reboot --force` skips the clean shutdown — the panel's SQLite x-ui.db
#     is written while it dies.
#
# What counts as trouble now (either one):
#   * headroom = MemAvailable + SwapFree < HEADROOM_MIN_KB — about to OOM;
#   * PSI memory "full" avg60 >= PSI_FULL_MAX percent — thrashing: for that
#     share of the last minute EVERY runnable task was stalled on memory
#     (normal here: ~0.3 %).
# STRIKES consecutive bad checks are needed; any good check resets the count.
#
# Escalation, cheapest first:
#   1. `docker restart 3x-ui` — x-ui + xray are the bulk of the memory. Hysteria
#      runs on the host, so hy2 calls survive; xray clients reconnect in seconds.
#      `restart`, never `compose up`: exit's compose file has diverged from the
#      repo (AGENTS.md §23) and must not be re-evaluated by a watchdog.
#   2. Bad again within ESCALATE_WINDOW_SEC of that restart -> graceful
#      `systemctl reboot` — at most once per REBOOT_MIN_INTERVAL_SEC; inside
#      that interval it restarts 3x-ui again instead (no reboot loops).
# Every action is posted to the admin forum topic (an invisible auto-action is
# indistinguishable from a bug). A host that FREEZES is out of reach here —
# cron doesn't run — that is the VPS provider's job (see 2026-09-12: 69 min).
#
# Test hooks (all optional): MEMINFO, PSI_FILE, STATE_DIR, NOTIFY_ENV,
# NOW_EPOCH, DRY_RUN=1 (log what it would do, touch nothing, send nothing).

set -uo pipefail

: "${HEADROOM_MIN_KB:=262144}"          # 256 MB of RAM+swap left
: "${PSI_FULL_MAX:=30}"                 # % of time every task stalled on memory
: "${STRIKES:=2}"                       # consecutive bad checks (x 5 min)
: "${ESCALATE_WINDOW_SEC:=3600}"        # bad again this soon after a restart -> reboot
: "${REBOOT_MIN_INTERVAL_SEC:=21600}"   # never two reboots within 6 h
: "${CONTAINER:=3x-ui}"
: "${MEMINFO:=/proc/meminfo}"
: "${PSI_FILE:=/proc/pressure/memory}"
: "${STATE_DIR:=/var/lib/exit-memory-watchdog}"
: "${NOTIFY_ENV:=/opt/vpn-bot/.env}"
: "${DRY_RUN:=0}"

TAG="memory-watchdog"
STATE="$STATE_DIR/state"
now="${NOW_EPOCH:-$(date +%s)}"

log() { logger -t "$TAG" "$*"; }

meminfo_kb() { awk -v k="$1:" '$1 == k {print $2; exit}' "$MEMINFO" 2>/dev/null; }

env_value() {  # KEY=VALUE parse; values never reach a log line
    awk -F= -v k="$1" '$1 == k {sub(/^[^=]*=/, ""); gsub(/^["'\'']|["'\'']$/, ""); print; exit}' \
        "$NOTIFY_ENV" 2>/dev/null
}

notify() {  # HTML text -> admin topic; failures are logged, never fatal
    local text="$1" token chat topic
    if [[ "$DRY_RUN" == 1 ]]; then log "DRY_RUN notify: $text"; return 0; fi
    token="$(env_value BOT_TOKEN)"; chat="$(env_value FORUM_GROUP_ID)"; topic="$(env_value TOPIC_AI)"
    if [[ -z "$token" || -z "$chat" ]]; then log "notify skipped: no BOT_TOKEN/FORUM_GROUP_ID in $NOTIFY_ENV"; return 0; fi
    local args=(-s -o /dev/null -w '%{http_code}' --max-time 10
                --data-urlencode "chat_id=$chat" --data-urlencode "parse_mode=HTML"
                --data-urlencode "text=$text")
    [[ -n "$topic" ]] && args+=(--data-urlencode "message_thread_id=$topic")
    # stderr dropped: curl error text can carry the URL, and the URL carries the token.
    local code
    code="$(curl "${args[@]}" "https://api.telegram.org/bot${token}/sendMessage" 2>/dev/null)" || code="000"
    log "notify: HTTP $code"
}

# ---- read the signals; a blind watchdog never acts ------------------------
mem_avail="$(meminfo_kb MemAvailable)"; swap_free="$(meminfo_kb SwapFree)"
if ! [[ "$mem_avail" =~ ^[0-9]+$ && "$swap_free" =~ ^[0-9]+$ ]]; then
    log "cannot read $MEMINFO — no decision this run"
    exit 0
fi
headroom=$((mem_avail + swap_free))

psi="0"
if [[ -r "$PSI_FILE" ]]; then
    psi="$(awk '$1 == "full" {for (i = 2; i <= NF; i++) if ($i ~ /^avg60=/) {sub(/^avg60=/, "", $i); print $i}}' "$PSI_FILE")"
    [[ "$psi" =~ ^[0-9]+(\.[0-9]+)?$ ]] || psi="0"
fi
psi_int="${psi%%.*}"

bad=0; why=""
if (( headroom < HEADROOM_MIN_KB )); then
    bad=1; why="RAM+swap left $((headroom / 1024)) MB < $((HEADROOM_MIN_KB / 1024)) MB"
fi
if (( psi_int >= PSI_FULL_MAX )); then
    bad=1; why="${why:+$why; }PSI full avg60 ${psi}% >= ${PSI_FULL_MAX}%"
fi

# ---- state -----------------------------------------------------------------
strikes=0; last_restart=0; last_reboot=0
if [[ -r "$STATE" ]]; then
    while IFS='=' read -r k v; do
        [[ "$v" =~ ^[0-9]+$ ]] || continue
        case "$k" in
            strikes) strikes="$v" ;; last_restart) last_restart="$v" ;; last_reboot) last_reboot="$v" ;;
        esac
    done < "$STATE"
fi
save_state() {
    [[ "$DRY_RUN" == 1 ]] && return 0
    mkdir -p "$STATE_DIR"
    printf 'strikes=%s\nlast_restart=%s\nlast_reboot=%s\n' "$strikes" "$last_restart" "$last_reboot" \
        > "$STATE.tmp" && mv "$STATE.tmp" "$STATE"
}

if (( bad )); then strikes=$((strikes + 1)); else strikes=0; fi

# One line every run: this is the trend log the next investigation reads.
log "MemAvailable=${mem_avail}kB SwapFree=${swap_free}kB headroom=${headroom}kB psi_full_avg60=${psi}% strikes=${strikes}/${STRIKES}${why:+ BAD: $why}"

if (( strikes < STRIKES )); then
    save_state
    exit 0
fi

numbers="RAM свободно $((mem_avail / 1024)) МБ, swap свободно $((swap_free / 1024)) МБ, PSI full ${psi}%"
restart_recent=0
(( last_restart > 0 && now - last_restart < ESCALATE_WINDOW_SEC )) && restart_recent=1
reboot_allowed=1
(( last_reboot > 0 && now - last_reboot < REBOOT_MIN_INTERVAL_SEC )) && reboot_allowed=0

if (( restart_recent && reboot_allowed )); then
    log "CRITICAL: $why — a 3x-ui restart $(( (now - last_restart) / 60 )) min ago did not hold; graceful reboot"
    notify "🔁 <b>exit: перезагружаю хост</b> — память не отпустило и после перезапуска 3x-ui $(( (now - last_restart) / 60 )) мин назад. ${numbers}. Все соединения переподключатся через ~1 мин."
    # Written BEFORE the reboot: the next bad episode after boot starts from
    # step 1 again, and the 6 h reboot guard holds across the reboot.
    strikes=0; last_restart=0; last_reboot="$now"
    save_state
    if [[ "$DRY_RUN" == 1 ]]; then log "DRY_RUN: would run systemctl reboot"; exit 0; fi
    sync
    systemctl reboot
    exit 0
fi

if (( restart_recent )); then
    log "CRITICAL: $why — reboot suppressed (last one $(( (now - last_reboot) / 60 )) min ago), restarting $CONTAINER again"
    extra=" Перезагрузку НЕ делаю: предыдущая была $(( (now - last_reboot) / 60 )) мин назад — нужен человек."
else
    log "WARN: $why for ${STRIKES} checks — restarting $CONTAINER"
    extra=""
fi
notify "🧠 <b>exit: мало памяти — перезапускаю ${CONTAINER}</b>. ${numbers}. Reality/WS/STLS переподключатся за секунды, hy2 и звонки по нему не затронуты.${extra}"
strikes=0; last_restart="$now"
save_state
if [[ "$DRY_RUN" == 1 ]]; then log "DRY_RUN: would run docker restart $CONTAINER"; exit 0; fi
if docker restart "$CONTAINER" >/dev/null 2>&1; then
    log "$CONTAINER restarted"
else
    log "ERROR: docker restart $CONTAINER failed"
    notify "⚠️ <b>exit: docker restart ${CONTAINER} не удался</b> — посмотри хост."
fi
exit 0
