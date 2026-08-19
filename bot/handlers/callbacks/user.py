"""User callback handlers."""

import asyncio
import html
import logging
import threading
from typing import TYPE_CHECKING, Optional

from bot.config import Platform, UserState, BYTES_PER_GB
from bot.core.state_machine import StateMachine
from bot.handlers.callbacks.base import BaseCallbackHandler
from bot.services.notifications import NotificationService
from bot.services.vpn import VPNService
from bot.services.account_verification import AccountVerificationService
from bot.utils.exceptions import VPNBotError

if TYPE_CHECKING:
    from bot.core.bot import Bot
    from bot.core.database import Database
    from bot.config import Settings

logger = logging.getLogger(__name__)


class DemoRequestHandler(BaseCallbackHandler):
    """Handle demo request callback."""
    
    CALLBACK_DATA = 'request_demo'
    
    # Rate limiting storage: chat_id -> last_request_timestamp
    _demo_request_times: dict = {}
    DEMO_RATE_LIMIT_SECONDS = 60  # 1 minute between demo requests
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data == self.CALLBACK_DATA
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process demo request."""
        if not self._check_rate_limit(chat_id):
            return
        
        user = self.db.get_user(chat_id)
        
        if not user:
            user = self._get_or_create_user(update)
        
        if not self._can_request_demo(user):
            self._notify_already_requested(chat_id)
            return
        
        self._process_demo_request(chat_id, user)
    
    def _check_rate_limit(self, chat_id: str) -> bool:
        """Check and update rate limit for demo requests."""
        import time
        current_time = time.time()
        
        # Cleanup old entries to prevent memory leak (H-04 fix)
        cleanup_threshold = self.DEMO_RATE_LIMIT_SECONDS * 2
        DemoRequestHandler._demo_request_times = {
            k: v for k, v in DemoRequestHandler._demo_request_times.items()
            if current_time - v < cleanup_threshold
        }
        
        last_request = DemoRequestHandler._demo_request_times.get(chat_id, 0)
        time_since_last = current_time - last_request
        
        if time_since_last < self.DEMO_RATE_LIMIT_SECONDS:
            remaining = int(self.DEMO_RATE_LIMIT_SECONDS - time_since_last)
            self.bot.send_message(
                chat_id=chat_id,
                text=f"⏳ Please wait {remaining} seconds before requesting demo again."
            )
            return False
        
        DemoRequestHandler._demo_request_times[chat_id] = current_time
        return True
    
    def _can_request_demo(self, user) -> bool:
        """Check if user can request demo."""
        if not user:
            return False
        if user.status == UserState.REJECTED.value:
            if user.reject_count >= self.config.MAX_REJECT_RETRIES:
                return False  # Rate limited
            return True
        return user.status in [UserState.NEW.value]
    
    def _notify_already_requested(self, chat_id: str) -> None:
        """Notify user they already have a request."""
        self.bot.send_message(
            chat_id=chat_id,
            text="⏳ You already have a pending request or active access."
        )
    
    def _process_demo_request(self, chat_id: str, user) -> None:
        """Process the demo request with account verification.

        Routing rule (added 2026-06):
          • Account verification check (username OR photos OR bio) → auto-approve.
            This catches real users while filtering out bots/throwaways.
            Auto-approving cuts the NEW → DEMO time to seconds.
          • No signals detected → manual flow (PENDING_DEMO + admin approve/reject).

        Either way we transition NEW/REJECTED → PENDING_DEMO first
        so the state machine sees a clean PENDING_DEMO → PLATFORM_SELECT
        edge (the only edge it allows out of PENDING_DEMO).
        """
        sm = StateMachine(self.db)
        notifier = NotificationService(self.bot, self.db, self.config)
        verifier = AccountVerificationService(self.bot)

        if user.status in (UserState.NEW.value, UserState.REJECTED.value):
            sm.transition(chat_id, UserState.PENDING_DEMO)

        # Run account verification
        verification = verifier.verify_account(chat_id)

        if verification.is_realistic:
            # Auto-approve: account looks real
            sm.transition(chat_id, UserState.PLATFORM_SELECT)
            notifier.notify_approved(chat_id, user.lang)

            # Log the auto-approval with signals
            try:
                self.db.log_admin_action(
                    'auto', 'auto_approve_demo', str(chat_id),
                    f"signals={','.join(verification.signals)}",
                )
            except Exception:
                pass

            logger.info(
                f"Demo auto-approved for {chat_id} "
                f"(signals: {verification.signals}, confidence: {verification.confidence})"
            )
            return

        # Manual approval needed
        notifier.notify_pending(chat_id, user.lang)
        notifier.notify_new_request(user)

        logger.info(
            f"Demo requested by {chat_id} (no verification signals — manual approval)"
        )


class PlatformSelectHandler(BaseCallbackHandler):
    """Handle platform selection callback."""
    
    CALLBACK_PATTERN = 'platform:'
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process platform selection."""
        data = kwargs.get('data', '')
        parts = data.split(':')
        
        if len(parts) < 2 or not parts[1]:
            logger.warning(f"Invalid platform callback data: {data}")
            return
        
        platform = parts[1]
        target_id = chat_id
        
        self._process_platform_selection(target_id, platform)
    
    def _process_platform_selection(self, chat_id: str, platform: str) -> None:
        """Process platform selection for user."""
        user = self.db.get_user(chat_id)
        if not user:
            return
        
        user.platform = platform
        self.db.save_user(user)
        
        sm = StateMachine(self.db)
        sm.transition(chat_id, UserState.DEMO)
        
        platform_enum = self._parse_platform(platform)
        
        notifier = NotificationService(self.bot, self.db, self.config)
        notifier.notify_platform_selected(chat_id, platform_enum, user.lang)
        
        logger.info(f"User {chat_id} selected platform {platform}")
    
    def _parse_platform(self, platform: str) -> Platform:
        """Parse platform string to enum."""
        try:
            return Platform(platform)
        except ValueError:
            return Platform.OTHER


def build_key_delivery_message(user, config) -> tuple:
    """    (text, keyboard) for the platform/lang-aware key delivery message.

    Returns ``(None, None)`` when the subscription URL can't be built
    (WEBAPP_URL unset) so the caller can degrade to a raw key. Shared by
    GetKeyHandler (first-time issuance) and SetPlatformHandler
    (re-selection from the /sub message — users switch devices). iOS gets
    Karing instructions (sing-box client — the RU App Store pulled both
    Hiddify and Happ); the URL itself is the plain sing-box subscription
    for every platform.
    """
    from bot.services.subscription import SubscriptionService
    sub = SubscriptionService(config)
    url = sub.build_subscription_url(user)
    if not url:
        return None, None
    lang = (getattr(user, 'lang', None) or 'ru')
    is_ios = (getattr(user, 'platform', None) or '') == 'ios'
    if is_ios and lang != 'en':
        text = (
            "✅ <b>Твой VPN готов</b>\n\n"
            "1. Установи Karing из App Store (Hiddify и Happ удалили "
            "из RU-маркета): https://karing.app/\n"
            "2. Добавь подписку — тапни по ссылке чтобы скопировать:\n\n"
            f"<code>{url}</code>\n\n"
            "3. В Karing: <b>+ → Добавить из ссылки → вставить → Сохранить</b>\n"
            "4. Нажми «Подключить». Клиент сам выбирает рабочий "
            "сервер и переключается если что-то падает.\n\n"
            "💡 Подписка сама обновляется каждые 6 часов — если мы "
            "меняем сервера, ничего переимпортировать не надо.\n\n"
            "Нужен сырой ключ одного протокола? /raw"
        )
    elif is_ios:
        text = (
            "✅ <b>Your VPN is ready</b>\n\n"
            "1. Install Karing from the App Store: https://karing.app/\n"
            "2. Add this subscription URL — tap the link below to copy:\n\n"
            f"<code>{url}</code>\n\n"
            "3. In Karing: <b>+ → Add from URL → paste → Save</b>\n"
            "4. Tap «Connect». The client picks the working outbound "
            "automatically and switches if something dies.\n\n"
            "💡 The subscription refreshes itself every 6 hours — "
            "server changes arrive automatically.\n\n"
            "Need a raw single-protocol key? Send /raw."
        )
    elif lang == 'en':
        text = (
            "✅ <b>Your VPN is ready</b>\n\n"
            "1. Install Hiddify: https://hiddify.com/\n"
            "2. Add this subscription URL — tap the link below to copy:\n\n"
            f"<code>{url}</code>\n\n"
            "3. In Hiddify: <b>+ → Add from URL → paste → Save</b>\n"
            "4. Tap «Connect». The client picks the working outbound "
            "automatically and switches if something dies.\n\n"
            "💡 Don't change settings — ECH is already on, RU sites "
            "bypass the VPN, foreign sites tunnel through us.\n\n"
            "Need a raw single-protocol key (legacy client)? Send /raw."
        )
    else:
        text = (
            "✅ <b>Твой VPN готов</b>\n\n"
            "1. Установи Hiddify: https://hiddify.com/\n"
            "2. Добавь subscription URL — тапни по ссылке чтобы скопировать:\n\n"
            f"<code>{url}</code>\n\n"
            "3. В Hiddify: <b>+ → Добавить из ссылки → вставить → Сохранить</b>\n"
            "4. Нажми «Подключить». Клиент сам выбирает рабочий "
            "протокол и переключается если что-то падает.\n\n"
            "💡 Ничего настраивать не надо — ECH уже включён, RU-сайты "
            "идут напрямую, заграничные — через нас.\n\n"
            "Нужен сырой ключ одного протокола для legacy-клиента? /raw"
        )
    btn_label = ("🆘 Не подключается? Сообщить" if lang == 'ru'
                 else "🆘 Not connecting? Report it")
    email_btn_label = ("📧 Указать email" if lang == 'ru'
                       else "📧 Add email")
    email_key_label = ("✉️ Ключ на почту" if lang == 'ru'
                       else "✉️ Email me the key")
    keyboard = {'inline_keyboard': [
        [{'text': email_btn_label, 'callback_data': 'add_email_prompt'},
         {'text': email_key_label, 'callback_data': 'email_key'}],
        [{'text': '🍎 iOS (Karing)', 'callback_data': 'setplat:ios'},
         {'text': '📱 Android', 'callback_data': 'setplat:android'},
         {'text': '💻 ПК', 'callback_data': 'setplat:windows'}],
        [{'text': btn_label, 'callback_data': 'report_failure'}]
    ]}
    return text, keyboard


class GetKeyHandler(BaseCallbackHandler):
    """Handle get key callback."""

    CALLBACK_DATA_EXACT = ['generate_key', 'my_key']
    CALLBACK_PATTERN = 'get_key:'

    # Retry parameters for X-UI sync. Class-level so tests can monkeypatch.
    SYNC_TIMEOUT_SEC: float = 15.0
    SYNC_MAX_ATTEMPTS: int = 3
    SYNC_BASE_BACKOFF_SEC: float = 2.0

    # Tracks chat_ids currently being processed so a double-tap on the button
    # doesn't trigger two parallel create_new_key flows (which used to create
    # two clients in X-UI for the same user). threading.Lock survives across
    # the fresh event loops _run_async may spin up via asyncio.run().
    _inflight_chat_ids: set = set()
    _inflight_guard = threading.Lock()

    def can_handle(self, callback_data: str) -> bool:
        if callback_data in self.CALLBACK_DATA_EXACT:
            return True
        return callback_data.startswith(self.CALLBACK_PATTERN)

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process get key request."""
        data = kwargs.get('data', '')

        # Extract target_id from callback data or use current chat_id
        if data.startswith('get_key:'):
            parts = data.split(':')
            target_id = parts[1] if len(parts) > 1 and parts[1] else chat_id
        else:
            target_id = chat_id

        # IDOR protection: users can only get their own key; admins can get any
        if str(user_id) != str(target_id) and not self._is_admin(user_id):
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ You can only retrieve your own VPN key."
            )
            logger.warning(f"IDOR attempt in get_key: user {user_id} tried to get key for {target_id}")
            return

        self._run_async(self._async_handle_get_key, target_id)

    async def _async_handle_get_key(self, chat_id: str) -> None:
        """Async handler for key generation."""
        if not self._claim_inflight(chat_id):
            logger.info(f"Key request for {chat_id} already in progress, ignoring duplicate")
            self.bot.send_message(
                chat_id=chat_id,
                text="⏳ Запрос уже обрабатывается, подождите..."
            )
            return
        try:
            await self._process_key_request(chat_id)
        except VPNBotError as e:
            logger.warning(f"VPN generation failed for {chat_id}: {e.message}")
            self.bot.send_message(chat_id=chat_id, text=e.user_message)
        except Exception as e:
            logger.exception(f"Unexpected error generating key for {chat_id}: {e}")
            self.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Произошла ошибка при генерации ключа. Попробуйте позже."
            )
        finally:
            self._release_inflight(chat_id)

    @classmethod
    def _claim_inflight(cls, chat_id: str) -> bool:
        with cls._inflight_guard:
            if chat_id in cls._inflight_chat_ids:
                return False
            cls._inflight_chat_ids.add(chat_id)
            return True

    @classmethod
    def _release_inflight(cls, chat_id: str) -> None:
        with cls._inflight_guard:
            cls._inflight_chat_ids.discard(chat_id)

    # Only users that already went through approval + platform pick are
    # allowed to receive a key. Without this gate a rejected/banned/new user
    # who still has a non-null uuid in the DB (e.g. they were rejected but
    # the cleanup didn't run on an older bot version) can press "Получить
    # ключ" and pull their old key back out.
    _KEY_ALLOWED_STATUSES = frozenset({
        UserState.DEMO.value,
        UserState.PAID.value,
        UserState.SUPPORT_TOPIC.value,
    })

    async def _process_key_request(self, chat_id: str) -> None:
        """Process key generation request."""
        user = self.validator.validate_user_exists(chat_id)

        if user.status not in self._KEY_ALLOWED_STATUSES:
            logger.warning(
                f"GetKey blocked for {chat_id}: status={user.status!r} not in allowed set"
            )
            self.bot.send_message(
                chat_id=chat_id,
                text=(
                    "⚠️ Для получения ключа нужно пройти одобрение и выбрать платформу. "
                    "Нажмите /start чтобы продолжить."
                ),
            )
            return

        if not user.uuid:
            await self._create_new_key(chat_id, user)
        else:
            await self._resync_existing_key(chat_id, user)

        await self._send_key_to_user(chat_id, user)

    async def _create_new_key(self, chat_id: str, user) -> None:
        """Create new VPN key for user.

        Order matters: X-UI sync must succeed before persisting uuid/email
        to the bot DB. If sync fails we raise without saving — the user
        keeps no orphan UUID pointing at a client that doesn't exist in
        X-UI, so the next request starts fresh instead of resyncing a
        ghost identifier.
        """
        vpn = VPNService(self.config)
        client = vpn.create_client_config(
            chat_id=chat_id,
            username=user.username,
            traffic_gb=self.config.DEMO_TRAFFIC_GB,
            expiry_days=self.config.DEMO_DAYS,
            comment=getattr(user, 'contact_email', None) or ''
        )

        user.uuid = client['id']
        user.email = client['email']

        await self._sync_to_xui(chat_id, client)
        self.db.save_user(user)

    async def _resync_existing_key(self, chat_id: str, user) -> None:
        """Resync existing key to X-UI."""
        await self._sync_to_xui(chat_id, {
            "id": user.uuid,
            "flow": "xtls-rprx-vision",
            "email": user.email,
            "limitIp": 1,
            "totalGB": self.config.DEMO_TRAFFIC_GB * BYTES_PER_GB,
            "expiryTime": 0,
            "enable": True
        })

    async def _sync_to_xui(self, chat_id: str, client_config: dict) -> None:
        """Sync client config to X-UI with bounded retries.

        Transient X-UI failures (502/timeout/socket errors) used to surface
        as a one-shot "couldn't create key" error to the user. We retry up
        to SYNC_MAX_ATTEMPTS with exponential backoff before giving up.
        """
        xui = self.bot.services.get('xui')
        if not xui:
            raise VPNBotError(
                "X-UI service unavailable",
                user_message="⚠️ Сервис VPN временно недоступен. Попробуйте позже."
            )

        last_error = "sync_user returned False"
        for attempt in range(1, self.SYNC_MAX_ATTEMPTS + 1):
            try:
                success = await asyncio.wait_for(
                    xui.sync_user(chat_id, client_config),
                    timeout=self.SYNC_TIMEOUT_SEC,
                )
                if success:
                    if attempt > 1:
                        logger.info(f"X-UI sync for {chat_id} succeeded on attempt {attempt}")
                    return
                logger.warning(f"X-UI sync for {chat_id} attempt {attempt}/{self.SYNC_MAX_ATTEMPTS}: returned False")
            except asyncio.TimeoutError:
                last_error = f"timeout after {self.SYNC_TIMEOUT_SEC}s"
                logger.warning(f"X-UI sync for {chat_id} attempt {attempt}/{self.SYNC_MAX_ATTEMPTS}: {last_error}")
            except Exception as e:
                last_error = f"{type(e).__name__}: {e}"
                logger.warning(f"X-UI sync for {chat_id} attempt {attempt}/{self.SYNC_MAX_ATTEMPTS}: {last_error}")

            if attempt < self.SYNC_MAX_ATTEMPTS:
                await asyncio.sleep(self.SYNC_BASE_BACKOFF_SEC * (2 ** (attempt - 1)))

        raise VPNBotError(
            f"Failed to sync user {chat_id} to X-UI after {self.SYNC_MAX_ATTEMPTS} attempts: {last_error}",
            user_message="⚠️ Не удалось создать VPN-ключ. Попробуйте через минуту."
        )

    async def _send_key_to_user(self, chat_id: str, user) -> None:
        """First-time delivery — hand the user their subscription URL.

        Post-round-robin onboarding: one URL, one paste, urltest does
        the rest. Message body/keyboard live in build_key_delivery_message
        (shared with SetPlatformHandler so a platform switch re-renders
        the exact same card).
        """
        if not user.uuid or not user.email:
            logger.error(
                f"Refusing to send key for {chat_id}: incomplete profile "
                f"(uuid={user.uuid!r} email={user.email!r})"
            )
            lang = (user.lang or 'ru')
            text = ("⚠️ Профиль повреждён. Напишите в поддержку." if lang == 'ru'
                    else "⚠️ Profile is corrupted. Contact support.")
            self.bot.send_message(chat_id=chat_id, text=text)
            return

        text, keyboard = build_key_delivery_message(user, self.config)
        if text is None:
            # WEBAPP_URL missing — degrade gracefully to a single CDN
            # raw key so onboarding doesn't completely fail.
            logger.error(f"WEBAPP_URL not configured, no sub URL for {chat_id}")
            vpn = VPNService(self.config)
            fallback = vpn.generate_vless_ws_link(user.uuid, user.email)
            if not fallback:
                msg = ("⚠️ Сервис не настроен — напишите в поддержку."
                       if (user.lang or 'ru') == 'ru'
                       else "⚠️ Service not configured — contact support.")
                self.bot.send_message(chat_id=chat_id, text=msg)
                return
            self.bot.send_message(
                chat_id=chat_id,
                text=f"<code>{fallback}</code>",
                parse_mode='HTML',
            )
            logger.info(f"Sent fallback raw key to {chat_id}")
            return
        self.bot.send_message(
            chat_id=chat_id, text=text, parse_mode='HTML',
            reply_markup=keyboard, disable_web_page_preview=True,
        )
        logger.info(f"Sent first-time sub URL to {chat_id}")


class SetPlatformHandler(BaseCallbackHandler):
    """Re-select platform from the key card (user switched devices).

    Callback data: ``setplat:<platform>``. Unlike the onboarding
    PlatformSelectHandler this must NOT touch the state machine — a paid
    user re-picking a platform is not a demo transition. It just stores
    the new platform and re-renders the key card in the right format
    (iOS → Karing instructions, everything else → Hiddify).
    The subscription URL itself is the plain sing-box one for all.
    """

    CALLBACK_PATTERN = 'setplat:'
    ALLOWED = ('android', 'ios', 'windows', 'macos', 'linux', 'other')
    KEY_STATUSES = ('demo', 'paid', 'support_topic')

    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        data = kwargs.get('data', '')
        try:
            platform = data.split(':', 1)[1]
        except IndexError:
            return
        if platform not in self.ALLOWED:
            logger.warning(f'setplat: unknown platform {platform!r}')
            return

        user = self.db.get_user(chat_id)
        lang = (getattr(user, 'lang', None) or 'ru') if user else 'ru'
        if (
            not user or user.status not in self.KEY_STATUSES
            or not user.uuid or not user.email
        ):
            self.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Сначала получите ключ — /start" if lang == 'ru'
                     else "⚠️ Get a key first — /start",
            )
            return

        user.platform = platform
        self.db.save_user(user)
        logger.info(f'User {chat_id} re-selected platform → {platform}')

        text, keyboard = build_key_delivery_message(user, self.config)
        self.bot.send_message(
            chat_id=chat_id, text=text, parse_mode='HTML',
            reply_markup=keyboard, disable_web_page_preview=True,
        )


class MyKeyAnswerHandler(BaseCallbackHandler):
    """Handles the "🆘 не подключается" button on every key message.

    Post-round-robin behavior: the button no longer rotates protocols
    (sing-box urltest does that on the client side from the subscription
    URL). Instead each tap is a *user-confirmed connectivity failure*
    that we log with full context for the dashboard heatmap and ping
    the support topic.

    Old mykey_yes / mykey_no / mykey_trigger callbacks from messages
    sent before this change still resolve here so a stale button in
    a user's TG history doesn't die silently:
      - mykey_yes  → polite ack ("noted, glad it worked")
      - mykey_no, mykey_trigger, report_failure → log a failure report
    """

    CALLBACK_DATA_EXACT = ['report_failure', 'mykey_trigger', 'mykey_yes', 'mykey_no']
    CALLBACK_PATTERN_TARGET = 'report_target:'
    REPORT_RATE_LIMIT_SECONDS = 600  # 10 min — keeps the support topic noise-free
    _last_report_times: dict = {}

    # Failure categories for the dropdown. Values map to user-facing labels
    # in both languages and are stored in user_failure_reports.target_domain.
    FAILURE_CATEGORIES = {
        'ru_services': {'ru': 'VK / Яндекс / RuTube', 'en': 'VK / Yandex / RuTube'},
        'banks': {'ru': 'Банковские приложения', 'en': 'Banking apps'},
        'games_streaming': {'ru': 'Игры / Стриминг', 'en': 'Games / Streaming'},
        'nothing_loads': {'ru': 'Вообще ничего не грузится', 'en': 'Nothing loads at all'},
        'other': {'ru': 'Другое', 'en': 'Other'},
    }

    # Maps short protocol names (used in cascade-order setting & API)
    # to the VPNService method that produces the link. The order in
    # which they appear in the cascade is dynamic — read from
    # app_settings['cascade_protocol_order'], with the default below
    # used when the setting is missing.
    PROTOCOL_METHOD_MAP = {
        'stls':    'generate_stls_link',
        'ws':      'generate_vless_ws_link',
        'xhttp':   'generate_vmess_xhttp_link',
        'hy2':     'generate_hy2_link',
        'reality': 'generate_vless_link',
    }
    # Per-protocol tier gates which user tiers see which protocols.
    # `free` = stealth-only set the demo cohort gets — every entry
    # hides our infra IP behind CF or a microsoft.com handshake.
    # `paid` = adds the direct-to-entry fast paths (Hy2, Reality).
    # Direct paths leak our entry IP to the client and to DPI on the
    # client's network, which is acceptable for paying users who get
    # priority IP rotation but not for the demo pool where one burnt
    # IP would melt the whole free tier.
    PROTOCOL_TIER = {
        'stls':    'free',
        'ws':      'free',
        'xhttp':   'free',
        'hy2':     'paid',
        'reality': 'paid',
    }
    PAID_USER_STATUSES = frozenset({'paid', 'support_topic'})
    # Resilience-first default ladder. Both CDN-fronted VMess variants
    # (httpupgrade `ws` and the new `xhttp`) sit between ShadowTLS and
    # the direct-to-entry protocols. Reality empirically banned first
    # by RKN once a new entry IP is profiled, so it stays at the
    # bottom. Admin can override via dashboard.
    DEFAULT_CASCADE_ORDER = ('stls', 'ws', 'xhttp', 'hy2', 'reality')
    SETTING_KEY = 'cascade_protocol_order'
    COUNTRY_SETTING_KEY = 'cascade_by_country'
    ASN_SETTING_KEY = 'cascade_by_asn'

    # Hardcoded per-country defaults. Used when the operator hasn't
    # set ``cascade_by_country`` and we know the user's last_country.
    # Russia / Belarus / Iran / China get the resilience-heavy ladder
    # (CDN + stealth first). Other regions get the fast direct-first
    # variant since DPI there is rarely an issue and Reality/Hy2 have
    # the best throughput. The free-tier filter still applies on top
    # of whatever ladder is picked here.
    COUNTRY_CASCADE_DEFAULTS = {
        'RU': ('stls', 'ws', 'xhttp', 'hy2', 'reality'),
        'BY': ('stls', 'ws', 'xhttp', 'hy2', 'reality'),
        'IR': ('stls', 'ws', 'xhttp', 'hy2', 'reality'),
        'CN': ('stls', 'ws', 'xhttp', 'hy2', 'reality'),
        # Direct-first for less-censored regions.
        'KZ': ('reality', 'hy2', 'stls', 'ws', 'xhttp'),
        'UA': ('reality', 'hy2', 'stls', 'ws', 'xhttp'),
        'KG': ('reality', 'hy2', 'stls', 'ws', 'xhttp'),
        'AM': ('reality', 'hy2', 'stls', 'ws', 'xhttp'),
        'GE': ('reality', 'hy2', 'stls', 'ws', 'xhttp'),
    }

    @classmethod
    def get_cascade_config(cls, db) -> list:
        """Read the operator-configured cascade as a list of dicts
        ``[{name: str, enabled: bool}, ...]``. Backward compatible
        with the legacy bare-string-list format (everything enabled).

        Used by the dashboard editor to render the full set with
        checkboxes. The bot uses ``get_cascade_order`` instead, which
        is the filtered enabled-only view of this.
        """
        raw = db.get_setting(cls.SETTING_KEY) if db else None
        if raw:
            try:
                import json
                parsed = json.loads(raw)
                if isinstance(parsed, list):
                    out = []
                    seen = set()
                    for item in parsed:
                        if isinstance(item, str):
                            name, enabled = item, True
                        elif isinstance(item, dict) and isinstance(item.get('name'), str):
                            name = item['name']
                            enabled = bool(item.get('enabled', True))
                        else:
                            continue
                        if name in cls.PROTOCOL_METHOD_MAP and name not in seen:
                            seen.add(name)
                            out.append({'name': name, 'enabled': enabled})
                    if out:
                        # Add any known protocol that's missing from the
                        # stored config as enabled at the end, so a new
                        # protocol shipped in code appears without the
                        # operator having to re-save.
                        for name in cls.PROTOCOL_METHOD_MAP:
                            if name not in seen:
                                out.append({'name': name, 'enabled': True})
                        return out
            except (ValueError, TypeError):
                pass
        return [{'name': n, 'enabled': True} for n in cls.DEFAULT_CASCADE_ORDER]

    @classmethod
    def _coerce_order(cls, candidate) -> Optional[tuple]:
        """Project an operator-supplied list onto known protocols."""
        if not isinstance(candidate, list) or not candidate:
            return None
        order = tuple(
            n for n in candidate
            if isinstance(n, str) and n in cls.PROTOCOL_METHOD_MAP
        )
        return order or None

    @classmethod
    def get_asn_cascade(cls, db, asn: str) -> Optional[tuple]:
        """Resolve the per-ASN ordered cascade from
        ``app_settings.cascade_by_asn`` (operator-set JSON of the form
        ``{"AS8359": ["xhttp", ...], "AS31133": [...]}``). Returns
        ``None`` if no override exists for this ASN. ASN is the finest
        per-RU granularity we have (each big operator = own ASN), so
        this is where most of the real tuning happens.
        """
        if not asn:
            return None
        asn = asn.upper()
        raw = db.get_setting(cls.ASN_SETTING_KEY) if db else None
        if not raw:
            return None
        try:
            import json
            parsed = json.loads(raw)
            if isinstance(parsed, dict):
                return cls._coerce_order(parsed.get(asn))
        except (ValueError, TypeError) as e:
            logger.warning(f"bad cascade_by_asn JSON: {e}")
        return None

    @classmethod
    def get_country_cascade(cls, db, country: str) -> Optional[tuple]:
        """Resolve the per-country ordered cascade.

        Lookup order:
        1. Operator override in ``app_settings.cascade_by_country``
           (JSON ``{"RU": ["stls", ...], "default": [...]}``).
        2. Hardcoded ``COUNTRY_CASCADE_DEFAULTS`` map.
        3. ``None`` — caller falls back to the global ``get_cascade_config``
           order.

        Returns a tuple of *all known* protocol names; the caller still
        intersects with the enabled set and tier filter.
        """
        if not country:
            return None
        country = country.upper()
        # Operator-set JSON override wins over hardcoded defaults.
        raw = db.get_setting(cls.COUNTRY_SETTING_KEY) if db else None
        if raw:
            try:
                import json
                parsed = json.loads(raw)
                if isinstance(parsed, dict):
                    order = cls._coerce_order(
                        parsed.get(country) or parsed.get('default')
                    )
                    if order:
                        return order
            except (ValueError, TypeError) as e:
                logger.warning(f"bad cascade_by_country JSON: {e}")
        # Fallback: hardcoded country defaults.
        return cls.COUNTRY_CASCADE_DEFAULTS.get(country)

    @classmethod
    def get_cascade_order(
        cls,
        db,
        user=None,
        country: Optional[str] = None,
        asn: Optional[str] = None,
    ) -> tuple:
        """Effective rotation: enabled-only protocols filtered by tier
        and (if known) ASN/country tuned.

        Lookup order for ordering (finest wins):
        1. ASN override (``cascade_by_asn``) — per-operator tuning,
           the practical sub-RU slice (MTS / MegaFon / Beeline / etc.).
        2. Country override (``cascade_by_country`` or hardcoded
           ``COUNTRY_CASCADE_DEFAULTS``) — broad region default.
        3. Global enabled cascade (``cascade_protocol_order``).

        Explicit ``asn=`` / ``country=`` args win over the user's
        stored ``last_asn`` / ``last_country`` — used by /sub which
        knows the request-time IP. Tier filtering applies after
        ordering. Passing ``user=None`` returns the unrestricted set,
        used by the dashboard preview.
        """
        cfg = cls.get_cascade_config(db)
        enabled_set = {c['name'] for c in cfg if c.get('enabled')}
        enabled_default_order = [c['name'] for c in cfg if c.get('enabled')]

        # ASN takes precedence over country (it's the finer slice).
        effective_asn = asn or getattr(user, 'last_asn', None)
        effective_country = country or getattr(user, 'last_country', None)
        chosen_order = (
            cls.get_asn_cascade(db, effective_asn)
            or (cls.get_country_cascade(db, effective_country) if effective_country else None)
        )

        if chosen_order:
            # Project chosen order onto the enabled set, then append
            # any enabled protocol the override didn't mention (so a
            # newly-shipped protocol still gets cascaded even before
            # the operator updates the override).
            seen = set()
            ordered = []
            for n in chosen_order:
                if n in enabled_set and n not in seen:
                    ordered.append(n); seen.add(n)
            for n in enabled_default_order:
                if n not in seen:
                    ordered.append(n); seen.add(n)
        else:
            ordered = enabled_default_order

        if user is None:
            return tuple(ordered)
        status = getattr(user, 'status', '') or ''
        if status in cls.PAID_USER_STATUSES:
            return tuple(ordered)
        return tuple(n for n in ordered if cls.PROTOCOL_TIER.get(n) == 'free')

    def can_handle(self, callback_data: str) -> bool:
        if callback_data in self.CALLBACK_DATA_EXACT:
            return True
        return callback_data.startswith(self.CALLBACK_PATTERN_TARGET)

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        import time
        data = kwargs.get('data', '')
        # Key the lookup on the PRESSER, not the chat: inside a forum
        # group chat_id is the group id and get_user(chat_id) is None,
        # which used to bounce the report with "get a key first".
        user = self.db.get_user(user_id) or self.db.get_user(chat_id)
        lang = (getattr(user, 'lang', None) or 'ru') if user else 'ru'
        # Reply into the same forum topic the button lives in — without
        # this the answer lands in the group's General topic where the
        # presser never sees it ("the button does nothing").
        cb_msg = (update.get('callback_query') or {}).get('message') or {}
        thread_id = cb_msg.get('message_thread_id')

        # 'mykey_yes' from legacy messages — user is *confirming* the
        # key works. Just acknowledge, don't log a failure.
        if data == 'mykey_yes':
            text = ("👍 Отлично, рад что работает." if lang == 'ru'
                    else "👍 Glad it works.")
            self.bot.send_message(chat_id=chat_id, text=text,
                                  message_thread_id=thread_id)
            return

        # report_target:<category> — user selected a specific problem
        # from the dropdown. Log it with the chosen category.
        if data.startswith(self.CALLBACK_PATTERN_TARGET):
            self._handle_target_selection(
                data, chat_id, user_id, user, lang, thread_id)
            return

        # report_failure, mykey_trigger, mykey_no — show the dropdown.
        if not user or not user.uuid:
            text = ("⚠️ Сначала получите ключ — /start" if lang == 'ru'
                    else "⚠️ Get a key first — /start")
            self.bot.send_message(chat_id=chat_id, text=text,
                                  message_thread_id=thread_id)
            return

        # Show category selection dropdown
        self._show_category_picker(chat_id, lang, thread_id)

    def _show_category_picker(self, chat_id: str, lang: str,
                              thread_id=None) -> None:
        """Show inline keyboard with failure category options."""
        prompt = (
            "🆘 <b>Что именно не работает?</b>\n\n"
            "Выбери вариант — это поможет нам быстрее найти проблему."
            if lang == 'ru' else
            "🆘 <b>What exactly isn't working?</b>\n\n"
            "Choose an option — this helps us diagnose faster."
        )
        buttons = []
        for key, labels in self.FAILURE_CATEGORIES.items():
            label = labels.get(lang, labels['ru'])
            buttons.append({'text': label, 'callback_data': f'report_target:{key}'})
        # Two columns for cleaner layout
        keyboard = {'inline_keyboard': [buttons[i:i+2] for i in range(0, len(buttons), 2)]}
        self.bot.send_message(
            chat_id=chat_id, text=prompt, parse_mode='HTML', reply_markup=keyboard,
            message_thread_id=thread_id,
        )

    def _handle_target_selection(self, data: str, chat_id: str, user_id: str,
                                 user, lang: str, thread_id=None) -> None:
        """Process the user's category selection and log the failure report."""
        try:
            category = data.split(':', 1)[1]
        except IndexError:
            return
        if category not in self.FAILURE_CATEGORIES:
            logger.warning(f"Invalid failure category: {category}")
            return

        # Rate-limit check (same as before)
        import time
        now = time.time()
        cutoff = now - self.REPORT_RATE_LIMIT_SECONDS * 2
        type(self)._last_report_times = {
            k: v for k, v in type(self)._last_report_times.items()
            if v > cutoff
        }
        # Rate-limit per presser, not per chat — in a group chat_id is
        # shared by everyone.
        rl_key = user_id or chat_id
        last_t = type(self)._last_report_times.get(rl_key, 0)
        if now - last_t < self.REPORT_RATE_LIMIT_SECONDS:
            remaining = int(self.REPORT_RATE_LIMIT_SECONDS - (now - last_t))
            text = (
                f"⏳ Мы уже получили твой сигнал. Подожди {remaining // 60} мин "
                "перед следующей жалобой — за это время мы успеем посмотреть."
                if lang == 'ru' else
                f"⏳ Got your signal already. Wait {remaining // 60} min before "
                "the next ping — we'll have a look in the meantime."
            )
            self.bot.send_message(chat_id=chat_id, text=text,
                                  message_thread_id=thread_id)
            return
        type(self)._last_report_times[rl_key] = now

        # Snapshot context for the report row
        country = getattr(user, 'last_country', None)
        asn = getattr(user, 'last_asn', None)
        city = getattr(user, 'last_city', None)
        lat = getattr(user, 'last_lat', None)
        lon = getattr(user, 'last_lon', None)
        last_traffic_ts = getattr(user, 'last_traffic_update', None)

        try:
            with self.db._connect() as conn:
                cur = conn.execute(
                    "INSERT INTO user_failure_reports "
                    "(chat_id, country, asn, city, lat, lon, "
                    " last_sub_fetch_ts, last_traffic_ts, target_domain) "
                    "VALUES (?, ?, ?, ?, ?, ?, NULL, ?, ?)",
                    # the report row references the REPORTING USER — in a
                    # group press chat_id would be the group id
                    (user_id or chat_id, country, asn, city, lat, lon,
                     last_traffic_ts, category),
                )
                report_id = cur.lastrowid
                conn.commit()
        except Exception as e:
            logger.error(f"report_failure: insert failed for {user_id or chat_id}: {e}")
            report_id = None

        # User-facing confirmation
        if lang == 'ru':
            self_help = (
                "🆘 <b>Сигнал получили.</b>\n\n"
                "Пока мы смотрим — попробуй:\n"
                "1. В Hiddify обнови подписку (⟳ или потяни вниз)\n"
                "2. Переключи режим: ⚙️ → Configuration → ECH = ON\n"
                "3. Если есть — попробуй мобильный интернет вместо Wi-Fi (другой оператор = другая блокировка)\n\n"
                "Если не помогло — напиши /support, заведём диалог."
            )
        else:
            self_help = (
                "🆘 <b>Got your signal.</b>\n\n"
                "While we look, try:\n"
                "1. In Hiddify pull-to-refresh the subscription\n"
                "2. Toggle: ⚙️ → Configuration → ECH = ON\n"
                "3. Try mobile data instead of Wi-Fi (different ISP = different blocking)\n\n"
                "Didn't help? Send /support to open a ticket."
            )
        self.bot.send_message(chat_id=chat_id, text=self_help, parse_mode='HTML',
                              message_thread_id=thread_id)

        # Operator-side ping with category
        try:
            forum_group = getattr(self.config, 'FORUM_GROUP_ID', 0)
            topic = getattr(self.config, 'TOPIC_SUPPORT', 0)
            if forum_group and topic:
                uname = getattr(user, 'username', None) or user_id or chat_id
                ctx = (country or 'unk') + (' / ' + asn if asn else '')
                last_seen = last_traffic_ts or 'нет'
                cat_label = self.FAILURE_CATEGORIES[category].get('ru', category)
                msg = (
                    f"🆘 <b>Failure report #{report_id or '?'}</b>\n"
                    f"User: <code>@{uname}</code> ({user_id or chat_id})\n"
                    f"Problem: {cat_label}\n"
                    f"Network: {ctx}\n"
                    f"Last traffic: {last_seen}"
                )
                self.bot.send_message(
                    chat_id=forum_group,
                    message_thread_id=topic,
                    text=msg,
                    parse_mode='HTML',
                )
        except Exception as e:
            logger.warning(f"report_failure: support ping failed: {e}")

        logger.info(
            f"report_failure: user={user_id or chat_id} country={country} asn={asn} "
            f"category={category} report_id={report_id}"
        )


class TryAltProtocolHandler(BaseCallbackHandler):
    """Sends an alt-protocol VPN link when the user reports the previous
    one didn't connect. Cascade order: Reality → Hy2 → WS → support.

    Callback data shape: ``try_alt:<protocol>`` where protocol is
    ``hy2`` or ``ws``. Look-up is stateless: we regenerate the link
    from the user's stored UUID/email each time, so a stale callback
    button from yesterday still produces a valid current key.
    """

    CALLBACK_PATTERN = 'try_alt:'

    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        data = kwargs.get('data', '')
        try:
            protocol = data.split(':', 1)[1]
        except IndexError:
            return
        if protocol not in {'hy2', 'ws', 'stls'}:
            return

        user = self.db.get_user(chat_id)
        if not user or not user.uuid or not user.email:
            self.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Сначала получите основной ключ — /start"
            )
            return

        vpn = VPNService(self.config)
        if protocol == 'hy2':
            link = vpn.generate_hy2_link(user.uuid, user.email)
            # Next step preference: WS if configured, else ShadowTLS, else support
            if vpn.generate_vless_ws_link(user.uuid, user.email):
                next_protocol = 'ws'
            elif vpn.generate_stls_link(user.uuid, user.email):
                next_protocol = 'stls'
            else:
                next_protocol = None
        elif protocol == 'ws':
            link = vpn.generate_vless_ws_link(user.uuid, user.email)
            next_protocol = 'stls' if vpn.generate_stls_link(user.uuid, user.email) else None
        else:  # stls — last rung of the cascade
            link = vpn.generate_stls_link(user.uuid, user.email)
            next_protocol = None

        if not link:
            self.bot.send_message(
                chat_id=chat_id,
                text=("⚠️ Этот резервный способ временно недоступен. "
                      "Напишите в /support — поможем подключиться.")
                if (user.lang or 'ru') == 'ru' else
                ("⚠️ This backup is temporarily unavailable. "
                 "Please contact /support.")
            )
            return

        notifier = NotificationService(self.bot, self.db, self.config)
        notifier.notify_alt_protocol(
            chat_id, link, protocol,
            next_protocol=next_protocol, lang=user.lang or 'ru',
        )
        logger.info(f"Sent alt-protocol {protocol} link to user {chat_id}")


class SupportRequestHandler(BaseCallbackHandler):
    """Handle support request callback."""

    CALLBACK_DATA = 'support'
    CALLBACK_PATTERN = 'support:'
    
    def can_handle(self, callback_data: str) -> bool:
        if callback_data == self.CALLBACK_DATA:
            return True
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process support request."""
        data = kwargs.get('data', '')
        
        if data.startswith('support:'):
            parts = data.split(':')
            target_id = parts[1] if len(parts) > 1 and parts[1] else chat_id
        else:
            target_id = chat_id
        
        # IDOR protection: users can only access their own support
        if str(user_id) != str(target_id):
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ You can only access your own support chat."
            )
            logger.warning(f"IDOR attempt in support: user {user_id} tried to access support for {target_id}")
            return
        
        self._open_support_ticket(target_id)
    
    def _open_support_ticket(self, chat_id: str) -> None:
        """Open support ticket for user."""
        sm = StateMachine(self.db)
        success = sm.transition(chat_id, UserState.SUPPORT_TOPIC)
        
        if not success:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Unable to create support ticket."
            )
            return
        
        self.bot.send_message(
            chat_id=chat_id,
            text="🆘 Please describe your issue. I'll forward it to support."
        )
        
        logger.info(f"User {chat_id} opened support ticket")


class EmailPromptHandler(BaseCallbackHandler):
    """Handle email prompt callback - user clicked 'Add email' button."""

    CALLBACK_DATA = 'add_email_prompt'

    def can_handle(self, callback_data: str) -> bool:
        return callback_data == self.CALLBACK_DATA

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Prompt user to enter their email.

        Arms the pending-email flag: the user's NEXT plain-text message
        is consumed as the address (see MessageHandler.PENDING_EMAIL).
        Asking for a /setemail command didn't work — users just replied
        with the address and it was lost in the "I don't understand"
        fallback.
        """
        import time as _time
        from bot.handlers.messages import PENDING_EMAIL

        user = self.db.get_user(chat_id)
        lang = user.lang if user else 'ru'
        PENDING_EMAIL[chat_id] = _time.time()

        if lang == 'en':
            text = (
                "📧 <b>Add your email</b>\n\n"
                "We'll use it to send you a backup key if your VPN gets blocked.\n\n"
                "👉 Just <b>reply to this message</b> with your email "
                "(e.g. john@gmail.com) — or send /setemail your@email.com"
            )
        else:
            text = (
                "📧 <b>Укажи свой email</b>\n\n"
                "Используем его для отправки резервного ключа если VPN заблокируют.\n\n"
                "👉 Просто <b>отправь email ответным сообщением</b> "
                "(например ivan@gmail.com) — или командой /setemail твой@email.com"
            )

        self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
        logger.info(f"User {chat_id} requested email prompt (pending armed)")


class EmailKeyHandler(BaseCallbackHandler):
    """Handle 'email_key' — send the user's subscription URL to their
    contact_email as a backup delivery channel.

    The SMTP handshake takes seconds, so the actual send runs on a
    worker thread — the polling loop must never wait on network I/O
    (same rule as the /ai agent turns).
    """

    CALLBACK_DATA = 'email_key'
    RATE_LIMIT_SECONDS = 600  # one letter per user per 10 min
    _last_sent_times: dict = {}

    def can_handle(self, callback_data: str) -> bool:
        return callback_data == self.CALLBACK_DATA

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        import time
        cb_msg = (update.get('callback_query') or {}).get('message') or {}
        thread_id = cb_msg.get('message_thread_id')
        user = self.db.get_user(user_id) or self.db.get_user(chat_id)
        lang = (getattr(user, 'lang', None) or 'ru') if user else 'ru'

        def say(text: str) -> None:
            self.bot.send_message(chat_id=chat_id, text=text,
                                  parse_mode='HTML', message_thread_id=thread_id)

        if not user or not user.uuid:
            say("⚠️ Сначала получите ключ — /start" if lang == 'ru'
                else "⚠️ Get a key first — /start")
            return

        from bot.services.email_service import EmailService
        mailer = EmailService(self.config)
        if not mailer.is_configured():
            say("📧 Отправка на почту пока не подключена — ключ доступен здесь, в чате."
                if lang == 'ru' else
                "📧 Email delivery isn't set up yet — your key is available here in the chat.")
            return

        to_addr = (getattr(user, 'contact_email', None) or '').strip()
        if not to_addr:
            say("📧 Сначала укажи почту:\n<code>/setemail твой@email.com</code>"
                if lang == 'ru' else
                "📧 Set your email first:\n<code>/setemail your@email.com</code>")
            return

        now = time.time()
        last_t = type(self)._last_sent_times.get(user_id or chat_id, 0)
        if now - last_t < self.RATE_LIMIT_SECONDS:
            say("⏳ Письмо уже отправлено. Проверь почту (и «Спам»), повторить можно через 10 минут."
                if lang == 'ru' else
                "⏳ Already sent. Check your inbox (and Spam); retry in 10 minutes.")
            return
        type(self)._last_sent_times[user_id or chat_id] = now

        from bot.services.subscription import SubscriptionService
        sub_url = SubscriptionService(self.config).build_subscription_url(user)
        if not sub_url:
            say("⚠️ Не удалось собрать ссылку — напишите в поддержку."
                if lang == 'ru' else
                "⚠️ Couldn't build the URL — contact support.")
            return

        def _worker() -> None:
            ok = mailer.send_key(to_addr, sub_url, lang,
                                 platform=getattr(user, 'platform', None))
            if ok:
                say(f"✉️ Ключ отправлен на <code>{html.escape(to_addr)}</code>. "
                    "Если письма нет — загляни в «Спам»."
                    if lang == 'ru' else
                    f"✉️ Key sent to <code>{html.escape(to_addr)}</code>. "
                    "Check Spam if it doesn't show up.")
            else:
                # let the user retry right away — the send didn't happen
                type(self)._last_sent_times.pop(user_id or chat_id, None)
                say("⚠️ Не получилось отправить письмо, попробуй позже."
                    if lang == 'ru' else
                    "⚠️ Couldn't send the letter, try again later.")

        threading.Thread(target=_worker, daemon=True,
                         name=f"email-key-{user_id or chat_id}").start()
        logger.info(f"email_key: queued send for user {user_id or chat_id}")


class StatsRequestHandler(BaseCallbackHandler):
    """Handle stats request callback."""
    
    CALLBACK_DATA = 'stats'
    CALLBACK_PATTERN = 'stats:'
    
    def can_handle(self, callback_data: str) -> bool:
        if callback_data == self.CALLBACK_DATA:
            return True
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process stats request."""
        data = kwargs.get('data', '')
        
        if data.startswith('stats:'):
            parts = data.split(':')
            target_id = parts[1] if len(parts) > 1 and parts[1] else chat_id
        else:
            target_id = chat_id
        
        # IDOR protection: users can only view their own stats; admins can view any
        if str(user_id) != str(target_id) and not self._is_admin(user_id):
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ You can only view your own statistics."
            )
            logger.warning(f"IDOR attempt in stats: user {user_id} tried to view stats of {target_id}")
            return
        
        self._send_stats(target_id)
    
    def _send_stats(self, chat_id: str) -> None:
        """Send stats to user."""
        from bot.utils.helpers import format_bytes

        if self._is_admin(chat_id):
            notifier = NotificationService(self.bot, self.db, self.config)
            notifier.notify_stats(chat_id, self.db.get_stats(), is_admin=True)
            return

        user = self.db.get_user(chat_id)
        if not user or not user.email:
            self.bot.send_message(
                chat_id=chat_id,
                text="❌ Your user profile is incomplete or not found."
            )
            return

        try:
            # Through XUIService, not a raw XUIDatabase: on the entry
            # node there is no local x-ui.db (API-only mode), and a raw
            # connect used to fabricate an empty stub file there.
            xui = None
            if getattr(self.bot, 'services', None):
                xui = self.bot.services.get('xui')
            if xui is None:
                from bot.services.xui_service import XUIService
                xui = XUIService(self.config)
            traffic = xui.get_client_traffic_sync(user.email)

            if traffic:
                used_traffic_bytes = (
                    traffic.get('upload', 0) + traffic.get('download', 0)
                )
                # Real per-client quota from the panel; only fall back
                # to the demo default when the panel has none.
                total_limit_bytes = traffic.get('total') or (
                    self.config.DEMO_TRAFFIC_GB * 1024 * 1024 * 1024
                )

                percentage = 0
                if total_limit_bytes > 0:
                    percentage = (used_traffic_bytes / total_limit_bytes) * 100

                text = (
                    f"📊 <b>Your Traffic Stats</b>\n\n"
                    f"⬆️ Upload: {format_bytes(traffic.get('upload', 0))}\n"
                    f"⬇️ Download: {format_bytes(traffic.get('download', 0))}\n"
                    f"📈 Total Used: {format_bytes(used_traffic_bytes)} / {format_bytes(total_limit_bytes)}\n"
                    f"({percentage:.2f}%)"
                )
            else:
                text = "Could not retrieve traffic statistics for your account."

            self.bot.send_message(chat_id=chat_id, text=text, parse_mode='HTML')
        except Exception as e:
            logger.error(f"Error getting user stats for {chat_id}: {e}")
            text = "An error occurred while fetching your stats."
            self.bot.send_message(chat_id=chat_id, text=text)


class FullVersionHandler(BaseCallbackHandler):
    """Legacy 💎 Полная версия button — now routes to the same plan
    menu as /buy. Kept so old menu messages with this callback don't
    silently break for users who scroll up.
    """

    CALLBACK_DATA = 'full'

    def can_handle(self, callback_data: str) -> bool:
        return callback_data == self.CALLBACK_DATA

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        from bot.handlers.payments import PaymentHandler
        PaymentHandler(self.bot, self.db, self.config)._show_plan_menu(str(chat_id))


class LanguageSetHandler(BaseCallbackHandler):
    """Handle language selection callback."""
    
    CALLBACK_PATTERN = 'set_lang:'
    
    def can_handle(self, callback_data: str) -> bool:
        return callback_data.startswith(self.CALLBACK_PATTERN)
    
    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Process language selection."""
        data = kwargs.get('data', '')
        parts = data.split(':')
        
        if len(parts) < 2:
            return
        
        lang = parts[1]
        self._change_language(update, chat_id, lang)
    
    def _change_language(self, update: dict, chat_id: str, lang: str) -> None:
        """Change user language."""
        user = self.db.get_user(chat_id)
        if not user:
            user = self._get_or_create_user(update)
        
        user.lang = lang
        self.db.save_user(user)
        
        self._send_language_confirmation(chat_id, lang)
        
        notifier = NotificationService(self.bot, self.db, self.config)
        notifier.notify_welcome(chat_id, lang)
        
        logger.info(f"User {chat_id} changed language to {lang}")
    
    def _send_language_confirmation(self, chat_id: str, lang: str) -> None:
        """Send language change confirmation."""
        if lang == 'en':
            text = "✅ Language changed to English."
        else:
            text = "✅ Язык изменен на русский."

        self.bot.send_message(chat_id=chat_id, text=text)


class EmailPromptHandler(BaseCallbackHandler):
    """Handle email prompt callback - shows instructions for setting email.

    NOTE: this is the ACTIVE copy — there is an older dead duplicate of
    this class higher up in the file (shadowed at import time). Edit this
    one.
    """

    CALLBACK_DATA_EXACT = ['add_email_prompt']

    def can_handle(self, callback_data: str) -> bool:
        return callback_data in self.CALLBACK_DATA_EXACT

    def handle(self, update: dict, chat_id: str, user_id: str, **kwargs) -> None:
        """Prompt user to enter their email.

        Arms the pending-email flag: the user's NEXT plain-text message
        is consumed as the address (see MessageHandler.PENDING_EMAIL).
        Asking for a /setemail command didn't work — users just replied
        with the address and it was lost in the "I don't understand"
        fallback (ziriki, 2026-07-25).
        """
        import time as _time
        from bot.handlers.messages import PENDING_EMAIL

        user = self.db.get_user(chat_id)
        lang = user.lang if user else 'ru'
        PENDING_EMAIL[chat_id] = _time.time()

        if lang == 'en':
            text = (
                "📧 <b>Add your email</b>\n\n"
                "Why add email?\n"
                "• If your VPN gets blocked, we'll email you a backup key\n"
                "• You can contact support via email if Telegram is blocked\n\n"
                "👉 Just <b>reply to this message</b> with your email "
                "(e.g. john@gmail.com) — or send /setemail your@email.com"
            )
        else:
            text = (
                "📧 <b>Укажи свой email</b>\n\n"
                "Зачем нужен email?\n"
                "• Если VPN заблокируют, мы пришлём резервный ключ на почту\n"
                "• Сможешь связаться с поддержкой через email если Telegram недоступен\n\n"
                "👉 Просто <b>отправь email ответным сообщением</b> "
                "(например ivan@gmail.com) — или командой /setemail твой@email.com"
            )

        self.bot.send_message(
            chat_id=chat_id,
            text=text,
            parse_mode='HTML'
        )
        logger.info(f"User {chat_id} requested email prompt (pending armed)")
