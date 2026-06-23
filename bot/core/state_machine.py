"""State machine for user state management"""

import logging
from typing import Optional

from bot.config.constants import UserState, STATE_TRANSITIONS
from bot.core.database import Database

logger = logging.getLogger(__name__)


class StateMachine:
    """Manages user states and validates transitions"""
    
    def __init__(self, db: Database):
        self.db = db
    
    def can_transition(self, from_state: UserState, to_state: UserState) -> bool:
        """Check if transition from one state to another is allowed.
        
        Args:
            from_state: Current user state
            to_state: Desired new state
            
        Returns:
            True if transition is allowed, False otherwise
        """
        # Same state is always allowed (no-op)
        if from_state == to_state:
            return True
        
        # Check if transition exists in allowed transitions
        allowed_states = STATE_TRANSITIONS.get(from_state, [])
        return to_state in allowed_states
    
    def transition(self, chat_id: str, new_state: UserState) -> bool:
        """Perform state transition if valid.
        
        Args:
            chat_id: User's chat ID
            new_state: Target state
            
        Returns:
            True if transition succeeded, False otherwise
        """
        user = self.db.get_user(chat_id)
        
        if user is None:
            logger.warning(f"Cannot transition: user {chat_id} not found")
            return False
        
        try:
            current_state = UserState(user.status)
        except ValueError:
            logger.warning(f"Unknown state '{user.status}' for user {chat_id}, defaulting to DEMO")
            current_state = UserState.DEMO
        
        # Validate transition
        if not self.can_transition(current_state, new_state):
            logger.warning(
                f"Invalid transition: {current_state.value} -> {new_state.value} "
                f"for user {chat_id}"
            )
            return False
        
        # Save previous state for return transitions
        user.previous_state = current_state.value
        self.db.save_user(user)
        
        # Perform transition
        success = self.db.update_status(chat_id, new_state.value)
        
        if success:
            logger.info(
                f"State transition: {current_state.value} -> {new_state.value} "
                f"for user {chat_id}"
            )
        else:
            logger.error(f"Failed to update status for user {chat_id}")
        
        return success
    
    def return_from_support(self, chat_id: str) -> bool:
        """Return user to previous state after support ticket closes.
        
        Args:
            chat_id: User's chat ID
            
        Returns:
            True if transition succeeded
        """
        user = self.db.get_user(chat_id)
        if not user or user.status != UserState.SUPPORT_TOPIC.value:
            logger.warning(f"Cannot return from support: user {chat_id} not in support topic")
            return False

        # Determine return state. Default to DEMO if there's already a
        # key issued (user.email), otherwise to NEW so the next /start
        # walks them through approval again.
        target = UserState.DEMO if getattr(user, 'email', None) else UserState.NEW
        if user.previous_state:
            try:
                prev = UserState(user.previous_state)
                # SUPPORT_TOPIC as previous_state means we hit a
                # self-loop (the support transition stored its own
                # state because /support was clicked twice) — fall
                # back to the email-based default above instead of
                # ping-ponging back into support.
                if prev in (UserState.DEMO, UserState.PAID):
                    target = prev
            except ValueError:
                pass

        # `transition` validates the move; if SUPPORT_TOPIC → target
        # isn't allowed, fall back to set_state which is unchecked.
        if not self.transition(chat_id, target):
            logger.warning(
                f"return_from_support: transition {user.status}→{target.value} "
                f"rejected for {chat_id}; forcing via set_state"
            )
            return self.set_state(chat_id, target)
        return True
    
    def get_state(self, chat_id: str) -> Optional[UserState]:
        """Get current user state.
        
        Args:
            chat_id: User's chat ID
            
        Returns:
            Current UserState or None if user not found
        """
        user = self.db.get_user(chat_id)
        
        if user is None:
            return None
        
        try:
            return UserState(user.status)
        except ValueError:
            logger.error(f"Invalid state stored for user {chat_id}: {user.status}")
            return None
    
    def set_state(self, chat_id: str, state: UserState) -> bool:
        """Set user state without validation (use with caution).
        
        Args:
            chat_id: User's chat ID
            state: New state to set
            
        Returns:
            True if update succeeded
        """
        success = self.db.update_status(chat_id, state.value)
        
        if success:
            logger.info(f"State set to {state.value} for user {chat_id}")
        
        return success


