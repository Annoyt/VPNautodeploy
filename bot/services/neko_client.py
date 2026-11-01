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
import re
import threading
import time
from typing import Optional
from urllib.parse import quote

logger = logging.getLogger(__name__)

TESTERS_KEY = 'client_testers'
PACKAGE = 'com.nekovpn.client'
SCHEME = 'nekovpn'
RELEASES_URL = 'https://github.com/Annoyt/vpnautodeploy_client/releases'
RELEASES_API = ('https://api.github.com/repos/Annoyt/vpnautodeploy_client/'
                'releases?per_page=20')
KEY_STATUSES = ('demo', 'paid', 'support_topic')
# The APK builds a release carries; the first is what nearly every phone
# takes.
ABIS = ('arm64-v8a', 'armeabi-v7a', 'x86_64')
KARING_URL = 'https://karing.app/'
FLCLASH_URL = 'https://chen08209.github.io/FlClash/#download'


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


def intent_link(url: str, fallback: str = RELEASES_URL) -> str:
    """The same link as an Android intent: opens NekoVPN when it is
    installed, ``fallback`` (the download page) otherwise."""
    return (
        f'intent://install-config?url={quote(url, safe="")}'
        f'#Intent;scheme={SCHEME};package={PACKAGE};'
        f'S.browser_fallback_url={quote(fallback, safe="")};end'
    )


def download_page_url(config, user=None) -> str:
    """``<WEBAPP_URL>/app`` (or ``/app/<token>``, which also adds the
    user's subscription): the download page. The GitHub release page when
    the bot does not know its own address."""
    base = (getattr(config, 'WEBAPP_URL', '') or '').rstrip('/')
    if not base:
        return RELEASES_URL
    if user is not None and getattr(user, 'uuid', None):
        from bot.services.subscription import SubscriptionService
        return f'{base}/app/{SubscriptionService(config).derive_token(user.uuid)}'
    return f'{base}/app'


# ---------- The newest release, for the download page ----------

_NEKO_TAG = re.compile(r'^v\d+\.\d+\.\d+-neko\.\d+$')


def pick_release(releases) -> Optional[dict]:
    """The newest published NekoVPN release with an APK for the common
    ABI: ``{'tag', 'page', 'apks': {abi: (url, size)}}``. GitHub lists
    releases newest first; drafts and other tags are skipped, pre-releases
    are taken (every NekoVPN build is one so far)."""
    for release in releases or []:
        if not isinstance(release, dict) or release.get('draft'):
            continue
        tag = release.get('tag_name') or ''
        if not _NEKO_TAG.match(tag):
            continue
        apks = {}
        for asset in release.get('assets') or []:
            name = asset.get('name') or ''
            for abi in ABIS:
                if name.endswith(f'-android-{abi}.apk') and asset.get('browser_download_url'):
                    apks[abi] = (asset['browser_download_url'], int(asset.get('size') or 0))
        if ABIS[0] in apks:
            return {'tag': tag, 'page': release.get('html_url') or RELEASES_URL,
                    'apks': apks}
    return None


class ReleaseCache:
    """The newest release, asked of GitHub at most every ``TTL`` seconds
    (its API allows 60 requests an hour per address). A failed request is
    remembered for ``FAILED_TTL``: a dead GitHub costs one timeout a
    minute, not one per page view."""

    TTL = 600
    FAILED_TTL = 60
    _lock = threading.Lock()
    _value: Optional[dict] = None
    _until = 0.0

    @classmethod
    def get(cls, fetch=None, now=None) -> Optional[dict]:
        now = time.monotonic() if now is None else now
        with cls._lock:
            if now < cls._until:
                return cls._value
        try:
            value = pick_release((fetch or _fetch_releases)())
        except Exception as e:
            logger.warning(f"neko_client: releases unavailable: {e}")
            value = None
        with cls._lock:
            cls._value = value
            cls._until = now + (cls.TTL if value else cls.FAILED_TTL)
        return value

    @classmethod
    def clear(cls) -> None:
        with cls._lock:
            cls._value, cls._until = None, 0.0


def _fetch_releases():
    import requests
    resp = requests.get(RELEASES_API, timeout=10,
                        headers={'Accept': 'application/vnd.github+json'})
    resp.raise_for_status()
    return resp.json()


def apk_redirect(release: Optional[dict], abi: Optional[str]) -> str:
    """Where ``/apk`` sends the browser: that ABI's APK of the newest
    release (the common one by default), the release page without one."""
    if not release:
        return RELEASES_URL
    apks = release['apks']
    return (apks.get(abi) or apks[ABIS[0]])[0]


def platform_of(user_agent: Optional[str]) -> str:
    """'android', 'ios' or 'desktop', from a browser's User-Agent."""
    ua = user_agent or ''
    if 'Android' in ua:
        return 'android'
    if any(device in ua for device in ('iPhone', 'iPad', 'iPod')):
        return 'ios'
    return 'desktop'


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


def render_open_page(url: str, lang: Optional[str],
                     download: str = RELEASES_URL) -> str:
    """The ``/open/<token>`` page for this profile URL; without the app the
    button and the link lead to ``download`` (our download page)."""
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
<a class="btn" href="{e(intent_link(url, download))}">{e(t['button'])}</a>
<p class="small">{e(t['no_app'])} <a href="{e(download)}">{e(t['download'])}</a></p>
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


# ---------- The download page: /app and /app/<token> ----------

_DOWNLOAD_TEXTS = {
    'ru': {
        'title': 'NekoVPN для Android',
        'download': 'Скачать NekoVPN',
        'version': 'Версия {version} · {size} МБ',
        'steps': [
            'Откройте скачанный файл. Android спросит, можно ли браузеру '
            'устанавливать приложения, — разрешите.',
            'Установите NekoVPN.',
        ],
        'step_add': 'Вернитесь на эту страницу и нажмите «Добавить подписку».',
        'add': 'Добавить подписку',
        'no_token': 'Подписку добавьте из бота {bot}: /sub → кнопка '
                    '«📲 Открыть в NekoVPN».',
        'demo': 'Демо в NekoVPN — {app} ГБ в месяц, в других приложениях — '
                '{other} ГБ.',
        'other_abis': 'Не устанавливается? Другие сборки:',
        'ios': 'NekoVPN пока только для Android. На iPhone подойдёт Karing:',
        'desktop': 'NekoVPN пока только для Android — откройте эту страницу '
                   'на телефоне. На компьютере подойдёт FlClash:',
        'subscription': 'Подписка — в боте {bot}: /sub.',
        'unavailable': 'Не удалось получить сборку. Скачайте со страницы '
                       'релизов:',
    },
    'en': {
        'title': 'NekoVPN for Android',
        'download': 'Download NekoVPN',
        'version': 'Version {version} · {size} MB',
        'steps': [
            'Open the downloaded file. Android asks whether the browser may '
            'install apps: allow it.',
            'Install NekoVPN.',
        ],
        'step_add': 'Come back to this page and tap “Add subscription”.',
        'add': 'Add subscription',
        'no_token': 'Add your subscription from the bot {bot}: /sub → '
                    '“📲 Open in NekoVPN”.',
        'demo': 'Demo in NekoVPN: {app} GB a month; {other} GB in other apps.',
        'other_abis': 'Does not install? Other builds:',
        'ios': 'NekoVPN is Android-only for now. On an iPhone, use Karing:',
        'desktop': 'NekoVPN is Android-only for now: open this page on your '
                   'phone. On a computer, use FlClash:',
        'subscription': 'Your subscription is in the bot {bot}: /sub.',
        'unavailable': 'Could not get the build. Download it from the release '
                       'page:',
    },
}

BOT_NAME = '@neko_vpnbot'


def render_download_page(*, platform: str, lang: Optional[str],
                         release: Optional[dict], apk_url: str,
                         demo_gb: Optional[tuple], profile_url: Optional[str] = None,
                         self_url: Optional[str] = None) -> str:
    """The download page. ``apk_url`` is our ``/apk`` (a stable link that
    follows new releases); with ``profile_url`` the page also adds the
    subscription, its intent falling back to ``self_url`` (this page).
    ``demo_gb`` (app, other) prints the demo allowances; None for a payer."""
    t = _DOWNLOAD_TEXTS['en' if lang == 'en' else 'ru']
    e = html.escape
    parts = ['<h1>🐱 NekoVPN</h1>']
    if platform == 'ios':
        parts += [f'<p>{e(t["ios"])} <a href="{e(KARING_URL)}">Karing</a></p>',
                  f'<p class="small">{e(t["subscription"].format(bot=BOT_NAME))}</p>']
    elif platform == 'desktop':
        parts += [f'<p>{e(t["desktop"])} <a href="{e(FLCLASH_URL)}">FlClash</a></p>',
                  f'<p class="small">{e(t["subscription"].format(bot=BOT_NAME))}</p>']
    elif not release:
        parts += [f'<p>{e(t["unavailable"])} <a href="{e(RELEASES_URL)}">GitHub</a></p>']
    else:
        url, size = release['apks'][ABIS[0]]
        version = release['tag'].lstrip('v')
        parts += [
            f'<p class="lead">{e(t["title"])}</p>',
            f'<a class="btn" href="{e(apk_url)}">{e(t["download"])}</a>',
            f'<p class="small">{e(t["version"].format(version=version, size=round(size / 1048576)))}</p>',
        ]
        steps = list(t['steps'])
        if profile_url:
            steps.append(t['step_add'])
        parts.append('<ol>' + ''.join(f'<li>{e(step)}</li>' for step in steps) + '</ol>')
        if profile_url:
            parts.append(f'<a class="btn" href="{e(intent_link(profile_url, self_url or RELEASES_URL))}">'
                         f'{e(t["add"])}</a>')
        else:
            parts.append(f'<p class="small">{e(t["no_token"].format(bot=BOT_NAME))}</p>')
        if demo_gb:
            app_gb, other_gb = demo_gb
            parts.append(f'<p class="small">{e(t["demo"].format(app=app_gb, other=other_gb))}</p>')
        others = [abi for abi in ABIS[1:] if abi in release['apks']]
        if others:
            links = ' · '.join(
                f'<a href="{e(apk_url)}?abi={e(abi)}">{e(abi)}</a>' for abi in others)
            parts.append(f'<p class="small">{e(t["other_abis"])} {links}</p>')
    body = '\n'.join(parts)
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
.lead{{font-size:18px}}
.btn{{display:block;padding:16px;border-radius:14px;background:#f57c1f;color:#fff;
font-size:18px;font-weight:600;text-decoration:none;margin:24px 0}}
.small{{font-size:15px;color:#555}}
ol{{text-align:left;font-size:15px;line-height:1.5;padding-left:22px}}
</style>
</head>
<body>
<main>
{body}
</main>
</body>
</html>
'''

