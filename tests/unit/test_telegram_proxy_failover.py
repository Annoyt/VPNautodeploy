"""Proxy failover + outage tracking in TelegramClient.

Covers the 2026-07-23 incident pattern: the primary tinyproxy dies or the
entry↔exit link flaps, and the bot must rotate to the reserve proxy
instead of silently going offline.
"""
import time
from unittest.mock import patch

import pytest
import requests

from bot.core.telegram_client import TelegramClient, TG_API_OUTAGE


P1 = 'http://u:p@exit:8888'
P2 = 'http://u:p@reserve:8888'


@pytest.fixture(autouse=True)
def _clean_outage_state():
    TG_API_OUTAGE.update({
        'since': None, 'last_duration': 0.0,
        'recovered_at': None, 'recovery_pending': False,
    })
    yield
    TG_API_OUTAGE.update({
        'since': None, 'last_duration': 0.0,
        'recovered_at': None, 'recovery_pending': False,
    })


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('TG_PROXY_URLS', f'{P1},{P2}')
    monkeypatch.setattr('bot.core.telegram_client.time.sleep', lambda *_: None)
    return TelegramClient('123:tok')


def _ok_response():
    r = requests.Response()
    r.status_code = 200
    r._content = b'{"ok": true, "result": {}}'
    return r


class TestPoolInit:
    def test_pool_parsed_and_env_bypassed(self, client):
        assert client._proxy_pool == [P1, P2]
        assert client.session.trust_env is False

    def test_legacy_mode_without_pool(self, monkeypatch):
        monkeypatch.delenv('TG_PROXY_URLS', raising=False)
        c = TelegramClient('123:tok')
        assert c._proxy_pool == []
        assert c.session.trust_env is True

    def test_creds_never_in_logs(self, client, caplog):
        with patch.object(client.session, 'post', side_effect=requests.ConnectionError('boom')):
            with pytest.raises(requests.RequestException):
                client._request('getMe')
        for rec in caplog.records:
            assert 'u:p@' not in rec.getMessage()


class TestRotation:
    def test_rotates_to_next_proxy_on_connection_error(self, client):
        calls = []

        def fake_post(url, json=None, timeout=None, proxies=None):
            calls.append(proxies)
            if proxies and proxies.get('https') == P2:
                return _ok_response()
            raise requests.ConnectionError('primary dead')

        with patch.object(client.session, 'post', side_effect=fake_post):
            result = client._request('getMe')

        assert result == {'ok': True, 'result': {}}
        assert calls[0]['https'] == P1
        assert calls[-1]['https'] == P2
        # primary got cooled down
        assert client._proxy_dead_until.get(P1, 0) > time.time()

    def test_sticks_with_healthy_proxy(self, client):
        with patch.object(client.session, 'post', return_value=_ok_response()) as post:
            client._request('getMe')
            client._request('getMe')
        proxies_used = {c.kwargs['proxies']['https'] for c in post.call_args_list}
        assert proxies_used == {P1}

    def test_all_proxies_dead_still_attempts(self, client):
        with patch.object(client.session, 'post', side_effect=requests.ConnectionError('down')):
            with pytest.raises(requests.RequestException):
                client._request('getMe')
        assert TG_API_OUTAGE['since'] is not None

    def test_direct_entry_supported(self, monkeypatch):
        monkeypatch.setenv('TG_PROXY_URLS', f'{P1},direct')
        c = TelegramClient('123:tok')
        c._mark_proxy_dead(P1)
        assert c._pick_proxy() == 'direct'
        assert c._proxies_kwarg('direct') == {}


class TestOutageTracking:
    def test_outage_marks_since_after_streak(self, client):
        with patch.object(client.session, 'post', side_effect=requests.ConnectionError('x')):
            with pytest.raises(requests.RequestException):
                client._request('getMe')
        assert TG_API_OUTAGE['since'] is not None

    def test_recovery_recorded_and_flagged(self, client):
        TG_API_OUTAGE['since'] = time.time() - 300
        client._conn_fail_streak = 3
        with patch.object(client.session, 'post', return_value=_ok_response()):
            client._request('getMe')
        assert TG_API_OUTAGE['since'] is None
        assert TG_API_OUTAGE['last_duration'] >= 299
        assert TG_API_OUTAGE['recovery_pending'] is True
        assert client._conn_fail_streak == 0

    def test_http_error_is_not_a_proxy_failure(self, client):
        r = requests.Response()
        r.status_code = 500
        r._content = b'{"ok": false, "description": "x"}'
        with patch.object(client.session, 'post', return_value=r):
            with pytest.raises(requests.RequestException):
                client._request('getMe')
        # no rotation, no outage — the API answered, payload problem
        assert client._proxy_dead_until == {}
        assert TG_API_OUTAGE['since'] is None
