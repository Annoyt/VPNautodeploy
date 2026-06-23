"""Callback handlers package.

Provides modular callback handling with SOLID principles.
"""

from bot.handlers.callbacks.handler import CallbackHandler
from bot.handlers.callbacks.base import (
    BaseCallbackHandler,
    ValidationService,
    ForumMessageService,
)
from bot.handlers.callbacks.dispatcher import CallbackDispatcher
from bot.handlers.callbacks.user import (
    DemoRequestHandler,
    PlatformSelectHandler,
    GetKeyHandler,
    MyKeyAnswerHandler,
    TryAltProtocolHandler,
    SupportRequestHandler,
    StatsRequestHandler,
    FullVersionHandler,
    LanguageSetHandler,
)
from bot.handlers.callbacks.admin import (
    ApproveUserHandler,
    RejectUserHandler,
    RevokeUserHandler,
    AdminMessageHandler,
    AdminProfileHandler,
    ResetApprovalHandler,
    CloseTicketHandler,
    BanFromTicketHandler,
)

__all__ = [
    'BaseCallbackHandler',
    'ValidationService',
    'ForumMessageService',
    'CallbackDispatcher',
    'DemoRequestHandler',
    'PlatformSelectHandler',
    'GetKeyHandler',
    'MyKeyAnswerHandler',
    'TryAltProtocolHandler',
    'SupportRequestHandler',
    'StatsRequestHandler',
    'FullVersionHandler',
    'LanguageSetHandler',
    'ApproveUserHandler',
    'RejectUserHandler',
    'RevokeUserHandler',
    'AdminMessageHandler',
    'AdminProfileHandler',
    'ResetApprovalHandler',
    'CloseTicketHandler',
    'BanFromTicketHandler',
    'CallbackHandler',
]
