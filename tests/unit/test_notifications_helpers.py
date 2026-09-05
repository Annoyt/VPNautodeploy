"""Tests for notification helper functions (admin + user)."""

import pytest
from unittest.mock import Mock, patch

from bot.services.admin_notifications import (
    build_admin_request_keyboard,
    build_approved_user_keyboard,
    build_rejected_user_keyboard,
    format_new_request_text,
    format_support_ticket_text,
    format_pm_support_text,
    format_payment_issue_text,
    format_admin_stats_text,
    format_user_stats_text,
)
from bot.services.user_notifications import (
    get_message,
    build_welcome_keyboard,
    build_platform_keyboard,
    build_main_menu_keyboard,
)
from bot.models import User


class TestAdminNotificationKeyboards:
    """Test admin notification keyboard builders."""
    
    @pytest.fixture
    def sample_user(self):
        """Create sample user."""
        user = Mock(spec=User)
        user.chat_id = "12345"
        return user
    
    def test_build_admin_request_keyboard(self, sample_user):
        """Test keyboard for new request notification."""
        keyboard = build_admin_request_keyboard(sample_user)
        
        assert 'inline_keyboard' in keyboard
        assert len(keyboard['inline_keyboard']) == 3  # 3 rows
        # First row: Approve, Reject
        assert keyboard['inline_keyboard'][0][0]['callback_data'] == 'approve:12345'
        assert keyboard['inline_keyboard'][0][1]['callback_data'] == 'reject:12345'
        # Second row: Message, Profile
        assert keyboard['inline_keyboard'][1][0]['callback_data'] == 'message:12345'
        assert keyboard['inline_keyboard'][1][1]['callback_data'] == 'profile:12345'
        # Third row: Reset approval
        assert keyboard['inline_keyboard'][2][0]['callback_data'] == 'reset_approval:12345'
        
    def test_build_approved_user_keyboard(self, sample_user):
        """Test keyboard for approved user management."""
        keyboard = build_approved_user_keyboard(sample_user)
        
        assert 'inline_keyboard' in keyboard
        assert len(keyboard['inline_keyboard']) == 1
        assert keyboard['inline_keyboard'][0][0]['callback_data'] == 'revoke:12345'
        assert keyboard['inline_keyboard'][0][1]['callback_data'] == 'reset_approval:12345'
        
    def test_build_rejected_user_keyboard(self, sample_user):
        """Test keyboard for rejected user management."""
        keyboard = build_rejected_user_keyboard(sample_user)
        
        assert 'inline_keyboard' in keyboard
        assert len(keyboard['inline_keyboard']) == 1
        assert keyboard['inline_keyboard'][0][0]['callback_data'] == 'reset_approval:12345'


class TestAdminNotificationTextFormatters:
    """Test admin notification text formatters."""
    
    @pytest.fixture
    def sample_user(self):
        """Create sample user."""
        user = Mock(spec=User)
        user.chat_id = "12345"
        user.username = "testuser"
        user.lang = "ru"
        user.created_at = "2026-04-13T10:00:00"
        return user
    
    def test_format_new_request_text_with_username(self, sample_user):
        """Test new request text with username."""
        text = format_new_request_text(sample_user)
        
        assert "New Demo Request" in text
        assert "@testuser" in text
        assert "12345" in text
        assert "ru" in text
        assert "2026-04-13" in text
        
    def test_format_new_request_text_without_username(self, sample_user):
        """Test new request text without username."""
        sample_user.username = None
        text = format_new_request_text(sample_user)
        
        assert "No username" in text
        
    def test_format_support_ticket_text(self, sample_user):
        """Test support ticket text formatting."""
        text = format_support_ticket_text(sample_user, "Help me please!")
        
        assert "Support Ticket" in text
        assert "@testuser" in text
        assert "Help me please!" in text
        
    def test_format_support_ticket_text_truncated(self, sample_user):
        """Test long message is truncated."""
        long_message = "A" * 1000
        text = format_support_ticket_text(sample_user, long_message)
        
        assert len(text) < len(long_message) + 100  # Should be truncated
        
    def test_format_pm_support_text(self, sample_user):
        """Test PM support text formatting."""
        text = format_pm_support_text(sample_user, "PM request")
        
        assert "Support Request" in text
        assert "Reply to this message" in text
        
    def test_format_payment_issue_text(self, sample_user):
        """Test payment issue text formatting."""
        text = format_payment_issue_text(sample_user, "Payment failed")
        
        assert "Payment Issue" in text
        assert "Payment failed" in text
        
    def test_format_admin_stats_text(self):
        """Test admin stats text formatting."""
        stats = {
            'total': 100,
            'by_status': {
                'pending_demo': 5,
                'demo': 20,
                'demo': 70,
                'banned': 5
            },
            'by_platform': {
                'ios': 40,
                'android': 35,
                'windows': 25
            }
        }
        text = format_admin_stats_text(stats)
        
        assert "System Statistics" in text
        assert "Total users: 100" in text
        assert "Pending: 5" in text
        assert "ios" in text
        assert "android" in text
        
    def test_format_user_stats_text_with_traffic_ru(self, sample_user):
        """Test user stats text in Russian with traffic."""
        traffic = {'total': 5 * 1024**3}  # 5 GB
        text = format_user_stats_text(sample_user, traffic, demo_traffic_gb=10)
        
        assert "Ваша статистика" in text
        assert "5.00 GB" in text  # Used
        assert "5.00 GB" in text  # Remaining
        
    def test_format_user_stats_text_no_traffic_en(self, sample_user):
        """Test user stats text in English without traffic."""
        sample_user.lang = "en"
        text = format_user_stats_text(sample_user, {}, demo_traffic_gb=10)
        
        assert "Your Statistics" in text
        assert "Used: 0.00 GB" in text
        assert "Remaining: 10.00 GB" in text


class TestUserNotificationHelpers:
    """Test user notification helpers."""
    
    def test_get_message_existing_key(self):
        """Test getting existing message."""
        with patch('bot.services.user_notifications.MESSAGES', {
            'ru': {'welcome': 'Добро пожаловать, {name}!'},
            'en': {'welcome': 'Welcome, {name}!'}
        }):
            text = get_message('welcome', 'ru', name='Иван')
            assert text == 'Добро пожаловать, Иван!'
            
    def test_get_message_fallback_lang(self):
        """Test fallback to Russian if language not found."""
        with patch('bot.services.user_notifications.MESSAGES', {
            'ru': {'welcome': 'Добро пожаловать!'},
            'en': {'welcome': 'Welcome!'}
        }):
            text = get_message('welcome', 'de')  # German not available
            assert text == 'Добро пожаловать!'
            
    def test_get_message_missing_key(self):
        """Test placeholder for missing key."""
        with patch('bot.services.user_notifications.MESSAGES', {
            'ru': {},
            'en': {}
        }):
            text = get_message('nonexistent', 'ru')
            assert text == '[nonexistent]'
            
    def test_get_message_format_error(self):
        """Test handling format error gracefully."""
        with patch('bot.services.user_notifications.MESSAGES', {
            'ru': {'test': 'Hello {missing_key}'}
        }):
            # Should not raise, returns template as-is
            text = get_message('test', 'ru', wrong_param='value')
            assert 'Hello' in text
            
    def test_build_welcome_keyboard(self):
        """Test welcome keyboard structure."""
        keyboard = build_welcome_keyboard('ru')
        
        assert 'inline_keyboard' in keyboard
        # Should have request demo and language buttons
        assert len(keyboard['inline_keyboard']) >= 1
        assert keyboard['inline_keyboard'][0][0]['callback_data'] == 'request_demo'
        
    def test_build_platform_keyboard(self):
        """Test platform selection keyboard."""
        keyboard = build_platform_keyboard("12345")
        
        assert 'inline_keyboard' in keyboard
        # Check all platforms are present
        callbacks = []
        for row in keyboard['inline_keyboard']:
            for btn in row:
                callbacks.append(btn['callback_data'])
                
        assert 'platform:android:12345' in callbacks
        assert 'platform:ios:12345' in callbacks
        assert 'platform:windows:12345' in callbacks
        assert 'platform:macos:12345' in callbacks
        assert 'platform:other:12345' in callbacks
        
    def test_build_main_menu_keyboard(self):
        """Test main menu keyboard structure."""
        keyboard = build_main_menu_keyboard('ru')
        
        assert 'inline_keyboard' in keyboard
        callbacks = []
        for row in keyboard['inline_keyboard']:
            for btn in row:
                callbacks.append(btn['callback_data'])
                
        assert 'stats' in callbacks
        assert 'my_key' in callbacks
        assert 'support' in callbacks
        assert 'full' in callbacks


class TestResetPaidQuota:
    """Monthly paid pass: counters reset, quota/expiry untouched,
    lapsed subscriptions skipped."""

    def _make_service(self, tmp_path, rows):
        import sqlite3
        from unittest.mock import Mock
        from bot.services.notifications import NotificationService

        db_path = str(tmp_path / "bot.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE users (chat_id TEXT, email TEXT, status TEXT, "
            "subscription_expiry TEXT, traffic_up REAL, traffic_down REAL, "
            "last_traffic_update TEXT)"
        )
        conn.executemany(
            "INSERT INTO users (chat_id, email, status, subscription_expiry, "
            "traffic_up, traffic_down) VALUES (?, ?, ?, ?, 1.5, 2.5)",
            rows,
        )
        conn.commit()
        conn.close()

        svc = NotificationService(Mock(), Mock(), Mock())
        return svc, db_path

    def test_resets_active_paid_and_skips_lapsed(self, tmp_path):
        import sqlite3
        from unittest.mock import Mock

        svc, db_path = self._make_service(tmp_path, [
            ("1", "a@x", "paid", "2099-01-01T00:00:00"),   # active
            ("2", "b@x", "paid", "2020-01-01T00:00:00"),   # lapsed — skip
            ("3", "c@x", "paid", None),                    # no expiry — active
            ("4", "d@x", "demo", None),                    # not paid — ignored
        ])
        xui = Mock()
        xui.sync_client_settings_sync.return_value = True
        xui.reset_client_traffic_sync.return_value = True

        svc._reset_paid_quota_sync(xui, db_path)

        reset_emails = [
            c.args[0] for c in xui.reset_client_traffic_sync.call_args_list
        ]
        assert reset_emails == ["a@x", "c@x"]
        # Re-enable only — quota amount and expiry stay admin-managed.
        for c in xui.sync_client_settings_sync.call_args_list:
            assert c.args[1] == {"enable": True}

        conn = sqlite3.connect(db_path)
        rows = dict(conn.execute(
            "SELECT email, traffic_up + traffic_down FROM users").fetchall())
        assert rows["a@x"] == 0 and rows["c@x"] == 0
        assert rows["b@x"] == 4.0   # lapsed user untouched
        assert svc.bot.send_message.call_count == 2

    def test_panel_failure_keeps_counters(self, tmp_path):
        import sqlite3
        from unittest.mock import Mock

        svc, db_path = self._make_service(tmp_path, [
            ("1", "a@x", "paid", None),
        ])
        xui = Mock()
        xui.sync_client_settings_sync.return_value = False   # panel said no

        svc._reset_paid_quota_sync(xui, db_path)

        xui.reset_client_traffic_sync.assert_not_called()
        conn = sqlite3.connect(db_path)
        up_down = conn.execute(
            "SELECT traffic_up + traffic_down FROM users").fetchone()[0]
        assert up_down == 4.0   # bot.db untouched on failure
        svc.bot.send_message.assert_not_called()


class TestQuotaThresholdAlerts:
    """80% / 100% warnings from the 10-min traffic mirror."""

    def _svc(self, notified=False):
        from unittest.mock import Mock
        from bot.services.notifications import NotificationService
        svc = NotificationService(Mock(), Mock(), Mock())
        svc.db.was_notified = Mock(return_value=notified)
        svc.db.mark_notified = Mock()
        return svc

    def test_warns_at_80_percent(self):
        svc = self._svc()
        gb = 1024 ** 3
        svc._send_quota_threshold_alerts(
            {'u@x': {'upload': 0, 'download': 4 * gb, 'total': 5 * gb}},
            {'u@x': ('42', 'demo')},
        )
        text = svc.bot.send_message.call_args.kwargs['text']
        assert '80%' in text
        kind = svc.db.mark_notified.call_args.args[1]
        assert kind.startswith('quota80:')

    def test_exhausted_demo_gets_buy_hint(self):
        svc = self._svc()
        gb = 1024 ** 3
        svc._send_quota_threshold_alerts(
            {'u@x': {'upload': gb, 'download': 4 * gb, 'total': 5 * gb}},
            {'u@x': ('42', 'demo')},
        )
        text = svc.bot.send_message.call_args.kwargs['text']
        assert 'исчерпан' in text and '/buy' in text
        assert svc.db.mark_notified.call_args.args[1].startswith('quota100:')

    def test_exhausted_paid_no_buy_hint(self):
        svc = self._svc()
        gb = 1024 ** 3
        svc._send_quota_threshold_alerts(
            {'u@x': {'upload': 0, 'download': 100 * gb, 'total': 100 * gb}},
            {'u@x': ('42', 'paid')},
        )
        text = svc.bot.send_message.call_args.kwargs['text']
        assert '/buy' not in text

    def test_dedup_within_month(self):
        svc = self._svc(notified=True)
        gb = 1024 ** 3
        svc._send_quota_threshold_alerts(
            {'u@x': {'upload': 0, 'download': 4 * gb, 'total': 5 * gb}},
            {'u@x': ('42', 'demo')},
        )
        svc.bot.send_message.assert_not_called()
        svc.db.mark_notified.assert_not_called()

    def test_unlimited_and_low_usage_silent(self):
        svc = self._svc()
        gb = 1024 ** 3
        svc._send_quota_threshold_alerts(
            {
                'unlim@x': {'upload': 0, 'download': 900 * gb, 'total': 0},
                'low@x': {'upload': 0, 'download': 1 * gb, 'total': 5 * gb},
                'unknown@x': {'upload': 0, 'download': 5 * gb, 'total': 5 * gb},
            },
            {'unlim@x': ('1', 'demo'), 'low@x': ('2', 'demo')},
        )
        svc.bot.send_message.assert_not_called()


class TestDemoResetReprovisionFallback:
    """Monthly demo pass must revive clients the panel fully detached.

    The in-place renew can't see a detached client; the job then
    re-adds it with the SAME uuid and — critically — renews again,
    because /clients/add re-attaches the relational record but leaves
    the stale client_traffics row (enable=0, old expiry) that gates
    xray and hy2. 11 revivals silently failed this way on 2026-08-19.
    """

    def _make(self, tmp_path, users):
        import sqlite3
        from unittest.mock import Mock
        from bot.services.notifications import NotificationService

        db_path = str(tmp_path / "bot.db")
        conn = sqlite3.connect(db_path)
        conn.execute(
            "CREATE TABLE users (chat_id TEXT, email TEXT, uuid TEXT, "
            "status TEXT, subscription_expiry TEXT, traffic_up REAL, "
            "traffic_down REAL, last_traffic_update TEXT)"
        )
        conn.executemany(
            "INSERT INTO users (chat_id, email, uuid, status) "
            "VALUES (?, ?, ?, ?)", users)
        conn.commit()
        conn.close()

        config = Mock()
        config.DB_PATH = db_path
        config.DEMO_TRAFFIC_GB = 10
        svc = NotificationService(Mock(), Mock(), config)
        return svc, db_path

    def test_detached_client_readded_and_renewed_again(self, tmp_path):
        from unittest.mock import Mock, patch

        svc, _ = self._make(tmp_path, [
            ("1", "detached@x", "uuid-1", "demo"),
            ("2", "healthy@x", "uuid-2", "demo"),
        ])
        xui = Mock()
        xui.api = Mock()
        # detached@x: renew fails, re-add, second renew succeeds;
        # healthy@x: renew succeeds first try.
        xui.renew_client_sync.side_effect = [False, True, True]
        xui.add_client_sync.return_value = True
        xui.client_attached_sync.return_value = False   # truly detached

        with patch('bot.services.xui_service.XUIService', return_value=xui):
            svc._reset_demo_quota_sync()

        client_cfg = xui.add_client_sync.call_args.args[0]
        assert client_cfg['id'] == 'uuid-1'
        assert client_cfg['email'] == 'detached@x'
        assert client_cfg['enable'] is True
        assert xui.renew_client_sync.call_count == 3
        # Both users refreshed → both notified.
        assert svc.bot.send_message.call_count == 2

    def test_readd_failure_leaves_user_unrenewed(self, tmp_path):
        from unittest.mock import Mock, patch

        svc, _ = self._make(tmp_path, [
            ("1", "gone@x", "uuid-1", "demo"),
        ])
        xui = Mock()
        xui.api = Mock()
        xui.renew_client_sync.return_value = False
        xui.add_client_sync.return_value = False
        xui.client_attached_sync.return_value = False

        with patch('bot.services.xui_service.XUIService', return_value=xui):
            svc._reset_demo_quota_sync()

        svc.bot.send_message.assert_not_called()

    def test_attached_client_is_never_readded(self, tmp_path):
        """A renew can fail for reasons other than detachment (refused
        body, panel hiccup). Re-adding a client the panel still holds is
        delete+re-add and ZEROES its traffic — the fallback must be
        gated on the client being truly absent."""
        from unittest.mock import Mock, patch

        svc, _ = self._make(tmp_path, [
            ("1", "hiccup@x", "uuid-1", "demo"),
        ])
        xui = Mock()
        xui.api = Mock()
        xui.renew_client_sync.return_value = False
        xui.client_attached_sync.return_value = True    # still in the panel

        with patch('bot.services.xui_service.XUIService', return_value=xui):
            svc._reset_demo_quota_sync()

        xui.add_client_sync.assert_not_called()
        svc.bot.send_message.assert_not_called()

    def test_no_uuid_skips_readd(self, tmp_path):
        from unittest.mock import Mock, patch

        svc, _ = self._make(tmp_path, [
            ("1", "nouuid@x", None, "demo"),
        ])
        xui = Mock()
        xui.api = Mock()
        xui.renew_client_sync.return_value = False
        xui.add_client_sync.return_value = True

        with patch('bot.services.xui_service.XUIService', return_value=xui):
            svc._reset_demo_quota_sync()

        xui.add_client_sync.assert_not_called()


class TestPendingDigest:
    """Stuck pending_demo requests must resurface hourly with buttons —
    the one-shot notify used to fail silently and requests rotted."""

    def _svc(self, tmp_path, users, settings=None):
        import sqlite3
        from unittest.mock import Mock
        from bot.services.notifications import NotificationService

        db_path = str(tmp_path / "bot.db")
        conn = sqlite3.connect(db_path)
        conn.execute("CREATE TABLE users (chat_id TEXT, username TEXT, "
                     "status TEXT, created_at TEXT)")
        conn.executemany("INSERT INTO users VALUES (?, ?, ?, ?)", users)
        conn.commit()
        conn.close()

        config = Mock()
        config.DB_PATH = db_path
        config.FORUM_ENABLED = True
        config.FORUM_GROUP_ID = '-100777'
        config.TOPIC_REQUESTS = 15
        config.SUPER_ADMIN_ID = '1652899'
        svc = NotificationService(Mock(), Mock(), config)
        stored = dict(settings or {})
        svc.db.get_setting = Mock(side_effect=lambda k, d=None: stored.get(k, d))
        svc.db.set_setting = Mock(
            side_effect=lambda k, v: stored.__setitem__(k, v))
        return svc, stored

    def test_digest_sent_with_buttons(self, tmp_path):
        svc, stored = self._svc(tmp_path, [
            ("111", "ivan", "pending_demo", "2026-05-17T07:26:15"),
            ("222", None, "pending_demo", "2026-06-16T13:26:42"),
            ("333", "x", "demo", "2026-06-01T00:00:00"),   # not pending
        ])
        svc._pending_digest_sync()

        kwargs = svc.bot.send_message.call_args.kwargs
        assert kwargs['chat_id'] == '-100777'
        assert kwargs['message_thread_id'] == 15
        assert 'Незакрытые заявки' in kwargs['text'] and '@ivan' in kwargs['text']
        rows = kwargs['reply_markup']['inline_keyboard']
        assert len(rows) == 2
        # ":digest" marks the button as living on the digest card, so the
        # approve/reject callbacks re-render the list instead of stamping
        # it (see tests/unit/test_pending_digest_refresh.py).
        assert rows[0][0]['callback_data'] == 'approve:111:digest'
        assert rows[0][1]['callback_data'] == 'reject:111:digest'
        assert stored['pending_digest_sig']

    def test_unchanged_set_not_respammed_within_day(self, tmp_path):
        import hashlib, time
        sig = hashlib.sha256(b"111").hexdigest()[:16]
        svc, _ = self._svc(
            tmp_path,
            [("111", "ivan", "pending_demo", "2026-05-17")],
            settings={'pending_digest_sig': sig,
                      'pending_digest_ts': str(time.time())},
        )
        svc._pending_digest_sync()
        svc.bot.send_message.assert_not_called()

    def test_daily_reminder_when_still_pending(self, tmp_path):
        import hashlib, time
        sig = hashlib.sha256(b"111").hexdigest()[:16]
        svc, _ = self._svc(
            tmp_path,
            [("111", "ivan", "pending_demo", "2026-05-17")],
            settings={'pending_digest_sig': sig,
                      'pending_digest_ts': str(time.time() - 25 * 3600)},
        )
        svc._pending_digest_sync()
        svc.bot.send_message.assert_called_once()

    def test_empty_pending_clears_signature(self, tmp_path):
        svc, stored = self._svc(tmp_path, [])
        svc._pending_digest_sync()
        svc.bot.send_message.assert_not_called()
        assert stored['pending_digest_sig'] == ''
