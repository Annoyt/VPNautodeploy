"""Fallback VPN node (DE) for paid users.

Why
---
The whole main cascade (entry+exit) sits behind one provider pair. When
it degrades — РКН pressure on the entry IP, hoster network flaps,
whatever — paid users need a second, fully independent server to switch
to. This service provisions them on the reserve x-ui panel (mytherm,
212.60.153.208) and the subscription builder adds the extra outbound.

Design
------
- **Lazy provisioning**: on a paid user's ``/sub`` fetch we check their
  email on the fallback inbound and add the client if missing. No
  changes to the key-issuance flow, and users who never fetch /sub
  never touch the reserve panel.
- **Same uuid as the main system** — one credential per user, revocation
  is a delete-by-uuid mirroring ``user_lifecycle.revoke_user_key``.
- **Panel is 2.8.x**: form login → session cookie, classic
  ``/panel/api/inbounds/addClient`` / ``delClient/<uuid>`` endpoints, no
  CSRF dance (unlike the 3.5 panel on the exit node).
- Self-signed panel cert → ``verify=False`` scoped to this session only.

Config (all optional — empty host disables everything):
    FALLBACK_NODE_HOST          client-facing address (212.60.153.208)
    FALLBACK_NODE_PORT          443
    FALLBACK_NODE_SNI           reality serverName (www.google.com)
    FALLBACK_NODE_PBK           reality public key
    FALLBACK_NODE_SID           reality short id
    FALLBACK_NODE_XUI_URL       panel base (https://host:2026)
    FALLBACK_NODE_XUI_BASE_PATH panel webBasePath (/sub)
    FALLBACK_NODE_XUI_USER / FALLBACK_NODE_XUI_PASS
    FALLBACK_NODE_INBOUND_ID    inbound to attach clients to (1)
"""

from __future__ import annotations

import json
import logging
import time
from typing import Optional

import requests
import urllib3

logger = logging.getLogger(__name__)

# Statuses that get the fallback node. Mirrors PAID_USER_STATUSES in
# callbacks/user.py — keep in sync.
FALLBACK_ALLOWED_STATUSES = ('paid', 'support_topic')

# Membership re-check interval per user. The /sub handler calls
# ensure_client on every fetch; without this cache each refresh of every
# paid user costs a panel login + GET. Clients don't vanish from the
# panel on their own, so a 10-minute "known good" window is safe.
_ENSURE_CACHE_TTL = 600


class FallbackNodeService:
    """Provisioning + outbound builder for the reserve node."""

    def __init__(self, config):
        self.config = config
        self._ensure_cache: dict[str, float] = {}

    # ----- config accessors -----

    def _cfg(self, name: str, default: str = '') -> str:
        return (getattr(self.config, name, default) or default)

    @property
    def enabled(self) -> bool:
        return bool(self._cfg('FALLBACK_NODE_HOST') and self._cfg('FALLBACK_NODE_PBK'))

    @property
    def _api_configured(self) -> bool:
        return bool(
            self._cfg('FALLBACK_NODE_XUI_URL')
            and self._cfg('FALLBACK_NODE_XUI_PASS')
        )

    def _base(self) -> str:
        url = self._cfg('FALLBACK_NODE_XUI_URL').rstrip('/')
        path = self._cfg('FALLBACK_NODE_XUI_BASE_PATH', '/sub').strip('/')
        return f'{url}/{path}' if path else url

    def _inbound_id(self) -> int:
        try:
            return int(self._cfg('FALLBACK_NODE_INBOUND_ID', '1'))
        except (ValueError, TypeError):
            return 1

    # ----- outbound -----

    def build_outbound(self, user) -> Optional[dict]:
        """Sing-box VLESS+Reality outbound for the reserve node.

        No flow — the reserve inbound's clients don't use vision. The
        tag suffix ``-de`` marks the German exit in the client's list.
        """
        if not self.enabled:
            return None
        uuid = getattr(user, 'uuid', None)
        email = getattr(user, 'email', None) or ''
        if not uuid:
            return None
        try:
            port = int(self._cfg('FALLBACK_NODE_PORT', '443'))
        except (ValueError, TypeError):
            port = 443
        sni = self._cfg('FALLBACK_NODE_SNI', 'www.google.com')
        ob = {
            'type': 'vless',
            'tag': f'{email.split("@")[0]}-de',
            'server': self._cfg('FALLBACK_NODE_HOST'),
            'server_port': port,
            'uuid': uuid,
            'packet_encoding': 'xudp',
            'tls': {
                'enabled': True,
                'server_name': sni,
                'utls': {'enabled': True, 'fingerprint': 'chrome'},
                'reality': {
                    'enabled': True,
                    'public_key': self._cfg('FALLBACK_NODE_PBK'),
                    'short_id': self._cfg('FALLBACK_NODE_SID'),
                },
            },
        }
        return ob

    # ----- panel API -----

    def _new_session(self) -> requests.Session:
        s = requests.Session()
        s.verify = False
        return s

    def _login(self, s: requests.Session) -> bool:
        try:
            r = s.post(
                f'{self._base()}/login',
                data={
                    'username': self._cfg('FALLBACK_NODE_XUI_USER', 'admin'),
                    'password': self._cfg('FALLBACK_NODE_XUI_PASS'),
                },
                timeout=15,
            )
            return r.status_code == 200 and r.json().get('success') is True
        except Exception as e:
            logger.warning(f'fallback_node: panel login failed: {e}')
            return False

    def _get_client_uuids(self, s: requests.Session) -> dict:
        """email → uuid map of the fallback inbound."""
        r = s.get(f'{self._base()}/panel/api/inbounds/get/{self._inbound_id()}', timeout=15)
        obj = r.json().get('obj') or {}
        settings = obj.get('settings') or '{}'
        if isinstance(settings, str):
            settings = json.loads(settings)
        return {c.get('email'): c.get('id') for c in settings.get('clients', [])}

    def ensure_client(self, user) -> bool:
        """Provision the user on the reserve node if missing. Idempotent.

        Returns True when the client is present (already or just added).
        Any panel failure is logged and returns False — the caller still
        emits the outbound; the client will just fail until the next
        /sub refresh retries provisioning.
        """
        email = getattr(user, 'email', None)
        uuid = getattr(user, 'uuid', None)
        if not (self.enabled and self._api_configured and email and uuid):
            return False

        cached = self._ensure_cache.get(email, 0)
        if time.time() - cached < _ENSURE_CACHE_TTL:
            return True

        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            s = self._new_session()
            if not self._login(s):
                return False
            existing = self._get_client_uuids(s)
            if existing.get(email) == uuid:
                self._ensure_cache[email] = time.time()
                return True
            if email in existing:
                logger.warning(
                    f'fallback_node: {email} exists with a different uuid, '
                    'leaving as-is'
                )
                self._ensure_cache[email] = time.time()
                return False
            client = {
                'id': uuid, 'flow': '', 'email': email,
                'limitIp': 0, 'totalGB': 0, 'expiryTime': 0,
                'enable': True, 'tgId': '', 'subId': '',
            }
            r = s.post(
                f'{self._base()}/panel/api/inbounds/addClient',
                json={'id': self._inbound_id(), 'settings': json.dumps({'clients': [client]})},
                timeout=15,
            )
            ok = r.status_code == 200 and r.json().get('success') is True
            if ok:
                self._ensure_cache[email] = time.time()
                logger.info(f'fallback_node: provisioned {email}')
            else:
                logger.warning(f'fallback_node: addClient failed: {r.json().get("msg")}')
            return ok
        except Exception as e:
            logger.warning(f'fallback_node: ensure_client failed for {email}: {e}')
            return False

    def remove_client(self, uuid: str) -> bool:
        """Delete a client by uuid (revocation path). Best-effort."""
        if not (self.enabled and self._api_configured and uuid):
            return False
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        try:
            s = self._new_session()
            if not self._login(s):
                return False
            r = s.post(
                f'{self._base()}/panel/api/inbounds/{self._inbound_id()}/delClient/{uuid}',
                timeout=15,
            )
            ok = r.status_code == 200 and r.json().get('success') is True
            if ok:
                logger.info('fallback_node: removed client by uuid')
            else:
                logger.warning(f'fallback_node: delClient failed: {r.json().get("msg")}')
            return ok
        except Exception as e:
            logger.warning(f'fallback_node: remove_client failed: {e}')
            return False
