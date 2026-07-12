"""API-only client management in XUIService (bot on a node without x-ui.db).

When self.db is None, add/remove_client_sync must route through the 3x-ui
HTTP API instead of returning False.
"""

import json
from unittest.mock import Mock, AsyncMock

import pytest

from bot.services.xui_service import XUIService


class _Cfg:
    XUI_API_URL = "http://84.75.76.109:2026"
    XUI_USERNAME = "admin"
    XUI_PASSWORD = "pw"
    XUI_BASE_PATH = "/this_is_fine"
    XUI_API_PATH = "/this_is_fine/panel/api/inbounds"
    XUI_DB_PATH = "/nonexistent/x-ui.db"  # → self.db stays None
    WS_INBOUND_ID = 0
    SS_INBOUND_ID = 0
    WS2_INBOUND_ID = 0
    INBOUND_ID = 3
    SS_USER_SALT = ""


@pytest.fixture
def svc():
    s = XUIService(_Cfg())
    assert s.db is None, "DB should be unset (API-only mode)"
    assert s.api is not None
    s.api = Mock()  # replace real client with a mock
    return s


def test_add_client_routes_to_api(svc):
    # v3.4.0: attaches the client to the primary inbound (INBOUND_ID=3)
    # in one relational call.
    svc.api.add_client = AsyncMock(return_value=True)

    assert svc.add_client_sync({"email": "u@x", "id": "uuid1"}) is True
    svc.api.add_client.assert_awaited()
    # first positional arg is the list of inbound ids to attach to
    assert svc.api.add_client.await_args[0][0] == [3]


def test_add_client_attaches_full_protocol_set(svc):
    # With WS/SS/WS2 inbounds set, the client attaches to all of them in
    # one call (primary + ws + ss + xhttp).
    svc.config.WS_INBOUND_ID = 4
    svc.config.SS_INBOUND_ID = 5
    svc.config.WS2_INBOUND_ID = 6
    svc.config.SS_USER_SALT = "salt"  # lets SS password derive so id 5 stays
    svc.api.add_client = AsyncMock(return_value=True)

    assert svc.add_client_sync({"email": "u@x", "id": "uuid1"}) is True
    assert svc.api.add_client.await_args[0][0] == [3, 4, 5, 6]
    # SS password was derived and injected for the SS inbound
    assert svc.api.add_client.await_args[0][1].get("password")


def test_add_client_drops_ss_without_salt(svc):
    # SS inbound is dropped when we can't derive its per-user password.
    svc.config.SS_INBOUND_ID = 5
    svc.config.SS_USER_SALT = ""
    svc.api.add_client = AsyncMock(return_value=True)

    assert svc.add_client_sync({"email": "u@x", "id": "uuid1"}) is True
    assert 5 not in svc.api.add_client.await_args[0][0]


def test_add_client_api_failure_returns_false(svc):
    svc.api.add_client = AsyncMock(return_value=False)
    assert svc.add_client_sync({"email": "u@x", "id": "uuid1"}) is False


def test_add_client_replaces_existing_email(svc):
    # v3.4.0 keys clients globally by email: a re-issue for a known user
    # collides with "email already in use" — the service must replace
    # the record (delete by email + re-add with the same UUID).
    svc.api.add_client = AsyncMock(side_effect=[False, True])
    svc.api.last_add_error = "Something went wrong (email already in use: u@x"
    svc.api.del_client_by_email = AsyncMock(return_value=True)

    assert svc.add_client_sync({"email": "u@x", "id": "uuid1"}) is True
    svc.api.del_client_by_email.assert_awaited_once()
    assert svc.api.add_client.await_count == 2


def test_add_client_no_delete_on_other_errors(svc):
    # Deleting on arbitrary failures could drop a working client without
    # putting anything back — only the duplicate error triggers replace.
    svc.api.add_client = AsyncMock(return_value=False)
    svc.api.last_add_error = "invalid inbound"
    svc.api.del_client_by_email = AsyncMock()

    assert svc.add_client_sync({"email": "u@x", "id": "uuid1"}) is False
    svc.api.del_client_by_email.assert_not_awaited()


def test_sync_user_api_mode_skips_reload(svc, monkeypatch):
    # API mode without a reload sidecar: the panel applies changes to
    # xray itself, so the missing XRAY_RELOAD_URL must not fail the sync
    # (it used to block every key issue on the entry node).
    import asyncio
    monkeypatch.delenv('XRAY_RELOAD_URL', raising=False)
    svc.api.add_client = AsyncMock(return_value=True)
    svc.reload_xray_sync = Mock(return_value=False)  # would fail if consulted

    ok = asyncio.run(svc.sync_user('42', {"email": "u@x", "id": "uuid1"}))
    assert ok is True
    svc.reload_xray_sync.assert_not_called()


def test_sync_user_honors_configured_sidecar(svc, monkeypatch):
    # An explicitly configured sidecar is still authoritative in any mode.
    import asyncio
    monkeypatch.setenv('XRAY_RELOAD_URL', 'http://127.0.0.1:8081')
    svc.api.add_client = AsyncMock(return_value=True)
    svc.reload_xray_sync = Mock(return_value=False)

    ok = asyncio.run(svc.sync_user('42', {"email": "u@x", "id": "uuid1"}))
    assert ok is False
    svc.reload_xray_sync.assert_called_once()


def test_remove_client_deletes_by_email(svc):
    # v3.4.0 removes clients globally by email (no per-inbound uuid lookup).
    svc.api.del_client_by_email = AsyncMock(return_value=True)
    assert svc.remove_client_sync("u@x") is True
    assert svc.api.del_client_by_email.await_args[0][0] == "u@x"


def test_remove_client_not_found(svc):
    svc.api.del_client_by_email = AsyncMock(return_value=False)
    assert svc.remove_client_sync("missing@x") is False


def test_find_client_id_by_email():
    ib = {"settings": json.dumps({"clients": [
        {"email": "a", "id": "x"}, {"email": "b", "id": "y"}]})}
    assert XUIService._find_client_id_by_email(ib, "b") == "y"
    assert XUIService._find_client_id_by_email(ib, "z") is None
    assert XUIService._find_client_id_by_email(None, "a") is None


def test_no_api_no_db_returns_false():
    class NoApi(_Cfg):
        XUI_API_URL = ""  # no API either
    s = XUIService(NoApi())
    assert s.db is None and s.api is None
    assert s.add_client_sync({"email": "u@x", "id": "u"}) is False
    assert s.remove_client_sync("u@x") is False
