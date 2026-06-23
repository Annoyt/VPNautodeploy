"""User notification helpers"""

from bot.config import Platform, MESSAGES
from bot.services.vpn import VPNService


def get_message(msg_key: str, lang: str = 'ru', **kwargs) -> str:
    """Get formatted message by key."""
    messages = MESSAGES.get(lang, MESSAGES['ru'])
    template = messages.get(msg_key, MESSAGES['ru'].get(msg_key, f"[{msg_key}]"))
    try:
        return template.format(**kwargs)
    except KeyError:
        return template


def build_welcome_keyboard(lang: str = 'ru') -> dict:
    """Build welcome message keyboard with language selection."""
    return {
        'inline_keyboard': [
            [{
                'text': get_message('btn_request_demo', lang),
                'callback_data': 'request_demo'
            }],
            [{
                'text': '🌐 English / Английский',
                'callback_data': 'set_lang:en'
            }]
        ]
    }


def build_platform_keyboard(chat_id: str) -> dict:
    """Build platform selection keyboard."""
    return {
        'inline_keyboard': [
            [
                {'text': '📱 Android', 'callback_data': f'platform:android:{chat_id}'},
                {'text': '🍎 iOS', 'callback_data': f'platform:ios:{chat_id}'}
            ],
            [
                {'text': '💻 Windows', 'callback_data': f'platform:windows:{chat_id}'},
                {'text': '🖥 macOS', 'callback_data': f'platform:macos:{chat_id}'}
            ],
            [
                {'text': '❓ Другое', 'callback_data': f'platform:other:{chat_id}'}
            ]
        ]
    }


def build_main_menu_keyboard(lang: str = 'ru', webapp_url: str = '') -> dict:
    """Build main menu keyboard after key generation.
    
    User menu: Statistics, My Key, Support, Full Version
    (No Dashboard - that's for admins only)
    """
    return {
        'inline_keyboard': [
            [
                {'text': get_message('btn_statistics', lang), 'callback_data': 'stats'},
                {'text': get_message('btn_my_key', lang), 'callback_data': 'my_key'}
            ],
            [
                {'text': get_message('btn_support', lang), 'callback_data': 'support'},
                {'text': get_message('btn_full_version', lang), 'callback_data': 'full'}
            ]
        ]
    }


def get_platform_message(platform: Platform, lang: str, vpn_service: VPNService) -> str:
    """Get platform selection message with instructions."""
    instructions = vpn_service.get_instructions(platform, lang)
    return get_message(
        'platform_selected',
        lang,
        platform=platform.value.upper(),
        instructions=instructions
    )
