"""Tests for forum.py empty text IndexError fix."""

import pytest
from unittest.mock import MagicMock, patch

from bot.handlers.forum import ForumHandler
from bot.config import Settings


class TestForumEmptyTextFix:
    """Test fix for IndexError on empty text in forum.py."""
    
    @pytest.fixture
    def forum_handler(self):
        """Create ForumHandler with mocks."""
        bot = MagicMock()
        db = MagicMock()
        config = MagicMock(spec=Settings)
        config.FORUM_GROUP_ID = '-1001234567890'
        config.FORUM_ENABLED = True
        config.TOPIC_REQUESTS = 1
        config.TOPIC_USERS = 2
        config.TOPIC_REJECTED = 3
        config.TOPIC_STATS = 4
        
        handler = ForumHandler(bot, db, config)
        return handler
    
    def test_handle_forum_message_empty_text(self, forum_handler):
        """Test that empty text doesn't cause IndexError."""
        update = {
            'message': {
                'message_id': 123,
                'chat': {'id': '-1001234567890', 'type': 'supergroup'},
                'from': {'id': '123456', 'username': 'admin'},
                'text': '',  # Empty text
                'message_thread_id': 456
            }
        }
        
        # Should not raise IndexError
        forum_handler.handle(update)
        
        # Should not call handle_close_ticket (no /close command)
        # and should attempt to log the message
    
    def test_handle_forum_message_whitespace_only(self, forum_handler):
        """Test that whitespace-only text doesn't cause IndexError."""
        update = {
            'message': {
                'message_id': 123,
                'chat': {'id': '-1001234567890', 'type': 'supergroup'},
                'from': {'id': '123456', 'username': 'admin'},
                'text': '   \n\t  ',  # Whitespace only
                'message_thread_id': 456
            }
        }
        
        # Should not raise IndexError
        forum_handler.handle(update)
    
    def test_handle_forum_message_none_text(self, forum_handler):
        """Test that None text doesn't cause error."""
        update = {
            'message': {
                'message_id': 123,
                'chat': {'id': '-1001234567890', 'type': 'supergroup'},
                'from': {'id': '123456', 'username': 'admin'},
                'text': None,  # None text
                'message_thread_id': 456
            }
        }
        
        # Should not raise error
        forum_handler.handle(update)
    
    def test_handle_forum_message_close_command(self, forum_handler):
        """Test that /close command still works."""
        update = {
            'message': {
                'message_id': 123,
                'chat': {'id': '-1001234567890', 'type': 'supergroup'},
                'from': {'id': '123456', 'username': 'admin'},
                'text': '/close',
                'message_thread_id': 456
            }
        }
        
        with patch.object(forum_handler, 'handle_close_ticket') as mock_close:
            forum_handler.handle(update)
            
            # Should call handle_close_ticket
            mock_close.assert_called_once_with(update, 456)
    
    def test_handle_forum_message_close_with_extra_text(self, forum_handler):
        """Test that /close with extra text works."""
        update = {
            'message': {
                'message_id': 123,
                'chat': {'id': '-1001234567890', 'type': 'supergroup'},
                'from': {'id': '123456', 'username': 'admin'},
                'text': '/close ticket resolved',
                'message_thread_id': 456
            }
        }
        
        with patch.object(forum_handler, 'handle_close_ticket') as mock_close:
            forum_handler.handle(update)
            
            # Should call handle_close_ticket
            mock_close.assert_called_once_with(update, 456)
