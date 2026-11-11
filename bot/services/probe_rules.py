"""What the probe rows say — one definition for every reader
(IMPROVEMENT_PLAN F, B5).

The pager (``alert_manager.check_protocol_probe_down``), DPIMonitor's probe
rules, ``/protocols`` and ``/sos`` (``admin/ops.read_probe_state``) and the
agent's ``scripts/protocol_healthcheck.py`` used to carry their own copies
of these numbers, and three different ideas of "degraded": below 25 % over
three runs, below 35 % over three runs, below half of the last run. An
alert, the agent's answer and /protocols could then disagree about the same
rows. They all read this module now.

Stdlib only, no ``bot`` imports: the healthcheck script runs on the entry
HOST, outside the container and without the bot's packages, and loads this
file by its path.

The rows are ``outbound_health``: one per (protocol, target) per run, every
15 min, written by ``health_checker.HealthChecker``. A window is the newest
``PROBE_RUNS`` runs of one protocol, newest first.
"""

from typing import Iterable, Optional, Sequence, Tuple

# The ten sites each protocol is probed against (HealthChecker reads this).
# All ten answer HEAD through a foreign exit: until 2026-11 two of them never
# did — vk.com answers 418 to every probe, sberbank.ru's chain is issued by
# the Russian national CA the probe does not trust (both checked on prod
# 2026-11-10) — so "8 of 10" was the everyday norm and every reader had to
# know it. Russian sites reach users direct anyway (RU-direct routing).
PROBE_TARGETS = (
    'yandex.ru',
    'rutube.ru',
    'youtube.com',
    'google.com',
    'facebook.com',
    'telegram.org',
    'github.com',
    'anthropic.com',
    'discord.com',
    'x.com',
)

PROBE_DOMAINS = len(PROBE_TARGETS)   # rows per protocol per run
PROBE_RUNS = 3              # a window = the newest three runs
PROBE_MIN_SAMPLES = 15      # fewer rows than this = too thin to judge
PROBE_WINDOW_H = 3          # only rows this recent are read
PROBE_STALE_MIN = 45        # newest row older than this = the probes stopped
# DEGRADED: the tunnel comes up but almost nothing passes — fewer than a
# quarter of the window's probes succeed, over at least 25 rows.
PROBE_DEGRADED_OK_RATIO = 0.25
PROBE_DEGRADED_MIN_SAMPLES = 25

# The DE reserve node, probed since 2026-11 (B6). It is not a cascade
# protocol: it never takes part in "every protocol dark at once" (exit down
# leaves it alive by design), and DPIMonitor never demotes it.
RESERVE_TAG = 'de'

VERDICT_DARK = 'dark'
VERDICT_DEGRADED = 'degraded'
VERDICT_OK = 'ok'
VERDICT_THIN = 'thin'


def is_alive(status, latency) -> bool:
    """The tunnel carried something: the probe succeeded, or failed AFTER a
    round trip (a latency means bytes came back — an HTTP 418 from a site
    that blocks the exit). Only "nothing ever connected" leaves both empty,
    which is what the 2026-09-01 outage rows looked like."""
    return latency is not None or status == 'ok'


def judge_window(rows: Sequence[Tuple]) -> dict:
    """The verdict on one protocol's window.

    ``rows``: ``(status, latency_ms, ...)`` tuples, newest first, at most
    ``PROBE_RUNS * PROBE_DOMAINS`` of them. Returns ``{'verdict', 'n', 'ok',
    'alive'}``, the verdict being:

    * ``thin``     — fewer than ``PROBE_MIN_SAMPLES`` rows: no verdict;
    * ``dark``     — not a single live row: the tunnel does not come up;
    * ``degraded`` — at least ``PROBE_DEGRADED_MIN_SAMPLES`` rows and fewer
      than ``PROBE_DEGRADED_OK_RATIO`` of them ok;
    * ``ok``       — anything else.
    """
    n = len(rows)
    ok = sum(1 for r in rows if r[0] == 'ok')
    alive = sum(1 for r in rows if is_alive(r[0], r[1]))
    if n < PROBE_MIN_SAMPLES:
        verdict = VERDICT_THIN
    elif alive == 0:
        verdict = VERDICT_DARK
    elif n >= PROBE_DEGRADED_MIN_SAMPLES and ok / n < PROBE_DEGRADED_OK_RATIO:
        verdict = VERDICT_DEGRADED
    else:
        verdict = VERDICT_OK
    return {'verdict': verdict, 'n': n, 'ok': ok, 'alive': alive}


def last_run_dark(rows: Sequence[Tuple]) -> Optional[bool]:
    """The early sign, before a window turns dark or degraded: the newest
    FULL run (``PROBE_DOMAINS`` rows) had not a single ok. None when the
    newest run is not complete. Not a verdict of its own — readers show it
    as "последний прогон без единого ответа", never as degradation."""
    if len(rows) < PROBE_DOMAINS:
        return None
    return not any(r[0] == 'ok' for r in rows[:PROBE_DOMAINS])


def degraded_threshold_text() -> str:
    """How the readers word the rule: ``ok < 25% за 3 прогона``."""
    return f"ok < {PROBE_DEGRADED_OK_RATIO:.0%} за {PROBE_RUNS} прогона"


def operator_disabled(cascade_setting) -> frozenset:
    """Protocols the operator switched off in the cascade editor
    (``app_settings.cascade_protocol_order`` = ``[{name, enabled}, …]``,
    JSON text or already parsed). The pager and the monitor leave them
    alone (B4, problem 7): Turbo has been off since 2026-09-21 while its
    instance and probe stay up, and its dark probe used to page critical
    and wake the agent every two hours. A bare list of names (the legacy
    format) means everything is on; anything unreadable disables nothing.
    """
    import json
    data = cascade_setting
    if isinstance(data, (str, bytes)):
        try:
            data = json.loads(data)
        except (TypeError, ValueError):
            return frozenset()
    if not isinstance(data, list):
        return frozenset()
    off = set()
    for item in data:
        if isinstance(item, dict) and isinstance(item.get('name'), str):
            if not bool(item.get('enabled', True)):
                off.add(item['name'])
    return frozenset(off)


def summary_line(windows: Iterable[Tuple[str, dict]], disabled=frozenset()) -> str:
    """``reality ok 24/30 · hy2 0/30 (живых 0) · …`` — every protocol's
    window on one line, so an alert about one protocol shows the others
    next to it (B3). ``windows``: ``(tag, judge_window(...))`` pairs."""
    parts = []
    for tag, w in windows:
        if tag in disabled:
            parts.append(f"{tag} выключен")
            continue
        part = f"{tag} ok {w['ok']}/{w['n']}"
        if w['verdict'] == VERDICT_DARK:
            part += " (живых 0)"
        elif w['verdict'] == VERDICT_THIN:
            part += " (мало строк)"
        parts.append(part)
    return ' · '.join(parts)
