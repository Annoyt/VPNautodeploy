"""Thin client for the host-side kimi-bridge HTTP service.

kimi-code is installed on the host (/root/.kimi-code), not in this
container. The bridge wraps `kimi -p ... -r <session>` behind a tiny
HTTP API; we talk to it via http://host.docker.internal:7077 (set up
in docker-compose via extra_hosts: "host.docker.internal:host-gateway").

Per-chat session_ids are persisted in the bot DB so a follow-up message
in the same Telegram thread continues the same kimi context.
"""

from __future__ import annotations

import logging
import os
import sqlite3
import tempfile
import time
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# Prepended to the very first user prompt in a fresh kimi session so the
# model learns our file-handover convention. Kimi keeps it in context for
# the rest of the session (262K tokens), so we only pay this cost once.
SYSTEM_PREAMBLE = (
    "Ты — ассистент админа NekoVPN, работаешь под root на проде. "
    "Когда хочешь прислать файл админу в Telegram, "
    "вставь в свой ответ маркер ровно в формате:\n"
    "  [[SEND_FILE: /абсолютный/путь | необязательная подпись]]\n"
    "Бот вытащит этот маркер, скачает файл с хоста через bridge и "
    "отправит как document. Маркер можно использовать несколько раз. "
    "Перед опасными операциями (rm, docker compose down, systemctl restart) "
    "сначала спрашивай подтверждение. Никогда не печатай содержимое "
    "/opt/vpn-bot/.env, /root/.kimi-code/credentials/* и токены. "
    "При обработке запросов, содержащих команды проверки, исправления или диагностики "
    "(например: 'проверь', 'почини', 'исправь', 'диагностируй', 'статус'), "
    "в первую очередь обратись к своим загруженным навыкам (skills: vpn-ops, server-admin, code-review) "
    "и строго следуй их регламентам и шагам. "
    "Дальше идёт сообщение от админа:\n\n"
)


# Domain markers — high-signal substrings that pin a prompt to a
# specific skill. Order in the tuple doesn't matter; case-insensitive
# substring match. Adding a false-positive marker costs Kimi one extra
# `cat SKILL.md` (a few KB); missing a true-positive costs a misrouted
# answer, so err on the side of more markers.
VPN_OPS_MARKERS = (
    # x-ui / xray ecosystem
    "x-ui", "xui", "x ui", "xray", "vless", "reality", "vmess",
    "sni", "sid", "pbk", "spx", "inbound", "xray config", "конфиг xray",
    # node topology
    "узел", "узл", "нод", "node", "exit node", "entry node",
    "vpn-bot", "vpn bot",
    # clients & keys
    "клиент", "client", "ключ", "vless://", "uuid", "qr",
    # connectivity (when issue is "не подключается" rather than "не оплатил")
    "соединен", "connect", "connection", "подключ", "коннект",
    "пинг", "ping", "tunnel", "тоннель",
    "iptables", "snat", "dnat", "роут", "route", "маршрут",
)
SERVER_ADMIN_MARKERS = (
    # docker
    "docker", "контейнер", "container", "compose", "image", "образ",
    "rebuild", "пересобр", "пересоздай", "rebuild image",
    # systemd
    "systemd", "systemctl", "journalctl", "journal", "демон", "daemon",
    "сервис", "service",
    # logs
    "лог", "logs", "журнал", "stderr", "stdout", "tail",
    # certs / web
    "сертификат", "certificate", "letsencrypt", "let's encrypt",
    "tls", "ssl", "caddy", "nginx",
    # SSH / git deploy
    "ssh", "ssh-key", "ssh ключ", "rsync", "scp",
    # state-change verbs (server-side)
    "перезапу", "запуст", "останов", "выключ", "включ",
    "restart", "reboot", "ребут", "stop", "start", "kill",
    "deploy", "redeploy", "rollback", "разверн", "редеплой",
    # symptoms (server-side)
    "упал", "падает", "падае", "crashed", "crash", "hung", "hang",
    "frozen", "freeze", "виси", "unreachable", "unavailable",
    "недоступ", "не отвеча", "unresponsive",
    # resources
    "memory", "память", "оом", "oom", "cpu", "disk", "диск",
    "процесс", "process", "port", "порт", "dns",
    "права", "permission", "chmod", "chown",
)
CODE_REVIEW_MARKERS = (
    "ревью", "review", "ауди", "audit",
    "pr ", " pr", "pull request", "pull-request",
    "merge", "diff", "коммит", "commit", "патч", "patch",
    "закоммит", "запуш", "закоммить", "запушить", "git push",
    "проверь код", "посмотри код", "look at the code", "code review",
    "refactor", "рефактор",
)


# Mass outage / incident-response triggers. Distinct from per-user
# troubleshooting (which goes to vpn-ops / server-admin) because the
# response is procedural: confirm scope, triage in order, broadcast.
INCIDENT_RESPONSE_MARKERS = (
    "лежит весь", "лежит прод", "лежит всё", "лежит все",
    "не работает у всех", "у всех не работает", "у всех клиентов",
    "все клиенты не работ", "все клиенты не подключ", "все не работ",
    "all users", "all clients", "all down", "everyone down",
    "упало всё", "упало все", "fell over",
    "паника", "panic", "outage", "авария", "инцидент", "incident",
    "массовый", "mass", "массово",
    "прод лежит", "production down", "prod down", "production is down",
    "срочно", "urgent", "asap", "p0", "p1",
)

# Billing / subscription / quota / payment triggers. Distinct from
# vpn-ops because the response is about DB state and money flow, not
# Xray internals.
DPI_ANALYSIS_MARKERS = (
    # English
    "dpi", "deep packet", "handshake fail", "hsfail", "tcp rst", "rst spike",
    "censorship", "blocking", "probing", "active probe", "scanner",
    "short session", "session drop", "throttle", "reality fail",
    "blocked", "censor",
    # Russian
    "дпи", "блокир", "цензур", "зондир", "режут", "коротк сесси",
    "хендшейк", "хэндшейк", "handshake", "reality",
    "проб", "детектиров", "обнаруж", "флаг",
    # Specific from this codebase
    "dpi_metrics", "as_org", "country/asn", "asn/country",
)


BILLING_OPS_MARKERS = (
    # payment verbs and money words
    "оплат", "оплати", "оплатил", "оплачен",
    "payment", "pay ", "paying", "paid",
    "платё", "плати", "платил", "не оплачено", "неоплат",
    # subscription lifecycle
    "продл", "extend", "renew", "renewal",
    "истёк", "истек", "истекает", "expir",
    "subscription", "подписк",
    # receipts and references
    "чек", "receipt", "квитанц",
    # plans / pricing
    "тариф", "plan", "pricing", "стоимост",
    # money
    "рубл", "money", "деньги", "сумма", "amount",
    "карт", "card",
    # refunds
    "refund", "вернуть деньги", "возврат",
    # quota grants
    "grant", "грант", "выдай гб", "добав гб", "добав gb",
    "сколько гб", "сколько до конца",
)
# Generic trouble/diagnostic markers — used only when no specific
# domain matched. They imply "do something about a problem" without
# pointing at a specific skill, so the reminder falls back to "ls
# skills/ and pick".
GENERIC_TROUBLE_MARKERS = (
    "провер", "почин", "исправ", "диагности", "чекн", "статус",
    "настрой", "проблем", "сломал", "не работа",
    "ошибк", "сбой",
    "check", "fix", "repair", "diagnose", "status", "troubleshoot",
    "broken", "issue", "error", "errors", "exception", "traceback",
    "что не так", "почему не", "what's wrong", "whats wrong",
)


def _detect_skill_domains(prompt: str) -> list[str]:
    """Return the list of skill names whose markers match the prompt.

    Incident-response wins over everything else if it fires — when prod
    is on fire the right answer is "triage by runbook", not "check
    Xray config". Falls back to ``["__generic__"]`` if only the generic
    trouble list matched. Empty list means "no skill routing needed".
    """
    lower = prompt.lower()

    # Incident takes priority — short-circuit so we don't drown the
    # reminder in 4 skill paths during an outage.
    if any(m in lower for m in INCIDENT_RESPONSE_MARKERS):
        return ["incident-response"]

    domains: list[str] = []
    # DPI takes priority right after incident-response — when DPI is
    # active the runbook is specific to that skill and we don't want
    # the generic vpn-ops reminder drowning it.
    if any(m in lower for m in DPI_ANALYSIS_MARKERS):
        domains.append("dpi-analysis")
    if any(m in lower for m in VPN_OPS_MARKERS):
        domains.append("vpn-ops")
    if any(m in lower for m in SERVER_ADMIN_MARKERS):
        domains.append("server-admin")
    if any(m in lower for m in CODE_REVIEW_MARKERS):
        domains.append("code-review")
    if any(m in lower for m in BILLING_OPS_MARKERS):
        domains.append("billing-ops")
    if not domains and any(m in lower for m in GENERIC_TROUBLE_MARKERS):
        return ["__generic__"]
    return domains


def _build_skill_reminder(domains: list[str]) -> str:
    if domains == ["__generic__"]:
        return (
            "\n\n[SYSTEM REMINDER: Запрос на проверку/диагностику/исправление. "
            "Перед действиями: `ls /root/.kimi-code/skills/`, прочитай SKILL.md "
            "подходящего навыка и следуй его регламенту.]"
        )
    paths = ", ".join(
        f"/root/.kimi-code/skills/{d}/SKILL.md" for d in domains
    )
    return (
        f"\n\n[SYSTEM REMINDER: Запрос относится к навыкам: {', '.join(domains)}. "
        f"ОБЯЗАТЕЛЬНО прочитай {paths} перед выполнением "
        f"и строго следуй описанным там шагам.]"
    )


class KimiBridgeUnavailable(RuntimeError):
    """Bridge URL is not configured or the service didn't reply at all."""


class KimiBridgeError(RuntimeError):
    """Bridge returned an HTTP error or kimi reported a failure."""


class KimiClient:
    """Stateless HTTP client + SQLite-backed per-session-key memory.

    `session_key` is whatever the caller decides identifies a conversation:
    `"pm:{chat_id}"` for private messages, `"topic:{chat_id}:{thread_id}"`
    for forum topics. The class doesn't care about the format.
    """

    # Substrings in bridge error details that indicate the kimi session
    # history is corrupted (e.g. a previous timeout left a dangling
    # tool_call without a response). Retrying with a fresh session fixes it.
    _CORRUPTED_SESSION_MARKERS = (
        "tool_call_id",
        "tool_calls",
        "tool messages responding",
    )

    def __init__(
        self,
        base_url: str,
        token: str,
        db_path: str,
        default_timeout: int = 300,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.db_path = db_path
        self.default_timeout = default_timeout
        self._ensure_table()

    # ----- persistent session memory -----

    def _conn(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def _ensure_table(self) -> None:
        with self._conn() as c:
            c.execute(
                """
                CREATE TABLE IF NOT EXISTS ai_sessions (
                    session_key   TEXT PRIMARY KEY,
                    kimi_session  TEXT NOT NULL,
                    created_at    INTEGER NOT NULL,
                    updated_at    INTEGER NOT NULL,
                    message_count INTEGER NOT NULL DEFAULT 0
                )
                """
            )

    def get_session(self, session_key: str) -> Optional[str]:
        with self._conn() as c:
            row = c.execute(
                "SELECT kimi_session FROM ai_sessions WHERE session_key = ?",
                (session_key,),
            ).fetchone()
        return row[0] if row else None

    def remember_session(self, session_key: str, kimi_session: str) -> None:
        now = int(time.time())
        with self._conn() as c:
            c.execute(
                """
                INSERT INTO ai_sessions (session_key, kimi_session, created_at, updated_at, message_count)
                VALUES (?, ?, ?, ?, 1)
                ON CONFLICT(session_key) DO UPDATE SET
                    kimi_session  = excluded.kimi_session,
                    updated_at    = excluded.updated_at,
                    message_count = ai_sessions.message_count + 1
                """,
                (session_key, kimi_session, now, now),
            )

    def forget_session(self, session_key: str) -> bool:
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM ai_sessions WHERE session_key = ?",
                (session_key,),
            )
            return cur.rowcount > 0

    # ----- bridge calls -----

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def ping(self) -> dict:
        if not self.is_configured():
            raise KimiBridgeUnavailable("KIMI_BRIDGE_URL not set")
        try:
            r = requests.get(f"{self.base_url}/health", timeout=5)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise KimiBridgeUnavailable(str(e)) from e

    def _do_ask(
        self,
        prompt: str,
        kimi_session: Optional[str],
        *,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        mode: Optional[str] = None,
        yolo: bool = False,
    ) -> requests.Response:
        """Fire a single /ask request to the bridge. Returns raw Response."""
        payload = {"prompt": prompt}
        if kimi_session:
            payload["session_id"] = kimi_session
        if model:
            payload["model"] = model
        if timeout:
            payload["timeout"] = timeout
        if mode:
            payload["mode"] = mode
        if yolo:
            payload["yolo"] = True

        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["X-Bridge-Token"] = self.token

        return requests.post(
            f"{self.base_url}/ask",
            json=payload,
            headers=headers,
            timeout=(timeout or self.default_timeout) + 15,
        )

    @staticmethod
    def _is_corrupted_session_error(detail: str) -> bool:
        """Check if an error detail indicates a corrupted kimi session."""
        lower = detail.lower()
        return any(m in lower for m in KimiClient._CORRUPTED_SESSION_MARKERS)

    def ask(
        self,
        session_key: str,
        prompt: str,
        *,
        model: Optional[str] = None,
        timeout: Optional[int] = None,
        mode: Optional[str] = None,
        yolo: bool = False,
    ) -> Tuple[str, int]:
        """Send a prompt under the given session_key. Returns (reply, ms).

        mode="plan"  → kimi runs with --plan (deeper thinking, slower)
        mode="fast"  → no extra flag, default kimi behavior (default)
        mode=None    → bridge picks default (currently "fast")

        If the bridge returns a 502 due to a corrupted session (e.g. dangling
        tool_call from a previous timeout), the client automatically drops the
        session and retries once with a fresh context.
        """
        if not self.is_configured():
            raise KimiBridgeUnavailable("KIMI_BRIDGE_URL not set")

        # Domain-aware skill routing: detect which skill(s) the prompt
        # touches and inject a reminder pointing at the exact SKILL.md
        # path. Old behavior was a flat keyword list + generic "look in
        # skills/" reminder, which made Kimi waste a turn on `ls` and
        # sometimes pick the wrong skill. Substring match, case-insensitive.
        domains = _detect_skill_domains(prompt)
        if domains:
            prompt = prompt + _build_skill_reminder(domains)

        kimi_session = self.get_session(session_key)

        # First message in a fresh session — prepend our system preamble so
        # Kimi learns the [[SEND_FILE: ...]] convention and the don't-print-secrets
        # rule for the rest of the session.
        effective_prompt = prompt if kimi_session else (SYSTEM_PREAMBLE + prompt)

        try:
            r = self._do_ask(
                effective_prompt, kimi_session,
                model=model, timeout=timeout, mode=mode, yolo=yolo,
            )
        except requests.RequestException as e:
            raise KimiBridgeUnavailable(str(e)) from e

        # --- Auto-retry on corrupted session ---
        # When a previous kimi invocation timed out mid-tool-call, the session
        # history contains a dangling tool_call without a response. The API
        # provider rejects this with HTTP 400/502. We detect this pattern,
        # drop the corrupted session, and retry once with a clean context.
        if r.status_code in (400, 502) and kimi_session:
            try:
                detail = str(r.json().get("detail", r.text))
            except Exception:
                detail = r.text
            if self._is_corrupted_session_error(detail):
                logger.warning(
                    "Corrupted session detected for %s (session=%s), "
                    "dropping and retrying with fresh context: %s",
                    session_key, kimi_session, detail[:200],
                )
                self.forget_session(session_key)
                # Retry with a fresh session (no session_id, with preamble).
                effective_prompt = SYSTEM_PREAMBLE + prompt
                try:
                    r = self._do_ask(
                        effective_prompt, None,
                        model=model, timeout=timeout, mode=mode, yolo=yolo,
                    )
                except requests.RequestException as e:
                    raise KimiBridgeUnavailable(str(e)) from e

        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise KimiBridgeError(f"HTTP {r.status_code}: {str(detail)[:400]}")

        data = r.json()
        new_session = data.get("session_id")
        if new_session and new_session.startswith("session_"):
            self.remember_session(session_key, new_session)
        return data.get("response", ""), int(data.get("duration_ms", 0))

    def download_file(self, host_path: str) -> str:
        """Stream a file from the host (where kimi runs) into a local temp.

        Returns the local path. Caller is responsible for cleaning it up
        with os.unlink(). Raises KimiBridge* on failure.
        """
        if not self.is_configured():
            raise KimiBridgeUnavailable("KIMI_BRIDGE_URL not set")

        headers = {}
        if self.token:
            headers["X-Bridge-Token"] = self.token

        try:
            r = requests.get(
                f"{self.base_url}/file",
                params={"path": host_path},
                headers=headers,
                stream=True,
                timeout=60,
            )
        except requests.RequestException as e:
            raise KimiBridgeUnavailable(str(e)) from e

        if r.status_code >= 400:
            try:
                detail = r.json().get("detail", r.text)
            except Exception:
                detail = r.text
            raise KimiBridgeError(f"HTTP {r.status_code}: {str(detail)[:200]}")

        suffix = os.path.splitext(host_path)[1]
        # delete=False — the caller (AIHandler) hands the path to
        # send_document(), which opens the file itself; we unlink after.
        with tempfile.NamedTemporaryFile(
            prefix="kimi-",
            suffix=suffix or "",
            delete=False,
        ) as tmp:
            for chunk in r.iter_content(chunk_size=64 * 1024):
                if chunk:
                    tmp.write(chunk)
            local_path = tmp.name
        return local_path
