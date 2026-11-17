"""Uploads (sendDocument) go through the same retry + proxy-pool path as
every other call — the offline kit (IMPROVEMENT_PLAN E27) is sent from
the entry node, which reaches api.telegram.org only through a proxy.

The old path-based ``send_document`` used a bare ``session.post``: with
TG_PROXY_URLS set the session ignores HTTPS_PROXY, so that upload went
direct and could not work on entry. Both shapes now share ``_request``.
"""

import json
from unittest.mock import Mock, patch

import pytest
import requests

from bot.core.telegram_client import TG_API_OUTAGE, TelegramClient

P1 = 'http://u:p@exit:8888'
P2 = 'http://u:p@reserve:8888'


@pytest.fixture(autouse=True)
def _clean_outage_state():
    snapshot = dict(TG_API_OUTAGE)
    yield
    TG_API_OUTAGE.clear()
    TG_API_OUTAGE.update(snapshot)


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setenv('TG_PROXY_URLS', f'{P1},{P2}')
    monkeypatch.setattr('bot.core.telegram_client.time.sleep', lambda *_: None)
    return TelegramClient('123:tok')


def _ok(result=None):
    r = requests.Response()
    r.status_code = 200
    r._content = json.dumps({'ok': True, 'result': result or {'message_id': 9}}).encode()
    return r


class TestSendDocumentBytes:

    def test_multipart_through_the_proxy_pool(self, client):
        calls = []

        def fake_post(url, data=None, files=None, timeout=None, proxies=None, json=None):
            calls.append(dict(url=url, data=data, files=files, proxies=proxies, json=json))
            return _ok()

        with patch.object(client.session, 'post', side_effect=fake_post):
            res = client.send_document_bytes(
                '42', 'NekoVPN-emergency.yaml', b'{"mode": "rule"}',
                caption='FlClash: Профили → + → Файл', parse_mode='HTML',
                reply_markup={'inline_keyboard': []}, message_thread_id=None)

        assert res == {'message_id': 9}
        (call,) = calls
        assert call['url'].endswith('/sendDocument')
        assert call['json'] is None
        assert call['files'] == {'document': ('NekoVPN-emergency.yaml', b'{"mode": "rule"}')}
        assert call['data'] == {'chat_id': '42', 'caption': 'FlClash: Профили → + → Файл',
                                'parse_mode': 'HTML', 'reply_markup': '{"inline_keyboard": []}'}
        assert call['proxies'] == {'http': P1, 'https': P1}

    def test_connection_error_rotates_and_resends_the_bytes(self, client):
        seen = []

        def fake_post(url, data=None, files=None, timeout=None, proxies=None, json=None):
            seen.append((proxies['https'], files['document'][1]))
            if proxies['https'] == P1:
                raise requests.ConnectionError('primary dead')
            return _ok()

        with patch.object(client.session, 'post', side_effect=fake_post):
            assert client.send_document_bytes('42', 'k.json', b'abc') == {'message_id': 9}
        assert seen == [(P1, b'abc'), (P2, b'abc')]

    def test_str_content_is_encoded(self, client):
        with patch.object(client.session, 'post', return_value=_ok()) as post:
            client.send_document_bytes('42', 'k.json', 'привет')
        assert post.call_args.kwargs['files']['document'] == ('k.json', 'привет'.encode())

    def test_failure_returns_none_and_never_raises(self, client):
        with patch.object(client.session, 'post',
                          side_effect=requests.ConnectionError('down')):
            assert client.send_document_bytes('42', 'k.json', b'x') is None
        bad = requests.Response()
        bad.status_code = 200
        bad._content = b'not json'
        with patch.object(client.session, 'post', return_value=bad):
            assert client.send_document_bytes('42', 'k.json', b'x') is None

    def test_telegram_refusal_returns_none(self, client):
        refused = requests.Response()
        refused.status_code = 200
        refused._content = b'{"ok": false, "description": "Bad Request: chat not found"}'
        with patch.object(client.session, 'post', return_value=refused):
            assert client.send_document_bytes('42', 'k.json', b'x') is None


class TestPathBasedSendDocument:

    def test_reads_the_file_and_uses_the_shared_path(self, client, tmp_path):
        f = tmp_path / 'report.html'
        f.write_bytes(b'<b>hi</b>')
        with patch.object(client.session, 'post', return_value=_ok()) as post:
            res = client.send_document('42', str(f), caption='cap', message_thread_id=7)
        assert res == {'message_id': 9}
        kw = post.call_args.kwargs
        assert kw['files'] == {'document': ('report.html', b'<b>hi</b>')}
        assert kw['data'] == {'chat_id': '42', 'message_thread_id': 7, 'caption': 'cap'}
        assert kw['proxies'] == {'http': P1, 'https': P1}

    def test_missing_file(self, client, tmp_path):
        with patch.object(client.session, 'post') as post:
            assert client.send_document('42', str(tmp_path / 'nope')) is None
        post.assert_not_called()


class TestBotWrapper:

    def test_bot_send_document_delegates(self):
        from bot.core.bot import Bot
        b = Bot.__new__(Bot)
        b.client = Mock()
        b.client.send_document_bytes.return_value = {'message_id': 1}
        assert b.send_document(chat_id='42', filename='a.yaml', content=b'x',
                               caption='c') == {'message_id': 1}
        b.client.send_document_bytes.assert_called_once_with(
            '42', 'a.yaml', b'x', caption='c', parse_mode=None)


def _refused(description, status=400):
    r = requests.Response()
    r.status_code = status
    r._content = json.dumps({'ok': False, 'description': description}).encode()
    return r


class TestRaiseErrors:
    """``raise_errors=True`` (the service backup, AGENTS.md §58): a failure
    raises with Telegram's own description, so the sender tells a deleted
    topic from a dead proxy. The default stays "None, never raises"."""

    def test_telegram_refusal_carries_the_description(self, client):
        with patch.object(client.session, 'post',
                          return_value=_refused('Bad Request: message thread not found')):
            with pytest.raises(requests.RequestException) as err:
                client.send_document_bytes('42', 'b.age', b'x', message_thread_id=9,
                                           raise_errors=True)
        assert err.value.tg_description == 'Bad Request: message thread not found'
        assert '123:tok' not in str(err.value)

    def test_dead_proxies_carry_no_description(self, client):
        with patch.object(client.session, 'post',
                          side_effect=requests.ConnectionError('proxy down')):
            with pytest.raises(requests.RequestException) as err:
                client.send_document_bytes('42', 'b.age', b'x', raise_errors=True)
        assert err.value.tg_description == ''

    def test_a_non_json_answer_raises_too(self, client):
        bad = requests.Response()
        bad.status_code = 200
        bad._content = b'<html>proxy page</html>'
        with patch.object(client.session, 'post', return_value=bad):
            with pytest.raises(requests.RequestException) as err:
                client.send_document_bytes('42', 'b.age', b'x', raise_errors=True)
        assert err.value.tg_description == ''

    def test_not_ok_with_200(self, client):
        weird = requests.Response()
        weird.status_code = 200
        weird._content = json.dumps({'ok': False, 'description': 'odd'}).encode()
        with patch.object(client.session, 'post', return_value=weird):
            with pytest.raises(requests.RequestException) as err:
                client.send_document_bytes('42', 'b.age', b'x', raise_errors=True)
        assert err.value.tg_description == 'odd'

    def test_the_flag_is_no_api_parameter(self, client):
        calls = []
        with patch.object(client.session, 'post',
                          side_effect=lambda *a, **kw: calls.append(kw) or _ok()):
            client.send_document_bytes('42', 'b.age', b'x', raise_errors=True)
        assert 'raise_errors' not in calls[0]['data']

    def test_default_still_never_raises(self, client):
        with patch.object(client.session, 'post',
                          return_value=_refused('Bad Request: message thread not found')):
            assert client.send_document_bytes('42', 'b.age', b'x') is None

    def test_other_calls_keep_the_description_too(self, client):
        with patch.object(client.session, 'post', return_value=_refused('Bad Request: x')):
            with pytest.raises(requests.RequestException) as err:
                client._request('sendMessage', chat_id='42', text='t')
        assert err.value.tg_description == 'Bad Request: x'
