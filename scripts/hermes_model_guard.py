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
  c) DECISION — the guard REPAIRS, it does not just report. A model is
     "dead" when it is PAID (every call bills: acted on at once) or
     MISSING on two runs in a row (':free' ids blink; one blink must not
     rewrite the config — the first sighting only warns, and Hermes walks
     fallback_providers on a 404 by itself meanwhile). Everything dead is
     fixed in ONE rewrite + ONE restart:
     - dead primary -> PROMOTE the first free fallback, else the first
       live model from FALLBACK_CANDIDATES; the old primary is REMOVED
       (it lives on in the backup);
     - dead fallbacks are cut — a paid one is a billing path Hermes takes
       the first time the free primary 429s;
     - after a cut, the chain is topped back up to MIN_FREE_FALLBACKS from
       FALLBACK_CANDIDATES (never before: a healthy chain shaped by hand is
       left alone);
     - see (e) for the side-task models.
     Replacements come ONLY from the curated, live-smoked candidate lists —
     never an arbitrary ':free' id: nobody has checked its ops judgment
     (2026-08-30 nemotron-super proposed ALTER TABLE on prod).
     Nothing free anywhere -> CRITICAL and NO rewrite. The backup is kept
     next to the file and hermes-api restarted, deferred while an /ai
     request is in flight (at most RESTART_DEFER_MAX runs; killing a
     running agent loop is worse than 30 more minutes on the old model).
  d) A fallback delisted for the FIRST time is a WARNING (on the
     transition only, state.json remembers); the second sighting cuts it.
  e) AUXILIARY models — config.yaml auxiliary.<task>.model (and each
     task's fallback_chain) — are priced the same way. Hermes calls them
     for side tasks (compression, smart-approval, curator, vision) outside
     the chain above; left unpinned they fall back to a model HARDCODED in
     Hermes (google/gemini-3-flash-preview, paid). From 2026-09-07 to -28
     that billed the key 22 times while (b) reported every model free,
     because a text-only primary sends every image there. A dead aux model
     is re-pinned in the same rewrite: text tasks follow the (new) primary
     — what 'auto' would use, but pinned, so an error can never slide onto
     the paid built-in default — and VISION_TASKS go to the first free
     image-capable model in the chain, then VISION_CANDIDATES; with none,
     to the text model (images then fail closed, the message says so).
     Dead entries leave a task's fallback_chain. Before this, a paid
     primary was promoted while 16 text tasks stayed pinned to it and kept
     billing until someone re-pinned them by hand. When money moves and
     vision is unpinned, the usage message names that as the likeliest
     source.

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
import hashlib
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

# Where the guard may take a REPLACEMENT from when it repairs the chain or
# re-pins a side task. Only ids live-smoked on 2026-09-28 (tool call +
# latency, image input where marked) — the guard never promotes an
# arbitrary ':free' id from the catalogue: nobody has checked its ops
# judgment (2026-08-30 nemotron-super proposed ALTER TABLE on prod).
# Order = preference. Vendor-diverse on purpose: a second model of a
# failing primary's vendor tends to share its outage (2026-09-28 NVIDIA
# "overloaded" hit nemotron while gemma answered).
FALLBACK_CANDIDATES = (
    "google/gemma-4-31b-it:free",                          # tools 1.7s, images
    "nvidia/nemotron-3.5-lightning:free",                  # tools 1.6s
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",  # tools 3.0s, images
    "cohere/north-mini-code:free",                         # tools 1.5s
    "nvidia/nemotron-3-super-120b-a12b:free",              # tools, slow (~14s/turn)
)
VISION_CANDIDATES = (
    "google/gemma-4-31b-it:free",                          # read the test image right, 2.5s
    "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free",  # right, 7.3s
)
# After the guard had to cut the chain it tops it back up to this many
# FREE fallbacks — never before: a healthy chain shaped by hand is left alone.
MIN_FREE_FALLBACKS = 2
# auxiliary.<task> keys that hand the model an image; everything else is text.
VISION_TASKS = ("vision", "video")
OPENROUTER_API = OPENROUTER_BASE

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
    # (label, status) — label is "vision" or "vision.fallback_chain[0]"
    auxiliary: List[Tuple[str, ModelStatus]] = field(default_factory=list)
    usage_delta: Optional[float] = None
    actions: List[str] = field(default_factory=list)
    notifications: List[Notification] = field(default_factory=list)
    new_config: Optional[dict] = None

    @property
    def critical(self) -> bool:
        return any(n.severity == CRITICAL for n in self.notifications)

    def model_states(self) -> Dict[str, str]:
        # One id can sit in several roles (the primary is usually also the
        # text auxiliary model); it has one price, so one state.
        out = {}
        chain = ([self.primary] if self.primary else []) + self.fallbacks
        for st in chain + [st for _, st in self.auxiliary]:
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


# Providers whose ids live in OpenRouter's /models index. 'auto' and ''
# resolve to the main provider, which is OpenRouter on this host; any other
# provider (custom / nous / codex) is a bill this guard cannot see.
_PRICEABLE_AUX_PROVIDERS = ("openrouter", "auto", "")


def config_aux_models(config: dict) -> List[Tuple[str, str]]:
    """(label, model_id) for every pinned auxiliary model, fallback_chain
    entries included ("vision.fallback_chain[0]").

    Hermes calls these for side tasks — compression, smart-approval,
    curator, vision — outside model/fallback_providers, so the chain check
    alone never saw them. Unpinned, a task lands on Hermes' HARDCODED
    OpenRouter default (google/gemini-3-flash-preview, paid): every image
    while the primary is text-only, and any text call whose model 429s.
    That billed the key 22 times after the 2026-09-07 promotion to a
    text-only primary, while this guard reported every model free.

    Never raises: auxiliary is optional, and a malformed block must not
    blind the whole guard — the primary check matters more. An entry with
    an empty model is skipped (it has no id to price; see the usage hint).
    """
    aux = config.get("auxiliary") if isinstance(config, dict) else None
    if not isinstance(aux, dict):
        return []
    out: List[Tuple[str, str]] = []
    for task, task_cfg in aux.items():
        if not isinstance(task_cfg, dict):
            continue
        entries = [(str(task), task_cfg)]
        chain = task_cfg.get("fallback_chain")
        if isinstance(chain, list):
            entries += [(f"{task}.fallback_chain[{i}]", e)
                        for i, e in enumerate(chain) if isinstance(e, dict)]
        for label, entry in entries:
            model = aux_model_id(entry)
            if model:
                out.append((label, model))
    return out


def aux_model_id(entry) -> Optional[str]:
    """The OpenRouter model id an auxiliary entry points at, or None when
    it has none this guard can price (empty model, another provider, or a
    base_url that is not OpenRouter — base_url wins over provider in Hermes)."""
    if not isinstance(entry, dict):
        return None
    model = str(entry.get("model") or "").strip()
    provider = str(entry.get("provider") or "").strip().lower()
    base_url = str(entry.get("base_url") or "").strip().lower()
    if base_url and "openrouter.ai" not in base_url:
        return None
    if model and provider in _PRICEABLE_AUX_PROVIDERS:
        return model
    return None


def vision_pinned(config: dict) -> bool:
    """True iff auxiliary.vision names a model. Unpinned vision is the one
    side task that reaches the paid default WITHOUT any error: whenever the
    primary cannot take images, Hermes hands every image to it directly."""
    aux = config.get("auxiliary") if isinstance(config, dict) else None
    vis = aux.get("vision") if isinstance(aux, dict) else None
    return isinstance(vis, dict) and bool(str(vis.get("model") or "").strip())


def _group_aux(auxiliary: List[Tuple[str, ModelStatus]]) -> List[Tuple[str, List[str], ModelStatus]]:
    """[(model_id, [labels], status)] in first-seen order, so sixteen tasks
    pinned to one model read (and alert) as one line, not sixteen."""
    groups: Dict[str, Tuple[ModelStatus, List[str]]] = {}
    for label, st in auxiliary:
        groups.setdefault(st.model, (st, []))[1].append(label)
    return [(model_id, labels, st) for model_id, (st, labels) in groups.items()]


def _labels_text(labels: List[str], limit: int = 6) -> str:
    shown = ", ".join(labels[:limit])
    return shown + (f" и ещё {len(labels) - limit}" if len(labels) > limit else "")


def _code(s: str) -> str:
    return f"<code>{html.escape(str(s))}</code>"


def _chain_text(primary: ModelStatus, fallbacks: List[ModelStatus],
                auxiliary: List[Tuple[str, ModelStatus]] = ()) -> str:
    lines = [f"default: {_code(primary.model)} — {primary.pricing_text()}"]
    for st in fallbacks:
        lines.append(f"fallback: {_code(st.model)} — {st.pricing_text()}")
    for model_id, labels, st in _group_aux(list(auxiliary)):
        lines.append(f"aux ({html.escape(_labels_text(labels))}): "
                     f"{_code(model_id)} — {st.pricing_text()}")
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
        plan.auxiliary = [(label, classify_model(m, models_index))
                          for label, m in config_aux_models(config)]

    # (a) money moved — always first, always critical, independent of (c).
    if usage is not None and prev_usage is not None:
        plan.usage_delta = usage - prev_usage
        if plan.usage_delta > USAGE_GROWTH_THRESHOLD:
            chain = (_chain_text(plan.primary, plan.fallbacks, plan.auxiliary)
                     if plan.primary else "цепочка моделей: не удалось проверить")
            # The chain above can be all-free and money still moved: that is
            # exactly the 2026-09 blind spot. Name the likeliest culprit.
            hint = ("" if vision_pinned(config) else
                    "\nauxiliary.vision НЕ закреплён — картинки Hermes отдаёт своей "
                    "встроенной модели по умолчанию (на 2026-09 это платная "
                    "google/gemini-3-flash-preview): вероятный источник.")
            plan.notifications.append(Notification(
                key="usage_grew", severity=CRITICAL,
                title="Hermes model guard: агент тратит деньги",
                detail=(
                    f"Usage ключа OpenRouter вырос с ${prev_usage:.4f} до "
                    f"${usage:.4f} (+${plan.usage_delta:.4f}) с прошлой проверки.\n"
                    f"{chain}{hint}\n"
                    "Действие: ничего не переключал — источник списаний смотреть "
                    "на openrouter.ai/activity; если это Hermes, остановить: "
                    "systemctl stop hermes-api.service."
                ),
            ))

    if plan.primary is None:
        return plan

    def dead(st: ModelStatus) -> bool:
        # Act on it: a price appeared (every call bills -> at once), or a
        # delisting was seen on two runs in a row (':free' ids blink; one
        # blink must not rewrite the config).
        return st.state == PAID or (st.state == MISSING and prev_states.get(st.model) == MISSING)

    def first_miss(st: ModelStatus) -> bool:
        return st.state == MISSING and prev_states.get(st.model) != MISSING

    # (d)/(e) early warnings — a delisting seen for the FIRST time. Nothing
    # is rewritten yet; if the next run still misses it, (f) repairs it.
    for st in plan.fallbacks:
        if first_miss(st):
            plan.notifications.append(Notification(
                key=f"fallback_degraded:{st.model}", severity=WARN,
                title="Hermes model guard: fallback-модель пропала из OpenRouter — жду подтверждения",
                detail=(
                    f"{_code(st.model)} — {st.pricing_text()}\n"
                    f"Основная {_code(plan.primary.model)} — {plan.primary.pricing_text()}, "
                    "работа не нарушена. Если через 30 мин модели всё ещё нет — уберу "
                    "её из fallback_providers и доберу цепочку из проверенного списка."
                ),
            ))
    for model_id, labels, st in _group_aux(plan.auxiliary):
        if first_miss(st):
            plan.notifications.append(Notification(
                key=f"aux_missing:{model_id}", severity=WARN,
                title="Hermes model guard: вспомогательная модель пропала из OpenRouter — жду подтверждения",
                detail=(
                    f"{_code(model_id)} — {st.pricing_text()}\n"
                    f"Задачи: {html.escape(_labels_text(labels))}. Закреплённая задача на "
                    "пропавшей модели падает (денег не тратит). Если через 30 мин модели "
                    "всё ещё нет — перезакреплю задачи на бесплатную автоматически."
                ),
            ))

    # (c) the primary. Promotion source: first FREE fallback in chain order,
    # else the first live-smoked candidate (FALLBACK_CANDIDATES).
    winner: Optional[ModelStatus] = None
    winner_index: Optional[int] = None      # into the chain, when promoted from it
    if plan.primary.state != FREE:
        winner_index = next((i for i, st in enumerate(plan.fallbacks) if st.state == FREE), None)
        if winner_index is not None:
            winner = plan.fallbacks[winner_index]
        else:
            cand = _first_candidate(models_index, FALLBACK_CANDIDATES, need_tools=True,
                                    exclude={plan.primary.model})
            winner = classify_model(cand, models_index) if cand else None
        if winner is None:
            plan.notifications.append(Notification(
                key="no_free_model", severity=CRITICAL,
                title="Hermes model guard: бесплатных моделей не осталось — агент НЕ переключён",
                detail=(
                    f"{_chain_text(plan.primary, plan.fallbacks)}\n"
                    "Ни в цепочке, ни в проверенном списке кандидатов нет живой :free "
                    "модели с tool-calling. Действие: config.yaml не трогал — Hermes на "
                    "платной/несуществующей модели пусть лучше падает, чем платит "
                    "(а списания поймает проверка usage). Нужно вручную выбрать новую "
                    ":free модель (openrouter.ai/models?q=free), поправить "
                    "/root/.hermes/config.yaml и systemctl restart hermes-api.service."
                ),
            ))
            return plan
        if first_miss(plan.primary):
            # Hermes is already failing over to the (free) fallback on its own,
            # so nothing burns. Confirm next run before rewriting the config
            # over a transient delisting.
            plan.notifications.append(Notification(
                key=f"primary_missing:{plan.primary.model}", severity=WARN,
                title="Hermes model guard: основная модель пропала из OpenRouter — жду подтверждения",
                detail=(
                    f"{_chain_text(plan.primary, plan.fallbacks)}\n"
                    f"Hermes сам уходит на fallback при 404. config.yaml пока не трогал: "
                    f"если через 30 мин модели всё ещё нет — переключу default на "
                    f"{_code(winner.model)} и перезапущу hermes-api."
                ),
            ))
            winner = winner_index = None

    # (f) build the repaired config — promotion, dead fallbacks, a topped-up
    # chain, re-pinned side tasks — as ONE rewrite and ONE restart.
    changes: List[str] = []      # human lines for the notification
    actions: List[str] = []
    paid_touched = False

    if winner is not None:
        if winner_index is not None:
            new = build_promoted_config(config, winner_index)
        else:
            new = copy.deepcopy(config)
            new["model"] = {**new["model"], "default": winner.model,
                            "provider": "openrouter", "base_url": OPENROUTER_API}
            new["model"].pop("api_key", None)
        what = "стала платной" if plan.primary.state == PAID else "пропала из OpenRouter"
        changes.append(f"основная: {_code(plan.primary.model)} ({what}) → {_code(winner.model)}")
        actions.append(f"promote:{winner.model}")
        paid_touched |= plan.primary.state == PAID
        new_primary = winner
    else:
        new = copy.deepcopy(config)
        new_primary = plan.primary

    # Dead fallbacks go: a PAID one is a billing path Hermes WILL take the
    # first time the free primary 429s, a delisted one is a guaranteed 404.
    status_of = {st.model: st for st in plan.fallbacks}
    kept, chain_changed = [], winner is not None
    for fb in new.get("fallback_providers") or []:
        st = status_of.get(fb.get("model")) if isinstance(fb, dict) else None
        if st is not None and dead(st):
            what = "стала платной" if st.state == PAID else "пропала из OpenRouter"
            changes.append(f"fallback убран: {_code(st.model)} ({what})")
            actions.append(f"drop_fallback:{st.model}")
            paid_touched |= st.state == PAID
            chain_changed = True
            continue
        kept.append(fb)

    # Top the chain back up — only after cutting it: a healthy config the
    # operator shaped by hand is left alone.
    if chain_changed:
        in_chain = {fb.get("model") for fb in kept if isinstance(fb, dict)}
        free_left = sum(1 for m in in_chain
                        if m and classify_model(m, models_index).state == FREE)
        while free_left < MIN_FREE_FALLBACKS:
            cand = _first_candidate(models_index, FALLBACK_CANDIDATES, need_tools=True,
                                    exclude=in_chain | {new_primary.model, plan.primary.model})
            if cand is None:
                break
            kept.append({"provider": "openrouter", "model": cand, "base_url": OPENROUTER_API})
            in_chain.add(cand)
            free_left += 1
            changes.append(f"fallback добавлен: {_code(cand)} (из проверенного списка)")
            actions.append(f"add_fallback:{cand}")
    new["fallback_providers"] = kept

    # Side tasks: text ones follow the (new) primary — what 'auto' would
    # use, but pinned, so an error can never slide onto Hermes' paid
    # built-in default. Image tasks need a model that takes images.
    free_chain = [classify_model(fb["model"], models_index) for fb in kept
                  if isinstance(fb, dict) and fb.get("model")]
    free_chain = [st for st in free_chain if st.state == FREE]
    text_target = (new_primary.model if new_primary.state == FREE
                   else (free_chain[0].model if free_chain else None))
    vision_target = next(
        (st.model for st in [new_primary] + free_chain
         if st.state == FREE and _accepts_images(models_index.get(st.model))), None,
    ) or _first_candidate(models_index, VISION_CANDIDATES, need_images=True)

    repins: Dict[Tuple[str, str], List[str]] = {}
    no_vision = False
    aux = new.get("auxiliary")
    if isinstance(aux, dict) and text_target:
        for task, tcfg in aux.items():
            if not isinstance(tcfg, dict):
                continue
            model = aux_model_id(tcfg)
            st = classify_model(model, models_index) if model else None
            if st is not None and dead(st):
                target = text_target
                if str(task) in VISION_TASKS:
                    target = vision_target or text_target
                    no_vision |= vision_target is None
                if target != model:
                    tcfg["model"] = target
                    repins.setdefault((model, target), []).append(str(task))
                    actions.append(f"repin:{task}:{model}->{target}")
                    paid_touched |= st.state == PAID
            chain = tcfg.get("fallback_chain")
            if isinstance(chain, list):
                keep_chain = []
                for e in chain:
                    m = aux_model_id(e)
                    s = classify_model(m, models_index) if m else None
                    if s is not None and dead(s):
                        actions.append(f"drop_aux_fallback:{task}:{m}")
                        paid_touched |= s.state == PAID
                        continue
                    keep_chain.append(e)
                tcfg["fallback_chain"] = keep_chain
    for (old, target), tasks in repins.items():
        why = "стала платной" if classify_model(old, models_index).state == PAID else "пропала из OpenRouter"
        changes.append(f"aux ({html.escape(_labels_text(tasks))}): {_code(old)} ({why}) → {_code(target)}")
    if no_vision:
        changes.append("⚠ бесплатной модели с картинками не нашлось — vision закреплён на "
                       "текстовой, картинки агент читать не сможет (денег не тратит)")

    if not actions:
        return plan

    plan.new_config = new
    if winner is not None:
        # Keep the classic promotion trail (tests, journal greps rely on it).
        actions.insert(1, f"write:drop:{plan.primary.model}")
    plan.actions = actions + ["backup:config.yaml", f"restart:{HERMES_UNIT}"]
    _, new_fallbacks = config_models(new)
    new_chain = ", ".join(_code(fb["model"]) for fb in new_fallbacks) or "(пусто)"
    body = "\n".join(changes)
    # Any PAID model involved = calls may already have been billed -> critical.
    severity = CRITICAL if paid_touched else WARN
    if winner is not None:
        what = "стала платной" if plan.primary.state == PAID else "пропала из OpenRouter"
        plan.notifications.append(Notification(
            key=f"promoted:{plan.primary.model}->{winner.model}",
            severity=severity,
            title=f"Hermes model guard: основная модель {what} — переключил на бесплатную",
            detail=(
                f"Было: {_code(plan.primary.model)} — {plan.primary.pricing_text()}\n"
                f"Стало: {_code(winner.model)} — {winner.pricing_text()}\n"
                f"fallback_providers теперь: {new_chain} — старая "
                f"{_code(plan.primary.model)} из цепочки УБРАНА (есть в бэкапе).\n"
                f"Все изменения:\n{body}"
            ),
        ))
    else:
        digest = hashlib.sha1("|".join(sorted(actions)).encode()).hexdigest()[:10]
        plan.notifications.append(Notification(
            key=f"rewrite:{digest}", severity=severity,
            title="Hermes model guard: починил конфиг агента автоматически",
            detail=f"{body}\nfallback_providers теперь: {new_chain}",
        ))
    return plan


def _accepts_images(entry) -> bool:
    arch = (entry or {}).get("architecture") if isinstance(entry, dict) else None
    return "image" in ((arch or {}).get("input_modalities") or [])


def _supports_tools(entry) -> bool:
    return isinstance(entry, dict) and "tools" in (entry.get("supported_parameters") or [])


def _first_candidate(models_index: Dict[str, dict], candidates, *, need_tools: bool = False,
                     need_images: bool = False, exclude=()) -> Optional[str]:
    """First id from a curated list that is live, free and capable — the
    guard never adds a model nobody has smoke-tested (see FALLBACK_CANDIDATES)."""
    for cid in candidates:
        if cid in exclude:
            continue
        entry = models_index.get(cid)
        if classify_model(cid, models_index).state != FREE:
            continue
        if need_tools and not _supports_tools(entry):
            continue
        if need_images and not _accepts_images(entry):
            continue
        return cid
    return None


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
        for model_id, labels, st in _group_aux(plan.auxiliary):
            print(f"{tag}aux      {model_id} [{len(labels)}: {_labels_text(labels, 4)}]: "
                  f"{st.pricing_text()}")
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
        text = format_message(n, outcome if n.key.startswith(("promoted:", "rewrite:")) else None)
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
