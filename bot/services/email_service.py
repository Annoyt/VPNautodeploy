"""Backup key delivery over email — a relay client, not a mail server.

Why no local MTA: the entry node's IP has no PTR record and zero sender
reputation, so direct-to-MX mail from it is rejected outright by Gmail
(mandatory PTR since 2024) and lands in spam everywhere else. All
submission ports (587/465/2525) are open outbound, so the lightest
setup that actually delivers is plain smtplib talking to an external
relay (Gmail app-password, Brevo, SMTP2GO, ...) — zero daemons on the
box, deliverability is the relay's problem.

Config lives in .env: SMTP_HOST/SMTP_PORT/SMTP_USER/SMTP_PASSWORD/
SMTP_FROM. With SMTP_HOST empty the feature is dormant and callers
should degrade gracefully (is_configured() → False).

Callers run send_key() on a worker thread — an SMTP handshake takes
seconds and the polling loop must never wait on it (same lesson as the
/ai agent turns).
"""

from __future__ import annotations

import logging
import smtplib
from email.mime.text import MIMEText
from email.utils import formataddr

logger = logging.getLogger(__name__)

SMTP_TIMEOUT_S = 20


def _key_email(sub_url: str, lang: str, platform: str = None) -> tuple[str, str]:
    """(subject, plain-text body) for the key letter.

    Plain text on purpose: HTML from a fresh sender scores worse with
    spam filters. The instructions are written for a first-time user
    who has never seen a VPN client — every step names the exact
    button to tap.

    iOS gets a dedicated Karing-first variant: both Hiddify and Happ were
    pulled from the RU App Store. Karing is sing-box based and reads the
    plain subscription URL — no special format needed.
    """
    if platform == 'ios':
        if lang == 'en':
            subject = "Your NekoVPN access key + iPhone setup guide"
            body = (
                "Hi! This letter contains your personal VPN access key and a\n"
                "step-by-step iPhone setup guide. It takes about 2 minutes.\n"
                "\n"
                "=== YOUR KEY (subscription link) ===\n"
                f"{sub_url}\n"
                "\n"
                "=== SETUP (step by step) ===\n"
                "1. Install the free Karing app from the App Store:\n"
                "   https://karing.app/ (or search \"Karing\" in the App Store)\n"
                "\n"
                "2. Copy the whole key link above (the long line starting\n"
                "   with https://). Tap and hold it, then choose \"Copy\".\n"
                "\n"
                "3. Open Karing. Tap \"+\" → \"Add from URL\" and paste\n"
                "   the link. Tap Save.\n"
                "\n"
                "4. A profile appears. Tap the big round Connect button.\n"
                "   When it shows connected you are done.\n"
                "\n"
                "=== GOOD TO KNOW ===\n"
                "- Don't change any settings - it's tuned already.\n"
                "- The subscription refreshes itself every 6 hours, so\n"
                "  server changes arrive automatically.\n"
                "- Keep this letter. The link keeps working, so you can set\n"
                "  up a new phone anytime, even if the Telegram bot is down.\n"
                "- Not connecting? Try mobile data instead of Wi-Fi (a\n"
                "  different network is often blocked differently).\n"
            )
        else:
            subject = "Ваш ключ доступа NekoVPN + инструкция для iPhone"
            body = (
                "Привет! В этом письме — ваш личный ключ доступа к VPN и\n"
                "пошаговая инструкция для iPhone. Займёт около 2 минут.\n"
                "\n"
                "=== ВАШ КЛЮЧ (ссылка-подписка) ===\n"
                f"{sub_url}\n"
                "\n"
                "=== НАСТРОЙКА (по шагам) ===\n"
                "1. Установите бесплатное приложение Karing из App Store:\n"
                "   https://karing.app/ (или поиск \"Karing\" в App Store)\n"
                "   Hiddify и Happ удалили из RU App Store — Karing\n"
                "   остался и полностью работает.\n"
                "\n"
                "2. Скопируйте ссылку-ключ целиком (длинная строка выше,\n"
                "   начинается с https://). Нажмите и удерживайте её,\n"
                "   затем выберите \"Копировать\".\n"
                "\n"
                "3. Откройте Karing. Нажмите \"+\" → \"Добавить из ссылки\",\n"
                "   вставьте ссылку и сохраните.\n"
                "\n"
                "4. Появится профиль. Нажмите большую круглую кнопку\n"
                "   подключения. Подключились — готово.\n"
                "\n"
                "=== ПОЛЕЗНО ЗНАТЬ ===\n"
                "- Ничего в настройках менять не нужно, всё уже настроено.\n"
                "- Подписка обновляется сама каждые 6 часов — изменения\n"
                "  серверов приезжают автоматически.\n"
                "- Сохраните это письмо. Ссылка остаётся рабочей: сможете\n"
                "  настроить новый телефон когда угодно, даже если\n"
                "  Telegram-бот будет недоступен.\n"
                "- Не подключается? Попробуйте мобильный интернет вместо\n"
                "  Wi-Fi (другая сеть часто блокируется иначе).\n"
            )
        return subject, body

    if lang == 'en':
        subject = "Your NekoVPN access key + setup guide"
        body = (
            "Hi! This letter contains your personal VPN access key and a\n"
            "step-by-step setup guide. It takes about 2 minutes.\n"
            "\n"
            "=== YOUR KEY (subscription link) ===\n"
            f"{sub_url}\n"
            "\n"
            "=== SETUP (step by step) ===\n"
            "1. Install the free app:\n"
            "   - iPhone/iPad: Karing from the App Store (Hiddify was\n"
            "     pulled from the RU App Store): https://karing.app/\n"
            "   - Android: Google Play, search \"Hiddify\"\n"
            "   - Windows/Mac: https://hiddify.com/\n"
            "\n"
            "2. Copy your key link above (the long line starting with\n"
            "   https://). Tap and hold it, then choose \"Copy\".\n"
            "\n"
            "3. Open the app. In Karing: \"+\" → \"Add from URL\".\n"
            "   In Hiddify: \"+\" → \"Add from clipboard\". Paste and Save.\n"
            "\n"
            "4. A profile appears. Tap the big round Power/Connect button\n"
            "   in the center. When it turns green you are connected.\n"
            "\n"
            "=== GOOD TO KNOW ===\n"
            "- Don't change any settings - it's tuned already.\n"
            "- Keep this letter. The link keeps working, so you can set\n"
            "  up a new phone anytime, even if the Telegram bot is down.\n"
            "- Not connecting? Close the app fully and reopen it, or try\n"
            "  mobile data instead of Wi-Fi (a different network is often\n"
            "  blocked differently).\n"
        )
    else:
        subject = "Ваш ключ доступа NekoVPN + инструкция"
        body = (
            "Привет! В этом письме — ваш личный ключ доступа к VPN и\n"
            "пошаговая инструкция по настройке. Займёт около 2 минут.\n"
            "\n"
            "=== ВАШ КЛЮЧ (ссылка-подписка) ===\n"
            f"{sub_url}\n"
            "\n"
            "=== НАСТРОЙКА (по шагам) ===\n"
            "1. Установите бесплатное приложение:\n"
            "   - iPhone/iPad: Karing из App Store (Hiddify и Happ удалили\n"
            "     из RU App Store): https://karing.app/\n"
            "   - Android: Google Play, поиск \"Hiddify\"\n"
            "   - Windows/Mac: https://hiddify.com/\n"
            "\n"
            "2. Скопируйте ссылку-ключ целиком (длинная строка выше,\n"
            "   начинается с https://). Нажмите и удерживайте её,\n"
            "   затем выберите \"Копировать\".\n"
            "\n"
            "3. Откройте приложение. В Karing: \"+\" → \"Добавить из ссылки\".\n"
            "   В Hiddify: \"+\" → \"Добавить из буфера обмена\".\n"
            "   Вставьте ссылку и сохраните.\n"
            "\n"
            "4. Появится профиль. Нажмите большую круглую кнопку\n"
            "   включения в центре. Стала зелёной — вы подключены.\n"
            "\n"
            "=== ПОЛЕЗНО ЗНАТЬ ===\n"
            "- Ничего в настройках менять не нужно, всё уже настроено.\n"
            "- Сохраните это письмо. Ссылка остаётся рабочей: сможете\n"
            "  настроить новый телефон когда угодно, даже если\n"
            "  Telegram-бот будет недоступен.\n"
            "- Не подключается? Полностью закройте приложение и откройте\n"
            "  заново, или попробуйте мобильный интернет вместо Wi-Fi\n"
            "  (другая сеть часто блокируется иначе).\n"
        )
    return subject, body


class EmailService:
    """Thin SMTP-submission client for one-off transactional mail."""

    def __init__(self, config) -> None:
        self.host = (getattr(config, 'SMTP_HOST', '') or '').strip()
        try:
            self.port = int(getattr(config, 'SMTP_PORT', 587) or 587)
        except (TypeError, ValueError):
            self.port = 587
        self.user = (getattr(config, 'SMTP_USER', '') or '').strip()
        self.password = getattr(config, 'SMTP_PASSWORD', '') or ''
        self.from_addr = (getattr(config, 'SMTP_FROM', '') or '').strip() or self.user
        self.from_name = (getattr(config, 'SMTP_FROM_NAME', '') or '').strip() or 'NekoVPN'

    def is_configured(self) -> bool:
        return bool(self.host and self.from_addr)

    def send_key(self, to_addr: str, sub_url: str, lang: str = 'ru',
                 platform: str = None) -> bool:
        """Send the backup-key letter. Returns True on relay acceptance.

        ``platform`` selects the Karing-first iPhone variant when 'ios'..
        """
        subject, body = _key_email(sub_url, lang, platform)
        return self._send(to_addr, subject, body)

    def send_notice(self, to_addr: str, subject: str, body: str) -> bool:
        """Plain transactional notice (quota warnings, renewals, request
        replies) for users whose only channel is email. Same relay path
        as the key letter; no per-user rate limit — callers de-dupe.
        """
        return self._send(to_addr, subject, body)

    def _send(self, to_addr: str, subject: str, body: str) -> bool:
        if not self.is_configured():
            logger.warning("email: SMTP_HOST not configured, drop send")
            return False
        msg = MIMEText(body, 'plain', 'utf-8')
        msg['Subject'] = subject
        msg['From'] = formataddr((self.from_name, self.from_addr))
        msg['To'] = to_addr
        try:
            # Port 465 is implicit TLS; everything else is submission
            # with STARTTLS (mandatory — creds never travel plaintext).
            if self.port == 465:
                server = smtplib.SMTP_SSL(self.host, self.port, timeout=SMTP_TIMEOUT_S)
            else:
                server = smtplib.SMTP(self.host, self.port, timeout=SMTP_TIMEOUT_S)
            with server:
                if self.port != 465:
                    server.starttls()
                if self.user:
                    server.login(self.user, self.password)
                server.sendmail(self.from_addr, [to_addr], msg.as_string())
            logger.info(f"email: key letter accepted by relay for {to_addr[:3]}***")
            return True
        except (smtplib.SMTPException, OSError) as e:
            logger.warning(f"email: send failed: {e}")
            return False
