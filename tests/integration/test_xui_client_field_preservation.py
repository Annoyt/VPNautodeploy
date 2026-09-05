"""Per-inbound client fields must survive a read-modify-write update.

The 2026-09-01 incident: the monthly quota-reset job pushed ~162 client
updates through the panel API at 00:00 UTC.
``XUIService._update_client_fields_async`` read each live record from the
SHADOWSOCKS inbound (it is first in ``lookup_order`` because it is the
only one carrying the per-user SS-2022 password) and then handed that one
body to ``update_client``, which rewrites the client on EVERY inbound it
is attached to. The SS record has no ``flow`` key, so 80 of 81 clients
lost ``xtls-rprx-vision`` on the VLESS-Reality inbound. xray refused them
("Unknown account type: x"), inbound-443 accepted zero connections, and
the outage ran for four days.

Why 2050 passing tests missed it: every test of this path
(tests/unit/test_xui_api_mode.py) stubs ``api.get_inbound`` with a single
``AsyncMock`` that returns the SAME inbound dict for every id. "The SS
record differs from the Reality record" cannot happen by construction —
the identical blind spot that
tests/integration/test_web_actions_integration.py was written to close
for the dashboard ("a mock returns the same object everywhere, so saving
a stale row cannot happen").

So these tests do not mock the panel; they run the real XUIService
against ``FakePanel``, a behavioural stand-in that keeps per-inbound
settings JSON and applies an update the way the fork does.
"""

import json
import sqlite3
from unittest.mock import Mock, patch

import pytest

from bot.services.xui_service import XUIService


REALITY_ID, WS_ID, SS_ID, WS2_ID = 1, 4, 5, 6
FLOW = 'xtls-rprx-vision'
SS_PASSWORD = 'CBPHnpCf/f9uH9L4vzWtIw=='


class FakePanel:
    """In-memory stand-in for the 3x-ui v3.4.0 fork's client API.

    Models the two behaviours that made the incident possible:

    * every inbound keeps its OWN settings JSON, so a per-protocol field
      exists on one inbound and not on the others (``flow`` only on
      VLESS-Reality, ``password``/``method`` only on Shadowsocks);
    * ``update_client`` takes ONE flat body and rebuilds the client on
      every inbound in ``inboundIds`` from it. The fork does this as
      delete + re-add ("Client edited on local" → "Old client deleted on
      local"), so a key the body omits comes back as the zero value —
      an omission is a WRITE, not a no-op.
    """

    def __init__(self, inbounds: dict):
        self.inbounds = {
            int(i): [dict(c) for c in clients]
            for i, clients in inbounds.items()
        }
        self.update_calls = []      # [(email, body, inbound_ids)]
        self.reset_calls = []
        self.last_add_error = ''

    # --- the slice of XUIAPIClient that XUIService actually calls ---

    async def get_inbound(self, inbound_id):
        clients = self.inbounds.get(int(inbound_id))
        if clients is None:
            return None
        return {
            'id': int(inbound_id),
            'settings': json.dumps({'clients': clients}),
        }

    async def get_client_traffic(self, email):
        for clients in self.inbounds.values():
            for c in clients:
                if c.get('email') == email:
                    return {
                        'email': email, 'up': 0, 'down': 0,
                        'total': int(c.get('totalGB') or 0),
                        'enable': bool(c.get('enable', True)),
                        'expiry_time': int(c.get('expiryTime') or 0),
                    }
        return None

    async def update_client(self, email, client_config, inbound_ids, **kw):
        body = dict(client_config)
        body.pop('inboundIds', None)
        self.update_calls.append((email, body, [int(i) for i in inbound_ids]))
        for iid in inbound_ids:
            clients = self.inbounds.setdefault(int(iid), [])
            for idx, c in enumerate(clients):
                if c.get('email') == email:
                    clients[idx] = dict(body)   # zero-valued by omission
                    break
        return True

    async def reset_client_traffic(self, email):
        self.reset_calls.append(email)
        return True

    async def add_client(self, inbound_ids, client_config):
        for iid in inbound_ids:
            self.inbounds.setdefault(int(iid), []).append(dict(client_config))
        return True

    async def del_client_by_email(self, email):
        for clients in self.inbounds.values():
            clients[:] = [c for c in clients if c.get('email') != email]
        return True

    # --- assertions helper ---

    def client_on(self, inbound_id, email):
        for c in self.inbounds.get(int(inbound_id), []):
            if c.get('email') == email:
                return c
        return None


class _Cfg:
    """Entry-node shape: API configured, no usable local x-ui.db."""
    XUI_API_URL = 'http://panel.invalid:2026'
    XUI_USERNAME = 'admin'
    XUI_PASSWORD = 'pw'
    XUI_BASE_PATH = '/this_is_fine'
    XUI_API_PATH = '/this_is_fine/panel/api/inbounds'
    XUI_DB_PATH = '/nonexistent/x-ui.db'
    INBOUND_ID = REALITY_ID
    WS_INBOUND_ID = WS_ID
    SS_INBOUND_ID = SS_ID
    WS2_INBOUND_ID = WS2_ID
    SS_USER_SALT = ''
    DEMO_TRAFFIC_GB = 10
    PAID_TRAFFIC_GB = 100


def _panel_records(email, uuid):
    """One user as the panel really stores them across four inbounds."""
    common = {
        'email': email, 'id': uuid, 'limitIp': 1,
        'totalGB': 10 * 1024 ** 3, 'expiryTime': 1756684800000,
        'enable': True, 'subId': 'sub_' + uuid, 'comment': '',
    }
    reality = dict(common, flow=FLOW)          # flow ONLY here
    ss = dict(common, password=SS_PASSWORD)    # password ONLY here
    ws = dict(common)                          # vmess mirrors: neither
    return reality, ws, ss, dict(ws)


def _panel_with(*users):
    """users: iterable of (email, uuid)."""
    by_inbound = {REALITY_ID: [], WS_ID: [], SS_ID: [], WS2_ID: []}
    for email, uuid in users:
        reality, ws, ss, ws2 = _panel_records(email, uuid)
        by_inbound[REALITY_ID].append(reality)
        by_inbound[WS_ID].append(ws)
        by_inbound[SS_ID].append(ss)
        by_inbound[WS2_ID].append(ws2)
    return FakePanel(by_inbound)


def _service(panel):
    svc = XUIService(_Cfg())
    assert svc.db is None, 'must be API-only mode, like entry'
    svc.api = panel
    return svc


EMAIL = 'user_bob_42@nekovo.ru'
UUID = 'ba3d0f5e-1111-4222-8333-444455556666'


# ===== (a) the read-modify-write itself =====

class TestUpdateFieldsPreservesPerInboundFields:

    @pytest.fixture
    def panel(self):
        return _panel_with((EMAIL, UUID))

    @pytest.fixture
    def svc(self, panel):
        return _service(panel)

    def test_flow_survives_when_record_is_read_from_ss_inbound(self, svc, panel):
        """THE regression. The record is read from Shadowsocks (which has
        no flow) and written to Reality (which needs it)."""
        # Sanity: the fixture reproduces the real asymmetry.
        assert 'flow' not in panel.client_on(SS_ID, EMAIL)
        assert panel.client_on(REALITY_ID, EMAIL)['flow'] == FLOW

        assert svc.sync_client_settings_sync(EMAIL, {'enable': True}) is True

        _email, body, inbound_ids = panel.update_calls[-1]
        assert body['flow'] == FLOW, (
            'update body must carry the live flow — the panel rebuilds the '
            'client from it, so a missing flow strips xtls-rprx-vision'
        )
        assert body['password'] == SS_PASSWORD, (
            'the SS-2022 password must survive too — that is why the SS '
            'inbound is read first in the first place'
        )
        assert set(inbound_ids) == {REALITY_ID, WS_ID, SS_ID, WS2_ID}
        assert panel.client_on(REALITY_ID, EMAIL)['flow'] == FLOW

    def test_ss_password_read_first_still_holds(self, svc, panel):
        """The merge must not regress the reason SS is read first."""
        assert svc.sync_client_settings_sync(EMAIL, {'totalGB': 5}) is True
        assert panel.client_on(SS_ID, EMAIL)['password'] == SS_PASSWORD

    def test_repeated_updates_do_not_erode_flow(self, svc, panel):
        """A fix that reads its own damage back would pass one update and
        fail the second. The quota job runs every month; renew issues two
        writes per user on the re-provision path."""
        for _ in range(3):
            assert svc.sync_client_settings_sync(EMAIL, {'enable': True}) is True
            assert panel.client_on(REALITY_ID, EMAIL)['flow'] == FLOW
        assert panel.client_on(SS_ID, EMAIL)['password'] == SS_PASSWORD

    def test_renew_client_round_trips_flow(self, svc, panel):
        """renew_client_sync is the demo half of the monthly job."""
        assert svc.renew_client_sync(
            EMAIL, 1759276800000, total_bytes=10 * 1024 ** 3) is True

        _email, body, _ids = panel.update_calls[-1]
        assert body['flow'] == FLOW
        assert body['expiryTime'] == 1759276800000
        assert body['totalGB'] == 10 * 1024 ** 3
        assert panel.client_on(REALITY_ID, EMAIL)['flow'] == FLOW
        assert panel.reset_calls == [EMAIL]

    def test_set_client_comment_round_trips_flow(self, svc, panel):
        """The third read-modify-write caller — same helper, same risk."""
        assert svc.set_client_comment_sync(EMAIL, 'bob@gmail.com') is True
        assert panel.client_on(REALITY_ID, EMAIL)['flow'] == FLOW
        assert panel.client_on(REALITY_ID, EMAIL)['comment'] == 'bob@gmail.com'

    def test_flow_is_not_invented_for_a_client_off_the_reality_inbound(self):
        """Scope the self-heal: a client that does NOT live on the
        VLESS-Reality inbound must not acquire a flow it never had."""
        panel = FakePanel({
            SS_ID: [{'email': EMAIL, 'id': UUID, 'password': SS_PASSWORD,
                     'totalGB': 1}],
        })
        svc = _service(panel)

        assert svc.sync_client_settings_sync(EMAIL, {'enable': True}) is True
        _email, body, _ids = panel.update_calls[-1]
        assert not body.get('flow')

    def test_flow_is_restored_when_it_is_gone_everywhere(self):
        """The state the outage actually left behind: the client is on
        the Reality inbound but no inbound holds a flow any more.
        Merging cannot recover it, and refusing the write is worse than
        useless — the monthly job's fallback is add_client, which is
        delete+re-add and ZEROES accumulated traffic. Restore the
        default instead: a Reality client without vision cannot connect,
        so preserving the blank preserves a broken client."""
        panel = FakePanel({
            REALITY_ID: [{'email': EMAIL, 'id': UUID, 'totalGB': 1}],
            SS_ID: [{'email': EMAIL, 'id': UUID, 'password': SS_PASSWORD,
                     'totalGB': 1}],
        })
        svc = _service(panel)

        assert svc.sync_client_settings_sync(EMAIL, {'enable': True}) is True
        _email, body, _ids = panel.update_calls[-1]
        assert body['flow'] == FLOW
        assert body['password'] == SS_PASSWORD
        assert panel.client_on(REALITY_ID, EMAIL)['flow'] == FLOW

    def test_missing_client_is_still_false(self, svc, panel):
        """Merging across inbounds must not turn 'not found' into a write."""
        assert svc.sync_client_settings_sync('ghost@x', {'enable': True}) is False
        assert panel.update_calls == []


# ===== (c) the exact caller that wiped 81 clients =====

class TestMonthlyQuotaResetRoundTripsFlow:
    """The monthly job, end to end, against the fake panel.

    Both halves funnel into ``_update_client_fields_async``:
    demo → ``renew_client_sync``, paid → ``sync_client_settings_sync``.
    The existing coverage
    (tests/unit/test_notifications_helpers.py::TestDemoResetReprovisionFallback)
    passes ``xui = Mock()``, so the read-modify-write never executes at
    all — that suite could not have caught this and still can't.
    """

    def _bot_db(self, tmp_path, rows):
        db_path = str(tmp_path / 'bot.db')
        conn = sqlite3.connect(db_path)
        conn.execute(
            'CREATE TABLE users (chat_id TEXT, email TEXT, uuid TEXT, '
            'status TEXT, subscription_expiry TEXT, traffic_up REAL, '
            'traffic_down REAL, last_traffic_update TEXT)'
        )
        conn.executemany(
            'INSERT INTO users (chat_id, email, uuid, status, '
            'subscription_expiry) VALUES (?, ?, ?, ?, ?)', rows)
        conn.commit()
        conn.close()
        return db_path

    def _run_job(self, tmp_path, rows, panel):
        from bot.services.notifications import NotificationService

        db_path = self._bot_db(tmp_path, rows)
        config = _Cfg()
        config.DB_PATH = db_path
        svc = NotificationService(Mock(), Mock(), config)
        service = _service(panel)
        with patch('bot.services.xui_service.XUIService', return_value=service):
            svc._reset_demo_quota_sync()
        return svc

    def test_demo_and_paid_reset_keep_flow_on_every_client(self, tmp_path):
        users = [(f'user_{i}@nekovo.ru', f'uuid-{i}') for i in range(6)]
        panel = _panel_with(*users)
        rows = [
            (str(i), email, uuid,
             'demo' if i % 2 == 0 else 'paid',
             None if i % 2 == 0 else '2099-01-01T00:00:00')
            for i, (email, uuid) in enumerate(users)
        ]

        self._run_job(tmp_path, rows, panel)

        # Every update the job issued carried the flow ...
        assert panel.update_calls, 'the job must actually have written'
        for email, body, _ids in panel.update_calls:
            assert body.get('flow') == FLOW, f'{email} lost flow in transit'
        # ... and the panel still holds it for all 6 (the incident was
        # 80 of 81 — a one-client test cannot show the blast radius).
        for email, _uuid in users:
            assert panel.client_on(REALITY_ID, email)['flow'] == FLOW
            assert panel.client_on(SS_ID, email)['password'] == SS_PASSWORD

    def test_reality_inbound_stays_usable_after_the_job(self, tmp_path):
        """The operational assertion, stated the way xray sees it: every
        client on inbound 1 must have a non-empty flow, or xray drops the
        whole user list with 'Unknown account type: x'."""
        users = [(f'user_{i}@nekovo.ru', f'uuid-{i}') for i in range(6)]
        panel = _panel_with(*users)
        rows = [(str(i), e, u, 'demo', None)
                for i, (e, u) in enumerate(users)]

        self._run_job(tmp_path, rows, panel)

        flowless = [c['email'] for c in panel.inbounds[REALITY_ID]
                    if not c.get('flow')]
        assert flowless == []
