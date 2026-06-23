"""Tests for notification service"""

import pytest
from unittest.mock import Mock, patch

from bot.config import Settings
from bot.models import User
from bot.services.notifications import NotificationService


class TestNotificationService:
    """Tests for NotificationService"""
    
    @pytest.fixture
    def config(self):
        """Create mock config"""
        cfg = Mock(spec=Settings)
        cfg.FORUM_ENABLED = True
        cfg.FORUM_GROUP_ID = '-1001234567890'
        cfg.SUPER_ADMIN_ID = '1652899'
        cfg.TOPIC_REQUESTS = 15
        cfg.TOPIC_PAYMENTS = 16
        cfg.TOPIC_SUPPORT = 17
        cfg.TOPIC_STATS = 18
        cfg.TOPIC_DEMO = 19
        cfg.TOPIC_REJECTED = 20
        cfg.XUI_DB_PATH = '/tmp/test-xui.db'
        cfg.DEMO_TRAFFIC_GB = 5
        return cfg
    
    @pytest.fixture
    def bot(self):
        """Create mock bot"""
        mock_bot = Mock()
        mock_bot.send_message.return_value = {'message_id': 123}
        mock_bot.send_message_to_topic.return_value = {'message_id': 456}
        mock_bot.create_forum_topic.return_value = 789
        mock_bot.forward_message.return_value = {'message_id': 999}
        
        # Mock DB
        mock_db = Mock()
        mock_bot.db = mock_db
        
        return mock_bot
    
    @pytest.fixture
    def mock_db(self):
        """Create mock database"""
        return Mock()
    
    @pytest.fixture
    def notifier(self, bot, mock_db, config):
        """Create NotificationService instance"""
        return NotificationService(bot, mock_db, config)
    
    @pytest.fixture
    def user(self):
        """Create test user"""
        return User(
            chat_id='123456789',
            username='testuser',
            status='pending_demo',
            lang='ru'
        )
    
    # ========== User Notification Tests ==========
    
    def test_notify_welcome(self, notifier, bot):
        """Test welcome notification"""
        result = notifier.notify_welcome('123456')
        
        assert result is True
        bot.send_message.assert_called_once()
        call_args = bot.send_message.call_args
        assert call_args[1]['chat_id'] == '123456'
        assert 'Добро пожаловать' in call_args[1]['text']
        assert 'reply_markup' in call_args[1]
    
    def test_notify_pending(self, notifier, bot):
        """Test pending notification"""
        result = notifier.notify_pending('123456')
        
        assert result is True
        bot.send_message.assert_called_once_with(
            chat_id='123456',
            text='⏳ Заявка отправлена администратору.\nОжидайте подтверждения.'
        )
    
    def test_notify_approved(self, notifier, bot):
        """Test approval notification"""
        result = notifier.notify_approved('123456')
        
        assert result is True
        bot.send_message.assert_called_once()
        call_args = bot.send_message.call_args
        assert '✅ Ваша заявка одобрена!' in call_args[1]['text']
        assert 'reply_markup' in call_args[1]
    
    def test_notify_platform_selected(self, notifier, bot):
        """Test platform selection notification"""
        from bot.config import Platform
        
        result = notifier.notify_platform_selected('123456', Platform.ANDROID)
        
        assert result is True
        bot.send_message.assert_called_once()
        call_args = bot.send_message.call_args
        assert 'ANDROID' in call_args[1]['text']
        assert 'reply_markup' in call_args[1]
    
    def test_notify_key_generated(self, notifier, bot):
        """Test key generation notification"""
        key = 'vless://test-uuid@example.com:443?security=reality'
        
        result = notifier.notify_key_generated('123456', key)
        
        assert result is True
        bot.send_message.assert_called_once()
        call_args = bot.send_message.call_args
        assert key in call_args[1]['text']
        assert call_args[1]['parse_mode'] == 'HTML'
    
    def test_notify_rejected(self, notifier, bot):
        """Test rejection notification"""
        result = notifier.notify_rejected('123456', 'Test reason')
        
        assert result is True
        bot.send_message.assert_called_once()
        call_args = bot.send_message.call_args
        assert '❌ Заявка отклонена' in call_args[1]['text']
        assert 'Test reason' in call_args[1]['text']
    
    def test_notify_rejected_no_reason(self, notifier, bot):
        """Test rejection without reason"""
        result = notifier.notify_rejected('123456', None)
        
        assert result is True
        call_args = bot.send_message.call_args
        assert 'Not specified' in call_args[1]['text']
    
    # ========== Admin Notification Tests ==========
    
    def test_notify_new_request_forum_mode(self, notifier, bot, config, user):
        """Test new request notification in forum mode"""
        config.FORUM_ENABLED = True
        
        result = notifier.notify_new_request(user)
        
        assert result == 456  # message_id from mock
        bot.send_message_to_topic.assert_called_once()
        call_args = bot.send_message_to_topic.call_args
        assert call_args[1]['chat_id'] == config.FORUM_GROUP_ID
        assert call_args[1]['message_thread_id'] == config.TOPIC_REQUESTS
        assert 'New Demo Request' in call_args[1]['text']
        assert call_args[1]['parse_mode'] == 'HTML'
    
    def test_notify_new_request_pm_mode(self, notifier, bot, config, user):
        """Test new request notification in PM mode"""
        config.FORUM_ENABLED = False
        
        result = notifier.notify_new_request(user)
        
        assert result == 123  # message_id from mock
        bot.send_message.assert_called_once()
        call_args = bot.send_message.call_args
        assert call_args[1]['chat_id'] == config.SUPER_ADMIN_ID
    
    def test_notify_new_request_callback_format(self, notifier, bot, config, user):
        """Test callback data format (CRITICAL: only chat_id, no username!)"""
        config.FORUM_ENABLED = False
        
        notifier.notify_new_request(user)
        
        call_args = bot.send_message.call_args
        keyboard = call_args[1]['reply_markup']
        
        # Check approve callback - should be 'approve:{chat_id}' only
        approve_callback = keyboard['inline_keyboard'][0][0]['callback_data']
        assert approve_callback == f'approve:{user.chat_id}'
        assert user.username not in approve_callback
        
        # Check reject callback
        reject_callback = keyboard['inline_keyboard'][0][1]['callback_data']
        assert reject_callback == f'reject:{user.chat_id}'
    
    def test_notify_new_support_ticket_forum_mode(self, notifier, bot, config, user):
        """Test support ticket creation in forum mode"""
        config.FORUM_ENABLED = True
        
        result = notifier.notify_new_support_ticket(user, 'Test issue')
        
        assert result == 789  # topic_id from mock
        bot.create_forum_topic.assert_called_once()
        bot.send_message_to_topic.assert_called_once()
    
    def test_notify_new_support_ticket_pm_mode(self, notifier, bot, config, user):
        """Test support ticket in PM mode"""
        config.FORUM_ENABLED = False
        
        result = notifier.notify_new_support_ticket(user, 'Test issue')
        
        assert result == 123  # message_id from mock
        bot.send_message.assert_called_once()
        call_args = bot.send_message.call_args
        assert 'Support Request' in call_args[1]['text']
    
    def test_notify_payment_issue_forum_mode(self, notifier, bot, config, user):
        """Test payment issue notification in forum mode"""
        config.FORUM_ENABLED = True
        
        result = notifier.notify_payment_issue(user, 'Payment failed')
        
        assert result is True
        bot.send_message_to_topic.assert_called_once()
        call_args = bot.send_message_to_topic.call_args
        assert call_args[1]['message_thread_id'] == config.TOPIC_PAYMENTS
    
    def test_notify_payment_issue_pm_mode(self, notifier, bot, config, user):
        """Test payment issue notification in PM mode"""
        config.FORUM_ENABLED = False
        
        result = notifier.notify_payment_issue(user, 'Payment failed')
        
        assert result is True
        bot.send_message.assert_called_once()
    
    # ========== Support Management Tests ==========
    
    def test_forward_to_support_with_topic(self, notifier, bot, user):
        """Test forwarding message to support topic"""
        user.support_topic_id = 789
        message = {'message_id': 555}
        
        result = notifier.forward_to_support(user, message)
        
        assert result is not None  # returns message_id (int)
        bot.forward_message.assert_called_once_with(
            chat_id=notifier.config.FORUM_GROUP_ID,
            from_chat_id=user.chat_id,
            message_id=555,
            message_thread_id=789
        )
    
    def test_forward_to_support_no_topic(self, notifier, user):
        """Test forwarding when no support topic exists"""
        user.support_topic_id = None
        message = {'message_id': 555}
        
        result = notifier.forward_to_support(user, message)
        
        assert result is None
    
    def test_reply_to_user(self, notifier, bot):
        """Test replying to user"""
        result = notifier.reply_to_user('123456', 'Hello!', admin_chat_id='111')
        
        assert result is True
        # First call is to user, second call is confirmation to admin
        assert bot.send_message.call_count == 2
        # Check first call (to user)
        first_call = bot.send_message.call_args_list[0]
        assert first_call[1]['chat_id'] == '123456'
        assert 'Hello!' in first_call[1]['text']
    
    # ========== Stats Tests ==========
    
    def test_notify_stats_admin(self, notifier, bot):
        """Test stats for admin"""
        stats = {
            'total': 100,
            'by_status': {'demo': 50, 'pending_demo': 10},
            'by_platform': {'android': 30, 'ios': 20}
        }
        
        result = notifier.notify_stats('1652899', stats, is_admin=True)
        
        assert result is True
        # notify_stats uses send_message, not send_message_to_topic
        bot.send_message.assert_called_once()
        call_args = bot.send_message.call_args
        assert '100' in call_args[1]['text']  # Total
        assert '30' in call_args[1]['text']   # Android
        assert '20' in call_args[1]['text']   # iOS
    
    def test_notify_stats_user_no_traffic(self, notifier, bot):
        """Test stats for user without traffic data"""
        user = Mock()
        user.chat_id = '123456'
        user.email = 'test@nekovo.ru'
        user.status = 'demo'
        bot.db.get_user.return_value = user
        
        with patch('bot.services.xui_service.XUIService') as mock_xui:
            mock_xui.return_value.get_client_traffic.return_value = None
            
            result = notifier.notify_stats('123456', {}, is_admin=False)
            
            assert result is True
            bot.send_message.assert_called_once()
