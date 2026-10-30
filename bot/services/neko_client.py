"""NekoVPN client (the FlClash fork, Annoyt/vpnautodeploy_client): adding the
subscription in one tap from the bot.

The app registers ``nekovpn://install-config?url=<profile URL>`` (Android
manifest: VIEW + BROWSABLE, host install-config) and answers it with one
«Добавить профиль?» dialog. Telegram buttons take only http(s) links, so
the bot points at ``/open/<token>`` on its own domain
(``WebAppServer.handle_open``): a page whose button is an Android intent
link. With the app installed it opens with the user's FlClash-format
subscription; without it, Chrome follows ``S.browser_fallback_url`` to the
release page. The page does not jump to the app on its own: an automatic
navigation to an app link needs a user gesture in Chrome, and Telegram's
own in-app browser may replace the page with an error, leaving no button.

Who sees the button: every key holder on Android (the owner opened it
to all on 2026-10-31, with the demo allowance that NekoVPN raises to
10 GB), and on any other platform but iOS the testers,
``app_settings.client_testers``, the list ``scripts/client_release_notify.py``
writes.
"""

import html
import json
import logging
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

TESTERS_KEY = 'client_testers'
PACKAGE = 'com.nekovpn.client'
SCHEME = 'nekovpn'
RELEASES_URL = 'https://github.com/Annoyt/vpnautodeploy_client/releases'
KEY_STATUSES = ('demo', 'paid', 'support_topic')


def load_testers(db) -> set:
    """chat_ids of the client testers; an unreadable list is empty."""
    try:
        raw = db.get_setting(TESTERS_KEY)
        ids = json.loads(raw) if raw else []
    except Exception as e:
        logger.warning(f"neko_client: {TESTERS_KEY} unreadable: {e}")
        return set()
    if not isinstance(ids, list):
        return set()
    return {str(i).strip() for i in ids if str(i).strip()}


def is_tester(db, user) -> bool:
    return bool(user) and str(getattr(user, 'chat_id', '') or '') in load_testers(db)


def profile_url(config, user) -> Optional[str]:
    """The subscription in the format the app reads (mihomo profile)."""
    from bot.services.subscription import SubscriptionService
    url = SubscriptionService(config).build_subscription_url(user)
    return f'{url}?format=clash' if url else None


def open_page_url(config, user) -> Optional[str]:
    """``<WEBAPP_URL>/open/<token>``: the page the bot's button opens."""
    from bot.services.subscription import SubscriptionService
    base = (getattr(config, 'WEBAPP_URL', '') or '').rstrip('/')
    if not base or not getattr(user, 'uuid', None):
        return None
    token = SubscriptionService(config).derive_token(user.uuid)
    return f'{base}/open/{token}'


def install_link(url: str) -> str:
    """``nekovpn://install-config?url=…``, the link the app answers."""
    return f'{SCHEME}://install-config?url={quote(url, safe="")}'


def intent_link(url: str) -> str:
    """The same link as an Android intent: opens NekoVPN when it is
    installed, the release page otherwise."""
    return (
        f'intent://install-config?url={quote(url, safe="")}'
        f'#Intent;scheme={SCHEME};package={PACKAGE};'
        f'S.browser_fallback_url={quote(RELEASES_URL, safe="")};end'
    )


def open_button_row(db, config, user) -> Optional[list]:
    """The «📲 Открыть в NekoVPN» row: a key holder on Android, or a tester
    with a key on any platform but iOS (NekoVPN has no iOS build). Never
    raises."""
    try:
        if not user or not getattr(user, 'uuid', None):
            return None
        if (getattr(user, 'status', '') or '') not in KEY_STATUSES:
            return None
        platform = getattr(user, 'platform', '') or ''
        if platform == 'ios':
            return None
        if platform != 'android' and not is_tester(db, user):
            return None
        url = open_page_url(config, user)
    except Exception as e:
        logger.warning(f"neko_client: open button skipped: {e}")
        return None
    if not url:
        return None
    label = '📲 Open in NekoVPN' if getattr(user, 'lang', None) == 'en' else '📲 Открыть в NekoVPN'
    return [{'text': label, 'url': url}]


_TEXTS = {
    'ru': {
        'lead': 'Нажмите кнопку: NekoVPN откроется и предложит добавить вашу подписку.',
        'button': 'Открыть в NekoVPN',
        'no_app': 'Нет приложения?',
        'download': 'Скачать NekoVPN для Android',
        'manual': 'Не открывается? Скопируйте ссылку и добавьте её в NekoVPN: «Профили» → + → «URL».',
        'copy': 'Скопировать',
        'copied': 'Скопировано',
    },
    'en': {
        'lead': 'Tap the button: NekoVPN opens and offers to add your subscription.',
        'button': 'Open in NekoVPN',
        'no_app': 'No app yet?',
        'download': 'Download NekoVPN for Android',
        'manual': 'Nothing happens? Copy the link and add it in NekoVPN: Profiles → + → URL.',
        'copy': 'Copy',
        'copied': 'Copied',
    },
}

# The page's only script: the copy button. Kept in one constant so the
# Content-Security-Policy can name it by hash instead of allowing inline code.
OPEN_PAGE_SCRIPT = (
    "document.getElementById('copy').onclick=function(){"
    "var i=document.getElementById('url');i.select();"
    "var b=this;var done=function(){b.textContent=b.dataset.done;};"
    "if(navigator.clipboard){navigator.clipboard.writeText(i.value).then(done);}"
    "else{document.execCommand('copy');done();}};"
)


def render_open_page(url: str, lang: Optional[str]) -> str:
    """The ``/open/<token>`` page for this profile URL."""
    t = _TEXTS['en' if lang == 'en' else 'ru']
    e = html.escape
    return f'''<!doctype html>
<html lang="{'en' if lang == 'en' else 'ru'}">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex">
<title>NekoVPN</title>
<style>
body{{font-family:system-ui,sans-serif;margin:0;background:#fafafa;color:#222}}
main{{max-width:420px;margin:0 auto;padding:32px 20px;text-align:center}}
h1{{font-size:28px;margin:8px 0 16px}}
.btn{{display:block;padding:16px;border-radius:14px;background:#f57c1f;color:#fff;
font-size:18px;font-weight:600;text-decoration:none;margin:24px 0}}
.small{{font-size:15px;color:#555}}
details{{margin-top:28px;text-align:left;font-size:14px;color:#444}}
input{{width:100%;box-sizing:border-box;padding:10px;margin:10px 0;font-size:13px}}
button{{padding:10px 14px;font-size:14px}}
</style>
</head>
<body>
<main>
<h1>🐱 NekoVPN</h1>
<p>{e(t['lead'])}</p>
<a class="btn" href="{e(intent_link(url))}">{e(t['button'])}</a>
<p class="small">{e(t['no_app'])} <a href="{e(RELEASES_URL)}">{e(t['download'])}</a></p>
<details>
<summary>{e(t['manual'])}</summary>
<input id="url" readonly value="{e(url)}">
<button id="copy" type="button" data-done="{e(t['copied'])}">{e(t['copy'])}</button>
</details>
</main>
<script>{OPEN_PAGE_SCRIPT}</script>
</body>
</html>
'''
