"""GET /api/admin/cascade_order — the ``auto`` block.

Why
---
The dashboard editor shows the operator's saved order; since
IMPROVEMENT_PLAN A1 the DPIMonitor may be handing users a different one
(auto-demoted protocols at the tail). Without ``auto`` in the payload
the editor would silently lie about what users receive. The block is
read from the app_settings keys the monitor writes (``cascade_auto``,
``dpi_monitor_state``, ``dpi_monitor_enabled``) and must degrade to
"nothing auto-demoted" on missing or hand-mangled JSON — the endpoint
is what the operator opens during an incident, a 500 there is the
worst outcome. ``config`` / ``order`` stay the RAW operator order (the
editor saves them back verbatim) and SET is untouched.

Same style as test_web_server.py: WebAppServer over a spec'd mock
Database whose ``get_setting`` answers per key.
"""

import json
from unittest.mock import Mock, patch

import pytest

from bot.config import Settings
from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK


DEFAULT = list(MK.DEFAULT_CASCADE_ORDER)
AUTO = {
    'global': {'ws': {'since': '2026-09-05T14:03:00', 'reason': 'пробы 0/30 за 45 мин (DARK)'}},
    'asn': {'AS31133': {'reality': {'since': '2026-09-05T13:40:00',
                                    'reason': 'hsfail 879 / conn 0 за 2 ч'}}},
}
STATE = {'targets': {'global:ws': {'bad': 2, 'good': 0}},
         'last_run': '2026-09-05T14:10:00', 'runs': 41}


@pytest.fixture
def settings():
    """``settings[key]`` is what the mock db returns for get_setting(key)."""
    return {}


@pytest.fixture
def mock_config():
    config = Mock(spec=Settings)
    config.BOT_TOKEN = 'test_token_12345'
    config.is_admin = Mock(return_value=True)
    return config


@pytest.fixture
def mock_db(settings):
    db = Mock(spec=Database)
    db.get_setting = Mock(side_effect=lambda key, default=None: settings.get(key, default))
    return db


@pytest.fixture
def server(mock_config, mock_db):
    with patch('bot.core.web_server.XUIService') as mock_xui:
        mock_xui.return_value = Mock()
        srv = WebAppServer(mock_config, mock_db)
        srv.xui = mock_xui.return_value
        return srv


async def get(server):
    request = Mock()
    request.query = {'admin_token': 'x'}
    request.remote = '127.0.0.1'
    with patch.object(server, '_validate_admin', return_value={'id': 1652899}):
        response = await server.handle_admin_cascade_order_get(request)
    return response.status, json.loads(response.text)


class TestAutoBlock:

    @pytest.mark.asyncio
    async def test_absent_keys_give_the_empty_enabled_shape(self, server):
        status, data = await get(server)
        assert status == 200
        assert data['auto'] == {'enabled': True, 'global': {}, 'asn': {},
                                'last_run': None}
        # the pre-existing payload is intact
        assert data['order'] == DEFAULT
        assert [c['name'] for c in data['config']] == DEFAULT
        assert {c['name'] for c in data['catalog']} == set(MK.PROTOCOL_METHOD_MAP)
        assert data['default'] == DEFAULT

    @pytest.mark.asyncio
    async def test_keys_are_reflected(self, server, settings):
        settings['cascade_auto'] = json.dumps(AUTO)
        settings['dpi_monitor_state'] = json.dumps(STATE)
        status, data = await get(server)
        assert status == 200
        assert data['auto']['global'] == AUTO['global']
        assert data['auto']['asn'] == AUTO['asn']
        assert data['auto']['last_run'] == '2026-09-05T14:10:00'
        assert data['auto']['enabled'] is True

    @pytest.mark.asyncio
    async def test_order_stays_the_raw_operator_order(self, server, settings):
        """The editor edits the operator's list; the demotion is an
        overlay users see, not a rewrite of the saved setting."""
        settings['cascade_auto'] = json.dumps(AUTO)
        _, data = await get(server)
        assert data['order'] == DEFAULT           # ws NOT moved to the tail here
        assert 'ws' in data['auto']['global']

    @pytest.mark.asyncio
    async def test_disabled_flag(self, server, settings):
        settings['dpi_monitor_enabled'] = '0'
        _, data = await get(server)
        assert data['auto']['enabled'] is False
        settings['dpi_monitor_enabled'] = '1'
        _, data = await get(server)
        assert data['auto']['enabled'] is True

    @pytest.mark.asyncio
    async def test_env_default_when_flag_never_written(self, server, settings, mock_config):
        mock_config.DPI_MONITOR_ENABLED = '0'
        _, data = await get(server)
        assert data['auto']['enabled'] is False
        settings['dpi_monitor_enabled'] = '1'    # the operator's toggle wins over env
        _, data = await get(server)
        assert data['auto']['enabled'] is True

    @pytest.mark.asyncio
    async def test_bad_json_degrades_to_empty_not_500(self, server, settings):
        settings['cascade_auto'] = '{"global": {"ws": '      # truncated by hand
        settings['dpi_monitor_state'] = 'not json at all'
        status, data = await get(server)
        assert status == 200
        assert data['auto']['global'] == {}
        assert data['auto']['asn'] == {}
        assert data['auto']['last_run'] is None
        assert data['order'] == DEFAULT

    @pytest.mark.asyncio
    async def test_wrong_json_types_degrade_to_empty(self, server, settings):
        settings['cascade_auto'] = json.dumps(['ws', 'reality'])       # list, not object
        settings['dpi_monitor_state'] = json.dumps({'last_run': 12345})  # not a string
        status, data = await get(server)
        assert status == 200
        assert data['auto']['global'] == {} and data['auto']['asn'] == {}
        assert data['auto']['last_run'] is None

    @pytest.mark.asyncio
    async def test_partial_shapes_are_normalised(self, server, settings):
        settings['cascade_auto'] = json.dumps({
            'global': {'ws': 'junk'},                    # meta not a dict → {}
            'asn': {'as31133': {'reality': {'since': 's'}}, 'AS1': {}},
        })
        _, data = await get(server)
        assert data['auto']['global'] == {'ws': {}}
        assert data['auto']['asn'] == {'AS31133': {'reality': {'since': 's'}}}

    @pytest.mark.asyncio
    async def test_get_setting_raising_does_not_500(self, server, mock_db):
        """Only the auto keys are read defensively; the operator order
        read is the pre-existing path and is not in scope here, so the
        failure is injected for the auto keys alone."""
        def flaky(key, default=None):
            if key in ('cascade_auto', 'dpi_monitor_state', 'dpi_monitor_enabled'):
                raise RuntimeError('db locked')
            return default
        mock_db.get_setting = Mock(side_effect=flaky)
        status, data = await get(server)
        assert status == 200
        assert data['auto'] == {'enabled': True, 'global': {}, 'asn': {},
                                'last_run': None}

    @pytest.mark.asyncio
    async def test_unauthorized_is_still_401(self, server):
        request = Mock()
        request.query = {}
        request.remote = '127.0.0.1'
        with patch.object(server, '_validate_admin', return_value=None):
            response = await server.handle_admin_cascade_order_get(request)
        assert response.status == 401
