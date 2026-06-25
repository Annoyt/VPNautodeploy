"""Test that all ADMIN_COMMANDS entries map to actual methods.

This test prevents regressions where a command is added to ADMIN_COMMANDS
but the handler method is missing or renamed (e.g., after refactoring mixins).
"""
import pytest
from unittest.mock import Mock
from bot.handlers.admin import AdminHandler, AdminHandlerBase


class TestAdminCommandsCompleteness:
    """Verify ADMIN_COMMANDS routing table is complete and consistent."""

    def test_all_commands_have_handler_methods(self):
        """Every command in ADMIN_COMMANDS must have a corresponding method."""
        commands = AdminHandler.ADMIN_COMMANDS
        assert commands, "ADMIN_COMMANDS is empty!"

        # Create a mock instance (we only need to inspect methods)
        mock_bot = Mock()
        mock_db = Mock()
        mock_config = Mock()
        mock_config.SUPER_ADMIN_ID = "123"
        mock_config.FORUM_ENABLED = False

        handler = AdminHandler(mock_bot, mock_db, mock_config)

        missing = []
        for cmd, method_name in commands.items():
            if not hasattr(handler, method_name):
                missing.append(f"{cmd} -> {method_name}")

        if missing:
            pytest.fail(
                f"ADMIN_COMMANDS references non-existent methods:\n" +
                "\n".join(f"  - {m}" for m in missing)
            )

    def test_admin_handler_has_same_commands_as_base(self):
        """AdminHandler must have the same ADMIN_COMMANDS as AdminHandlerBase.

        This catches the regression where AdminHandler was defined without
        inheriting or copying ADMIN_COMMANDS from AdminHandlerBase.
        """
        handler_commands = AdminHandler.ADMIN_COMMANDS
        base_commands = AdminHandlerBase.ADMIN_COMMANDS

        # Must be identical (same keys, same values)
        assert handler_commands is not None, "AdminHandler.ADMIN_COMMANDS is None"
        assert base_commands is not None, "AdminHandlerBase.ADMIN_COMMANDS is None"

        missing_in_handler = set(base_commands.keys()) - set(handler_commands.keys())
        extra_in_handler = set(handler_commands.keys()) - set(base_commands.keys())

        if missing_in_handler or extra_in_handler:
            msg = ["ADMIN_COMMANDS mismatch between AdminHandler and AdminHandlerBase:"]
            if missing_in_handler:
                msg.append(f"  Missing in AdminHandler: {missing_in_handler}")
            if extra_in_handler:
                msg.append(f"  Extra in AdminHandler: {extra_in_handler}")
            pytest.fail("\n".join(msg))

        # Values (method names) must also match
        for cmd in base_commands:
            if base_commands[cmd] != handler_commands[cmd]:
                pytest.fail(
                    f"Command {cmd}: Base maps to {base_commands[cmd]}, "
                    f"Handler maps to {handler_commands[cmd]}"
                )

    def test_all_ops_commands_present(self):
        """Verify all ops commands from ops.py are in ADMIN_COMMANDS."""
        ops_commands = [
            '/status', '/whoami', '/onlines', '/find', '/recent',
            '/repair_stuck', '/topics', '/quota', '/expire',
        ]
        handler_commands = AdminHandler.ADMIN_COMMANDS
        missing = [cmd for cmd in ops_commands if cmd not in handler_commands]
        if missing:
            pytest.fail(f"Ops commands missing from ADMIN_COMMANDS: {missing}")

    def test_critical_admin_commands_routing(self):
        """Spot-check critical commands route to correct methods."""
        commands = AdminHandler.ADMIN_COMMANDS

        # Admin ops
        assert commands.get('/onlines') == 'show_onlines'
        assert commands.get('/status') == 'show_status'
        assert commands.get('/find') == 'find_user'
        assert commands.get('/quota') == 'set_quota'
        assert commands.get('/expire') == 'set_expire'

        # User management
        assert commands.get('/approve') == 'approve_user'
        assert commands.get('/reject') == 'reject_user'
        assert commands.get('/ban') == 'ban_user'
        assert commands.get('/reset') == 'reset_user'

        # Stats/broadcast
        assert commands.get('/stats') == 'show_overall_stats'
        assert commands.get('/broadcast') == 'broadcast_preview'
