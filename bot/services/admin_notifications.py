"""Admin notification helpers"""

from bot.config import Settings
from bot.config.constants import BYTES_PER_GB
from bot.models import User


def build_admin_request_keyboard(user: User) -> dict:
    """Build keyboard for new request notification.
    
    CRITICAL: Callback format must be 'action:{chat_id}' only!
    Never include username in callback data.
    """
    return {
        'inline_keyboard': [
            [
                {'text': '✅ Approve', 'callback_data': f'approve:{user.chat_id}'},
                {'text': '❌ Reject', 'callback_data': f'reject:{user.chat_id}'}
            ],
            [
                {'text': '💬 Message', 'callback_data': f'message:{user.chat_id}'},
                {'text': '👁 Profile', 'callback_data': f'profile:{user.chat_id}'}
            ],
            [
                {'text': '🔄 Сбросить одобрение', 'callback_data': f'reset_approval:{user.chat_id}'}
            ]
        ]
    }


def build_approved_user_keyboard(user: User) -> dict:
    """Build keyboard for approved user management."""
    return {
        'inline_keyboard': [
            [
                {'text': '🚫 Revoke', 'callback_data': f'revoke:{user.chat_id}'},
                {'text': '🔄 Reset Approval', 'callback_data': f'reset_approval:{user.chat_id}'}
            ]
        ]
    }


def build_rejected_user_keyboard(user: User) -> dict:
    """Build keyboard for rejected user management."""
    return {
        'inline_keyboard': [
            [
                {'text': '🔄 Reset Approval', 'callback_data': f'reset_approval:{user.chat_id}'}
            ]
        ]
    }


def format_new_request_text(user: User) -> str:
    """Format new request notification text."""
    username_display = f"@{user.username}" if user.username else "No username"
    return (
        f"🆕 <b>New Demo Request</b>\n\n"
        f"👤 User: {username_display}\n"
        f"🆔 Chat ID: <code>{user.chat_id}</code>\n"
        f"🌐 Language: {user.lang}\n"
        f"📅 Created: {user.created_at[:10]}"
    )


def format_support_ticket_text(user: User, message: str) -> str:
    """Format support ticket notification text."""
    username_display = f"@{user.username}" if user.username else "No username"
    return (
        f"🆘 <b>Support Ticket</b>\n\n"
        f"👤 User: {username_display}\n"
        f"🆔 Chat ID: <code>{user.chat_id}</code>\n\n"
        f"💬 Message:\n{message[:500]}"
    )


def format_pm_support_text(user: User, message: str) -> str:
    """Format support request for PM mode."""
    username_display = f"@{user.username}" if user.username else "No username"
    return (
        f"🆘 <b>Support Request</b>\n\n"
        f"👤 User: {username_display}\n"
        f"🆔 Chat ID: <code>{user.chat_id}</code>\n\n"
        f"💬 Message:\n{message[:500]}\n\n"
        f"<i>Reply to this message to respond to the user.</i>"
    )


def format_payment_issue_text(user: User, issue: str) -> str:
    """Format payment issue notification text."""
    username_display = f"@{user.username}" if user.username else "No username"
    return (
        f"💳 <b>Payment Issue</b>\n\n"
        f"👤 User: {username_display}\n"
        f"🆔 Chat ID: <code>{user.chat_id}</code>\n\n"
        f"⚠️ Issue: {issue[:500]}"
    )


def format_admin_stats_text(stats: dict) -> str:
    """Format admin statistics text."""
    text = (
        f"📊 <b>System Statistics</b>\n\n"
        f"👥 Total users: {stats.get('total', 0)}\n"
        f"⏳ Pending: {stats.get('by_status', {}).get('pending_demo', 0)}\n"
        f"🎁 Demo: {stats.get('by_status', {}).get('demo', 0)}\n"
        f"✅ Active: {stats.get('by_status', {}).get('active', 0)}\n"
        f"🚫 Banned: {stats.get('by_status', {}).get('banned', 0)}\n\n"
        f"📱 Platforms:\n"
    )
    for platform, count in stats.get('by_platform', {}).items():
        text += f"  {platform}: {count}\n"
    return text


def format_user_stats_text(
    user: User,
    traffic: dict,
    demo_traffic_gb: int
) -> str:
    """Format user statistics text in Russian."""
    lang = user.lang or 'ru'
    
    if traffic:
        used_gb = traffic.get('total', 0) / BYTES_PER_GB
        remaining = max(0, demo_traffic_gb - used_gb)
        
        if lang == 'ru':
            return (
                f"📊 <b>Ваша статистика</b>\n\n"
                f"📦 Всего: {demo_traffic_gb} GB\n"
                f"📥 Использовано: {used_gb:.2f} GB\n"
                f"📤 Остаток: {remaining:.2f} GB\n"
                f"Статус: {user.status}"
            )
        else:
            return (
                f"📊 <b>Your Statistics</b>\n\n"
                f"📦 Total: {demo_traffic_gb} GB\n"
                f"📥 Used: {used_gb:.2f} GB\n"
                f"📤 Remaining: {remaining:.2f} GB\n"
                f"Status: {user.status}"
            )
    else:
        if lang == 'ru':
            return (
                f"📊 <b>Ваша статистика</b>\n\n"
                f"📦 Всего: {demo_traffic_gb} GB\n"
                f"📥 Использовано: 0.00 GB\n"
                f"📤 Остаток: {demo_traffic_gb}.00 GB\n"
                f"Статус: {user.status}"
            )
        else:
            return (
                f"📊 <b>Your Statistics</b>\n\n"
                f"📦 Total: {demo_traffic_gb} GB\n"
                f"📥 Used: 0.00 GB\n"
                f"📤 Remaining: {demo_traffic_gb}.00 GB\n"
                f"Status: {user.status}"
            )
