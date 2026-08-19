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
    # Default for the stale-settings recovery lookup in
    # _update_client_fields_async; tests override when relevant.
    s.api.get_client_traffic = AsyncMock(return_value=None)
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


# ---- panel comment (real contact email shown next to a synthetic key) ----

def _inbound_with(client: dict) -> dict:
    return {"settings": json.dumps({"clients": [client]})}


def test_set_client_comment_updates_in_place(svc):
    """The note is written via update, never by re-keying the client:
    panel emails are globally unique, so using a user-supplied address
    as the key would let two users collide."""
    svc.config.WS_INBOUND_ID = 4
    svc.config.SS_INBOUND_ID = 5
    svc.config.WS2_INBOUND_ID = 6
    existing = {"email": "user_bob_42@nekovo.ru", "id": "uuid1",
                "password": "sspw", "totalGB": 123, "comment": ""}
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(existing))
    svc.api.update_client = AsyncMock(return_value=True)

    assert svc.set_client_comment_sync(
        "user_bob_42@nekovo.ru", "bob@gmail.com") is True

    email_arg, client_arg, inbound_arg = svc.api.update_client.await_args[0]
    assert email_arg == "user_bob_42@nekovo.ru"
    assert client_arg["comment"] == "bob@gmail.com"
    # Untouched fields must survive — update rewrites everything it's sent.
    assert client_arg["password"] == "sspw"
    assert client_arg["totalGB"] == 123
    assert inbound_arg == [3, 4, 5, 6]


def test_set_client_comment_reads_ss_inbound_first(svc):
    """The SS inbound is the only one carrying the per-user SS-2022
    password, so it must be the lookup source when present."""
    svc.config.SS_INBOUND_ID = 5
    svc.api.get_inbound = AsyncMock(
        return_value=_inbound_with({"email": "u@x", "id": "i", "comment": ""}))
    svc.api.update_client = AsyncMock(return_value=True)

    svc.set_client_comment_sync("u@x", "real@mail.com")
    assert svc.api.get_inbound.await_args_list[0][0][0] == 5


def test_set_client_comment_noop_when_unchanged(svc):
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(
        {"email": "u@x", "id": "i", "comment": "real@mail.com"}))
    svc.api.update_client = AsyncMock(return_value=True)

    assert svc.set_client_comment_sync("u@x", "real@mail.com") is True
    svc.api.update_client.assert_not_awaited()


def test_set_client_comment_missing_client_is_false(svc):
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(
        {"email": "someone_else@x", "id": "i"}))
    svc.api.update_client = AsyncMock(return_value=True)

    assert svc.set_client_comment_sync("u@x", "real@mail.com") is False
    svc.api.update_client.assert_not_awaited()


def test_set_client_comment_without_api_is_false():
    class NoApi(_Cfg):
        XUI_API_URL = ""
    assert XUIService(NoApi()).set_client_comment_sync("u@x", "a@b") is False


def test_find_client_by_email_returns_copy():
    ib = _inbound_with({"email": "a", "id": "x", "totalGB": 5})
    got = XUIService._find_client_by_email(ib, "a")
    assert got == {"email": "a", "id": "x", "totalGB": 5}
    got["id"] = "mutated"
    assert XUIService._find_client_by_email(ib, "a")["id"] == "x"
    assert XUIService._find_client_by_email(ib, "zz") is None


# ---- monthly demo renew (freemium: allowance refreshed every month) ----

def test_renew_client_updates_expiry_enable_quota(svc):
    svc.config.SS_INBOUND_ID = 5
    existing = {"email": "u@x", "id": "uuid1", "password": "sspw",
                "totalGB": 1, "expiryTime": 111, "enable": False}
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(existing))
    svc.api.update_client = AsyncMock(return_value=True)
    svc.api.reset_client_traffic = AsyncMock(return_value=True)

    assert svc.renew_client_sync("u@x", 999000,
                                 total_bytes=5 * 1024 ** 3) is True

    _email, client_arg, _ids = svc.api.update_client.await_args[0]
    assert client_arg["expiryTime"] == 999000
    assert client_arg["enable"] is True
    assert client_arg["totalGB"] == 5 * 1024 ** 3
    # Untouched fields must survive — update rewrites everything it's sent.
    assert client_arg["password"] == "sspw"
    svc.api.reset_client_traffic.assert_awaited_once_with("u@x")


def test_renew_client_keeps_quota_when_not_given(svc):
    existing = {"email": "u@x", "id": "uuid1", "totalGB": 42}
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(existing))
    svc.api.update_client = AsyncMock(return_value=True)
    svc.api.reset_client_traffic = AsyncMock(return_value=True)

    assert svc.renew_client_sync("u@x", 999000) is True
    assert svc.api.update_client.await_args[0][1]["totalGB"] == 42


def test_renew_client_missing_in_panel_is_false(svc):
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(
        {"email": "someone_else@x", "id": "i"}))
    svc.api.update_client = AsyncMock(return_value=True)
    svc.api.reset_client_traffic = AsyncMock(return_value=True)

    assert svc.renew_client_sync("u@x", 999000) is False
    svc.api.update_client.assert_not_awaited()
    svc.api.reset_client_traffic.assert_not_awaited()


def test_renew_client_no_reset_when_update_fails(svc):
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(
        {"email": "u@x", "id": "uuid1"}))
    svc.api.update_client = AsyncMock(return_value=False)
    svc.api.reset_client_traffic = AsyncMock(return_value=True)

    assert svc.renew_client_sync("u@x", 999000) is False
    svc.api.reset_client_traffic.assert_not_awaited()


def test_renew_client_without_api_is_false():
    class NoApi(_Cfg):
        XUI_API_URL = ""
    assert XUIService(NoApi()).renew_client_sync("u@x", 1) is False


# ---- dashboard set_quota / set_expire panel sync (API mode) ----

def test_sync_client_settings_routes_to_api(svc):
    """On entry there is no local x-ui.db; dashboard quota/expiry edits
    must reach the panel through the update endpoint instead of being
    silently dropped."""
    existing = {"email": "u@x", "id": "uuid1", "password": "sspw",
                "totalGB": 1, "enable": False}
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(existing))
    svc.api.update_client = AsyncMock(return_value=True)

    assert svc.sync_client_settings_sync(
        "u@x", {"expiryTime": 123000, "enable": True}) is True

    _email, client_arg, _ids = svc.api.update_client.await_args[0]
    assert client_arg["expiryTime"] == 123000
    assert client_arg["enable"] is True
    assert client_arg["password"] == "sspw"   # untouched fields survive
    assert client_arg["totalGB"] == 1


def test_sync_client_settings_api_missing_client_is_false(svc):
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(
        {"email": "someone_else@x", "id": "i"}))
    svc.api.update_client = AsyncMock(return_value=True)

    assert svc.sync_client_settings_sync("u@x", {"totalGB": 5}) is False
    svc.api.update_client.assert_not_awaited()


def test_update_fields_recovers_quota_from_accounting_row(svc):
    """SQL-backfilled quotas live only in client_traffics — the
    settings JSON copy still says 0. An update that trusted the stale
    JSON would rewrite totalGB=0 (unlimited); the recovery lookup must
    restore the real quota first. Bit the paid trio on 2026-08-19."""
    existing = {"email": "u@x", "id": "uuid1"}   # no totalGB/expiryTime
    svc.api.get_inbound = AsyncMock(return_value=_inbound_with(existing))
    svc.api.update_client = AsyncMock(return_value=True)
    svc.api.get_client_traffic = AsyncMock(return_value={
        "email": "u@x", "up": 0, "down": 0,
        "total": 100 * 1024 ** 3, "enable": True, "expiry_time": 555000,
    })

    assert svc.sync_client_settings_sync("u@x", {"enable": True}) is True

    _e, client_arg, _i = svc.api.update_client.await_args[0]
    assert client_arg["totalGB"] == 100 * 1024 ** 3
    assert client_arg["expiryTime"] == 555000
    assert client_arg["enable"] is True


def test_stub_db_file_disables_db_mode(tmp_path):
    """An empty SQLite file at XUI_DB_PATH (created by a stray connect
    on the sentinel path — happened on entry 2026-08-01) must NOT
    enable DB mode: every db-first method would route into a dead end
    while the panel API sits configured and working."""
    stub = tmp_path / "__no_local_xui_db__"
    import sqlite3
    sqlite3.connect(stub).close()   # 0-table SQLite, like the real stub

    class StubCfg(_Cfg):
        XUI_DB_PATH = str(stub)

    s = XUIService(StubCfg())
    assert s.db is None
    assert s.api is not None
