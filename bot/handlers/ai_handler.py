"""AI commands + auto-routing to kimi-code.

Three ways the admin can talk to Kimi from Telegram:

1. /ai <prompt>           — any chat (PM, forum group, even a non-AI topic).
2. /ai_reset              — forget the current session for THIS chat/topic.
3. Plain message in TOPIC_AI — if the message_thread_id matches the
   TOPIC_AI env var, every non-command admin message is forwarded
   as a prompt, no /ai prefix required.

Conversation memory keys:
- PM:    "pm:<chat_id>"
- Group: "topic:<chat_id>:<thread_id_or_0>"
"""

from __future__ import annotations

import html
import logging
import os
import re
import threading
import time
from typing import List, Optional


# Shared scratch dir between this container and the host (mounted via
# docker-compose). Photos pulled from Telegram land here so the agent on
# the host can read them off the same path. Cleaned hourly by the
# NotificationService scheduler; per-request cleanup happens in
# handle().
TG_MEDIA_DIR = "/tmp/tg_media"

# Shared out dir: OpenCode (on the host) writes files it wants to send
# back to the admin here; the bot reads them from the same bind-mount and
# ships them as Telegram documents. See _send_file / SYSTEM_PREAMBLE.
AGENT_OUT_DIR = "/tmp/agent_out"

from bot.handlers.base import BaseHandler
from bot.services.agent_client import (
    AgentError,
    AgentUnavailable,
    AgentClient,
)

logger = logging.getLogger(__name__)

# Hard cap on the chunk we send back to Telegram per message. The full
# Telegram limit is 4096 chars; leave headroom for HTML tags + a footer.
TELEGRAM_TEXT_LIMIT = 4000

# Kimi can emit one or more file markers in its reply: the bot strips the
# marker out of the text and sends each file as a Telegram document.
# Format the system prompt asks Kimi to use:   [[SEND_FILE: /path/to/file]]
# (optional caption on the same line, before the closing brackets).
FILE_MARKER_RE = re.compile(r"\[\[SEND_FILE:\s*(?P<path>[^\]\|]+?)(?:\s*\|\s*(?P<caption>[^\]]+))?\]\]")

# Telegram cancels a typing indicator after ~5 seconds. Refresh it every
# 4 seconds while Kimi is thinking so the admin sees a continuous "typing".
TYPING_REFRESH_SEC = 4.0


def _start_typing_keepalive(bot, chat_id: str, thread_id) -> threading.Event:
    """Spin a daemon thread that re-sends sendChatAction("typing") every
    TYPING_REFRESH_SEC until the returned Event is set."""
    stop = threading.Event()

    def _loop():
        while not stop.is_set():
            try:
                bot.client.send_chat_action(
                    chat_id=chat_id,
                    action="typing",
                    message_thread_id=thread_id,
                )
            except Exception:
                pass
            # Don't busy-wait; the Event lets us interrupt the sleep cleanly.
            stop.wait(TYPING_REFRESH_SEC)

    t = threading.Thread(target=_loop, name="kimi-typing", daemon=True)
    t.start()
    return stop


def _extract_files(text: str) -> tuple[str, List[tuple[str, Optional[str]]]]:
    """Pull every [[SEND_FILE: ...]] marker out of `text`.

    Returns the cleaned text + a list of (path, optional_caption) tuples.
    Bot will send each as a Telegram document after the main reply.
    """
    files: List[tuple[str, Optional[str]]] = []

    def _capture(m: re.Match) -> str:
        path = (m.group("path") or "").strip()
        caption = (m.group("caption") or "").strip() or None
        if path:
            files.append((path, caption))
        return ""

    cleaned = FILE_MARKER_RE.sub(_capture, text).strip()
    return cleaned, files


class AIHandler(BaseHandler):
    """Handles /ai, /ai_reset, and free-text inside TOPIC_AI."""

    def __init__(self, bot, db, config) -> None:
        super().__init__(bot, db, config)
        self._client: Optional[AgentClient] = None
        # Conversations with an agent turn currently running in a worker
        # thread — one in-flight turn per session key.
        self._inflight: set = set()
        self._inflight_lock = threading.Lock()
        # Last spawned turn thread — tests join it to assert
        # deterministically; ops can use it for graceful shutdown.
        self._last_turn_thread: Optional[threading.Thread] = None
        try:
            ai_timeout = int(getattr(config, "OPENCODE_TIMEOUT", 600) or 600)
        except (TypeError, ValueError):
            ai_timeout = 600
        if getattr(config, "OPENCODE_URL", ""):
            self._client = AgentClient(
                base_url=config.OPENCODE_URL,
                password=getattr(config, "OPENCODE_SERVER_PASSWORD", ""),
                db_path=config.DB_PATH,
                username=getattr(config, "OPENCODE_USERNAME", "opencode"),
                default_timeout=ai_timeout,
                default_model=getattr(config, "OPENCODE_DEFAULT_MODEL", "") or None,
                agent_default=getattr(config, "OPENCODE_AGENT_DEFAULT", "") or None,
                agent_plan=getattr(config, "OPENCODE_AGENT_PLAN", "") or None,
                agent_yolo=getattr(config, "OPENCODE_AGENT_YOLO", "") or None,
                node_type=getattr(config, "AGENT_NODE_TYPE", "control"),
                sshfs_mount=getattr(config, "ENTRY_NODE_SSHFS_MOUNT", "/mnt/entry_node"),
            )

    # ----- Routing -----

    def can_handle(self, update: dict) -> bool:
        msg = update.get("message") or {}
        # Accept text-only OR photo (with optional caption). A photo
        # message has no "text" field — only "caption" — so the old
        # `if not text: return False` path silently dropped every
        # screenshot the admin pasted into the topic.
        text = (msg.get("text") or msg.get("caption") or "").strip()
        has_photo = bool(msg.get("photo"))
        if not text and not has_photo:
            return False

        from_id = (msg.get("from") or {}).get("id")
        is_admin = self.config.is_admin(str(from_id))
        if not is_admin:
            return False

        # Slash commands we own (admin-only). Caption can also start
        # with a slash command when admin sends a photo with /ai_plan
        # in the caption.
        first_token = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""
        if first_token in ("/ai", "/ai_reset", "/ai_fast", "/ai_plan",
                            "/ai_status", "/ai_skill", "/ai_yolo"):
            return True

        # Free-text or photo inside TOPIC_AI from the super-admin.
        topic_ai = getattr(self.config, "TOPIC_AI", 0)
        if not topic_ai:
            return False
        if msg.get("message_thread_id") != topic_ai:
            return False
        if text.startswith("/"):  # unknown / non-AI slash command
            return False
        return True

    def handle(self, update: dict) -> None:
        msg = update.get("message") or {}
        # Caption falls back to empty string; same fallback as can_handle.
        text = (msg.get("text") or msg.get("caption") or "").strip()
        chat_id = str((msg.get("chat") or {}).get("id"))
        thread_id = msg.get("message_thread_id")
        from_id = str((msg.get("from") or {}).get("id"))
        has_photo = bool(msg.get("photo"))

        # Authorisation: only super-admin.
        if not self.config.is_admin(from_id):
            self._reply(chat_id, thread_id, "🚫 Только админ.")
            return

        # Download photo (if any) to the shared /tmp/tg_media volume so
        # Kimi can read it off the host. We delete it again in the
        # `finally` block at the bottom of this method — temp files
        # should not pile up.
        photo_path: Optional[str] = None
        if has_photo:
            photo_path = self._download_photo(msg, chat_id, thread_id)

        first = text.split(maxsplit=1)[0].split("@", 1)[0].lower() if text else ""

        if first == "/ai_reset":
            self._handle_reset(chat_id, thread_id, from_id)
            return

        if first == "/ai_status":
            self._handle_status(chat_id, thread_id)
            return

        if first == "/ai_skill":
            self._handle_skill(chat_id, thread_id, text)
            return

        if first == "/ai_yolo":
            self._handle_yolo(chat_id, thread_id, text)
            return

        # Mode resolution: /ai_plan forces deep thinking, /ai_fast skips it,
        # /ai uses AI_DEFAULT_MODE from config. Free-text in TOPIC_AI also
        # follows the default.
        mode: Optional[str] = None
        if first == "/ai_fast":
            mode = "fast"
            command = first
        elif first == "/ai_plan":
            mode = "plan"
            command = first
        elif first == "/ai":
            command = first
        else:
            command = None  # free-text in TOPIC_AI

        if command:
            prompt = text[len(command):].lstrip()
            if not prompt and not photo_path:
                self._reply(
                    chat_id, thread_id,
                    f"Использование: <code>{command} твой запрос</code>",
                    parse_mode="HTML",
                )
                return
        else:
            prompt = text

        # Splice the photo path into the prompt so Kimi knows where
        # the screenshot lives. If the admin sent a photo without any
        # caption (e.g. drag-and-drop), fall back to a generic question
        # so Kimi has something to respond to.
        if photo_path:
            attach = f"[Вложение от админа: {photo_path}]"
            prompt = (
                f"{attach}\n\n{prompt}" if prompt
                else f"{attach}\n\nПосмотри файл и опиши что видишь."
            )

        try:
            self._handle_prompt(chat_id, thread_id, prompt, mode=mode)
        finally:
            # Always delete the local photo, even if Kimi errored out.
            # The hourly scheduler is a safety net — this is the primary
            # cleanup so /tmp/tg_media doesn't accumulate.
            if photo_path:
                try:
                    os.unlink(photo_path)
                except OSError as e:
                    logger.warning(f"AI photo cleanup failed for {photo_path}: {e}")

    # ----- Worker actions -----

    def _download_photo(self, msg: dict, chat_id: str, thread_id) -> Optional[str]:
        """Pull the largest photo size from a message into TG_MEDIA_DIR.

        Telegram's ``message.photo`` is an array of progressively
        larger thumbnails; the last element is the original. We take
        that, hand the file_id to ``TelegramClient.download_file`` and
        return the absolute path on success. The caller is responsible
        for cleaning the file up after use (see ``handle`` finally).
        """
        photos = msg.get("photo") or []
        if not photos:
            return None
        largest = photos[-1]
        file_id = largest.get("file_id")
        if not file_id:
            return None

        ts = int(time.time())
        filename = f"tg_photo_{chat_id}_{ts}_{file_id[-8:]}.jpg"
        local_path = os.path.join(TG_MEDIA_DIR, filename)

        if not self.bot.client.download_file(file_id, local_path):
            self._reply(
                chat_id, thread_id,
                "⚠️ Не удалось скачать фото с Telegram.",
            )
            return None

        logger.info(f"AI: saved Telegram photo to {local_path}")
        return local_path

    def _session_key(self, chat_id: str, thread_id: Optional[int]) -> str:
        if thread_id:
            return f"topic:{chat_id}:{thread_id}"
        return f"pm:{chat_id}"

    def _handle_reset(self, chat_id: str, thread_id, from_id: str) -> None:
        if not self.config.is_admin(from_id):
            self._reply(chat_id, thread_id, "🚫 Только админ.")
            return
        if not self._client:
            self._reply(chat_id, thread_id, "⚠️ AI не сконфигурирован.")
            return
        key = self._session_key(chat_id, thread_id)
        existed = self._client.forget_session(key)
        self._reply(
            chat_id, thread_id,
            "🔄 Сессия очищена." if existed else "ℹ️ Активной сессии не было.",
        )

    def _handle_status(self, chat_id: str, thread_id) -> None:
        if not self._client:
            self._reply(chat_id, thread_id, "⚠️ AI не сконфигурирован — задайте OPENCODE_URL.")
            return

        try:
            health_info = self._client.ping()
            status_text = "OK" if health_info.get("status") == "ok" else "Degraded"
            version_text = health_info.get("version", "unknown")
        except Exception as e:
            status_text = f"Unavailable ({e})"
            version_text = "unknown"

        total_sessions = 0
        try:
            with self.db._connect() as conn:
                cur = conn.cursor()
                row = cur.execute("SELECT COUNT(*) FROM ai_sessions").fetchone()
                if row:
                    total_sessions = row[0]
        except Exception as e:
            logger.warning(f"Failed to query ai_sessions: {e}")

        key = self._session_key(chat_id, thread_id)
        current_session = self._client.get_session(key)

        msg_text = (
            "🤖 <b>AI Status:</b>\n"
            f"• <b>Server Status:</b> {status_text}\n"
            f"• <b>OpenCode Version:</b> <code>{version_text}</code>\n"
            f"• <b>Total Active Sessions:</b> {total_sessions}\n"
            f"• <b>Current Chat Session:</b> <code>{current_session or 'None'}</code>"
        )
        self._reply(chat_id, thread_id, msg_text, parse_mode="HTML")

    def _handle_skill(self, chat_id: str, thread_id, text: str) -> None:
        if not self._client:
            self._reply(chat_id, thread_id, "⚠️ AI не сконфигурирован.")
            return

        parts = text.split(maxsplit=2)
        if len(parts) < 3:
            self._reply(
                chat_id, thread_id,
                "Использование: <code>/ai_skill [vpn-ops|server-admin|code-review] твой запрос</code>",
                parse_mode="HTML",
            )
            return

        skill_name = parts[1].strip()
        prompt = parts[2].strip()

        allowed_skills = ("vpn-ops", "server-admin", "code-review")
        if skill_name not in allowed_skills:
            self._reply(
                chat_id, thread_id,
                f"⚠️ Неверное имя навыка. Доступные навыки: <code>{', '.join(allowed_skills)}</code>",
                parse_mode="HTML",
            )
            return

        effective_prompt = f"/skill:{skill_name} {prompt}"
        self._handle_prompt(chat_id, thread_id, effective_prompt)

    def _handle_yolo(self, chat_id: str, thread_id, text: str) -> None:
        if not self._client:
            self._reply(chat_id, thread_id, "⚠️ AI не сконфигурирован.")
            return

        parts = text.split(maxsplit=1)
        if len(parts) < 2:
            self._reply(
                chat_id, thread_id,
                "Использование: <code>/ai_yolo твой запрос</code>",
                parse_mode="HTML",
            )
            return

        prompt = parts[1].strip()
        self._handle_prompt(chat_id, thread_id, prompt, yolo=True)

    def _handle_prompt(
        self,
        chat_id: str,
        thread_id,
        prompt: str,
        *,
        mode: Optional[str] = None,
        yolo: bool = False,
    ) -> None:
        if not self._client:
            self._reply(chat_id, thread_id, "⚠️ AI не сконфигурирован — задайте OPENCODE_URL.")
            return

        # mode resolution: explicit /ai_plan or /ai_fast wins; otherwise the
        # configured default ("fast" by default, see settings.AI_DEFAULT_MODE).
        if mode is None:
            mode = getattr(self.config, "AI_DEFAULT_MODE", "fast") or "fast"

        key = self._session_key(chat_id, thread_id)

        # The agent call runs in a worker thread: handlers execute on the
        # polling thread, so a synchronous ask() froze the WHOLE bot for
        # up to the full agent timeout (minutes). One turn per session —
        # a second prompt while the first is running gets bounced.
        with self._inflight_lock:
            if key in self._inflight:
                self._reply(
                    chat_id, thread_id,
                    "⏳ Агент ещё работает над предыдущим вопросом — "
                    "дождись ответа, потом спрашивай дальше."
                )
                return
            self._inflight.add(key)

        # Keep "typing…" alive for the whole turn — Telegram cancels the
        # indicator after ~5 s and the agent in plan mode can think minutes.
        typing_stop = _start_typing_keepalive(self.bot, chat_id, thread_id)

        def _worker() -> None:
            try:
                self._run_agent_turn(chat_id, thread_id, key, prompt, mode, yolo)
            except Exception:
                logger.exception("unexpected agent worker failure")
                self._reply(chat_id, thread_id, "⚠️ Неожиданная ошибка, см. логи.")
            finally:
                typing_stop.set()
                with self._inflight_lock:
                    self._inflight.discard(key)

        t = threading.Thread(
            target=_worker, daemon=True, name=f"ai-turn-{chat_id}"
        )
        self._last_turn_thread = t
        t.start()

    def _run_agent_turn(
        self,
        chat_id: str,
        thread_id,
        key: str,
        prompt: str,
        mode: Optional[str],
        yolo: bool,
    ) -> None:
        """The blocking part of a /ai turn. Runs on a worker thread."""
        try:
            reply, duration_ms = self._client.ask(key, prompt, mode=mode, yolo=yolo)
        except AgentUnavailable as e:
            logger.warning(f"opencode server unavailable: {e}")
            self._reply(chat_id, thread_id, f"⚠️ AI-сервер не отвечает: {e}")
            return
        except AgentError as e:
            logger.warning(f"opencode agent error: {e}")
            # Pretty-print the most common upstream conditions instead
            # of dumping the full provider message with stray HTML.
            err_text = str(e)
            low = err_text.lower()
            if 'rate_limit' in low or '429' in err_text:
                self._reply(
                    chat_id, thread_id,
                    "⏳ Квота API провайдера исчерпана.\n\n"
                    "Лимит обнулится в следующем биллинговом периоде, "
                    "либо переключи модель/провайдера в opencode.json."
                )
            elif '504' in err_text or 'timed out' in low:
                self._reply(
                    chat_id, thread_id,
                    "⏱ Агент не уложился в лимит времени и был остановлен. "
                    "Разбей задачу на шаги поменьше или спроси ещё раз — "
                    "контекст сессии сохранён."
                )
            else:
                self._reply(chat_id, thread_id, f"⚠️ Ошибка агента: {err_text[:300]}")
            return

        if not reply:
            reply = "(пустой ответ от агента)"

        # Pull out any [[SEND_FILE: ...]] markers the agent emitted so we can
        # ship those as Telegram documents after the text reply.
        clean_reply, files = _extract_files(reply)
        if not clean_reply:
            clean_reply = "(нет текстового ответа)" if files else "(пустой ответ от агента)"

        footer_bits = [f"⏱ {duration_ms/1000:.1f}s"]
        if mode == "plan":
            footer_bits.append("🧠 plan")
        elif mode == "fast":
            footer_bits.append("⚡ fast")
        if yolo:
            footer_bits.append("☠️ yolo")
        footer_bits.append("/ai_reset чтобы сбросить")
        footer = "\n\n<i>" + " · ".join(footer_bits) + "</i>"

        # The agent replies in plain text/markdown, not HTML — a raw '<'
        # (code, generics, reprs) kills sendMessage and the answer is
        # silently lost. Escape BEFORE truncating: escaping expands
        # '<' → '&lt;' etc., so a 4000-char reply can overshoot Telegram's
        # 4096 hard cap after escape.
        body = html.escape(clean_reply)
        if len(body) > TELEGRAM_TEXT_LIMIT:
            # don't leave a sliced-open entity like '&am' at the cut point
            body = re.sub(r'&[#a-zA-Z0-9]{0,7}$', '', body[:TELEGRAM_TEXT_LIMIT])
            body += "\n… (обрезано)"
        self._reply(chat_id, thread_id, body + footer, parse_mode="HTML")

        for path, caption in files:
            self._send_file(chat_id, thread_id, path, caption)

    def _send_file(self, chat_id: str, thread_id, host_path: str, caption: Optional[str]) -> None:
        """Send a file the agent referenced via [[SEND_FILE: ...]] as a Telegram document.

        OpenCode runs on the host and writes files it wants to hand back
        into the shared out dir (AGENT_OUT_DIR), which is bind-mounted into
        this container at the same path. So we read the file directly — no
        HTTP round-trip. Only paths under the shared dir are accepted, so a
        stray marker can't exfiltrate arbitrary host files through the bot.
        """
        norm = os.path.normpath(host_path)
        if not (norm == AGENT_OUT_DIR or norm.startswith(AGENT_OUT_DIR + os.sep)):
            self._reply(
                chat_id, thread_id,
                f"⚠️ Файл вне разрешённого каталога <code>{AGENT_OUT_DIR}</code>: "
                f"<code>{host_path}</code>",
                parse_mode="HTML",
            )
            return
        if not os.path.isfile(norm):
            self._reply(
                chat_id, thread_id,
                f"⚠️ Файл не найден: <code>{host_path}</code>",
                parse_mode="HTML",
            )
            return

        # Avoid passing message_thread_id=None — some Telegram API methods
        # reject it. Only include the kwarg when we have a real thread.
        extra_kwargs = {}
        if thread_id:
            extra_kwargs["message_thread_id"] = thread_id
        try:
            result = self.bot.client.send_document(
                chat_id=chat_id,
                document=norm,
                caption=caption,
                **extra_kwargs,
            )
            if not result:
                self._reply(
                    chat_id, thread_id,
                    f"⚠️ Telegram отверг файл <code>{host_path}</code>",
                    parse_mode="HTML",
                )
        except Exception as e:
            logger.warning(f"AI send_document failed for {host_path}: {e}")
            self._reply(
                chat_id, thread_id,
                f"⚠️ Не удалось отправить файл <code>{host_path}</code>: {e}",
                parse_mode="HTML",
            )
        finally:
            # Clean up the agent's output file after sending so /tmp/agent_out
            # doesn't accumulate. Best-effort.
            try:
                os.unlink(norm)
            except OSError:
                pass

    # ----- Telegram glue -----

    def _reply(self, chat_id: str, thread_id, text: str, parse_mode: Optional[str] = None) -> None:
        try:
            self.bot.send_message(
                chat_id=chat_id,
                text=text,
                message_thread_id=thread_id,
                parse_mode=parse_mode,
            )
        except Exception as e:
            logger.warning(f"Failed to send AI reply: {e}")
