"""Client for a local OpenCode server (`opencode serve`).

Replaces the old kimi-code CLI + hand-rolled FastAPI bridge. OpenCode
already ships a headless HTTP server, so there is no custom bridge
anymore — the bot talks straight to ``http://host.docker.internal:4096``
(or wherever ``opencode serve`` listens) using HTTP basic auth
(``OPENCODE_SERVER_PASSWORD``).

Why a thin hand-written client instead of the official ``opencode-ai``
SDK: the SDK pins to a generated OpenAPI snapshot and pulls httpx +
deps into the container; our usage is one prompt-per-turn with session
memory, so a small ``requests`` client in the existing project style is
enough and avoids version coupling. All the HTTP specifics live in
``_create_session`` / ``_send_message`` — if the pinned OpenCode version
changes an endpoint or field name, that's the only place to touch.
Response parsing is intentionally tolerant (accepts ``text`` or
``content`` parts, ``id``/``sessionID`` keys) so a minor server-side
shape change doesn't break the bot.

Per-chat session ids are persisted in the bot DB (``ai_sessions`` table,
reused as-is) so a follow-up message in the same Telegram thread
continues the same OpenCode context.
"""

from __future__ import annotations

import base64
import logging
import sqlite3
import time
from typing import Optional, Tuple

import requests

logger = logging.getLogger(__name__)


# Prepended to the very first user prompt in a fresh session so the model
# learns our project conventions. OpenCode keeps it in the session context
# for the rest of the conversation, so we only pay this cost once.
# NOTE: destructive-op safety is now enforced structurally by opencode.json
# permissions (ask/deny), not just by this preamble — but we keep the
# reminder as belt-and-suspenders plus the file-handover convention.
SYSTEM_PREAMBLE = (
    "Ты — ассистент админа NekoVPN, работаешь под root на проде. "
    "Когда хочешь прислать файл админу в Telegram, "
    "запиши файл в общий каталог /tmp/agent_out/ и вставь в свой ответ "
    "маркер ровно в формате:\n"
    "  [[SEND_FILE: /tmp/agent_out/имя_файла | необязательная подпись]]\n"
    "Бот прочитает файл из этого общего каталога и отправит как document. "
    "Маркер можно использовать несколько раз. "
    "Перед опасными операциями (rm, docker compose down, systemctl restart) "
    "сначала спрашивай подтверждение. Никогда не печатай содержимое "
    "/opt/vpn-bot/.env, секретов и токенов. "
    "При обработке запросов, содержащих команды проверки, исправления или диагностики "
    "(например: 'проверь', 'почини', 'исправь', 'диагностируй', 'статус'), "
    "в первую очередь обратись к своим навыкам (skills: vpn-ops, server-admin, code-review) "
    "и строго следуй их регламентам и шагам. "
    "Дальше идёт сообщение от админа:\n\n"
)


# Domain markers — high-signal substrings that pin a prompt to a
# specific skill. Case-insensitive substring match. Adding a
# false-positive marker costs the agent one extra skill read; missing a
# true-positive costs a misrouted answer, so err on the side of more.
VPN_OPS_MARKERS = (
    "x-ui", "xui", "x ui", "xray", "vless", "reality", "vmess",
    "sni", "sid", "pbk", "spx", "inbound", "xray config", "конфиг xray",
    "узел", "узл", "нод", "node", "exit node", "entry node",
    "vpn-bot", "vpn bot",
    "клиент", "client", "ключ", "vless://", "uuid", "qr",
    "соединен", "connect", "connection", "подключ", "коннект",
    "пинг", "ping", "tunnel", "тоннель",
    "iptables", "snat", "dnat", "роут", "route", "маршрут",
)
SERVER_ADMIN_MARKERS = (
    "docker", "контейнер", "container", "compose", "image", "образ",
    "rebuild", "пересобр", "пересоздай", "rebuild image",
    "systemd", "systemctl", "journalctl", "journal", "демон", "daemon",
    "сервис", "service",
    "лог", "logs", "журнал", "stderr", "stdout", "tail",
    "сертификат", "certificate", "letsencrypt", "let's encrypt",
    "tls", "ssl", "caddy", "nginx",
    "ssh", "ssh-key", "ssh ключ", "rsync", "scp",
    "перезапу", "запуст", "останов", "выключ", "включ",
    "restart", "reboot", "ребут", "stop", "start", "kill",
    "deploy", "redeploy", "rollback", "разверн", "редеплой",
    "упал", "падает", "падае", "crashed", "crash", "hung", "hang",
    "frozen", "freeze", "виси", "unreachable", "unavailable",
    "недоступ", "не отвеча", "unresponsive",
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
DPI_ANALYSIS_MARKERS = (
    "dpi", "deep packet", "handshake fail", "hsfail", "tcp rst", "rst spike",
    "censorship", "blocking", "probing", "active probe", "scanner",
    "short session", "session drop", "throttle", "reality fail",
    "blocked", "censor",
    "дпи", "блокир", "цензур", "зондир", "режут", "коротк сесси",
    "хендшейк", "хэндшейк", "handshake", "reality",
    "проб", "детектиров", "обнаруж", "флаг",
    "dpi_metrics", "as_org", "country/asn", "asn/country",
)
BILLING_OPS_MARKERS = (
    "оплат", "оплати", "оплатил", "оплачен",
    "payment", "pay ", "paying", "paid",
    "платё", "плати", "платил", "не оплачено", "неоплат",
    "продл", "extend", "renew", "renewal",
    "истёк", "истек", "истекает", "expir",
    "subscription", "подписк",
    "чек", "receipt", "квитанц",
    "тариф", "plan", "pricing", "стоимост",
    "рубл", "money", "деньги", "сумма", "amount",
    "карт", "card",
    "refund", "вернуть деньги", "возврат",
    "grant", "грант", "выдай гб", "добав гб", "добав gb",
    "сколько гб", "сколько до конца",
)
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

    Incident-response wins over everything else if it fires. Falls back
    to ``["__generic__"]`` if only the generic trouble list matched.
    Empty list means "no skill routing needed".
    """
    lower = prompt.lower()

    if any(m in lower for m in INCIDENT_RESPONSE_MARKERS):
        return ["incident-response"]

    domains: list[str] = []
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
            "Перед действиями открой подходящий навык (skills/) и следуй его регламенту.]"
        )
    return (
        f"\n\n[SYSTEM REMINDER: Запрос относится к навыкам: {', '.join(domains)}. "
        f"ОБЯЗАТЕЛЬНО открой соответствующий(ие) навык(и) перед выполнением "
        f"и строго следуй описанным там шагам.]"
    )


class AgentUnavailable(RuntimeError):
    """Server URL is not configured or the service didn't reply at all."""


class AgentError(RuntimeError):
    """Server returned an HTTP error or the agent reported a failure."""


class AgentClient:
    """OpenCode-server HTTP client + SQLite-backed per-session-key memory.

    ``session_key`` is whatever the caller uses to identify a
    conversation: ``"pm:{chat_id}"`` for DMs, ``"topic:{chat_id}:{thread_id}"``
    for forum topics. The class doesn't care about the format.
    """

    def __init__(
        self,
        base_url: str,
        password: str,
        db_path: str,
        *,
        username: str = "opencode",
        default_timeout: int = 300,
        default_model: Optional[str] = None,
        agent_plan: Optional[str] = None,
        agent_yolo: Optional[str] = None,
        agent_default: Optional[str] = None,
        node_type: str = "control",
        sshfs_mount: str = "/mnt/entry_node",
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.username = username or "opencode"
        self.password = password or ""
        self.db_path = db_path
        self.default_timeout = default_timeout
        self.default_model = default_model or None
        # Optional named OpenCode agents (defined in opencode.json) mapped
        # to our mode/yolo switches. None → let the server pick its default.
        self.agent_plan = agent_plan or None
        self.agent_yolo = agent_yolo or None
        self.agent_default = agent_default or None
        self.node_type = node_type
        self.sshfs_mount = sshfs_mount
        self._ensure_table()

    # ----- persistent session memory (ai_sessions table, reused as-is) -----

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

    def remember_session(self, session_key: str, agent_session: str) -> None:
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
                (session_key, agent_session, now, now),
            )

    def forget_session(self, session_key: str) -> bool:
        agent_session = self.get_session(session_key)
        # Best-effort server-side delete; local forget is what matters.
        if agent_session:
            try:
                requests.delete(
                    f"{self.base_url}/session/{agent_session}",
                    headers=self._headers(),
                    timeout=15,
                )
            except requests.RequestException as e:
                logger.warning("opencode session delete failed: %s", e)
        with self._conn() as c:
            cur = c.execute(
                "DELETE FROM ai_sessions WHERE session_key = ?",
                (session_key,),
            )
            return cur.rowcount > 0

    # ----- HTTP plumbing -----

    def is_configured(self) -> bool:
        return bool(self.base_url)

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json"}
        if self.password:
            token = base64.b64encode(
                f"{self.username}:{self.password}".encode()
            ).decode()
            h["Authorization"] = f"Basic {token}"
        return h

    def ping(self) -> dict:
        """GET /global/health. Returns a dict with 'status' and 'version'
        so callers get a stable shape regardless of the server's wording."""
        if not self.is_configured():
            raise AgentUnavailable("OPENCODE_URL not set")
        try:
            r = requests.get(
                f"{self.base_url}/global/health",
                headers=self._headers(),
                timeout=5,
            )
            r.raise_for_status()
            data = r.json() if r.content else {}
        except requests.RequestException as e:
            raise AgentUnavailable(str(e)) from e
        healthy = bool(data.get("healthy", True))
        return {
            "status": "ok" if healthy else "degraded",
            "version": data.get("version", "unknown"),
        }

    def _create_session(self, timeout: int) -> str:
        """POST /session → new session id. Endpoint isolated here."""
        r = requests.post(
            f"{self.base_url}/session",
            json={"title": "nekovpn-admin"},
            headers=self._headers(),
            timeout=timeout,
        )
        if r.status_code >= 400:
            raise AgentError(f"create session HTTP {r.status_code}: {r.text[:300]}")
        data = r.json()
        sid = data.get("id") or data.get("sessionID") or data.get("session_id")
        if not sid:
            raise AgentError(f"create session: no id in response {str(data)[:200]}")
        return sid

    def _send_message(
        self,
        session_id: str,
        text: str,
        *,
        model: Optional[str],
        agent: Optional[str],
        timeout: int,
    ) -> requests.Response:
        """POST the user message to a session. Endpoint + body shape
        isolated here — the one place to adjust if the pinned OpenCode
        version renames the route or fields (verify against /doc)."""
        body: dict = {"parts": [{"type": "text", "text": text}]}
        if model:
            # OpenCode's message endpoint expects model as an object
            # {providerID, modelID}, not a "provider/model" string — a
            # bare string gets rejected with HTTP 400 "Expected object".
            if "/" in model:
                prov, mod = model.split("/", 1)
                body["model"] = {"providerID": prov, "modelID": mod}
            else:
                body["model"] = {"modelID": model}
        if agent:
            body["agent"] = agent
        return requests.post(
            f"{self.base_url}/session/{session_id}/message",
            json=body,
            headers=self._headers(),
            timeout=timeout + 15,
        )

    @staticmethod
    def _extract_text(data: dict) -> str:
        """Pull assistant text out of a prompt response. Tolerant of
        shape drift: accepts top-level ``parts`` or ``info.parts``, and
        parts using ``text`` or ``content``."""
        parts = data.get("parts")
        if parts is None and isinstance(data.get("info"), dict):
            parts = data["info"].get("parts")
        chunks: list[str] = []
        for p in parts or []:
            if not isinstance(p, dict):
                continue
            if p.get("type") not in (None, "text"):
                continue
            piece = p.get("text") or p.get("content")
            if isinstance(piece, str) and piece:
                chunks.append(piece)
        if chunks:
            return "\n".join(chunks).strip()
        # Last-resort fallbacks for older/simpler shapes.
        for key in ("response", "text", "content"):
            v = data.get(key)
            if isinstance(v, str) and v:
                return v.strip()
        return ""

    # ----- public API (same surface the old KimiClient exposed) -----

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

        mode="plan"  → uses the configured 'plan' agent (deeper thinking)
        mode="fast"  → default agent
        yolo=True    → uses the configured 'yolo' agent (fewer confirmations)

        If the server reports the stored session is gone (404), we drop it
        and retry once with a fresh session.
        """
        if not self.is_configured():
            raise AgentUnavailable("OPENCODE_URL not set")

        eff_timeout = timeout or self.default_timeout
        eff_model = model or self.default_model
        agent = self.agent_default
        if yolo and self.agent_yolo:
            agent = self.agent_yolo
        elif (mode or "").lower() == "plan" and self.agent_plan:
            agent = self.agent_plan

        # Domain-aware skill routing: inject a reminder pointing at the
        # relevant skill(s). Substring match, case-insensitive.
        domains = _detect_skill_domains(prompt)
        if domains:
            prompt = prompt + _build_skill_reminder(domains)

        session_id = self.get_session(session_key)
        is_fresh = session_id is None
        effective_prompt = prompt if not is_fresh else (SYSTEM_PREAMBLE + prompt)

        started = time.time()
        try:
            if session_id is None:
                session_id = self._create_session(eff_timeout)
            r = self._send_message(
                session_id, effective_prompt,
                model=eff_model, agent=agent, timeout=eff_timeout,
            )
        except requests.RequestException as e:
            raise AgentUnavailable(str(e)) from e

        # Stale session on the server → recreate once with the preamble.
        if r.status_code == 404 and not is_fresh:
            logger.warning(
                "opencode session %s gone for %s, recreating", session_id, session_key
            )
            self.forget_session(session_key)
            try:
                session_id = self._create_session(eff_timeout)
                r = self._send_message(
                    session_id, SYSTEM_PREAMBLE + prompt,
                    model=eff_model, agent=agent, timeout=eff_timeout,
                )
            except requests.RequestException as e:
                raise AgentUnavailable(str(e)) from e

        if r.status_code >= 400:
            try:
                detail = r.json().get("error", r.text)
            except Exception:
                detail = r.text
            raise AgentError(f"HTTP {r.status_code}: {str(detail)[:400]}")

        duration_ms = int((time.time() - started) * 1000)
        try:
            data = r.json()
        except ValueError as e:
            raise AgentError(f"bad JSON from opencode: {e}") from e

        self.remember_session(session_key, session_id)
        return self._extract_text(data), duration_ms
