"""Application constants"""

from enum import Enum


# Byte conversion constants
BYTES_PER_KB = 1024
BYTES_PER_MB = 1024 ** 2
BYTES_PER_GB = 1024 ** 3

# Timeout constants (seconds)
TIMEOUT_API_DEFAULT = 10
TIMEOUT_API_LONG_POLL = 30
TIMEOUT_API_FILE_UPLOAD = 60
TIMEOUT_XUI_API = 30
TIMEOUT_DOCKER_EXEC = 10
TIMEOUT_DB_OPERATION = 5
TIMEOUT_HEALTH_CHECK = 30

# Retry constants
MAX_RETRIES_API = 3
RETRY_DELAY_BASE = 1  # seconds

# VPN defaults
DEFAULT_VPN_PORT = 443
DEFAULT_FLOW = "xtls-rprx-vision"
DEFAULT_SECURITY = "reality"
DEFAULT_FINGERPRINT = "chrome"
DEFAULT_SPX = "/"

# Traffic defaults
DEFAULT_DEMO_TRAFFIC_GB = 10
DEFAULT_DEMO_DAYS = 7
DEFAULT_LIMIT_IP = 1

# Admin constants
MAX_USERNAME_LENGTH = 32
MAX_MESSAGE_LENGTH = 4096


class UserState(str, Enum):
    """User states in state machine"""
    NEW = "new"
    PENDING_DEMO = "pending_demo"
    REJECTED = "rejected"
    PLATFORM_SELECT = "platform_select"
    DEMO = "demo"
    PAID = "paid"
    SUPPORT_TOPIC = "support_topic"
    BANNED = "banned"


class Platform(str, Enum):
    """Supported platforms"""
    ANDROID = "android"
    IOS = "ios"
    WINDOWS = "windows"
    MACOS = "macos"
    LINUX = "linux"
    OTHER = "other"


# State transitions (from -> allowed_to)
STATE_TRANSITIONS = {
    UserState.NEW: [UserState.PENDING_DEMO],
    UserState.PENDING_DEMO: [UserState.PLATFORM_SELECT, UserState.REJECTED, UserState.BANNED],
    UserState.REJECTED: [UserState.PENDING_DEMO],
    UserState.PLATFORM_SELECT: [UserState.DEMO],
    UserState.DEMO: [UserState.SUPPORT_TOPIC, UserState.PAID, UserState.BANNED],
    UserState.PAID: [UserState.SUPPORT_TOPIC, UserState.BANNED],
    UserState.SUPPORT_TOPIC: [UserState.DEMO, UserState.PAID],
    UserState.BANNED: [UserState.NEW],
}


# Messages by language
MESSAGES = {
    'ru': {
        'welcome': (
            "👋 Добро пожаловать в <b>NekoVPN</b>\n\n"
            "🌍 <b>Стабильный доступ без блокировок</b>\n"
            "Автоматическое переключение между протоколами\n"
            "🚀 РФ сайты не ругаются на VPN\n\n"
            "🎁 <b>Демо бесплатно</b> — попробуй сам\n\n"
            "💳 Оплата через Telegram Stars\n"
            "(другие способы — через /support)\n\n"
            "Нажми кнопку ниже для начала:"
        ),
        'request_sent': (
            "⏳ <b>Заявка на рассмотрении</b>\n\n"
            "Мы проверяем аккаунт для защиты от ботов.\n\n"
            "✅ Есть @username, фото или описание?\n"
            "   → Одобрим автоматически\n\n"
            "❌ Нет ничего из этого?\n"
            "   → Проверим вручную (2-4 часа)\n\n"
            "🔔 Уведомление придёт в этот чат"
        ),
        'approved': (
            "✅ <b>Доступ открыт!</b>\n\n"
            "Выбери свою платформу:"
        ),
        'already_active': "✅ Вы уже активный пользователь. Вот ваше меню:",
        'rejected': "❌ Заявка отклонена. Причина: {reason}",
        'platform_selected': (
            "📱 {platform}\n\n"
            "{instructions}\n\n"
            "Нажмите кнопку для получения ключа:"
        ),
        'key_generated': (
            "🔑 Ваш VLESS ключ:\n\n"
            "<code>{key}</code>\n\n"
            "Нажмите для копирования, затем импортируйте в приложение."
        ),
        'support_prompt': (
            "🆘 <b>Опиши проблему:</b>\n\n"
            "• Что именно не работает?\n"
            "• Когда началось?\n"
            "• Какой интернет (МТС/Билайн/дом.ру)?\n\n"
            "⏱ Обычно отвечаем в течение часа"
        ),
        'support_ticket_created': "🎫 Тикет #{ticket_id} создан. Мы ответим в ближайшее время.",
        'error_generic': (
            "⚠️ <b>Что-то пошло не так</b>\n\n"
            "Попробуй через пару минут.\n\n"
            "Если не помогает — напиши /support"
        ),
        'btn_request_demo': "🚀 Запросить демо",
        'btn_my_key': "🔑 Мой ключ",
        'btn_statistics': "📊 Статистика",
        'btn_support': "💬 Поддержка",
        'btn_full_version': "💎 Полная версия",
        'btn_dashboard': "📈 Дашборд",
        'btn_upgrade_100gb': "🚀 Продлить 100GB",
    },
    'en': {
        'welcome': (
            "👋 Welcome to <b>NekoVPN</b>\n\n"
            "🌍 <b>Stable access without blocks</b>\n"
            "Auto-switching between protocols\n"
            "🚀 RU sites work fine with VPN\n\n"
            "🎁 <b>Free demo</b> — try it yourself\n\n"
            "💳 Payment via Telegram Stars\n"
            "(other methods via /support)\n\n"
            "Click button below to start:"
        ),
        'request_sent': (
            "⏳ <b>Request pending</b>\n\n"
            "We check accounts to prevent bots.\n\n"
            "✅ Have @username, photo or bio?\n"
            "   → Auto-approved\n\n"
            "❌ Nothing above?\n"
            "   → Manual check (2-4 hours)\n\n"
            "🔔 Notification will arrive here"
        ),
        'approved': (
            "✅ <b>Access granted!</b>\n\n"
            "Choose your platform:"
        ),
        'already_active': "✅ You are already an active user. Here is your menu:",
        'rejected': "❌ Request rejected. Reason: {reason}",
        'platform_selected': (
            "📱 {platform}\n\n"
            "{instructions}\n\n"
            "Click button to get your key:"
        ),
        'key_generated': (
            "🔑 Your VLESS key:\n\n"
            "<code>{key}</code>\n\n"
            "Tap to copy, then import to app."
        ),
        'support_prompt': (
            "🆘 <b>Describe your issue:</b>\n\n"
            "• What exactly isn't working?\n"
            "• When did it start?\n"
            "• Which internet (MTS/Beeline/home.ru)?\n\n"
            "⏱ Usually reply within an hour"
        ),
        'support_ticket_created': "🎫 Ticket #{ticket_id} created. We'll reply soon.",
        'error_generic': (
            "⚠️ <b>Something went wrong</b>\n\n"
            "Try again in a couple of minutes.\n\n"
            "If it doesn't help — write /support"
        ),
        'btn_request_demo': "🚀 Request demo",
        'btn_my_key': "🔑 My key",
        'btn_statistics': "📊 Statistics",
        'btn_support': "💬 Support",
        'btn_full_version': "💎 Full version",
        'btn_dashboard': "📈 Dashboard",
        'btn_upgrade_100gb': "🚀 Upgrade 100GB",
    }
}


# Platform instructions
PLATFORM_INSTRUCTIONS = {
    'ru': {
        Platform.ANDROID: (
            "1. Установи Hiddify:\n"
            "   📱 Google Play: https://play.google.com/store/apps/details?id=com.hiddify.desktop\n"
            "   или с сайта: https://hiddify.com/\n"
            "2. Открой приложение\n"
            "3. Нажми «+» → «Импорт из буфера обмена»\n"
            "4. 💡 Для устойчивости: Settings → Config Options →\n"
            "   включи «Enable ECH» (Encrypted ClientHello)\n\n"
            "❓ Если что-то не удалось — напиши /support"
        ),
        Platform.IOS: (
            "1. Установите Karing из App Store\n"
            "   📱 https://karing.app/\n\n"
            "   Hiddify и Happ удалили из RU App Store —\n"
            "   Karing остался и полностью работает.\n\n"
            "2. Откройте приложение\n"
            "3. Нажмите «+» → «Добавить из ссылки» и вставьте ссылку из\n"
            "   сообщения с ключом (придёт следующим сообщением)\n"
            "4. Нажмите кнопку подключения\n\n"
            "❓ Если что-то не удалось — напишите /support"
        ),
        Platform.WINDOWS: (
            "1. Скачай Hiddify для Windows:\n"
            "   💻 https://hiddify.com/en/download (выбери Windows)\n"
            "2. Запусти установщик (hiddify-windows-x.x.exe)\n"
            "3. В приложении нажми «+» → «Из буфера обмена»\n"
            "4. 💡 Для устойчивости: Settings → Config Options →\n"
            "   включи «Enable ECH» (Encrypted ClientHello)\n\n"
            "❓ Если что-то не удалось — напиши /support"
        ),
        Platform.MACOS: (
            "1. Скачайте Hiddify: https://hiddify.com/\n"
            "2. Установите .dmg\n"
            "3. В приложении нажмите «+» → «Из буфера обмена»\n"
            "4. 💡 Для устойчивости: Settings → Config Options →\n"
            "   включи «Enable ECH» (Encrypted ClientHello)"
        ),
        Platform.LINUX: (
            "1. Скачайте Hiddify (.AppImage / .deb): https://hiddify.com/\n"
            "2. Запустите\n"
            "3. В приложении нажмите «+» → «Из буфера обмена»\n"
            "4. 💡 Для устойчивости: Settings → Config Options →\n"
            "   включи «Enable ECH» (Encrypted ClientHello)"
        ),
        Platform.OTHER: (
            "Рекомендуем Hiddify: https://hiddify.com/\n"
            "(поддерживает VLESS Reality + Hysteria2)"
        ),
    },
    'en': {
        Platform.ANDROID: (
            "1. Install Hiddify:\n"
            "   📱 Google Play: https://play.google.com/store/apps/details?id=com.hiddify.desktop\n"
            "   or from website: https://hiddify.com/\n"
            "2. Open the app\n"
            "3. Tap «+» → «Import from clipboard»\n"
            "4. 💡 For DPI resistance: Settings → Config Options →\n"
            "   turn on «Enable ECH» (Encrypted ClientHello)\n\n"
            "❓ If something doesn't work — write /support"
        ),
        Platform.IOS: (
            "1. Install Karing from the App Store\n"
            "   📱 https://karing.app/\n\n"
            "   (Hiddify and Happ were pulled from the RU App Store —\n"
            "   Karing remains and works fine.)\n\n"
            "2. Open the app\n"
            "3. Tap «+» → «Add from URL» and paste the link from the key\n"
            "   message (arrives next)\n"
            "4. Tap the connect button\n\n"
            "❓ If something doesn't work — write /support"
        ),
        Platform.WINDOWS: (
            "1. Download Hiddify for Windows:\n"
            "   💻 https://hiddify.com/en/download (select Windows)\n"
            "2. Run the installer (hiddify-windows-x.x.exe)\n"
            "3. In the app tap «+» → «From clipboard»\n"
            "4. 💡 For DPI resistance: Settings → Config Options →\n"
            "   turn on «Enable ECH» (Encrypted ClientHello)\n\n"
            "❓ If something doesn't work — write /support"
        ),
        Platform.MACOS: (
            "1. Download Hiddify: https://hiddify.com/\n"
            "2. Install the .dmg\n"
            "3. In the app tap «+» → «From clipboard»\n"
            "4. 💡 For DPI resistance: Settings → Config Options →\n"
            "   turn on «Enable ECH» (Encrypted ClientHello)"
        ),
        Platform.LINUX: (
            "1. Download Hiddify (.AppImage / .deb): https://hiddify.com/\n"
            "2. Run it\n"
            "3. In the app tap «+» → «From clipboard»\n"
            "4. 💡 For DPI resistance: Settings → Config Options →\n"
            "   turn on «Enable ECH» (Encrypted ClientHello)"
        ),
        Platform.OTHER: (
            "We recommend Hiddify: https://hiddify.com/\n"
            "(supports both VLESS Reality and Hysteria2)"
        ),
    }
}
