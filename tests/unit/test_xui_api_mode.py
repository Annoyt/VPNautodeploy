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
    INBOUND_ID = 0


@pytest.fixture
def svc():
    s = XUIService(_Cfg())
    assert s.db is None, "DB should be unset (API-only mode)"
    assert s.api is not None
    s.api = Mock()  # replace real client with a mock
    return s


def test_add_client_routes_to_api(svc):
    svc.api.get_inbounds = AsyncMock(return_value=[{"id": 3, "protocol": "vless"}])
    svc.api.add_client = AsyncMock(return_value=True)

    assert svc.add_client_sync({"email": "u@x", "id": "uuid1"}) is True
    svc.api.add_client.assert_awaited()
    # resolved the vless inbound (id=3) and passed the client through
    assert svc.api.add_client.await_args[0][0] == 3


def test_add_client_api_failure_returns_false(svc):
    svc.api.get_inbounds = AsyncMock(return_value=[{"id": 3, "protocol": "vless"}])
    svc.api.add_client = AsyncMock(return_value=False)
    assert svc.add_client_sync({"email": "u@x", "id": "uuid1"}) is False


def test_remove_client_resolves_uuid_and_deletes(svc):
    inbound = {
        "id": 3, "protocol": "vless",
        "settings": json.dumps({"clients": [{"email": "u@x", "id": "uuid1"}]}),
    }
    svc.api.get_inbounds = AsyncMock(return_value=[inbound])
    svc.api.get_inbound = AsyncMock(return_value=inbound)
    svc.api.del_client = AsyncMock(return_value=True)

    assert svc.remove_client_sync("u@x") is True
    assert tuple(svc.api.del_client.await_args[0]) == (3, "uuid1")


def test_remove_client_not_found(svc):
    inbound = {"id": 3, "protocol": "vless", "settings": json.dumps({"clients": []})}
    svc.api.get_inbounds = AsyncMock(return_value=[inbound])
    svc.api.get_inbound = AsyncMock(return_value=inbound)
    svc.api.del_client = AsyncMock(return_value=True)
    assert svc.remove_client_sync("missing@x") is False
    svc.api.del_client.assert_not_awaited()


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
