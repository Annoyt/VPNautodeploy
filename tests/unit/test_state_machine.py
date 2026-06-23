"""Tests for state machine module"""

import os
import tempfile
import pytest
from unittest.mock import Mock

from bot.config.constants import UserState
from bot.core.database import Database, User
from bot.core.state_machine import StateMachine

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)


class TestStateMachine:
    """Tests for StateMachine class"""
    
    @pytest.fixture
    def db(self):
        """Create temporary database for testing"""
        with tempfile.NamedTemporaryFile(suffix='.db', delete=False) as f:
            db_path = f.name
        
        database = Database(db_path)
        yield database
        
        # Cleanup
        os.unlink(db_path)
    
    @pytest.fixture
    def sm(self, db):
        """Create StateMachine instance"""
        return StateMachine(db)
    
    def test_can_transition_valid(self, sm):
        """Test valid transitions"""
        # NEW -> PENDING_DEMO
        assert sm.can_transition(UserState.NEW, UserState.PENDING_DEMO) == True
        
        # PENDING_DEMO -> PLATFORM_SELECT
        assert sm.can_transition(UserState.PENDING_DEMO, UserState.PLATFORM_SELECT) == True
        
        # PENDING_DEMO -> BANNED
        assert sm.can_transition(UserState.PENDING_DEMO, UserState.BANNED) == True
        
        # PLATFORM_SELECT -> DEMO
        assert sm.can_transition(UserState.PLATFORM_SELECT, UserState.DEMO) == True
        
        # DEMO -> SUPPORT_TOPIC
        assert sm.can_transition(UserState.DEMO, UserState.SUPPORT_TOPIC) == True
        
        # SUPPORT_TOPIC -> DEMO
        assert sm.can_transition(UserState.SUPPORT_TOPIC, UserState.DEMO) == True
    
    def test_can_transition_invalid(self, sm):
        """Test invalid transitions"""
        # NEW cannot go directly to DEMO
        assert sm.can_transition(UserState.NEW, UserState.DEMO) == False
        
        # NEW cannot go to BANNED
        assert sm.can_transition(UserState.NEW, UserState.BANNED) == False
        
        # BANNED can only go to NEW (unban)
        assert sm.can_transition(UserState.BANNED, UserState.NEW) == True
        assert sm.can_transition(UserState.BANNED, UserState.DEMO) == False
        assert sm.can_transition(UserState.BANNED, UserState.PENDING_DEMO) == False
        
        # Cannot skip steps
        assert sm.can_transition(UserState.PENDING_DEMO, UserState.SUPPORT_TOPIC) == False
    
    def test_can_transition_same_state(self, sm):
        """Test transition to same state is always allowed"""
        for state in UserState:
            assert sm.can_transition(state, state) == True
    
    def test_transition_success(self, sm, db):
        """Test successful state transition"""
        # Create user
        user = User(chat_id='12345', status=UserState.NEW.value)
        db.save_user(user)
        
        # Perform transition
        result = sm.transition('12345', UserState.PENDING_DEMO)
        
        assert result == True
        
        # Verify
        updated = db.get_user('12345')
        assert updated.status == UserState.PENDING_DEMO.value
    
    def test_transition_invalid(self, sm, db):
        """Test invalid transition is rejected"""
        # Create user
        user = User(chat_id='12345', status=UserState.NEW.value)
        db.save_user(user)
        
        # Try invalid transition (NEW -> ACTIVE)
        result = sm.transition('12345', UserState.DEMO)
        
        assert result == False
        
        # Verify state unchanged
        unchanged = db.get_user('12345')
        assert unchanged.status == UserState.NEW.value
    
    def test_transition_nonexistent_user(self, sm):
        """Test transition for non-existent user"""
        result = sm.transition('99999', UserState.PENDING_DEMO)
        assert result == False
    
    def test_get_state_existing_user(self, sm, db):
        """Test getting state for existing user"""
        user = User(chat_id='12345', status=UserState.DEMO.value)
        db.save_user(user)
        
        state = sm.get_state('12345')
        
        assert state == UserState.DEMO
    
    def test_get_state_nonexistent_user(self, sm):
        """Test getting state for non-existent user"""
        state = sm.get_state('99999')
        assert state is None
    
    def test_get_state_invalid_state(self, sm, db):
        """Test getting state when invalid state stored"""
        # Manually insert user with invalid state
        conn = db._connect()
        c = conn.cursor()
        c.execute(
            "INSERT INTO users (chat_id, status) VALUES (?, ?)",
            ('12345', 'invalid_state')
        )
        conn.commit()
        conn.close()
        
        state = sm.get_state('12345')
        assert state is None
    
    def test_set_state(self, sm, db):
        """Test set_state method"""
        user = User(chat_id='12345', status=UserState.NEW.value)
        db.save_user(user)
        
        # Set state directly
        result = sm.set_state('12345', UserState.BANNED)
        
        assert result == True
        
        # Verify
        updated = db.get_user('12345')
        assert updated.status == UserState.BANNED.value
    
    def test_full_user_flow(self, sm, db):
        """Test complete user state flow"""
        # Create new user
        user = User(chat_id='12345', status=UserState.NEW.value)
        db.save_user(user)
        
        # NEW -> PENDING_DEMO
        assert sm.transition('12345', UserState.PENDING_DEMO) == True
        assert sm.get_state('12345') == UserState.PENDING_DEMO
        
        # PENDING_DEMO -> PLATFORM_SELECT
        assert sm.transition('12345', UserState.PLATFORM_SELECT) == True
        assert sm.get_state('12345') == UserState.PLATFORM_SELECT
        
        # PLATFORM_SELECT -> ACTIVE
        assert sm.transition('12345', UserState.DEMO) == True
        assert sm.get_state('12345') == UserState.DEMO
        
        # ACTIVE -> SUPPORT_TOPIC
        assert sm.transition('12345', UserState.SUPPORT_TOPIC) == True
        assert sm.get_state('12345') == UserState.SUPPORT_TOPIC
        
        # SUPPORT_TOPIC -> ACTIVE
        assert sm.transition('12345', UserState.DEMO) == True
        assert sm.get_state('12345') == UserState.DEMO
    
    def test_ban_user_flow(self, sm, db):
        """Test banning a user"""
        # Create pending user
        user = User(chat_id='12345', status=UserState.PENDING_DEMO.value)
        db.save_user(user)
        
        # Ban user
        assert sm.transition('12345', UserState.BANNED) == True
        assert sm.get_state('12345') == UserState.BANNED
        
        # Banned user can only be unbanned (transition to NEW)
        assert sm.transition('12345', UserState.DEMO) == False
        assert sm.transition('12345', UserState.NEW) == True
        assert sm.get_state('12345') == UserState.NEW
