"""client_attached_sync: the gate before every re-add of a client.

An add of a client the panel still has is delete + re-add on the
relational panel, and that zeroes the key's traffic. The demo job and
«🔑 Мой ключ» re-add only what this check calls lost. It must say
"attached" when it cannot tell. Until 2026-10-30 it did not when an
inbound read failed: XUIAPIClient.get_inbound answers None on any error
(a timeout, a refused login), and None read as "not on that inbound", so
a panel hiccup made an attached client look lost.

The real XUIService runs against a fake that serves get_inbound only.
"""

import json

import pytest

from bot.services.xui_service import XUIService

REALITY, WS, SS, WS2 = 1, 4, 5, 6
EMAIL = 'user_bob_42@nekovo.ru'


class Inbounds:
    """Per-inbound clients; ids in ``failing`` answer None, as a failed
    read does."""

    def __init__(self, clients, failing=()):
        self.clients = clients
        self.failing = set(failing)
        self.read = []

    async def get_inbound(self, inbound_id):
        self.read.append(int(inbound_id))
        if int(inbound_id) in self.failing:
            return None
        return {'id': int(inbound_id),
                'settings': json.dumps({'clients': self.clients.get(int(inbound_id), [])})}


class _Cfg:
    """Entry's shape: API configured, no usable local x-ui.db."""
    XUI_API_URL = 'http://panel.invalid:2026'
    XUI_USERNAME = 'admin'
    XUI_PASSWORD = 'pw'
    XUI_BASE_PATH = '/this_is_fine'
    XUI_API_PATH = '/this_is_fine/panel/api/inbounds'
    XUI_DB_PATH = '/nonexistent/x-ui.db'
    INBOUND_ID = REALITY
    WS_INBOUND_ID = WS
    SS_INBOUND_ID = SS
    WS2_INBOUND_ID = WS2


def attached(panel):
    svc = XUIService(_Cfg())
    assert svc.db is None, 'must be API-only mode, like entry'
    svc.api = panel
    return svc.client_attached_sync(EMAIL)


def on(*ids):
    return {i: [{'email': EMAIL, 'id': 'uuid-1'}] for i in ids}


@pytest.mark.parametrize('ids', [(REALITY,), (WS2,), (REALITY, WS, SS, WS2)])
def test_a_client_on_any_inbound_is_attached(ids):
    assert attached(Inbounds(on(*ids))) is True


def test_a_client_on_no_inbound_is_lost():
    panel = Inbounds({REALITY: [{'email': 'someone@else', 'id': 'u2'}]})
    assert attached(panel) is False
    assert sorted(panel.read) == [REALITY, WS, SS, WS2]


@pytest.mark.parametrize('failing', [REALITY, WS, SS, WS2])
def test_a_failed_read_is_not_a_lost_key(failing):
    assert attached(Inbounds({}, failing={failing})) is True


def test_a_client_found_despite_a_failed_read():
    assert attached(Inbounds(on(SS), failing={REALITY})) is True


def test_no_panel_api_counts_as_attached():
    svc = XUIService(_Cfg())
    svc.api = None
    assert svc.client_attached_sync(EMAIL) is True
