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


def _key_email(sub_url: str, lang: str) -> tuple[str, str]:
    """(subject, plain-text body) for the backup-key letter.

    Plain text on purpose: HTML from a fresh sender scores worse with
    spam filters, and the letter is one URL plus four steps.
    """
    if lang == 'en':
        subject = "Your NekoVPN access key"
        body = (
            "This is your backup VPN access key.\n"
            "\n"
            "Subscription URL:\n"
            f"{sub_url}\n"
            "\n"
            "How to use it:\n"
            "1. Install Hiddify: https://hiddify.com/\n"
            "2. Copy the URL above\n"
            "3. In Hiddify: + -> Add from URL -> paste -> Save\n"
            "4. Tap Connect\n"
            "\n"
            "Keep this letter: the URL stays valid and works even if\n"
            "the Telegram bot is unreachable.\n"
        )
    else:
        subject = "Ваш ключ доступа NekoVPN"
        body = (
            "Это резервный ключ доступа к вашему VPN.\n"
            "\n"
            "Subscription URL:\n"
            f"{sub_url}\n"
            "\n"
            "Как использовать:\n"
            "1. Установите Hiddify: https://hiddify.com/\n"
            "2. Скопируйте ссылку выше\n"
            "3. В Hiddify: + -> Добавить из ссылки -> вставить -> Сохранить\n"
            "4. Нажмите «Подключить»\n"
            "\n"
            "Сохраните это письмо: ссылка остаётся рабочей и выручит,\n"
            "если Telegram-бот будет недоступен.\n"
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

    def send_key(self, to_addr: str, sub_url: str, lang: str = 'ru') -> bool:
        """Send the backup-key letter. Returns True on relay acceptance."""
        subject, body = _key_email(sub_url, lang)
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
