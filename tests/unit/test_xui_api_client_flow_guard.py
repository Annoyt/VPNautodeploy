"""The wire format must never blank a per-protocol field.

Second half of the 2026-09-01 fix. ``XUIService`` now merges the client
record across inbounds so the body it hands down carries the live
``flow``; this file pins the layer below it, so a future caller that
rebuilds a body by hand cannot repeat the outage.

Two distinct paths, two distinct invariants:

* ``_to_v34_client`` (the ADD path) used to emit ``"flow": ""`` for any
  config without a flow. On the Reality inbound that is a write, not a
  default: it creates a client xray will reject.
* ``update_client`` (the UPDATE path — the one that actually fired in
  the incident) does NOT go through ``_to_v34_client`` at all; it ships
  ``dict(client_config)`` raw, so an absent ``flow`` key is zero-valued
  by the panel's Go decoder with exactly the same result. It now fails
  closed instead.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from bot.config.constants import DEFAULT_FLOW
from bot.services.xui_api.client import XUIAPIClient, XUIClientConfig


def _async_cm(response):
    cm = MagicMock()
    cm.__aenter__ = AsyncMock(return_value=response)
    cm.__aexit__ = AsyncMock(return_value=False)
    return cm


def _ok_response():
    r = MagicMock()
    r.status = 200
    r.json = AsyncMock(return_value={"success": True})
    return r


@pytest.fixture
def client():
    c = XUIAPIClient(XUIClientConfig(base_url="http://test:2026"))
    c._csrf_token = "tok"
    return c


@pytest.fixture
def session():
    s = MagicMock()
    s.post = MagicMock(return_value=_async_cm(_ok_response()))
    return s


class TestToV34ClientFlow:
    """ADD path."""

    def test_preserves_a_real_flow(self):
        out = XUIAPIClient._to_v34_client(
            {"email": "u@x", "id": "uuid1", "flow": DEFAULT_FLOW})
        assert out["flow"] == DEFAULT_FLOW

    def test_never_emits_an_empty_flow(self):
        """``"flow": ""`` is indistinguishable from a deliberate 'clear
        it' instruction on the panel side. If we don't know the flow we
        must say nothing, not say 'none'."""
        for cfg in (
            {"email": "u@x", "id": "uuid1"},               # key absent
            {"email": "u@x", "id": "uuid1", "flow": ""},   # key blank
            {"email": "u@x", "id": "uuid1", "flow": None},
            {"email": "u@x", "id": "uuid1", "flow": "   "},
        ):
            out = XUIAPIClient._to_v34_client(cfg)
            assert "flow" not in out, f"emitted a blank flow for {cfg}"

    def test_other_fields_are_untouched_by_the_flow_rule(self):
        out = XUIAPIClient._to_v34_client({
            "email": "u@x", "id": "uuid1", "flow": DEFAULT_FLOW,
            "password": "sspw", "totalGB": 500, "expiryTime": 111,
            "limitIp": 1, "subId": "sb",
        })
        assert out["password"] == "sspw"
        assert out["totalGB"] == out["total_gb"] == 500
        assert out["expiryTime"] == out["expiry_time"] == 111
        assert out["id"] == "uuid1"


class TestUpdateClientRefusesToBlankFlow:
    """UPDATE path — the one that fired on 2026-09-01."""

    @pytest.mark.asyncio
    async def test_ships_the_flow_it_was_given(self, client, session):
        with patch.object(client, "_get_session", return_value=session), \
                patch.object(client, "_ensure_auth", new=AsyncMock(return_value=True)):
            ok = await client.update_client(
                "u@x",
                {"email": "u@x", "id": "uuid1", "flow": DEFAULT_FLOW,
                 "totalGB": 500},
                [1, 4, 5, 6],
            )

        assert ok is True
        body = session.post.call_args[1]["json"]
        assert body["flow"] == DEFAULT_FLOW
        assert body["inboundIds"] == [1, 4, 5, 6]

    @pytest.mark.asyncio
    async def test_refuses_a_body_with_no_flow(self, client, session):
        """A body assembled from the Shadowsocks inbound. The panel would
        rebuild the client from it and strip xtls-rprx-vision from
        inbound-443 — so the request must never leave the process."""
        with patch.object(client, "_get_session", return_value=session), \
                patch.object(client, "_ensure_auth", new=AsyncMock(return_value=True)):
            ok = await client.update_client(
                "u@x",
                {"email": "u@x", "id": "uuid1", "password": "sspw",
                 "totalGB": 500},
                [1, 4, 5, 6],
            )

        assert ok is False
        session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_refuses_a_body_with_a_blank_flow(self, client, session):
        with patch.object(client, "_get_session", return_value=session), \
                patch.object(client, "_ensure_auth", new=AsyncMock(return_value=True)):
            ok = await client.update_client(
                "u@x", {"email": "u@x", "id": "uuid1", "flow": ""}, 1)

        assert ok is False
        session.post.assert_not_called()

    @pytest.mark.asyncio
    async def test_caller_can_opt_out_for_flowless_clients(self, client, session):
        """A genuinely flow-less client (plain VLESS / SS-only inbound)
        must still be manageable — but only when the caller says so out
        loud, never by accident."""
        with patch.object(client, "_get_session", return_value=session), \
                patch.object(client, "_ensure_auth", new=AsyncMock(return_value=True)):
            ok = await client.update_client(
                "u@x", {"email": "u@x", "id": "uuid1"}, 1,
                required_fields=(),
            )

        assert ok is True
        session.post.assert_called_once()

    @pytest.mark.asyncio
    async def test_refusal_is_loud(self, client, session, caplog):
        """Silent failure is how this cost four days. The refusal must
        leave something greppable in the log."""
        import logging
        with caplog.at_level(logging.ERROR), \
                patch.object(client, "_get_session", return_value=session), \
                patch.object(client, "_ensure_auth", new=AsyncMock(return_value=True)):
            await client.update_client("u@x", {"email": "u@x"}, 1)

        assert any("flow" in r.message for r in caplog.records)
