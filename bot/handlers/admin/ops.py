"""Operational admin commands: status, find, recent, repair, topics, quota, expire, whoami.

These cover the gap between the dashboard (rich UI, mouse-friendly) and
inline ops where you just need a quick text answer in the same forum
topic — e.g. "what's the bot's RAM right now", "find user with uuid
starting with abc", "set @ivan's quota to 50".
"""

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from bot.config.constants import BYTES_PER_GB
from .base import AdminHandlerBase

logger = logging.getLogger(__name__)


def _fmt_bytes(n: float) -> str:
    """Render bytes as GB / MB / KB."""
    if n >= 1024 ** 3:
        return f"{n / 1024 ** 3:.2f} GB"
    if n >= 1024 ** 2:
        return f"{n / 1024 ** 2:.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{int(n)} B"


def _fmt_uptime(seconds: float) -> str:
    s = int(seconds)
    d, s = divmod(s, 86400)
    h, s = divmod(s, 3600)
    m, _ = divmod(s, 60)
    parts = []
    if d:
        parts.append(f"{d}d")
    if h:
        parts.append(f"{h}h")
    if m or not parts:
        parts.append(f"{m}m")
    return " ".join(parts)


class AdminOpsMixin(AdminHandlerBase):
    """In-chat ops commands."""

    # ----- /status -----

    def show_status(self, chat_id: str, args: list) -> None:
        """Compact health card: services, host metrics, user counts."""
        from bot.services.system_stats import SystemStatsService

        try:
            sys_stats = SystemStatsService.get_stats() or {}
        except Exception as e:
            sys_stats = {'error': str(e)}

        cpu = (sys_stats.get('cpu') or {}).get('percent', '—')
        ram = sys_stats.get('ram') or {}
        ram_pct = ram.get('percent', '—')
        ram_used = ram.get('used', 0)
        ram_total = ram.get('total', 0)
        disk = sys_stats.get('disk') or {}
        disk_pct = disk.get('percent', '—')
        disk_used = disk.get('used', 0)
        disk_total = disk.get('total', 0)
        uptime = _fmt_uptime(sys_stats.get('uptime', 0))

        # Service checks — anything that has a sync ping endpoint
        xui = self.bot.services.get('xui') if hasattr(self.bot, 'services') else None
        xui_ok = bool(xui and getattr(xui, 'db', None))

        kimi_status = '—'
        try:
            from bot.services.kimi_client import KimiClient, KimiBridgeUnavailable
            kbu = getattr(self.config, 'KIMI_BRIDGE_URL', '')
            if kbu:
                client = KimiClient(
                    kbu,
                    getattr(self.config, 'KIMI_BRIDGE_TOKEN', ''),
                    self.config.DB_PATH,
                )
                try:
                    h = client.ping()
                    kimi_status = h.get('status', '?')
                except KimiBridgeUnavailable as e:
                    kimi_status = f'down ({e})'[:40]
        except Exception as e:
            kimi_status = f'err: {e}'[:40]

        # User counts
        try:
            counts = {}
            for row in self.db.get_stats().get('by_status', {}).items():
                counts[row[0]] = row[1]
        except Exception:
            counts = {}

        active = counts.get('demo', 0) + counts.get('paid', 0) + counts.get('support_topic', 0)
        pending = counts.get('pending_demo', 0) + counts.get('platform_select', 0)
        total = sum(counts.values()) if counts else 0

        lines = [
            "🩺 <b>Status</b>",
            f"• Bot: <b>up</b> · Uptime <code>{uptime}</code>",
            f"• X-UI DB: {'<b>ok</b>' if xui_ok else '<b>missing</b>'}",
            f"• Kimi bridge: <b>{kimi_status}</b>",
            "",
            f"💻 CPU <b>{cpu}%</b> · RAM <b>{ram_pct}%</b> "
            f"({ram_used:.1f}/{ram_total:.1f} GB) · "
            f"Disk <b>{disk_pct}%</b> ({disk_used:.0f}/{disk_total:.0f} GB)",
            "",
            f"👥 Users: <b>{total}</b> total · <b>{active}</b> active "
            f"(demo {counts.get('demo', 0)} / paid {counts.get('paid', 0)} / "
            f"support {counts.get('support_topic', 0)}) · "
            f"<b>{pending}</b> pending · <b>{counts.get('rejected', 0)}</b> rejected · "
            f"<b>{counts.get('banned', 0)}</b> banned",
        ]
        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /whoami -----

    def show_whoami(self, chat_id: str, args: list) -> None:
        """Echo back the caller's id + admin flag — useful when callback
        ids look wrong or you wonder if your forum-group send is admin-
        gated correctly."""
        lines = [
            "🆔 <b>whoami</b>",
            f"• chat_id: <code>{chat_id}</code>",
            f"• super_admin_id: <code>{getattr(self.config, 'SUPER_ADMIN_ID', '?')}</code>",
            f"• forum group id: <code>{getattr(self.config, 'FORUM_GROUP_ID', '?')}</code>",
            f"• you are admin here: <b>yes</b>",
        ]
        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /onlines -----

    def show_onlines(self, chat_id: str, args: list) -> None:
        """Live snapshot of who's connected right now.

        Aggregates three sources: access.log (xray_log.summarize_activity)
        for emails + per-IP counts, 3x-ui's /onlines API for the panel
        signal, and the xray-reload sidecar's /tcp-stats for per-IP
        RTTs from `ss -tin` on the entry node. Output mirrors what the
        dashboard shows so the admin doesn't have to leave the chat to
        check who's live.
        """
        try:
            from bot.services.xray_log import summarize_activity
            from bot.services.xui_reload import get_tcp_stats
            activity = summarize_activity()
        except Exception as e:
            activity = {}
            logger.warning(f"/onlines: log parse failed: {e}")

        try:
            xui = self.bot.services.get('xui') if hasattr(self.bot, 'services') else None
            panel_emails: set = set()
            if xui and hasattr(xui, 'api') and xui.api:
                panel_emails = set(xui.api.get_online_clients_sync())
        except Exception as e:
            panel_emails = set()
            logger.warning(f"/onlines: xui api failed: {e}")

        try:
            rtt_by_ip = get_tcp_stats()
        except Exception:
            rtt_by_ip = {}

        # GeoIP (soft import — works even if maxminddb is missing)
        try:
            from bot.services.geoip import lookup as geo_lookup
        except Exception:
            geo_lookup = None

        emails = sorted(set(activity.keys()) | panel_emails)
        if not emails:
            self.bot.send_message(
                chat_id=chat_id,
                text="⚪ Сейчас никто не подключён.",
                message_thread_id=self._get_thread_id(chat_id),
            )
            return

        # Map email → user row (for username + consumed_gb).
        users_by_email = {}
        try:
            for u in self.db.get_all_users():
                if u.email:
                    users_by_email[u.email] = u
        except Exception as e:
            logger.warning(f"/onlines: users fetch failed: {e}")

        # Per-email traffic from x-ui
        traffic_by_email = {}
        try:
            xui_db = (
                self.bot.services.get('xui').db
                if hasattr(self.bot, 'services') and self.bot.services.get('xui')
                else None
            )
            if xui_db:
                traffic_by_email = xui_db.get_all_client_traffic() or {}
        except Exception as e:
            logger.warning(f"/onlines: traffic fetch failed: {e}")

        lines = [f"🟢 <b>Сейчас онлайн: {len(emails)}</b>\n"]
        for email in emails:
            user = users_by_email.get(email)
            uname = (f"@{user.username}" if user and user.username
                     else (f"user_{user.chat_id}" if user else email[:30]))
            act = activity.get(email, {})
            ips = act.get('ips') or []
            # Build "🇷🇺 91.246… , 🇰🇿 95.55…" with flags when GeoIP
            # is available — useful to spot one-key-two-countries.
            geo_per_ip = []
            cc_set = set()
            for ip in ips:
                if geo_lookup:
                    g = geo_lookup(ip)
                    if g:
                        cc, flag = g
                        cc_set.add(cc)
                        geo_per_ip.append(f"{flag} {ip}")
                        continue
                geo_per_ip.append(ip)
            ips_str = ", ".join(geo_per_ip) if geo_per_ip else "—"
            sharing_marker = " 🚨" if len(cc_set) > 1 else ""
            ip_count = act.get('distinct_ips') or 0
            limit = (user.limit_ip if user else None) or '—'
            conns = act.get('active_connections') or 0
            dests = act.get('distinct_destinations') or 0
            # Avg RTT across user's IPs
            rtts = [rtt_by_ip[ip] for ip in ips if ip in rtt_by_ip]
            rtt_str = f"{round(sum(rtts) / len(rtts), 1)} ms" if rtts else "—"
            # Consumption (bot DB knows quota, x-ui DB knows actual usage)
            t = traffic_by_email.get(email) or {}
            consumed = (t.get('upload', 0) + t.get('download', 0)) / (1024 ** 3)
            quota = user.quota_gb if user else None
            traffic_str = (
                f"{consumed:.2f}/{quota} GB" if quota
                else f"{consumed:.2f} GB"
            )
            lines.append(
                f"• {uname}{sharing_marker} "
                f"· 🔢 {ip_count}/{limit} IP "
                f"· 📶 {rtt_str} "
                f"· 📊 {traffic_str}\n"
                f"   <code>{ips_str}</code> "
                f"· {conns} соед. · {dests} назн."
            )

        text = "\n".join(lines)
        if len(text) > 3900:  # Telegram cap is 4096
            text = text[:3900] + "\n…(обрезано)"
        self.bot.send_message(
            chat_id=chat_id, text=text, parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /find <text> -----

    def find_user(self, chat_id: str, args: list) -> None:
        """Fuzzy search across username / chat_id / email / uuid prefix."""
        if not args:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат: /find <текст>\n"
                     "Ищет по username, chat_id, email и uuid (префикс).",
            )
            return

        query = ' '.join(args).strip().lstrip('@')
        if len(query) < 2:
            self.bot.send_message(chat_id=chat_id, text="❌ Минимум 2 символа.")
            return

        like = f"%{query}%"
        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT chat_id, username, status, email, uuid, quota_gb "
                    "FROM users "
                    "WHERE chat_id LIKE ? COLLATE NOCASE "
                    "   OR username LIKE ? COLLATE NOCASE "
                    "   OR email LIKE ? COLLATE NOCASE "
                    "   OR uuid LIKE ? COLLATE NOCASE "
                    "ORDER BY (status='paid') DESC, (status='demo') DESC, chat_id "
                    "LIMIT 20",
                    (like, like, like, like),
                ).fetchall()
        except Exception as e:
            logger.exception(f"find_user query failed: {e}")
            self.bot.send_message(chat_id=chat_id, text=f"❌ DB error: {e}")
            return

        if not rows:
            self.bot.send_message(
                chat_id=chat_id,
                text=f"🔍 Ничего не найдено по <code>{query}</code>.",
                parse_mode='HTML',
            )
            return

        lines = [f"🔍 <b>Найдено {len(rows)} (топ 20)</b>"]
        for r in rows:
            cid, uname, status, email, uuid, quota = r
            uname_part = f"@{uname}" if uname else "—"
            email_short = email or "—"
            uuid_short = (uuid[:8] + "…") if uuid else "—"
            lines.append(
                f"• <code>{cid}</code> · {uname_part} · {status} · "
                f"{quota or 0}GB · <code>{uuid_short}</code> · {email_short}"
            )
        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /recent [N] -----

    def show_recent_actions(self, chat_id: str, args: list) -> None:
        """Last N admin_actions rows (default 15, max 50)."""
        try:
            n = int(args[0]) if args else 15
        except (ValueError, IndexError):
            n = 15
        n = max(1, min(n, 50))

        try:
            with self.db._connect() as conn:
                rows = conn.execute(
                    "SELECT admin_id, action, target_id, created_at "
                    "FROM admin_actions ORDER BY id DESC LIMIT ?",
                    (n,),
                ).fetchall()
        except Exception as e:
            self.bot.send_message(chat_id=chat_id, text=f"❌ DB error: {e}")
            return

        if not rows:
            self.bot.send_message(chat_id=chat_id, text="📭 Журнал пуст.")
            return

        lines = [f"📜 <b>Последние {len(rows)} действий</b>"]
        for r in rows:
            adm, act, tgt, when = r
            when_short = (when or '')[5:16].replace('T', ' ')  # MM-DD HH:MM
            tgt_short = f" → <code>{tgt}</code>" if tgt else ""
            lines.append(f"• <code>{when_short}</code> {act}{tgt_short} <i>(by {adm})</i>")
        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /repair_stuck -----

    def repair_stuck_support(self, chat_id: str, args: list) -> None:
        """Manually trigger the support_state_repair scheduler job.

        Reverts users whose ``status=support_topic`` AND
        ``support_topic_id IS NULL`` back to their previous_state
        (or DEMO/NEW based on whether a key was issued). Useful when
        you don't want to wait for the next hourly tick.
        """
        notifier = (
            self.bot.services.get('notifications')
            if hasattr(self.bot, 'services') else None
        )
        if not notifier:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ NotificationService недоступен — попробуй /restart_bot через kimi.",
            )
            return
        try:
            before_count = 0
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE status='support_topic' AND support_topic_id IS NULL"
                ).fetchone()
                if row:
                    before_count = row[0]
            notifier._repair_stuck_support_users_sync()
            with self.db._connect() as conn:
                row = conn.execute(
                    "SELECT COUNT(*) FROM users WHERE status='support_topic' AND support_topic_id IS NULL"
                ).fetchone()
                after_count = row[0] if row else 0
            fixed = before_count - after_count
            self.bot.send_message(
                chat_id=chat_id,
                text=(
                    f"🩹 Восстановлено: <b>{fixed}</b> юзеров.\n"
                    f"До: {before_count} застрявших · после: {after_count}"
                ),
                parse_mode='HTML',
                message_thread_id=self._get_thread_id(chat_id),
            )
        except Exception as e:
            logger.exception("repair_stuck_support failed")
            self.bot.send_message(chat_id=chat_id, text=f"❌ {e}")

    # ----- /topics -----

    def show_topics(self, chat_id: str, args: list) -> None:
        """Dump current TOPIC_* env values + the forum group id.

        Handy after forum_bootstrap re-created topics with new ids."""
        cfg = self.config
        forum_id = getattr(cfg, 'FORUM_GROUP_ID', None)
        topic_attrs = [
            'TOPIC_REQUESTS', 'TOPIC_USERS', 'TOPIC_DEMO',
            'TOPIC_REJECTED', 'TOPIC_STATS', 'TOPIC_PAYMENTS',
            'TOPIC_SUPPORT', 'TOPIC_SOLVED', 'TOPIC_AI',
        ]
        lines = [
            "📋 <b>Forum topology</b>",
            f"• Group: <code>{forum_id}</code>",
            f"• Enabled: <b>{getattr(cfg, 'FORUM_ENABLED', False)}</b>",
            "",
        ]
        for name in topic_attrs:
            val = getattr(cfg, name, None)
            mark = '✓' if val else '✗'
            lines.append(f"{mark} <code>{name}</code> = {val}")
        self.bot.send_message(
            chat_id=chat_id, text="\n".join(lines), parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /quota @user N -----

    def set_quota(self, chat_id: str, args: list) -> None:
        """Set arbitrary quota in GB (replaces, not adds — unlike grant_100gb)."""
        if len(args) < 2:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат: /quota @username N\n(N — лимит в ГБ; ставит, не прибавляет)",
            )
            return

        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return

        try:
            gb = float(args[1])
        except ValueError:
            self.bot.send_message(chat_id=chat_id, text="❌ N должно быть числом.")
            return
        if gb < 0 or gb > 100000:
            self.bot.send_message(chat_id=chat_id, text="❌ N вне диапазона 0–100000.")
            return

        user = self.db.get_user(target.chat_id)
        if not user:
            self.bot.send_message(chat_id=chat_id, text="❌ Юзер исчез между запросом и записью.")
            return
        old = user.quota_gb or 0
        user.quota_gb = gb
        self.db.save_user(user)

        # Propagate to x-ui
        xui_msg = ''
        if user.email:
            xui = self.bot.services.get('xui') if hasattr(self.bot, 'services') else None
            if xui:
                try:
                    client = xui.get_client_sync(user.email)
                    if client:
                        client['totalGB'] = int(gb * BYTES_PER_GB)
                        xui.add_client_sync(client, client.get('inbound_id', 1))
                        xui_msg = ' (x-ui sync OK)'
                except Exception as e:
                    xui_msg = f' (x-ui error: {e})'

        try:
            admin_id = str(self.config.SUPER_ADMIN_ID)
            self.db.log_admin_action(admin_id, 'cmd_quota', str(target.chat_id), f"{old} → {gb}")
        except Exception:
            pass

        uname = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(
            chat_id=chat_id,
            text=f"⚙️ Квота {uname}: <b>{old}</b> → <b>{gb}</b> ГБ{xui_msg}",
            parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )

    # ----- /expire @user YYYY-MM-DD -----

    def set_expire(self, chat_id: str, args: list) -> None:
        """Set ``subscription_expiry`` to an explicit date. Also updates
        the active row in ``subscriptions`` if one exists, so the
        dashboard buckets stay consistent.
        """
        if len(args) < 2:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат: /expire @username YYYY-MM-DD",
            )
            return

        target = self._resolve_target(args[0])
        if not target:
            self.bot.send_message(chat_id=chat_id, text="❌ Пользователь не найден.")
            return

        date_str = args[1].strip()
        try:
            dt = datetime.strptime(date_str, '%Y-%m-%d')
        except ValueError:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Формат даты: YYYY-MM-DD (например, 2026-12-31).",
            )
            return

        # Store as ISO string at end-of-day for clarity
        new_expiry = (dt + timedelta(hours=23, minutes=59)).isoformat()

        user = self.db.get_user(target.chat_id)
        if not user:
            self.bot.send_message(chat_id=chat_id, text="❌ Юзер исчез между запросом и записью.")
            return
        old = user.subscription_expiry or '—'
        user.subscription_expiry = new_expiry
        self.db.save_user(user)

        # Mirror into subscriptions.expires_at if active row exists.
        subs_msg = ''
        try:
            with self.db._connect() as conn:
                cur = conn.execute(
                    "UPDATE subscriptions SET expires_at = ? "
                    "WHERE chat_id = ? AND is_active = 1",
                    (new_expiry, str(target.chat_id)),
                )
                if cur.rowcount:
                    subs_msg = f' (subscriptions: {cur.rowcount} row)'
        except Exception as e:
            subs_msg = f' (subscriptions sync error: {e})'

        try:
            admin_id = str(self.config.SUPER_ADMIN_ID)
            self.db.log_admin_action(
                admin_id, 'cmd_expire', str(target.chat_id),
                f"{old} → {date_str}",
            )
        except Exception:
            pass

        uname = f"@{target.username}" if target.username else f"user_{target.chat_id}"
        self.bot.send_message(
            chat_id=chat_id,
            text=f"📅 Подписка {uname} до <b>{date_str}</b>{subs_msg}",
            parse_mode='HTML',
            message_thread_id=self._get_thread_id(chat_id),
        )
