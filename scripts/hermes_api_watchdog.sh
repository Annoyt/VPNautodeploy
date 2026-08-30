#!/bin/bash
# Restart hermes-api if the API server stops answering on :4097.
#
# NOT on the first miss: a long agent tool-call (terminal timeout is 300s)
# can legitimately hold the gateway busy, and an instant restart kills the
# in-flight /ai request (observed 2026-08-30: watchdog killed a running
# agent loop). Restart only after 2 consecutive misses with no in-flight
# request, or after 5 consecutive misses (~10 min with the 2-min timer)
# regardless — a truly hung gateway can keep connections ESTABLISHED too.
#
# Deployed to entry:/usr/local/bin/hermes-api-watchdog.sh, driven by
# hermes-api-watchdog.timer (every 2 min). State lives in /run (tmpfs,
# resets on reboot).
STATE=/run/hermes-watchdog.fails

key=$(cat /root/.hermes_api_key 2>/dev/null)
code=$(curl -s -o /dev/null -w "%{http_code}" --max-time 15 \
  http://127.0.0.1:4097/v1/models -H "Authorization: Bearer ${key}")
if [ -n "$code" ] && [ "$code" != "000" ]; then
  rm -f "$STATE"
  exit 0
fi

fails=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$fails" > "$STATE"

# An ESTABLISHED client connection on :4097 = an /ai request mid-flight
# (the bot holds the HTTP connection open for the whole agent loop).
inflight=$(ss -Htn state established '( sport = :4097 )' 2>/dev/null | wc -l)

if [ "$fails" -ge 5 ] || { [ "$fails" -ge 2 ] && [ "$inflight" -eq 0 ]; }; then
  logger -t hermes-watchdog \
    "no response from :4097 (fails=${fails}, inflight=${inflight}) — restarting hermes-api"
  rm -f "$STATE"
  systemctl restart hermes-api
else
  logger -t hermes-watchdog \
    "no response from :4097 (fails=${fails}, inflight=${inflight}) — deferring restart"
fi
