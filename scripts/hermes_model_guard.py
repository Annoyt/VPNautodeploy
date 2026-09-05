#!/usr/bin/env python3
"""Free-model billing guard for the Hermes agent (runs on the ENTRY host).

Hermes (the /ai backend) talks to OpenRouter with a PAID-tier key but is
pinned to ':free' model ids, so every call costs $0. The operator's fear:
a model quietly stops being free — OpenRouter either drops the ':free'
id (404 is the normal way a free model "stops being free") or puts a
price on it — and the agent starts eating the balance with nobody the
wiser. Nothing in Hermes itself would complain: the calls just succeed
and get billed.

This oneshot (systemd timer, every 30 min) closes that gap. In order:

  a) KEY USAGE   GET /api/v1/key -> data.usage (cumulative USD). The
     previous value lives in state.json; growth > $0.001 since the last
     run is CRITICAL regardless of cause. This is the check that catches
     *any* charging, including the kinds the pricing check can't explain.
  b) PRICING     GET /api/v1/models; model.default and every
     fallback_providers[].model from config.yaml is classified
     free / paid / missing. Pricing values are STRINGS ("0") — parsed as
     floats; anything unparsable counts as paid (fail closed).
  c) DECISION    primary free -> nothing. Primary paid/missing and a free
     fallback exists -> PROMOTE: config.yaml is rewritten with the first
     free fallback as default and the old primary REMOVED from the chain
     (it lives on in the backup) — the guard never leaves a paid path in
     the chain it just cleaned, and a 404 id is pure noise. A timestamped
     backup is kept next to the file and hermes-api restarted (unless an
     /ai request is in flight — then the restart is deferred to the next
     run, at most RESTART_DEFER_MAX times; killing a running agent loop
     is worse than 30 more minutes on the old model). Nothing free left
     -> CRITICAL and NO rewrite: Hermes failing closed on a dead model id
     is cheaper than Hermes working fine on a paid one.
     A MISSING primary is promoted only when it was already missing on
     the previous run: Hermes itself walks fallback_providers on 4xx, so
     nothing is burning, and one transient delisting must not rewrite
     the operator's config. A PAID primary is promoted at once.
  d) A fallback (not the primary) turning paid/missing is a WARNING — the
     chain is thinner than the operator thinks. Fired on the transition
     only (state.json remembers the last classification), otherwise a
     deliberately-kept paid fallback would nag every 6 h forever.

Notifications go to the forum topic TOPIC_AI in FORUM_GROUP_ID with
BOT_TOKEN from /opt/vpn-bot/.env, through that file's HTTPS_PROXY
(api.telegram.org is RKN-blocked from entry). Never to the admin's PM —
operator rule: while the group is alive, admins are told in topics.
The same notification key is not repeated within 6 h (state.json).

A blind guard must never act: if /models or /key can't be fetched (or
the list comes back implausibly small), or config.yaml itself cannot be
read/parsed, NO pricing decision is made — otherwise a proxy hiccup
would read as "every model is missing" and either promote or scream.
Three consecutive blind runs raise a warning instead (naming the layer
that is blind), modelled on the API watchdog's "not on the first miss".
An unreadable config.yaml is NOT an exit-2-and-silence: it counts as a
blind run like an API failure does, so it reaches the topic too.

A message that could not be delivered is queued in state.json
("pending") and re-sent on the next run — never dropped. This matters
most right after a promotion: the config was rewritten and hermes-api
restarted, and the next run (primary now free) would never regenerate
that message.

Secrets: env files are parsed for KEY=VALUE only; values are never
logged or printed (also not in --dry-run), and every logged exception
text is passed through redact() because requests embeds the request URL
— which for Telegram contains the bot token — into its error messages.

Usage:
    hermes_model_guard.py [--dry-run] [--once] [--state-dir DIR]
                          [--hermes-env F] [--hermes-config F] [--bot-env F]

Exit codes: 0 = fine, 1 = a critical condition was seen (spending, no
free model left, or a PAID primary was just demoted — the balance may
have moved, check it), 2 = could not check. The timer keeps firing
regardless; the code is for `systemctl --failed` and the deploy script.
"""

import argparse
import copy
import html
import json
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Tuple

import requests
import yaml

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
HERMES_ENV = "/root/.hermes/.env"
HERMES_CONFIG = "/root/.hermes/config.yaml"
BOT_ENV = "/opt/vpn-bot/.env"
STATE_DIR = "/var/lib/hermes-guard"
HERMES_UNIT = "hermes-api.service"

# $0.001 — OpenRouter reports usage with ~7 decimals; a genuine :free call
# adds exactly 0, so anything above float noise means a billed request.
USAGE_GROWTH_THRESHOLD = 0.001
DEDUPE_WINDOW = timedelta(hours=6)
# Prune dedupe keys older than this so state.json can't grow forever.
DEDUPE_RETENTION = timedelta(days=7)
# Warn only after this many consecutive OpenRouter fetch failures
# (3 x 30 min = the guard has been blind for ~1.5 h).
API_FAIL_NOTIFY_AFTER = 3
# /models returns ~430 entries (2026-09). A response with fewer than this
# is a truncated/garbage body, not "the catalogue shrank" — treat as a
# failed fetch, never as "everything is missing".
MIN_MODELS_SANE = 50
HTTP_TIMEOUT = 30
# Undelivered messages kept in state.json for the next run (oldest dropped
# beyond this — a dead Telegram proxy must not grow the file forever).
PENDING_MAX = 20
# A restart is deferred while an /ai request is in flight, but not forever:
# after this many deferrals (x 30 min) it happens regardless — the paid
# primary is already gone from config.yaml, only the running process
# still uses it.
RESTART_DEFER_MAX = 3
HERMES_API_PORT = 4097

FREE, PAID, MISSING = "free", "paid", "missing"
CRITICAL, WARN = "critical", "warn"

logger = logging.getLogger("hermes-guard")


class GuardError(Exception):
    """Config/env problem that makes a check impossible (exit 2)."""


# --------------------------------------------------------------------------
# Secrets hygiene
# --------------------------------------------------------------------------

_SECRETS: List[str] = []


def register_secret(value: Optional[str]) -> None:
    """Remember a value so redact() can scrub it from any logged text."""
    if value and value not in _SECRETS:
        _SECRETS.append(value)


def redact(text: str) -> str:
    """Scrub every registered secret from text (used on exception strings).

    requests puts the full URL into ConnectionError/HTTPError messages,
    and the Telegram URL embeds the bot token; proxy URLs may carry
    user:pass. Longest first so a prefix of one secret never leaves the
    tail of a longer one exposed.
    """
    for secret in sorted(_SECRETS, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def parse_env_file(path: str, keys) -> Dict[str, str]:
    """Read KEY=VALUE lines for the requested keys; never logs values.

    Accepts the docker-compose / systemd EnvironmentFile dialect used by
    both /root/.hermes/.env and /opt/vpn-bot/.env: blank lines and
    '#' comments skipped, an optional 'export ' prefix, one layer of
    matching single or double quotes stripped, and a trailing ' # note'
    dropped from UNQUOTED values only (a '#' inside quotes is data —
    Telegram tokens and proxy passwords can contain anything).
    """
    wanted = set(keys)
    values: Dict[str, str] = {}
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.startswith("export "):
                line = line[len("export "):].lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            if key not in wanted:
                continue
            value = value.strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]
            else:
                value = value.split(" #", 1)[0].rstrip()
            values[key] = value
    logger.debug("read %d/%d keys from %s", len(values), len(wanted), path)
    return values


# --------------------------------------------------------------------------
# Pure core: classification + decision
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ModelStatus:
    model: str
    state: str                 # FREE | PAID | MISSING
    pricing: Dict[str, str]    # {} when missing

    def pricing_text(self) -> str:
        """Human line for notifications: what the guard actually saw."""
        if self.state == MISSING:
            return "нет в списке моделей OpenRouter"
        parts = [f"{k}={self.pricing.get(k, '?')}" for k in ("prompt", "completion")]
        if self.pricing.get("request") not in (None, "0"):
            parts.append(f"request={self.pricing['request']}")
        return " ".join(parts) + f" $/tok ({self.state})"


@dataclass
class Notification:
    key: str
    severity: str    # CRITICAL | WARN
    title: str
    detail: str      # HTML-safe body (model ids already escaped)


@dataclass
class Plan:
    primary: Optional[ModelStatus] = None
    fallbacks: List[ModelStatus] = field(default_factory=list)
    usage_delta: Optional[float] = None
    actions: List[str] = field(default_factory=list)
    notifications: List[Notification] = field(default_factory=list)
    new_config: Optional[dict] = None

    @property
    def critical(self) -> bool:
        return any(n.severity == CRITICAL for n in self.notifications)

    def model_states(self) -> Dict[str, str]:
        out = {}
        for st in ([self.primary] if self.primary else []) + self.fallbacks:
            out[st.model] = st.state
        return out


def is_free_price(value) -> bool:
    """"0", "0.0", 0, 0.0 -> free; None / "" / garbage -> NOT free."""
    try:
        return float(value) == 0.0
    except (TypeError, ValueError):
        return False


def classify_model(model_id: str, models_index: Dict[str, dict]) -> ModelStatus:
    """free iff prompt AND completion are 0 (and request, when present).

    A model with no pricing block at all is PAID, not free: the guard
    fails closed on anything it cannot prove costs nothing.
    """
    entry = models_index.get(model_id)
    if entry is None:
        return ModelStatus(model_id, MISSING, {})
    pricing = entry.get("pricing") or {}
    if not isinstance(pricing, dict):
        pricing = {}
    free = (
        is_free_price(pricing.get("prompt"))
        and is_free_price(pricing.get("completion"))
        and is_free_price(pricing.get("request", "0"))
    )
    shown = {k: str(v) for k, v in pricing.items() if k in ("prompt", "completion", "request")}
    return ModelStatus(model_id, FREE if free else PAID, shown)


def config_models(config: dict) -> Tuple[dict, List[dict]]:
    """(model section, fallback_providers list) with the malformed filtered.

    Raises GuardError when there is no model.default — a guard that
    "promotes" into a config it doesn't understand would do damage.
    """
    if not isinstance(config, dict):
        raise GuardError("config.yaml is not a mapping")
    model = config.get("model")
    if not isinstance(model, dict) or not model.get("default"):
        raise GuardError("config.yaml: model.default missing")
    raw = config.get("fallback_providers") or []
    if not isinstance(raw, list):
        raise GuardError("config.yaml: fallback_providers is not a list")
    fallbacks = [fb for fb in raw if isinstance(fb, dict) and fb.get("model")]
    return model, fallbacks


def _code(s: str) -> str:
    return f"<code>{html.escape(str(s))}</code>"


def _chain_text(primary: ModelStatus, fallbacks: List[ModelStatus]) -> str:
    lines = [f"default: {_code(primary.model)} — {primary.pricing_text()}"]
    for st in fallbacks:
        lines.append(f"fallback: {_code(st.model)} — {st.pricing_text()}")
    return "\n".join(lines)


def build_promoted_config(config: dict, free_index: int) -> dict:
    """New config with fallback[free_index] as default; input left untouched.

    The old primary is REMOVED from the chain, whatever happened to it: a
    paid model left as "last resort" is a paid path the operator never
    configured (free models rate-limit often enough that Hermes would
    land on it), and a 404 id is pure noise. It stays in the backup.
    """
    new = copy.deepcopy(config)
    model = new["model"]
    fallbacks = list(new.get("fallback_providers") or [])
    # Index is into the *filtered* list from config_models(); map back to
    # the raw list by identity of the model id (malformed entries have none).
    valid = [fb for fb in fallbacks if isinstance(fb, dict) and fb.get("model")]
    winner = valid[free_index]
    fallbacks = [fb for fb in fallbacks if fb is not winner]

    model["default"] = winner["model"]
    if winner.get("provider"):
        model["provider"] = winner["provider"]
    for key in ("base_url", "api_key"):
        if winner.get(key):
            model[key] = winner[key]
        else:
            # Never let the winner inherit the old primary's endpoint or
            # inline credentials by accident — Hermes resolves the
            # provider's own defaults when these are absent.
            model.pop(key, None)
    new["fallback_providers"] = fallbacks
    return new


def decide(config: dict, models_index: Optional[Dict[str, dict]],
           prev_usage: Optional[float], usage: Optional[float],
           prev_states: Optional[Dict[str, str]] = None) -> Plan:
    """The whole policy, no I/O.

    config       parsed config.yaml (full mapping — the plan may carry a
                 rewritten copy in new_config)
    models_index {model_id: entry} from /models, or None when the fetch
                 failed -> pricing checks are skipped entirely
    prev_usage / usage   data.usage from /key, None when unknown
    prev_states  {model_id: state} from the previous run; drives the
                 transition-only fallback warnings
    """
    plan = Plan()
    prev_states = prev_states or {}

    # (b) classify first so the usage message can name the chain.
    if models_index is not None:
        model, fallbacks = config_models(config)
        plan.primary = classify_model(model["default"], models_index)
        plan.fallbacks = [classify_model(fb["model"], models_index) for fb in fallbacks]

    # (a) money moved — always first, always critical, independent of (c).
    if usage is not None and prev_usage is not None:
        plan.usage_delta = usage - prev_usage
        if plan.usage_delta > USAGE_GROWTH_THRESHOLD:
            chain = (_chain_text(plan.primary, plan.fallbacks)
                     if plan.primary else "цепочка моделей: не удалось проверить")
            plan.notifications.append(Notification(
                key="usage_grew", severity=CRITICAL,
                title="Hermes model guard: агент тратит деньги",
                detail=(
                    f"Usage ключа OpenRouter вырос с ${prev_usage:.4f} до "
                    f"${usage:.4f} (+${plan.usage_delta:.4f}) с прошлой проверки.\n"
                    f"{chain}\n"
                    "Действие: ничего не переключал — источник списаний смотреть "
                    "на openrouter.ai/activity; если это Hermes, остановить: "
                    "systemctl stop hermes-api.service."
                ),
            ))

    if plan.primary is None:
        return plan

    # (d) fallback got worse — on the transition only.
    for st in plan.fallbacks:
        if st.state == FREE or prev_states.get(st.model) == st.state:
            continue
        what = "стала платной" if st.state == PAID else "пропала из OpenRouter"
        consequence = (
            "если основная откажет, Hermes уйдёт на ПЛАТНУЮ модель"
            if st.state == PAID else
            "если основная откажет, этот fallback просто отдаст 404"
        )
        plan.notifications.append(Notification(
            key=f"fallback_degraded:{st.model}", severity=WARN,
            title=f"Hermes model guard: fallback-модель {what}",
            detail=(
                f"{_code(st.model)} — {st.pricing_text()}\n"
                f"Основная {_code(plan.primary.model)} — {plan.primary.pricing_text()}, "
                f"работа не нарушена, но {consequence}.\n"
                "Действие: ничего не менял — поправь fallback_providers в "
                "/root/.hermes/config.yaml."
            ),
        ))

    # (c) the primary itself.
    if plan.primary.state == FREE:
        return plan

    free_index = next((i for i, st in enumerate(plan.fallbacks) if st.state == FREE), None)
    if free_index is None:
        plan.notifications.append(Notification(
            key="no_free_model", severity=CRITICAL,
            title="Hermes model guard: бесплатных моделей не осталось — агент НЕ переключён",
            detail=(
                f"{_chain_text(plan.primary, plan.fallbacks)}\n"
                "Действие: config.yaml не трогал — Hermes на платной/несуществующей "
                "модели пусть лучше падает, чем платит (а списания поймает проверка "
                "usage). Нужно вручную выбрать новую :free модель "
                "(openrouter.ai/models?q=free), поправить /root/.hermes/config.yaml "
                "и systemctl restart hermes-api.service."
            ),
        ))
        return plan

    winner = plan.fallbacks[free_index]

    if plan.primary.state == MISSING and prev_states.get(plan.primary.model) != MISSING:
        # First sighting: Hermes is already failing over to the (free)
        # fallback on its own, so nothing burns. Confirm next run before
        # rewriting the operator's config over a transient delisting.
        plan.notifications.append(Notification(
            key=f"primary_missing:{plan.primary.model}", severity=WARN,
            title="Hermes model guard: основная модель пропала из OpenRouter — жду подтверждения",
            detail=(
                f"{_chain_text(plan.primary, plan.fallbacks)}\n"
                f"Hermes сам уходит на fallback {_code(winner.model)} при 404. "
                "config.yaml пока не трогал: если через 30 мин модели всё ещё нет — "
                "переключу default на этот fallback и перезапущу hermes-api."
            ),
        ))
        return plan

    plan.new_config = build_promoted_config(config, free_index)
    plan.actions = [
        f"promote:{winner.model}",
        "backup:config.yaml",
        f"write:drop:{plan.primary.model}",
        f"restart:{HERMES_UNIT}",
    ]
    _, new_fallbacks = config_models(plan.new_config)
    new_chain = ", ".join(_code(fb["model"]) for fb in new_fallbacks) or "(пусто)"
    what = "стала платной" if plan.primary.state == PAID else "пропала из OpenRouter"
    # PAID primary = calls in the last <=30 min may already have been
    # billed -> critical; MISSING = they were failing, no money moved.
    plan.notifications.append(Notification(
        key=f"promoted:{plan.primary.model}->{winner.model}",
        severity=CRITICAL if plan.primary.state == PAID else WARN,
        title=f"Hermes model guard: основная модель {what} — переключил на бесплатную",
        detail=(
            f"Было: {_code(plan.primary.model)} — {plan.primary.pricing_text()}\n"
            f"Стало: {_code(winner.model)} — {winner.pricing_text()}\n"
            f"fallback_providers теперь: {new_chain} — старая "
            f"{_code(plan.primary.model)} из цепочки УБРАНА (есть в бэкапе)."
        ),
    ))
    return plan


# --------------------------------------------------------------------------
# Dedupe (pure)
# --------------------------------------------------------------------------

def _parse_ts(value) -> Optional[datetime]:
    try:
        ts = datetime.fromisoformat(value)
    except (TypeError, ValueError):
        return None
    return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)


def filter_notifications(notifications: List[Notification], notified: Dict[str, str],
                         now: datetime, window: timedelta = DEDUPE_WINDOW
                         ) -> Tuple[List[Notification], Dict[str, str]]:
    """Drop notifications whose key fired within `window`; return the
    survivors and the updated {key: iso_ts} map (old keys pruned)."""
    updated = {
        k: v for k, v in notified.items()
        if (_parse_ts(v) or now) > now - DEDUPE_RETENTION
    }
    survivors = []
    for n in notifications:
        last = _parse_ts(notified.get(n.key))
        if last is not None and now - last < window:
            logger.info("notification %s suppressed (sent %s)", n.key, last.isoformat())
            continue
        survivors.append(n)
        updated[n.key] = now.isoformat()
    return survivors, updated


def format_message(n: Notification, outcome: Optional[str] = None) -> str:
    """Same look as bot/services/alert_manager.py: emoji + bold title."""
    prefix = "🔥" if n.severity == CRITICAL else "⚠️"
    text = f"{prefix} <b>{html.escape(n.title)}</b>\n{n.detail}"
    if outcome:
        text += f"\nИтог: {outcome}"
    return text


# --------------------------------------------------------------------------
# Thin I/O
# --------------------------------------------------------------------------

def load_state(state_dir: str) -> dict:
    path = os.path.join(state_dir, "state.json")
    try:
        with open(path, encoding="utf-8") as fh:
            state = json.load(fh)
        if not isinstance(state, dict):
            raise ValueError("state is not a dict")
    except FileNotFoundError:
        state = {}
    except (OSError, ValueError) as exc:
        logger.warning("state.json unreadable (%s) — starting fresh", type(exc).__name__)
        state = {}
    state.setdefault("last_usage", None)
    state.setdefault("last_usage_at", None)
    state.setdefault("notified", {})
    state.setdefault("model_states", {})
    state.setdefault("api_failures", 0)       # consecutive blind runs (API or config)
    state.setdefault("pending", [])           # undelivered messages: [{key, text, queued_at}]
    state.setdefault("restart_pending", None)  # {since, deferrals} while an /ai call blocks it
    if not isinstance(state["pending"], list):
        state["pending"] = []
    return state


def save_state(state_dir: str, state: dict) -> None:
    os.makedirs(state_dir, mode=0o700, exist_ok=True)
    path = os.path.join(state_dir, "state.json")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_config(path: str) -> dict:
    """Parsed config.yaml; GuardError on anything that is not a usable
    mapping with model.default (YAML syntax errors included — a bare
    yaml.YAMLError would escape main()'s handlers as a traceback)."""
    try:
        with open(path, encoding="utf-8") as fh:
            config = yaml.safe_load(fh)
    except yaml.YAMLError as exc:
        raise GuardError(f"{path}: YAML parse error: {type(exc).__name__}") from exc
    if not isinstance(config, dict):
        raise GuardError(f"{path}: not a YAML mapping")
    config_models(config)   # validates model.default / fallback_providers shape
    # Hermes allows a per-provider api_key inline; the live file keeps it
    # in .env instead, but if one ever appears it must not reach the log
    # or the --dry-run printout.
    for section in [config.get("model")] + list(config.get("fallback_providers") or []):
        if isinstance(section, dict):
            register_secret(section.get("api_key"))
    return config


def masked(config: dict) -> dict:
    """Deep copy with every api_key value replaced — for printing only."""
    out = copy.deepcopy(config)
    for section in [out.get("model")] + list(out.get("fallback_providers") or []):
        if isinstance(section, dict) and section.get("api_key"):
            section["api_key"] = "***"
    return out


def render_config(new_config: dict, reason: str, backup_name: str, now: datetime) -> str:
    """YAML text for the rewritten config.

    PyYAML cannot keep comments, and the live file is full of WHY-notes —
    so the header says who rewrote it and where the commented original
    went. The text is parsed back before it is returned: a config Hermes
    cannot load would take /ai down with it.
    """
    header = (
        f"# Rewritten by hermes_model_guard.py at {now.strftime('%Y-%m-%d %H:%M:%S')} UTC:\n"
        f"#   {reason}\n"
        f"# The previous file (with its comments) is kept next to this one as\n"
        f"#   {backup_name}\n"
    )
    body = yaml.safe_dump(new_config, sort_keys=False, default_flow_style=False,
                          allow_unicode=True, width=100)
    text = header + body
    if yaml.safe_load(text) != new_config:
        raise GuardError("rendered config does not round-trip — refusing to write")
    return text


def write_config(path: str, new_config: dict, reason: str, now: datetime) -> str:
    """Backup next to the file, atomic replace, same mode. Returns backup path."""
    backup = f"{path}.bak-{now.strftime('%Y%m%d-%H%M%S')}"
    text = render_config(new_config, reason, os.path.basename(backup), now)
    shutil.copy2(path, backup)
    tmp = path + ".tmp"
    # 0600 from the first byte: the file may carry an inline api_key, and
    # the umask default (0644) would expose it until copymode() below.
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())
    shutil.copymode(path, tmp)
    os.replace(tmp, path)
    return backup


class GuardIO:
    """Everything that touches the network or the service manager.

    Tests swap in a fake; the pure core never sees this class.
    """

    def __init__(self, api_key: str, or_proxy: Optional[str],
                 bot_token: Optional[str], chat_id: Optional[str],
                 thread_id: Optional[str], tg_proxy: Optional[str]):
        self.api_key = api_key
        self.or_proxies = {"https": or_proxy, "http": or_proxy} if or_proxy else None
        self.bot_token = bot_token
        self.chat_id = chat_id
        # Parsed here, not in send_telegram(): a ValueError there would
        # escape AFTER the config rewrite and before state.json is saved.
        self.thread_id: Optional[int] = None
        if thread_id is not None and str(thread_id).strip().lstrip("-").isdigit():
            self.thread_id = int(str(thread_id).strip())
        elif thread_id:
            logger.warning("TOPIC_AI is not numeric — messages go to the forum's General topic")
        self.tg_proxies = {"https": tg_proxy, "http": tg_proxy} if tg_proxy else None
        self.session = requests.Session()
        # Explicit proxies only — never let a stray env var reroute a call.
        self.session.trust_env = False

    def now(self) -> datetime:
        return datetime.now(timezone.utc)

    def fetch_usage(self) -> Optional[float]:
        try:
            r = self.session.get(f"{OPENROUTER_BASE}/key",
                                 headers={"Authorization": f"Bearer {self.api_key}"},
                                 proxies=self.or_proxies, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            usage = r.json()["data"]["usage"]
            return float(usage)
        except Exception as exc:  # noqa: BLE001 — any failure = unknown
            logger.warning("GET /key failed: %s: %s", type(exc).__name__, redact(str(exc)))
            return None

    def fetch_models(self) -> Optional[Dict[str, dict]]:
        try:
            r = self.session.get(f"{OPENROUTER_BASE}/models",
                                 proxies=self.or_proxies, timeout=HTTP_TIMEOUT)
            r.raise_for_status()
            data = r.json()["data"]
            index = {m["id"]: m for m in data if isinstance(m, dict) and m.get("id")}
        except Exception as exc:  # noqa: BLE001
            logger.warning("GET /models failed: %s: %s", type(exc).__name__, redact(str(exc)))
            return None
        return sanitize_models_index(index)

    def inflight_requests(self) -> int:
        """ESTABLISHED client connections on the Hermes API port.

        The bot holds the HTTP connection open for the whole agent loop,
        so >0 means an /ai request is mid-flight (same probe as
        hermes_api_watchdog.sh). 0 on any failure: an unreadable `ss`
        must not block the promotion forever.
        """
        try:
            proc = subprocess.run(
                ["ss", "-Htn", "state", "established", f"( sport = :{HERMES_API_PORT} )"],
                capture_output=True, text=True, timeout=15)
        except (OSError, subprocess.TimeoutExpired) as exc:
            logger.warning("ss failed (%s) — assuming no in-flight request", type(exc).__name__)
            return 0
        if proc.returncode != 0:
            return 0
        return len([ln for ln in proc.stdout.splitlines() if ln.strip()])

    def restart_hermes(self) -> Tuple[bool, str]:
        try:
            proc = subprocess.run(["systemctl", "restart", HERMES_UNIT],
                                  capture_output=True, text=True, timeout=120)
        except (OSError, subprocess.TimeoutExpired) as exc:
            return False, f"{type(exc).__name__}: {redact(str(exc))}"
        if proc.returncode != 0:
            return False, redact((proc.stderr or proc.stdout or "").strip()[:300])
        return True, ""

    def send_telegram(self, text: str) -> bool:
        if not (self.bot_token and self.chat_id):
            logger.error("telegram not configured (BOT_TOKEN/FORUM_GROUP_ID) — not sent")
            return False
        payload = {"chat_id": self.chat_id, "text": text, "parse_mode": "HTML",
                   "disable_web_page_preview": True}
        if self.thread_id is not None:
            payload["message_thread_id"] = self.thread_id
        try:
            r = self.session.post(f"https://api.telegram.org/bot{self.bot_token}/sendMessage",
                                  json=payload, proxies=self.tg_proxies, timeout=HTTP_TIMEOUT)
            if r.status_code != 200:
                logger.error("telegram sendMessage -> %s %s", r.status_code,
                             redact(r.text[:200]))
                return False
        except Exception as exc:  # noqa: BLE001 — message contains the URL+token
            logger.error("telegram sendMessage failed: %s: %s",
                         type(exc).__name__, redact(str(exc)))
            return False
        return True


def sanitize_models_index(index: Optional[Dict[str, dict]]) -> Optional[Dict[str, dict]]:
    """An implausibly small catalogue is a broken fetch, not a shrunk one."""
    if index is None:
        return None
    if len(index) < MIN_MODELS_SANE:
        logger.warning("GET /models returned only %d models — treating as failed", len(index))
        return None
    return index


def build_io(hermes_env: str, bot_env: str) -> GuardIO:
    h = parse_env_file(hermes_env, ("OPENROUTER_API_KEY", "HTTPS_PROXY", "https_proxy"))
    api_key = h.get("OPENROUTER_API_KEY")
    if not api_key:
        raise GuardError(f"OPENROUTER_API_KEY missing in {hermes_env}")
    or_proxy = h.get("HTTPS_PROXY") or h.get("https_proxy") or None
    b = parse_env_file(bot_env, ("BOT_TOKEN", "FORUM_GROUP_ID", "TOPIC_AI", "HTTPS_PROXY"))
    for v in (api_key, or_proxy, b.get("BOT_TOKEN"), b.get("HTTPS_PROXY")):
        register_secret(v)
    if not b.get("BOT_TOKEN") or not b.get("FORUM_GROUP_ID"):
        logger.warning("BOT_TOKEN/FORUM_GROUP_ID missing in %s — notifications disabled", bot_env)
    return GuardIO(api_key, or_proxy, b.get("BOT_TOKEN"), b.get("FORUM_GROUP_ID"),
                   b.get("TOPIC_AI"), b.get("HTTPS_PROXY") or None)


# --------------------------------------------------------------------------
# One cycle
# --------------------------------------------------------------------------

def _print_plan(plan: Plan, extra: List[Notification], usage: Optional[float],
                api_failed: bool, dry_run: bool) -> None:
    tag = "[dry-run] " if dry_run else ""
    if plan.primary:
        print(f"{tag}default  {plan.primary.model}: {plan.primary.pricing_text()}")
        for st in plan.fallbacks:
            print(f"{tag}fallback {st.model}: {st.pricing_text()}")
    else:
        print(f"{tag}pricing: not checked (fetch failed)" if api_failed else
              f"{tag}pricing: not checked")
    usage_txt = "unknown" if usage is None else f"{usage:.7f}"
    delta_txt = ("n/a" if plan.usage_delta is None else f"{plan.usage_delta:+.7f}")
    print(f"{tag}usage    {usage_txt} USD (delta {delta_txt})")
    print(f"{tag}actions  {plan.actions or 'none'}")
    for n in plan.notifications + extra:
        print(f"{tag}notify   [{n.severity}] {n.key}: {n.title}")
    if dry_run and plan.new_config is not None:
        print(f"{tag}new config.yaml would be:")
        print(yaml.safe_dump(masked(plan.new_config), sort_keys=False,
                             default_flow_style=False, allow_unicode=True))


def _restart_or_defer(io: GuardIO, state: dict, now: datetime) -> Tuple[Optional[bool], str]:
    """Restart hermes-api unless an /ai request is mid-flight.

    Returns (True, text) restarted, (False, text) restart failed,
    (None, text) deferred — state["restart_pending"] then carries the
    debt to the next run. After RESTART_DEFER_MAX deferrals the restart
    happens regardless (the watchdog's "5 misses = restart anyway").
    """
    pending = state.get("restart_pending") or {}
    deferrals = int(pending.get("deferrals") or 0)
    inflight = io.inflight_requests()
    if inflight > 0 and deferrals < RESTART_DEFER_MAX:
        state["restart_pending"] = {"since": pending.get("since") or now.isoformat(),
                                    "deferrals": deferrals + 1}
        logger.info("restart %s deferred: %d in-flight /ai request(s), deferral %d/%d",
                    HERMES_UNIT, inflight, deferrals + 1, RESTART_DEFER_MAX)
        return None, (f"перезапуск {HERMES_UNIT} ОТЛОЖЕН — {inflight} /ai-запрос(ов) в работе; "
                      f"повторю в следующий запуск (до {RESTART_DEFER_MAX} раз)")
    ok, err = io.restart_hermes()
    state["restart_pending"] = None
    logger.info("restart %s: %s", HERMES_UNIT, "ok" if ok else f"FAILED {err}")
    if ok:
        return True, f"{HERMES_UNIT} перезапущен"
    return False, f"НО systemctl restart {HERMES_UNIT} упал: {html.escape(err)}"


def _queue_pending(state: dict, key: str, text: str, now: datetime) -> None:
    """Keep an undelivered message for the next run (bounded)."""
    pending = [p for p in state.get("pending") or [] if isinstance(p, dict)]
    pending.append({"key": key, "text": text, "queued_at": now.isoformat()})
    state["pending"] = pending[-PENDING_MAX:]


def run_once(io: GuardIO, hermes_config: str, state_dir: str, dry_run: bool) -> int:
    state = load_state(state_dir)
    now = io.now()

    # An unreadable config is a blind run, not a silent exit 2: the
    # pricing check is skipped, the blind counter runs, the topic hears
    # about it after API_FAIL_NOTIFY_AFTER misses like any other blindness.
    config: Optional[dict] = None
    config_err: Optional[str] = None
    try:
        config = load_config(hermes_config)
    except (OSError, GuardError) as exc:
        config_err = redact(f"{type(exc).__name__}: {exc}")
        logger.error("config.yaml unusable — pricing check skipped: %s", config_err)

    usage = io.fetch_usage()
    index = io.fetch_models() if config is not None else None
    api_failed = usage is None or index is None

    extra: List[Notification] = []
    if api_failed:
        state["api_failures"] = int(state.get("api_failures") or 0) + 1
        if state["api_failures"] >= API_FAIL_NOTIFY_AFTER:
            why = (f"config.yaml не читается ({html.escape(config_err)})" if config_err else
                   "GET /api/v1/models или /api/v1/key не отвечает через прокси — "
                   "проверь HTTPS_PROXY в /root/.hermes/.env")
            extra.append(Notification(
                key="guard_blind", severity=WARN,
                title=f"Hermes model guard: слеп {state['api_failures']} запусков подряд",
                detail=(f"{why}. Биллинг-гард не может проверить модели/списания "
                        "(journalctl -u hermes-model-guard). Ничего не менял."),
            ))
    else:
        state["api_failures"] = 0

    # With index None (always the case when config failed) decide() never
    # looks at the config, so an empty mapping is safe there.
    plan = decide(config or {}, index, state.get("last_usage"), usage,
                  prev_states=state.get("model_states"))
    _print_plan(plan, extra, usage, api_failed, dry_run)

    if dry_run:
        # No state, no config, no restart, no message — the plan above is all.
        return 2 if api_failed and not plan.critical else (1 if plan.critical else 0)

    outcome = None
    if plan.new_config is not None:
        reason = "; ".join(plan.actions)
        try:
            backup = write_config(hermes_config, plan.new_config, reason, now)
        except (OSError, GuardError) as exc:
            # No restart on a failed write: the running process is on the
            # old config and a restart would change nothing but kill /ai.
            logger.error("config rewrite failed: %s", redact(str(exc)))
            outcome = (f"переписать config.yaml НЕ удалось ({html.escape(type(exc).__name__)}) — "
                       f"{HERMES_UNIT} не трогал")
        else:
            logger.info("config.yaml rewritten (%s), backup %s", reason, backup)
            _, text = _restart_or_defer(io, state, now)
            outcome = f"бэкап {_code(os.path.basename(backup))}, {text}"
    elif state.get("restart_pending"):
        # Debt from an earlier promotion: config is already rewritten,
        # only the running process still has the old model loaded.
        ok, text = _restart_or_defer(io, state, now)
        if ok is not None:
            extra.append(Notification(
                key=f"restart_done:{now.isoformat()}", severity=WARN if ok else CRITICAL,
                title="Hermes model guard: отложенный перезапуск hermes-api "
                      + ("выполнен" if ok else "НЕ удался"),
                detail=f"{text}. Новый default из config.yaml теперь в работе."
                       if ok else f"{text}. Hermes всё ещё на старой модели — перезапусти руками.",
            ))

    # Deliver: first what earlier runs could not, then this run's news.
    still_pending = []
    for item in state.get("pending") or []:
        if not isinstance(item, dict) or not item.get("text"):
            continue
        sent = io.send_telegram(item["text"])
        logger.info("pending %s: %s", item.get("key"), "sent" if sent else "still NOT sent")
        if not sent:
            still_pending.append(item)
    state["pending"] = still_pending

    to_send, state["notified"] = filter_notifications(
        plan.notifications + extra, state.get("notified") or {}, now)
    for n in to_send:
        text = format_message(n, outcome if n.key.startswith("promoted:") else None)
        sent = io.send_telegram(text)
        logger.info("notify %s [%s]: %s", n.key, n.severity, "sent" if sent else "NOT sent")
        if not sent:
            # Queue the rendered text: the next run may not be able to
            # regenerate it (after a promotion the primary IS free again).
            _queue_pending(state, n.key, text, now)

    if usage is not None:
        state["last_usage"] = usage
        state["last_usage_at"] = now.isoformat()
    if index is not None:
        state["model_states"] = plan.model_states()
    save_state(state_dir, state)

    if plan.critical or any(n.severity == CRITICAL for n in extra):
        return 1
    return 2 if api_failed else 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--dry-run", action="store_true",
                    help="print the plan; change nothing, send nothing")
    ap.add_argument("--once", action="store_true", default=True,
                    help="run one cycle (the only mode; the timer provides the loop)")
    ap.add_argument("--state-dir", default=STATE_DIR)
    ap.add_argument("--hermes-env", default=HERMES_ENV)
    ap.add_argument("--hermes-config", default=HERMES_CONFIG)
    ap.add_argument("--bot-env", default=BOT_ENV)
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args(argv)
    # Logging is configured here, not at import time: tests import the
    # module and must not get a stray root handler.
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="[%(asctime)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    try:
        io = build_io(args.hermes_env, args.bot_env)
        return run_once(io, args.hermes_config, args.state_dir, args.dry_run)
    except GuardError as exc:
        logger.error("cannot check: %s", redact(str(exc)))
        return 2
    except OSError as exc:
        logger.error("cannot check: %s: %s", type(exc).__name__, redact(str(exc)))
        return 2
    except Exception as exc:  # noqa: BLE001 — a crash is "could not check", not "critical"
        logger.error("guard crashed: %s: %s", type(exc).__name__, redact(str(exc)),
                     exc_info=logger.isEnabledFor(logging.DEBUG))
        return 2


if __name__ == "__main__":
    sys.exit(main())
