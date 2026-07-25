"""Backup key delivery over SMTP (EmailService + email_key callback)."""

from unittest.mock import MagicMock, Mock, patch

from bot.services.email_service import EmailService, _key_email


class _Cfg:
    SMTP_HOST = "smtp.example.com"
    SMTP_PORT = 587
    SMTP_USER = "relay-user"
    SMTP_PASSWORD = "relay-pass"
    SMTP_FROM = "keys@example.com"
    SMTP_FROM_NAME = "NekoVPN"


class TestEmailServiceConfig:
    def test_configured(self):
        assert EmailService(_Cfg()).is_configured() is True

    def test_unconfigured_without_host(self):
        class NoHost(_Cfg):
            SMTP_HOST = ""
        assert EmailService(NoHost()).is_configured() is False

    def test_send_refused_when_unconfigured(self):
        class NoHost(_Cfg):
            SMTP_HOST = ""
        assert EmailService(NoHost()).send_key("u@x.com", "https://sub") is False

    def test_from_defaults_to_user(self):
        class NoFrom(_Cfg):
            SMTP_FROM = ""
        svc = EmailService(NoFrom())
        assert svc.from_addr == "relay-user"

    def test_bad_port_falls_back(self):
        class BadPort(_Cfg):
            SMTP_PORT = "oops"
        assert EmailService(BadPort()).port == 587


class TestEmailServiceSend:
    def test_send_starttls_path(self):
        svc = EmailService(_Cfg())
        server = MagicMock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        with patch("smtplib.SMTP", return_value=server) as smtp_cls:
            assert svc.send_key("u@x.com", "https://sub.url/abc", lang="ru") is True
            smtp_cls.assert_called_once()
            server.starttls.assert_called_once()
            server.login.assert_called_once_with("relay-user", "relay-pass")
            args = server.sendmail.call_args[0]
            assert args[0] == "keys@example.com"
            assert args[1] == ["u@x.com"]
            # utf-8 MIME body is base64 on the wire — decode to assert
            import email as email_pkg
            parsed = email_pkg.message_from_string(args[2])
            body = parsed.get_payload(decode=True).decode("utf-8")
            assert "https://sub.url/abc" in body

    def test_send_implicit_tls_on_465(self):
        class SSLCfg(_Cfg):
            SMTP_PORT = 465
        svc = EmailService(SSLCfg())
        server = MagicMock()
        server.__enter__ = Mock(return_value=server)
        server.__exit__ = Mock(return_value=False)
        with patch("smtplib.SMTP_SSL", return_value=server) as ssl_cls:
            assert svc.send_key("u@x.com", "https://sub") is True
            ssl_cls.assert_called_once()
            server.starttls.assert_not_called()

    def test_send_failure_returns_false(self):
        svc = EmailService(_Cfg())
        with patch("smtplib.SMTP", side_effect=OSError("refused")):
            assert svc.send_key("u@x.com", "https://sub") is False

    def test_letter_contains_url_both_langs(self):
        for lang in ("ru", "en"):
            subject, body = _key_email("https://sub.url/abc", lang)
            assert subject
            assert "https://sub.url/abc" in body
            assert "hiddify" in body.lower()


class TestEmailKeyCallback:
    def _handler(self):
        from bot.handlers.callbacks.user import EmailKeyHandler
        h = EmailKeyHandler(MagicMock(), MagicMock(), _Cfg())
        EmailKeyHandler._last_sent_times = {}
        return h

    def _update(self):
        return {"callback_query": {"id": "x", "message": {"chat": {"id": 1}}}}

    def test_no_key_yet(self):
        h = self._handler()
        h.db.get_user.return_value = None
        h.handle(self._update(), "1", "1", data="email_key")
        text = h.bot.send_message.call_args.kwargs["text"]
        assert "/start" in text

    def test_prompts_for_email_when_missing(self):
        h = self._handler()
        user = Mock(uuid="u-1", lang="ru", contact_email=None)
        h.db.get_user.return_value = user
        h.handle(self._update(), "1", "1", data="email_key")
        text = h.bot.send_message.call_args.kwargs["text"]
        assert "/setemail" in text

    def test_graceful_when_smtp_off(self):
        class NoHost(_Cfg):
            SMTP_HOST = ""
        from bot.handlers.callbacks.user import EmailKeyHandler
        EmailKeyHandler._last_sent_times = {}
        h = EmailKeyHandler(MagicMock(), MagicMock(), NoHost())
        user = Mock(uuid="u-1", lang="ru", contact_email="u@x.com")
        h.db.get_user.return_value = user
        h.handle(self._update(), "1", "1", data="email_key")
        text = h.bot.send_message.call_args.kwargs["text"]
        assert "не подключена" in text

    def test_sends_and_confirms(self):
        h = self._handler()
        user = Mock(uuid="u-1", lang="ru", contact_email="u@x.com")
        h.db.get_user.return_value = user
        with patch("bot.services.email_service.EmailService.send_key",
                   return_value=True) as send, \
             patch("bot.services.subscription.SubscriptionService"
                   ".build_subscription_url", return_value="https://sub.url/abc"):
            h.handle(self._update(), "1", "1", data="email_key")
            import threading
            for t in threading.enumerate():
                if t.name.startswith("email-key-"):
                    t.join(timeout=5)
            send.assert_called_once_with("u@x.com", "https://sub.url/abc", "ru",
                                         platform=user.platform)
        text = h.bot.send_message.call_args.kwargs["text"]
        assert "отправлен" in text.lower()

    def test_rate_limited_second_send(self):
        h = self._handler()
        user = Mock(uuid="u-1", lang="ru", contact_email="u@x.com")
        h.db.get_user.return_value = user
        import time
        type(h)._last_sent_times["1"] = time.time()
        h.handle(self._update(), "1", "1", data="email_key")
        text = h.bot.send_message.call_args.kwargs["text"]
        assert "уже отправлено" in text.lower()
