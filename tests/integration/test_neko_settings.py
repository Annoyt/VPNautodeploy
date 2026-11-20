"""The NekoVPN app's signed remote settings (client plan E14).

scripts/neko_settings.py signs bot/neko/settings.source.json OFFLINE (the
key lives on the dev machine only); the bot serves the signed file as is at
GET /neko/settings and the app checks the signature with the public key it
carries. These tests pin the committed file to the source and to the key,
the validation the app relies on, and the endpoint.
"""

import base64
import importlib.util
import json
import os
from datetime import datetime, timezone
from unittest.mock import Mock

import pytest

ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
pytest.importorskip('cryptography')
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402


def _load_script():
    spec = importlib.util.spec_from_file_location(
        'neko_settings', os.path.join(ROOT, 'scripts', 'neko_settings.py'))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


ns = _load_script()


def _settings(**over):
    settings = {
        'schema': 1,
        'endpoints': ['https://a.example', 'https://b.example:2096'],
        'min_version': None,
        'message': None,
        'flags': {},
    }
    settings.update(over)
    return settings


def _public(key):
    raw = key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)
    return base64.b64encode(raw).decode()


class TestCommittedFile:

    def test_signed_with_the_key_the_app_carries(self):
        signed = json.loads((ns.SIGNED).read_text(encoding='utf-8'))
        doc = ns.verify(signed)
        assert signed['kid'] == ns.KID
        assert isinstance(doc['version'], int) and doc['version'] >= 1

    def test_is_the_source_signed(self):
        # Edit settings.source.json, then `scripts/neko_settings.py sign`:
        # a source that differs from what is signed never reaches the app.
        source = json.loads(ns.SOURCE.read_text(encoding='utf-8'))
        doc, _, _ = ns.decode(json.loads(ns.SIGNED.read_text(encoding='utf-8')))
        assert {k: v for k, v in doc.items() if k not in ('version', 'signed_at')} == source
        ns.validate(source)

    def test_public_key_is_a_raw_ed25519_key(self):
        assert len(base64.b64decode(ns.PUBLIC_KEY, validate=True)) == 32


class TestSigning:

    def test_round_trip(self):
        key = Ed25519PrivateKey.generate()
        now = datetime(2026, 10, 25, 12, 0, tzinfo=timezone.utc)
        signed = ns.sign_doc(ns.build_doc(_settings(), 7, now=now), key)
        doc = ns.verify(signed, public_key=_public(key))
        assert doc['version'] == 7 and doc['signed_at'] == '2026-10-25T12:00:00Z'
        assert doc['endpoints'] == ['https://a.example', 'https://b.example:2096']

    def test_a_changed_doc_or_another_key_fails(self):
        key = Ed25519PrivateKey.generate()
        signed = ns.sign_doc(ns.build_doc(_settings(), 1), key)
        doc, _, _ = ns.decode(signed)
        forged = json.dumps({**doc, 'endpoints': ['https://evil.example']}).encode()
        tampered = {**signed, 'doc': base64.b64encode(forged).decode()}
        with pytest.raises(Exception):
            ns.verify(tampered, public_key=_public(key))
        with pytest.raises(Exception):
            ns.verify(signed, public_key=_public(Ed25519PrivateKey.generate()))

    def test_version_follows_the_signed_file(self, tmp_path):
        path = tmp_path / 'settings.json'
        assert ns.current_version(path) == 0
        key = Ed25519PrivateKey.generate()
        path.write_text(json.dumps(ns.sign_doc(ns.build_doc(_settings(), 41), key)))
        assert ns.current_version(path) == 41
        path.write_text('not json')
        assert ns.current_version(path) == 0

    def test_doc_bytes_are_compact_and_sorted(self):
        signed = ns.sign_doc(ns.build_doc(_settings(), 1), Ed25519PrivateKey.generate())
        _, doc_bytes, _ = ns.decode(signed)
        assert doc_bytes.startswith(b'{"endpoints":[') and b', ' not in doc_bytes


class TestValidate:

    @pytest.mark.parametrize('over', [
        {'schema': 2},
        {'endpoints': []},
        {'endpoints': ['http://a.example']},
        {'endpoints': ['https://a.example/sub']},
        {'endpoints': ['https://a.example'] * 11},
        {'min_version': '0.9.2'},
        {'min_version': 'latest'},
        {'message': {'ru': 'без id'}},
        {'message': {'id': ' ', 'ru': 'x'}},
        {'message': {'id': 'a'}},
        {'message': {'id': 'a', 'ru': 'x', 'url': 'http://a.example'}},
        {'message': {'id': 'a', 'ru': 'x', 'extra': 1}},
        {'flags': {'a': [1]}},
        {'flags': []},
        {'surprise': 1},
    ])
    def test_rejects(self, over):
        with pytest.raises(ValueError):
            ns.validate(_settings(**over))

    def test_accepts_a_full_document(self):
        ns.validate(_settings(
            min_version='0.9.2-neko.1',
            message={'id': '2026-10-25-a', 'ru': 'Текст', 'en': 'Text',
                     'url': 'https://t.me/neko_vpnbot'},
            flags={'multipath': False, 'sos_channels': 3, 'note': 'x', 'ratio': 0.5},
        ))


class TestNeko6Fields:
    """docs/NEKO6_PLAN.md D1: ``bypass_apps`` (the Android packages the app
    sends past the VPN), ``mail`` (the mailbox its mailto: links open) and
    ``flags.netstat`` (false: the app sends no network diagnostics)."""

    @pytest.mark.parametrize('over', [
        {'bypass_apps': 'ru.sberbankmobile'},
        {'bypass_apps': None},
        {'bypass_apps': {'ru.sberbankmobile': True}},
        {'bypass_apps': ['sberbankmobile']},                  # one segment
        {'bypass_apps': ['ru.1sber']},                        # a segment from a digit
        {'bypass_apps': ['ru._sber']},
        {'bypass_apps': ['1ru.sber']},
        {'bypass_apps': ['ru..sber']},
        {'bypass_apps': ['ru.sber.']},
        {'bypass_apps': ['.ru.sber']},
        {'bypass_apps': ['ru.sber-bank']},
        {'bypass_apps': ['ru.sber bank']},
        {'bypass_apps': ['ru.sber\n']},
        {'bypass_apps': ['ru.сбер']},                         # not ASCII
        {'bypass_apps': ['']},
        {'bypass_apps': [42]},
        {'bypass_apps': [['ru.sber']]},
        {'bypass_apps': ['ru.sber', 'ru.vtb', 'ru.sber']},    # twice
        {'bypass_apps': [f'ru.app{i}' for i in range(301)]},
        {'mail': ''},
        {'mail': 'support'},
        {'mail': 'support@'},
        {'mail': '@example.com'},
        {'mail': 'support@example'},
        {'mail': 'support@-example.com'},
        {'mail': 'support@example.c'},
        {'mail': '.support@example.com'},
        {'mail': 'sup..port@example.com'},
        {'mail': 'support@example.com?subject=x'},              # mailto: headers
        {'mail': 'support@example.com&cc=x@example.org'},
        {'mail': 'support@example.com#x'},
        {'mail': 'sup%40port@example.com'},
        {'mail': 'sup port@example.com'},
        {'mail': 'support@example.com\n'},
        {'mail': 'mailto:support@example.com'},
        {'mail': ['support@example.com']},
        {'mail': 'a' * 243 + '@example.com'},                  # 255 characters
        {'flags': {'netstat': 'false'}},
        {'flags': {'netstat': 0}},
        {'flags': {'netstat': None}},
    ])
    def test_rejects(self, over):
        with pytest.raises(ValueError):
            ns.validate(_settings(**over))

    def test_accepts(self):
        ns.validate(_settings(
            bypass_apps=['ru.sberbankmobile', 'ru.vtb24.mobilebanking.android',
                         'com.vkontakte.android', 'ru.gosuslugi.pos', 'Com.Example_App.X1'],
            mail='support.neko+vpn@mail.nekoweather.xyz',
            flags={'netstat': False},
        ))
        ns.validate(_settings(bypass_apps=[], mail=None, flags={'netstat': True}))
        ns.validate(_settings(bypass_apps=[f'ru.app{i}' for i in range(300)],
                              mail='a' * 242 + '@example.com'))         # 254 characters
        # the fields are optional: older settings stay valid
        ns.validate(_settings())

    def test_the_error_says_what_is_wrong(self):
        with pytest.raises(ValueError, match=r"bypass_apps: 'ru\.1sber' is not an Android "):
            ns.validate(_settings(bypass_apps=['ru.sber', 'ru.1sber']))
        with pytest.raises(ValueError, match='bypass_apps: listed more than once: ru.a, ru.b$'):
            ns.validate(_settings(bypass_apps=['ru.b', 'ru.a', 'ru.b', 'ru.a', 'ru.c']))
        with pytest.raises(ValueError, match='bypass_apps: a list of at most 300 '):
            ns.validate(_settings(bypass_apps=[f'ru.app{i}' for i in range(301)]))
        with pytest.raises(ValueError, match=r"^mail: .*not 'support@example\.com\?subject=x'$"):
            ns.validate(_settings(mail='support@example.com?subject=x'))
        with pytest.raises(ValueError, match='^flags.netstat: true or false'):
            ns.validate(_settings(flags={'netstat': 'false'}))

    def test_build_doc_refuses_them_too(self):
        # what `sign` runs before it signs: a bad list never gets a signature
        with pytest.raises(ValueError, match='bypass_apps'):
            ns.build_doc(_settings(bypass_apps=['ru.sber', 'ru.sber']), 2)
        doc = ns.build_doc(_settings(bypass_apps=['ru.sber'], mail=None), 2)
        assert doc['bypass_apps'] == ['ru.sber'] and doc['mail'] is None


class TestEndpoint:

    @pytest.fixture
    def server(self, tmp_path):
        from bot.core.database import Database
        from bot.core.web_server import WebAppServer
        cfg = Mock()
        cfg.BOT_TOKEN = 'test_token'
        cfg.WEBAPP_URL = 'https://dash.example.com'
        return WebAppServer(cfg, Database(str(tmp_path / 'bot.db')), xui_service=Mock())

    @pytest.mark.asyncio
    async def test_serves_the_signed_file_as_is(self, server):
        from bot.core import web_server
        resp = await server.handle_neko_settings(Mock())
        assert resp.status == 200 and resp.content_type == 'application/json'
        assert resp.body == web_server.NEKO_SETTINGS_PATH.read_bytes()
        assert resp.headers['Cache-Control'] == 'public, max-age=300'

    @pytest.mark.asyncio
    async def test_missing_file_is_404(self, server, monkeypatch, tmp_path):
        from bot.core import web_server
        monkeypatch.setattr(web_server, 'NEKO_SETTINGS_PATH', tmp_path / 'none.json')
        assert (await server.handle_neko_settings(Mock())).status == 404

    def test_route(self, server):
        paths = {r.resource.canonical for r in server.app.router.routes()
                 if getattr(r, 'method', '') == 'GET'}
        assert '/neko/settings' in paths
