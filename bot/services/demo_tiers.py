"""The demo allowance by client app (owner's decision, 2026-10-30).

A demo key gets DEMO_TRAFFIC_GB (10) a month when the user runs our
NekoVPN app and DEMO_OTHER_TRAFFIC_GB (5) otherwise. The quota belongs to
the key, not to a device: the app on one phone gives the key 10 GB on
every device it is used on.

The app is recognised by the User-Agent of a profile fetch (/sub, not the
provider refreshes): ``NekoVPN/v<version> clash-verge Platform/<os>``.
Each such fetch is recorded in ``client_apps``. The first one raises a
demo key to the app allowance at once (in the background,
WebAppServer._schedule_neko_app). On the 1st the monthly reset gives the
app allowance to keys the app fetched within NEKO_APP_SEEN_DAYS and the
other one to the rest, so nobody is cut mid-month.

Only traffic through the VPN counts: the panel meters what passes the
exit. In NekoVPN and FlClash our profile sends Russian sites direct, so
they do not count; the texts below say so, because a phone's own counter
counts everything.
"""

import logging
import re
from datetime import datetime, timedelta, timezone
from typing import Optional

from bot.config.constants import BYTES_PER_GB

logger = logging.getLogger(__name__)

NEKO_APP = 'nekovpn'
# A key the app fetched within this window counts as an app key at the
# monthly reset. The app refreshes its profile every 6 hours.
NEKO_APP_SEEN_DAYS = 7
DEFAULT_APP_GB = 10      # DEMO_TRAFFIC_GB
DEFAULT_OTHER_GB = 5     # DEMO_OTHER_TRAFFIC_GB

_NEKO_UA = re.compile(r'NekoVPN/v?([0-9][0-9A-Za-z.+-]{0,39})(?:\s|$)')


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def neko_app_version(user_agent) -> Optional[str]:
    """The NekoVPN app's version from a User-Agent, None for any other
    client (FlClash says ``FlClash/v…``, our own self-test
    ``NekoVPN-selftest``)."""
    m = _NEKO_UA.match((user_agent or '').strip())
    return m.group(1) if m else None


def demo_traffic_gb(config, in_app: bool) -> int:
    """The monthly demo allowance in GB: with the NekoVPN app or without."""
    key, default = (('DEMO_TRAFFIC_GB', DEFAULT_APP_GB) if in_app
                    else ('DEMO_OTHER_TRAFFIC_GB', DEFAULT_OTHER_GB))
    value = getattr(config, key, default)
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return default
    try:
        return max(int(value), 0)
    except ValueError:
        return default


def mark_app_seen(db, chat_id, version: Optional[str], now: Optional[datetime] = None) -> None:
    """Record a profile fetch by the NekoVPN app."""
    seen = (now or _utcnow()).isoformat(timespec='seconds')
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO client_apps (chat_id, app, version, last_seen) "
            "VALUES (?, ?, ?, ?) ON CONFLICT(chat_id, app) DO UPDATE SET "
            "version = excluded.version, last_seen = excluded.last_seen",
            (str(chat_id), NEKO_APP, version, seen))
        conn.commit()


def app_users(conn, now: Optional[datetime] = None) -> set:
    """chat_ids the NekoVPN app fetched within NEKO_APP_SEEN_DAYS.
    ``conn`` is an open sqlite connection to bot.db."""
    cutoff = ((now or _utcnow()) - timedelta(days=NEKO_APP_SEEN_DAYS)
              ).isoformat(timespec='seconds')
    rows = conn.execute(
        "SELECT chat_id FROM client_apps WHERE app = ? AND last_seen >= ?",
        (NEKO_APP, cutoff)).fetchall()
    return {str(row[0]) for row in rows}


def grant_app_allowance(db, config, xui, chat_id) -> bool:
    """Raise a demo key to the app allowance: the panel first (in place,
    re-enabled: a key that ran out of the other allowance gets the rest
    at once), then users.quota_gb. Never lowers a bigger quota, never
    touches a key that is not demo. False when there was nothing to do
    or the panel refused; the app's next fetch tries again."""
    user = db.get_user(str(chat_id))   # fresh: never save a stale row
    if user is None or (user.status or '') != 'demo' or not user.email:
        return False
    gb = demo_traffic_gb(config, in_app=True)
    if float(user.quota_gb or 0) >= gb:
        return False
    if not xui.sync_client_settings_sync(
            user.email, {'totalGB': gb * BYTES_PER_GB, 'enable': True}):
        logger.warning(f"demo app allowance: panel update failed for {chat_id}")
        return False
    user.quota_gb = float(gb)
    db.save_user(user)
    return True


# ---------- What the user reads ----------

def _download_url() -> str:
    from bot.services.neko_client import RELEASES_URL
    return RELEASES_URL


def vpn_only_note(lang: str, ios: bool = False) -> str:
    """The counting rule, in the words of the 2026-10-30 broadcast. Bound
    to NekoVPN and FlClash: only there does our profile's routing apply
    (Hiddify drops it and sends Russian sites through the VPN, where
    they count). On iOS the plain rule: the iPhone card names Karing
    only, and Karing's routing is unverified."""
    if lang == 'en':
        if ios:
            return "Only traffic through the VPN counts."
        return ("In NekoVPN and FlClash, ONLY traffic outside Russia counts: "
                "Russian sites open directly and do not count.")
    if ios:
        return "Учитывается только трафик через VPN."
    return ("В NekoVPN и FlClash учитывается ТОЛЬКО трафик вне РФ: российские "
            "сайты открываются напрямую и в лимит не входят.")


def app_hint(config, lang: str) -> str:
    """For a demo key on the other allowance: where the bigger one is."""
    app_gb = demo_traffic_gb(config, in_app=True)
    if lang == 'en':
        return f"With the NekoVPN app (Android): {app_gb} GB a month: {_download_url()}"
    return f"С приложением NekoVPN (Android) — {app_gb} ГБ в месяц: {_download_url()}"


def demo_terms(config, lang: str, ios: bool = False) -> str:
    """Both allowances and the counting rule, for the key card and /sub.
    The quota belongs to the key, so an iPhone user with an Android phone
    gets the bigger one too."""
    app_gb = demo_traffic_gb(config, in_app=True)
    other_gb = demo_traffic_gb(config, in_app=False)
    if lang == 'en':
        head = (f"🎁 <b>Demo: {other_gb} GB a month, {app_gb} GB with the NekoVPN "
                f"app.</b> NekoVPN is Android-only for now: {_download_url()}")
        if not ios:   # a FlClash fork: it reads the FlClash link
            head += " — it takes the same link as FlClash."
    else:
        head = (f"🎁 <b>Демо: {other_gb} ГБ в месяц, с приложением NekoVPN — "
                f"{app_gb} ГБ.</b> NekoVPN пока только для Android: {_download_url()}")
        if not ios:
            head += " — подписка в нём та же, что для FlClash."
    return f"{head}\n{vpn_only_note(lang, ios=ios)}"


def below_app_allowance(config, total_bytes) -> bool:
    """A demo key whose panel quota is under the app allowance."""
    try:
        total = int(total_bytes or 0)
    except (TypeError, ValueError):
        return False
    return 0 < total < demo_traffic_gb(config, in_app=True) * BYTES_PER_GB


def stats_footer(user, config, total_bytes) -> str:
    """Under the traffic numbers: the counting rule, and for a demo key
    on the other allowance where the bigger one is."""
    lang = 'en' if getattr(user, 'lang', None) == 'en' else 'ru'
    ios = getattr(user, 'platform', None) == 'ios'
    lines = []
    if ((getattr(user, 'status', '') or '') == 'demo'
            and below_app_allowance(config, total_bytes)):
        lines.append(app_hint(config, lang))
    lines.append(vpn_only_note(lang, ios=ios))
    return '\n'.join(lines)

