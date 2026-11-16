"""The SSH emergency path gets a probe (IMPROVEMENT_PLAN F, E4; 2026-11-15).

Since PR #58 the healthcheck watches the ssh-exit container and its user
sync on exit, but nothing checked that a user gets in and forwards traffic.
Now:

* the probe set: ``ssh`` joins HealthChecker's probes wherever the bot hands
  the path out (``ssh_exit.enabled``), through the mihomo sidecar (18088);
* the sidecar config: one listener bound to the very proxy a payer's
  emergency Clash profile carries, for the probe user — whom the exit's
  user feed lists;
* the prober: rows in outbound_health and outbound_bulk. A sidecar without
  the listener writes nothing, so the rollout pages nothing; a listener
  whose ssh login fails writes DARK rows (mihomo answers a CONNECT before it
  dials), so a dead path pages;
* the readers: the pager pages it under its own key and titles, as a warn
  (the dashboard unless ALERT_TG_MIN_SEVERITY=warn), and never counts it in
  "every protocol dark at once"; DPIMonitor never measures or demotes it;
  /protocols and the healthcheck script show it.

Level 2: real sqlite (Database in tmp_path), the real checks; the prober runs
against local stand-ins for the sidecar on 127.0.0.1. The stand at the bottom
(``requires_docker``, offline) runs the generated sidecar config in mihomo
1.19.33 against the sshd of scripts/setup_ssh_exit.sh and the real prober.
"""

import asyncio
import importlib.util
import json
import os
import socket
import subprocess
import time
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp import web

from bot.core.database import Database
from bot.models.user import User
from bot.services import probe_rules as pr
from bot.services import ssh_exit
from bot.services.agent_client import _detect_skill_domains
from bot.services.alert_manager import (AlertManager, _protocol_agent_prompt,
                                        build_default_checks)
from bot.services.dpi_monitor import DPIMonitor
from bot.services.health_checker import HealthChecker
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

PROBE_UUID = '5f0c2a7e-9b1d-4e3f-8a6b-1c2d3e4f5a60'
TARGETS = list(pr.PROBE_TARGETS)
CASCADE = ('reality', 'hy2', 'ws', 'stls')
SSH = dict(SSH_EXIT_HOST='198.51.100.4', SSH_EXIT_PORT='22022',
           SSH_HOST_KEY='ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIStandHostKey',
           SSH_USER_SECRET='s3cret')
REPO = os.path.join(os.path.dirname(__file__), '..', '..')


def _config(**over):
    cfg = dict(
        BOT_TOKEN='t', WEBAPP_URL='https://dash.example.com',
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='reality-pbk', SNI_VALUE='www.bing.com', SID_VALUE='0123456789abcdef',
        HY2_HOST='203.0.113.20', HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='', HY2T_PORT='',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api', WS_SNI='cdn.example.com',
        WS2_HOST='', STLS_HOST='203.0.113.20', STLS_PORT=443, STLS_SNI='www.microsoft.com',
        STLS_VERSION=3, STLS_PASSWORD='stls-pw', SS_METHOD='2022-blake3-aes-128-gcm',
        SS_SERVER_PASSWORD='srv-pw', SS_USER_SALT='salt', SUB_MIRROR_URLS='',
        FALLBACK_NODE_HOST='', FALLBACK_NODE_PBK='', TLS_FINGERPRINTS='',
        XHR_SNI='', XHR_PATH='/xhr', XHR_PUBLIC_KEY='', XHR_SID='', XHR_PORT=0,
        XHR_MAX_CONNECTIONS=3,
        SSH_EXIT_HOST='', SSH_EXIT_PORT='22022', SSH_HOST_KEY='', SSH_USER_SECRET='',
        PROBE_PROXY_HOST='probe-proxy', PROBE_PROXY_MIHOMO_HOST='probe-proxy-mihomo',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def add_probe_user(db, *, status='paid', expiry='2099-12-31T23:59:59', uuid=PROBE_UUID):
    """``probe_internal`` as prod has it (paid until 2099), read back the way
    the generators' main() reads it."""
    db._users.save(User(chat_id='probe_internal', username='probe_internal', status=status,
                        uuid=uuid, email='probe_internal@nekovo.ru', quota_gb=0.0, lang='ru',
                        subscription_expiry=expiry))
    return db.get_user('probe_internal')


def _load(name):
    path = os.path.join(REPO, 'scripts', name)
    spec = importlib.util.spec_from_file_location(name.replace('.py', '') + '_sshprobe', path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def seed_window(db, tag, ok_per_run, *, runs=3, alive_fail=True, minutes_ago=5):
    """``runs`` runs of ten rows, newest first — HealthChecker's shape.
    Failures carry a latency when ``alive_fail``, none otherwise."""
    now = datetime.utcnow()
    with db._connect() as conn:
        for run in range(runs):
            ts = (now - timedelta(minutes=minutes_ago + run * 15)).isoformat()
            for i, domain in enumerate(TARGETS):
                ok = i < ok_per_run
                conn.execute(
                    "INSERT INTO outbound_health (outbound_tag, target_domain, status,"
                    " latency_ms, error_msg, ts) VALUES (?,?,?,?,?,?)",
                    (tag, domain, 'ok' if ok else 'error',
                     120 if ok else (900 if alive_fail else None),
                     None if ok else 'x', ts))
        conn.commit()


def seed_bulk(db, tag, statuses, *, minutes_ago=5):
    """Bulk rows, newest first."""
    now = datetime.utcnow()
    with db._connect() as conn:
        for i, status in enumerate(statuses):
            conn.execute(
                "INSERT INTO outbound_bulk (ts, outbound_tag, status, bytes, duration_ms,"
                " error_msg) VALUES (?,?,?,?,?,?)",
                ((now - timedelta(minutes=minutes_ago + i * 15)).isoformat(), tag, status,
                 HealthChecker.BULK_BYTES if status == 'ok' else 20 * 1024, 900, None))
        conn.commit()


def pager_run(db):
    """The real pager check on this db: ``({key: Alert}, judged)``."""
    cfg = Mock()
    cfg.DB_PATH = db.db_path
    check = next(c for c in build_default_checks(cfg, Mock())
                 if c.__name__ == 'check_protocol_probe_down')
    prefix, alerts, judged = check()
    assert prefix == 'protocol_down:'
    return {a.key: a for a in alerts}, judged


def pager(db):
    return pager_run(db)[0]


def health_rows(db):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT outbound_tag, target_domain, status, latency_ms FROM outbound_health "
            "ORDER BY id").fetchall()]


def bulk_rows(db):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT outbound_tag, status, bytes FROM outbound_bulk ORDER BY id").fetchall()]


# ------------------------------------------------------------ probe set ------

class TestProbeSet:

    def test_joins_wherever_the_bot_hands_the_path_out(self):
        assert HealthChecker.probe_ports_for(_config(**SSH))['ssh'] == 18088
        assert 'ssh' in HealthChecker.probe_tags_for(_config(**SSH))
        assert 'ssh' not in HealthChecker.probe_ports_for(_config())
        for key in ('SSH_EXIT_HOST', 'SSH_USER_SECRET', 'SSH_HOST_KEY'):
            # the emergency profiles' own gate: any of the three missing = off
            off = _config(**{**SSH, key: '  '})
            assert ssh_exit.enabled(off) is False
            assert 'ssh' not in HealthChecker.probe_ports_for(off)
        assert 'ssh' not in HealthChecker.probe_ports_for(Mock())

    def test_through_the_mihomo_sidecar(self):
        hc = HealthChecker(Mock(), _config(**SSH))
        assert hc._proxy_url('ssh') == 'http://probe-proxy-mihomo:18088'
        assert hc._proxy_url('reality') == 'http://probe-proxy:18081'
        hc = HealthChecker(Mock(), _config(**SSH, PROBE_PROXY_MIHOMO_HOST='10.9.9.9'))
        assert hc._proxy_url('ssh') == 'http://10.9.9.9:18088'
        assert HealthChecker(Mock(), _config())._proxy_url('ssh') is None

    def test_one_definition_of_what_is_no_cascade_protocol(self):
        from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK
        assert pr.NON_CASCADE_TAGS == (pr.RESERVE_TAG, pr.SSH_TAG, pr.SSH_ENTRY_TAG) == (
            'de', 'ssh', 'ssh-entry')
        assert HealthChecker.SSH_TAG == pr.SSH_TAG
        assert HealthChecker.SSH_ENTRY_TAG == pr.SSH_ENTRY_TAG
        assert not set(pr.NON_CASCADE_TAGS) & set(MK.PROTOCOL_METHOD_MAP)


# ------------------------------------------------------------ sidecar config -

class TestSidecarConfig:

    def test_the_listener_carries_what_a_payers_emergency_profile_carries(self, db):
        """Built by the bot's own Clash builder: the proxy is byte for byte
        the ssh server of the probe user's emergency profile."""
        user = add_probe_user(db)
        cfg = _config(**SSH)
        mih = _load('gen_probe_config_mihomo.py').build_config(cfg, user)
        profile = json.loads(SubscriptionService(cfg).build_clash_config(
            user, CASCADE, lockdown=True))
        expected = next(p for p in profile['proxies'] if p['type'] == 'ssh')
        assert mih['proxies'] == [expected]
        assert mih['listeners'] == [{'name': 'probe-in-ssh', 'type': 'http',
                                     'listen': '0.0.0.0', 'port': 18088,
                                     'proxy': expected['name']}]
        assert (expected['server'], expected['port']) == ('198.51.100.4', 22022)
        assert expected['host-key'] == [SSH['SSH_HOST_KEY']]
        assert expected['username'] == ssh_exit.username_for(PROBE_UUID)
        assert expected['password'] == ssh_exit.password_for(cfg, PROBE_UUID)

    def test_the_exits_user_feed_lists_the_probe_user(self, db):
        """ssh-exit takes its users from GET /api/ssh/users: the probe user is
        there, under the name the probe logs in with, and the hash it gets
        verifies the password the probe sends."""
        user = add_probe_user(db)
        cfg = _config(**SSH)
        proxy = _load('gen_probe_config_mihomo.py').build_config(cfg, user)['proxies'][0]
        feed = {u['user']: u['hash'] for u in ssh_exit.users_payload(db, cfg)}
        hashed = feed[proxy['username']]
        assert ssh_exit.sha512_crypt(proxy['password'], hashed.split('$')[2]) == hashed

    def test_xhr_and_ssh_side_by_side(self, db):
        user = add_probe_user(db)
        mih = _load('gen_probe_config_mihomo.py').build_config(
            _config(**SSH, XHR_SNI='dl.google.com'), user)
        assert [(lst['name'], lst['port']) for lst in mih['listeners']] == [
            ('probe-in-xhr', 18087), ('probe-in-ssh', 18088)]
        types = {p['name']: p['type'] for p in mih['proxies']}
        assert [types[lst['proxy']] for lst in mih['listeners']] == ['vless', 'ssh']

    def test_the_reserve_stays_with_the_sing_box_sidecar(self, db):
        """With the DE reserve configured the emergency server list holds it
        BEFORE the ssh server: the listener still binds the ssh one, and the
        reserve keeps its sing-box inbound."""
        user = add_probe_user(db)
        cfg = _config(**SSH, FALLBACK_NODE_HOST='198.51.100.9', FALLBACK_NODE_PBK='de-pbk')
        mih = _load('gen_probe_config_mihomo.py').build_config(cfg, user)
        assert [p['type'] for p in mih['proxies']] == ['ssh']
        assert [lst['name'] for lst in mih['listeners']] == ['probe-in-ssh']
        sing = _load('gen_probe_config.py').build_config(cfg, user)
        assert 'probe-in-de' in {i['tag'] for i in sing['inbounds']}

    @pytest.mark.parametrize('status,expiry', [
        ('paid', '2000-01-01T00:00:00'),       # lapsed: ssh-exit does not list it
        ('demo', None),
    ])
    def test_a_probe_user_who_is_no_payer_gets_no_listener(self, db, status, expiry):
        user = add_probe_user(db, status=status, expiry=expiry)
        lines = []
        mih = _load('gen_probe_config_mihomo.py').build_config(_config(**SSH), user,
                                                               log=lines.append)
        assert mih['listeners'] == [] and mih['proxies'] == []
        assert len(lines) == 1 and lines[0].startswith(
            'skip ssh: the probe user is not an eligible payer')

    def test_nothing_without_the_paths_settings(self, db):
        user = add_probe_user(db)
        lines = []
        mih = _load('gen_probe_config_mihomo.py').build_config(_config(), user,
                                                               log=lines.append)
        assert mih['listeners'] == [] and lines == []

    def test_the_sing_box_sidecar_leaves_it_to_mihomo(self, db):
        user = add_probe_user(db)
        lines = []
        cfg = _load('gen_probe_config.py').build_config(_config(**SSH), user, log=lines.append)
        assert 'probe-in-ssh' not in {i['tag'] for i in cfg['inbounds']}
        assert not any(o['type'] == 'ssh' for o in cfg['outbounds'])
        assert 'skip ssh: probed by the mihomo sidecar' in lines

    def test_main_prints_it_for_the_probe_user(self, db, monkeypatch, capsys):
        add_probe_user(db)
        mod = _load('gen_probe_config_mihomo.py')
        monkeypatch.setattr(mod, 'Settings', lambda: _config(**SSH, DB_PATH=db.db_path))
        monkeypatch.setattr(mod, 'Database', lambda path: db)
        assert mod.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert [lst['name'] for lst in out['listeners']] == ['probe-in-ssh']
        assert out['proxies'][0]['username'] == ssh_exit.username_for(PROBE_UUID)


# ------------------------------------------------------------ prober ---------

class _ForwardProxy:
    """A working sidecar: a plain-http forward proxy on 127.0.0.1 that
    answers every HEAD and serves the bulk path in full."""

    async def handle(self, request):
        if 'bulk' in request.path_qs:
            resp = web.StreamResponse(status=200)
            await resp.prepare(request)
            for _ in range(HealthChecker.BULK_BYTES // 16384):
                await resp.write(b'x' * 16384)
            return resp
        return web.Response(status=200)

    async def __aenter__(self):
        self.runner = web.ServerRunner(web.Server(self.handle))
        await self.runner.setup()
        site = web.TCPSite(self.runner, '127.0.0.1', 0)
        await site.start()
        self.port = site._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        await self.runner.cleanup()


class _DeadTunnel:
    """What mihomo does when the ssh login behind its listener fails: it
    answers a CONNECT with 200 BEFORE it dials, then closes the connection."""

    async def _handle(self, reader, writer):
        try:
            await reader.readuntil(b'\r\n\r\n')
            writer.write(b'HTTP/1.1 200 Connection established\r\n\r\n')
            await writer.drain()
        except Exception:
            pass
        finally:
            writer.close()

    async def __aenter__(self):
        self.server = await asyncio.start_server(self._handle, '127.0.0.1', 0)
        self.port = self.server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *exc):
        self.server.close()
        await self.server.wait_closed()


@pytest.fixture
def unused_tcp_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def ssh_checker(db, port, monkeypatch):
    """The real checker for the ssh tag, its sidecar on 127.0.0.1:``port``
    (the port table is the class's, as in production)."""
    monkeypatch.setattr(HealthChecker, 'SSH_PROBE_PORT', port)
    hc = HealthChecker(db, _config(**SSH, PROBE_PROXY_MIHOMO_HOST='127.0.0.1'))
    hc.TIMEOUT_SEC = 2.0
    hc.BULK_TIMEOUT_SEC = 3.0
    return hc


class TestProber:

    @pytest.mark.asyncio
    async def test_rows_in_both_tables(self, db, monkeypatch):
        async with _ForwardProxy() as sidecar:
            hc = ssh_checker(db, sidecar.port, monkeypatch)
            hc.TARGET_SCHEME = 'http'
            hc.BULK_URL = 'http://bulk.test/bulk?bytes={bytes}'
            assert hc.protocol_tags[-1] == 'ssh'
            res = await hc._check_protocol('ssh')
        assert {r['status'] for r in res.values()} == {'ok'}
        assert [(t, d, s) for t, d, s, _lat in health_rows(db)] == [
            ('ssh', d, 'ok') for d in TARGETS]
        assert bulk_rows(db) == [('ssh', 'ok', HealthChecker.BULK_BYTES)]

    @pytest.mark.asyncio
    async def test_a_sidecar_without_the_listener_writes_nothing(self, db, monkeypatch,
                                                                 unused_tcp_port):
        """The rollout: the bot is deployed, the sidecar still runs a config
        without the listener. No rows at all, so nothing pages."""
        hc = ssh_checker(db, unused_tcp_port, monkeypatch)
        assert await hc._check_protocol('ssh') is None
        assert health_rows(db) == [] and bulk_rows(db) == []

    @pytest.mark.asyncio
    async def test_a_login_that_fails_behind_the_listener_is_dark(self, db, monkeypatch):
        """Not "no listener": the sidecar answers, the tunnel never comes up.
        Dark rows (no latency, never proxy_down), a failed bulk, and three
        such runs page protocol_down:ssh while the cascade is healthy."""
        async with _DeadTunnel() as sidecar:
            hc = ssh_checker(db, sidecar.port, monkeypatch)
            for _ in range(pr.PROBE_RUNS):
                assert await hc._check_protocol('ssh') is not None
        rows = health_rows(db)
        assert len(rows) == pr.PROBE_RUNS * pr.PROBE_DOMAINS
        assert {t for t, _d, _s, _l in rows} == {'ssh'}
        assert all(lat is None and st == 'error' for _t, _d, st, lat in rows)
        assert bulk_rows(db) == [('ssh', 'error', 0)] * pr.PROBE_RUNS
        for tag in CASCADE:
            seed_window(db, tag, 10)
        alerts = pager(db)
        assert list(alerts) == ['protocol_down:ssh']


# ------------------------------------------------------------ the pager ------

class TestPager:

    def test_a_dark_ssh_path_pages_as_itself(self, db):
        for tag in CASCADE:
            seed_window(db, tag, 10)
        seed_window(db, 'ssh', 0, alive_fail=False)
        alerts, judged = pager_run(db)
        assert list(alerts) == ['protocol_down:ssh']
        alert = alerts['protocol_down:ssh']
        assert alert.severity == 'warn' and alert.min_cycles == 2
        assert alert.title.startswith('Аварийный SSH-выход не пускает (лежит ')
        assert 'контейнер ssh-exit' in alert.detail
        assert 'exit_server:ssh-exit, exit_sync:ssh-exit' in alert.detail
        line = alert.detail.rsplit('Сейчас по пробам: ', 1)[1]
        assert line.startswith('hy2 ok 30/30 · reality ok 30/30 · ssh ok 0/30 (живых 0)')
        assert {'protocol_down:ssh', 'protocol_down:ssh:degraded'} <= judged

    def test_its_agent_turn_goes_to_vpn_ops(self, db):
        """The alert rides in the agent prompt: the incident-response and
        code-review markers must not fire (alert_manager._protocol_agent_prompt)."""
        for tag in CASCADE:
            seed_window(db, tag, 10)
        seed_window(db, 'ssh', 0, alive_fail=False)
        domains = _detect_skill_domains(_protocol_agent_prompt(pager(db)['protocol_down:ssh']))
        assert 'vpn-ops' in domains
        assert 'incident-response' not in domains and 'code-review' not in domains

    def test_a_degraded_ssh_path_warns_as_itself(self, db):
        for tag in CASCADE:
            seed_window(db, tag, 10)
        seed_window(db, 'ssh', 2)
        alerts = pager(db)
        assert list(alerts) == ['protocol_down:ssh:degraded']
        alert = alerts['protocol_down:ssh:degraded']
        assert alert.severity == 'warn' and alert.min_cycles == 3
        assert alert.title == 'Аварийный SSH-выход деградировал: ok 6/30'
        assert 'ssh-exit' in alert.detail
        domains = _detect_skill_domains(_protocol_agent_prompt(alert))
        assert 'vpn-ops' in domains and 'code-review' not in domains

    def test_an_answering_ssh_path_heals(self, db):
        for tag in CASCADE + ('ssh',):
            seed_window(db, tag, 10)
        alerts, judged = pager_run(db)
        assert alerts == {}
        assert {'protocol_down:ssh', 'protocol_down:ssh:degraded'} <= judged

    def test_a_thin_ssh_window_has_no_verdict(self, db):
        for tag in CASCADE:
            seed_window(db, tag, 10)
        seed_window(db, 'ssh', 0, runs=1, alive_fail=False)
        alerts, judged = pager_run(db)
        assert alerts == {} and 'protocol_down:ssh' not in judged

    def test_exit_down_is_one_incident_even_with_ssh_alive(self, db):
        """The SSH path reaches exit without entry: answering while every
        cascade protocol is dark it proves nothing about their common path
        (the sing-box sidecar, entry's forwarding)."""
        for tag in CASCADE:
            seed_window(db, tag, 0, alive_fail=False)
        seed_window(db, 'ssh', 10)
        alerts = pager(db)
        assert list(alerts) == ['protocol_down:all']
        assert alerts['protocol_down:all'].title == 'Все протоколы молчат (4 шт.)'

    def test_ssh_is_no_second_cascade_protocol(self, db):
        """One cascade protocol dark with ssh dark too is two paths down, not
        "every protocol dark at once" (that needs two cascade protocols)."""
        seed_window(db, 'reality', 0, alive_fail=False)
        seed_window(db, 'ssh', 0, alive_fail=False)
        assert set(pager(db)) == {'protocol_down:reality', 'protocol_down:ssh'}

    def test_switched_off_by_hand_it_still_proves_nothing(self, db):
        """Someone put ssh into the cascade setting, switched off: its answers
        still do not rule out "every protocol dark" (as for the reserve)."""
        db.set_setting('cascade_protocol_order', json.dumps(
            [{'name': n, 'enabled': True} for n in CASCADE]
            + [{'name': 'ssh', 'enabled': False}]))
        for tag in CASCADE:
            seed_window(db, tag, 0, alive_fail=False)
        seed_window(db, 'ssh', 10)
        assert list(pager(db)) == ['protocol_down:all']


class TestWhereTheAlertGoes:
    """A dead SSH path is a warn (owner, 2026-11-15), like the healthcheck's
    exit_server:ssh-exit: below prod's floor (ALERT_TG_MIN_SEVERITY unset =
    critical) it stays on the dashboard. A real AlertManager tick on the
    pager check; the agent turn is captured, not run."""

    @staticmethod
    def _manager(db, floor):
        cfg = Mock()
        cfg.DB_PATH = db.db_path
        cfg.XUI_DB_PATH = ''
        cfg.TOPIC_AI = 55
        cfg.FORUM_GROUP_ID = -100123
        cfg.SUPER_ADMIN_ID = '1652899'
        cfg.ALERT_TG_MIN_SEVERITY = floor
        bot = Mock()
        mgr = AlertManager(bot, cfg, db=db)
        mgr.register(next(c for c in build_default_checks(cfg, Mock())
                          if c.__name__ == 'check_protocol_probe_down'))
        turns = []
        mgr._kick_protocol_batch = turns.append
        return mgr, bot, turns

    @staticmethod
    def _dark_ssh(db):
        for tag in CASCADE:
            seed_window(db, tag, 10)
        seed_window(db, 'ssh', 0, alive_fail=False)

    def test_below_the_default_floor_it_stays_on_the_dashboard(self, db):
        self._dark_ssh(db)
        mgr, bot, turns = self._manager(db, 'critical')
        mgr.run_once()
        mgr.run_once()                                 # min_cycles=2
        bot.send_message.assert_not_called()           # no topic, no PM
        with db._connect() as conn:
            rows = [tuple(r) for r in conn.execute(
                "SELECT key, severity FROM alert_history").fetchall()]
        assert rows == [('protocol_down:ssh', 'warn')]
        [[(alert, _row_id, post_to_topic)]] = turns    # diagnosed, not posted
        assert alert.key == 'protocol_down:ssh' and post_to_topic is False

    def test_with_the_floor_at_warn_it_reaches_the_topic(self, db):
        self._dark_ssh(db)
        mgr, bot, turns = self._manager(db, 'warn')
        mgr.run_once()
        mgr.run_once()
        bot.send_message.assert_called_once()
        kw = bot.send_message.call_args.kwargs
        assert (kw['chat_id'], kw['message_thread_id']) == (-100123, 55)
        assert kw['text'].startswith('⚠️ <b>Аварийный SSH-выход не пускает (лежит ')
        [[(_alert, _row_id, post_to_topic)]] = turns
        assert post_to_topic is True

    def test_a_failed_topic_post_falls_back_to_no_pm(self, db):
        self._dark_ssh(db)
        mgr, bot, _turns = self._manager(db, 'warn')
        bot.send_message.return_value = None           # Telegram refused it
        mgr.run_once()
        mgr.run_once()
        assert [c.kwargs['chat_id'] for c in bot.send_message.call_args_list] == [-100123]


# ------------------------------------------------------------ DPIMonitor -----

class TestMonitor:

    def test_never_measured_never_demoted(self, db):
        for tag in CASCADE:
            seed_window(db, tag, 10)
        seed_window(db, 'ssh', 0, alive_fail=False)
        mon = DPIMonitor(db, Mock(), None)
        probe = mon.collect_signals()['probe']
        assert 'ssh' not in probe['measured'] and not probe['dark']
        for _ in range(3):
            assert mon.run_once() == []
        assert 'ssh' not in json.dumps(json.loads(db.get_setting('cascade_auto') or '{}'))

    def test_alive_ssh_does_not_break_the_all_dark_guard(self, db):
        for tag in CASCADE:
            seed_window(db, tag, 0, alive_fail=False)
        seed_window(db, 'ssh', 10)
        probe = DPIMonitor(db, Mock(), None).collect_signals()['probe']
        assert probe['all_dark'] is True and 'ssh' not in probe['measured']

    def test_its_download_is_no_control_for_the_bulk_rule(self, db):
        """probe_bulk demotes a frozen protocol only while another CASCADE
        protocol's download passes; a passing ssh download is not that."""
        for tag in CASCADE + ('ssh',):
            seed_window(db, tag, 10)
        for tag in CASCADE:
            seed_bulk(db, tag, ['partial'] * 3)
        seed_bulk(db, 'ssh', ['ok'] * 3)
        probe = DPIMonitor(db, Mock(), None).collect_signals()['probe']
        assert probe['bulk_frozen'] == {} and 'ssh' not in probe['bulk_newest']


# ------------------------------------------------------------ /protocols -----

class TestProtocolsCard:

    def _card(self, db, cfg):
        from bot.handlers.admin.ops import AdminOpsMixin
        h = AdminOpsMixin.__new__(AdminOpsMixin)
        h.db, h.config = db, cfg
        h._panel_audit_line = lambda: 'audit: skipped'
        h._recent_alert_lines = lambda now: []
        return h._build_protocols_report()

    def test_a_line_of_its_own(self, db):
        for tag in CASCADE:
            seed_window(db, tag, 10)
        seed_window(db, 'ssh', 0, alive_fail=False)
        text = self._card(db, _config(**SSH))
        line = next(ln for ln in text.splitlines() if '<b>ssh</b>' in ln)
        assert line.startswith('🔴 <b>ssh</b> — 0/10 ok, лежит ')

    def test_expected_before_its_first_row(self, db):
        # deployed, the sidecar not regenerated yet: a grey line, no verdict
        for tag in CASCADE:
            seed_window(db, tag, 10)
        text = self._card(db, _config(**SSH))
        assert '⚪ <b>ssh</b> — нет проб за 3 ч' in text.splitlines()
        assert '<b>ssh</b>' not in self._card(db, _config())


# ------------------------------------------------------------ healthcheck ----

def _hc_script():
    return _load('protocol_healthcheck.py')


class TestHealthcheckScript:

    NOW = datetime(2026, 11, 15, 12, 0, 0)

    def _rows(self, ok, dark=0):
        out = []
        for run in range(3):
            ts = (self.NOW - timedelta(minutes=5 + 15 * run)).isoformat()
            out += [[ts, 'ok', 120] for _ in range(ok)]
            out += [[ts, 'error', 300] for _ in range(10 - ok - dark)]
            out += [[ts, 'timeout', None] for _ in range(dark)]
        return out

    def _layers(self, hc, *, ssh=None, cascade_dark=False, containers=None):
        alive = (self.NOW - timedelta(minutes=5)).isoformat()
        probes = {t: {'rows': self._rows(0, dark=10) if cascade_dark else self._rows(10),
                      'last_alive': None if cascade_dark else alive}
                  for t in hc.PROBED}
        if ssh is not None:
            probes['ssh'] = ssh
        # hy2t off in the cascade, as on prod since 2026-09-21: no layer C
        # here, and hy2t without probe rows would be judged from it alone
        a = {'ok': True, 'probes': probes, 'alerts': [],
             'newest_ts': max(p['rows'][0][0] for p in probes.values()),
             'audit': {'rc': 0, 'lines': ['OK: 81 client records on 4 inbounds']},
             'health': {'status': 'healthy', 'version': 'x'},
             'cascade_order': json.dumps(
                 [{'name': n, 'enabled': n != 'hy2t'}
                  for n in ('reality', 'hy2', 'ws', 'stls', 'hy2t')])}
        b = {'ok': True, 'services': {'shadow-tls': 'active', 'haproxy': 'active'},
             'containers': containers or {'probe-proxy': 'running',
                                          'probe-proxy-mihomo': 'running',
                                          'vpn-bot': 'running'},
             'nat_prerouting': []}
        return {'a': a, 'b': b, 'c': {'ok': False, 'error': 'not collected here'}}

    def test_a_dark_ssh_path_is_down_with_its_suspect(self):
        hc = _hc_script()
        since = (self.NOW - timedelta(hours=2, minutes=5)).isoformat()
        report = hc.assess(self._layers(hc, ssh={'rows': self._rows(0, dark=10),
                                                 'last_alive': since}), now=self.NOW)
        assert report['protocols']['ssh']['state'] == 'DOWN'
        assert report['exit_code'] == 1
        assert report['verdict'] == ('ИТОГ: ssh DOWN 2ч 05мин, остальные OK; '
                                     'выключены в каскаде: hy2t')
        s = next(s for s in report['suspects'] if s['protocol'] == 'ssh')
        assert 'ssh-exit' in s['title'] and 'SSH_HOST_KEY' in s['title']
        assert s['next'].startswith("ssh exit-node 'docker ps -a --filter name=ssh-exit;")
        assert 'journalctl -u ssh-exit-sync' in s['next']
        assert '[ssh] DOWN' in hc.render_human(report)

    def test_an_answering_ssh_path_is_ok_and_named(self):
        hc = _hc_script()
        report = hc.assess(self._layers(hc, ssh={'rows': self._rows(10),
                                                 'last_alive': self.NOW.isoformat()}),
                           now=self.NOW)
        assert report['protocols']['ssh']['state'] == 'OK'
        assert 'ssh 30/30' in report['verdict']
        assert not [s for s in report['suspects'] if s['protocol'] == 'ssh']

    def test_no_rows_no_line(self):
        # not probed on this deployment (or not yet): nothing to say
        hc = _hc_script()
        report = hc.assess(self._layers(hc), now=self.NOW)
        assert 'ssh' not in report['protocols']

    def test_exit_down_is_one_upstream_suspect_even_with_ssh_alive(self):
        hc = _hc_script()
        layers = self._layers(hc, cascade_dark=True,
                              ssh={'rows': self._rows(10), 'last_alive': self.NOW.isoformat()})
        report = hc.assess(layers, now=self.NOW)
        assert report['protocols']['ssh']['state'] == 'OK'
        assert report['verdict'].startswith('ИТОГ: ВСЕ протоколы DOWN')
        assert report['suspects'][0]['title'].startswith('upstream')

    def test_switched_off_by_hand_it_still_proves_nothing(self):
        hc = _hc_script()
        layers = self._layers(hc, cascade_dark=True,
                              ssh={'rows': self._rows(10), 'last_alive': self.NOW.isoformat()})
        layers['a']['cascade_order'] = json.dumps(
            [{'name': n, 'enabled': n != 'hy2t'}
             for n in ('reality', 'hy2', 'ws', 'stls', 'hy2t')]
            + [{'name': 'ssh', 'enabled': False}])
        report = hc.assess(layers, now=self.NOW)
        assert report['verdict'].startswith('ИТОГ: ВСЕ протоколы DOWN')

    def test_the_sidecar_named_is_its_own(self):
        """The ssh (and xhr) probes ride probe-proxy-mihomo: a stopped mihomo
        sidecar blinds them, not reality; a stopped sing-box one the reverse."""
        hc = _hc_script()
        ssh = {'rows': self._rows(10), 'last_alive': self.NOW.isoformat()}

        def evidence(containers, tag):
            report = hc.assess(self._layers(hc, ssh=dict(ssh), containers=containers),
                               now=self.NOW)
            return '\n'.join(report['protocols'][tag]['evidence'])

        mihomo_down = {'probe-proxy': 'running', 'probe-proxy-mihomo': 'exited',
                       'vpn-bot': 'running'}
        assert 'контейнер probe-proxy-mihomo exited' in evidence(mihomo_down, 'ssh')
        assert 'probe-proxy-mihomo' not in evidence(mihomo_down, 'reality')
        singbox_down = {'probe-proxy': 'exited', 'probe-proxy-mihomo': 'running',
                        'vpn-bot': 'running'}
        assert 'контейнер probe-proxy exited' in evidence(singbox_down, 'reality')
        assert 'контейнер probe-proxy' not in evidence(singbox_down, 'ssh')
        # a layer B that did not look at the mihomo sidecar claims nothing
        assert 'контейнер' not in evidence({'probe-proxy': 'running', 'vpn-bot': 'running'},
                                           'ssh')

    def test_layer_b_inspects_both_sidecars(self, monkeypatch):
        hc = _hc_script()
        seen = []

        def fake_run(cmd, timeout, stdin=None):
            seen.append(cmd)
            return 0, 'running\n' if cmd[0] == 'docker' else '', ''
        monkeypatch.setattr(hc, '_run', fake_run)
        out = hc.collect_layer_b()
        assert out['containers']['probe-proxy-mihomo'] == 'running'
        assert ['docker', 'inspect', '-f', '{{.State.Status}}', 'probe-proxy-mihomo'] in seen

    def test_the_same_rows_the_same_verdict_as_the_pager(self, db):
        """The pager, /protocols and the script read ssh's rows alike."""
        from bot.handlers.admin.ops import probe_verdict, read_probe_state
        hc = _hc_script()
        for tag in CASCADE:
            seed_window(db, tag, 10)
        for ok, expect in ((0, 'dark'), (2, 'degraded'), (10, 'ok')):
            with db._connect() as conn:
                conn.execute("DELETE FROM outbound_health WHERE outbound_tag = 'ssh'")
                conn.commit()
            seed_window(db, 'ssh', ok, alive_fail=ok != 0)
            alerts = pager(db)
            pager_says = ('dark' if 'protocol_down:ssh' in alerts else
                          'degraded' if 'protocol_down:ssh:degraded' in alerts else 'ok')
            state = read_probe_state(db, _config(**SSH), datetime.utcnow(), window_h=3, runs=3)
            card = {'down': 'dark', 'degraded': 'degraded', 'ok': 'ok', 'silent': 'ok'}[
                probe_verdict(state['per_tag']['ssh'], min_samples=15)]
            with db._connect() as conn:
                rows = [[r[0], r[1], r[2]] for r in conn.execute(
                    "SELECT ts, status, latency_ms FROM outbound_health "
                    "WHERE outbound_tag = 'ssh' ORDER BY ts DESC").fetchall()]
            script = hc._probe_verdict('ssh', {'ssh': {'rows': rows, 'last_alive': None}},
                                       datetime.utcnow())['state']
            script_says = {'DOWN': 'dark', 'DEGRADED': 'degraded', 'OK': 'ok'}[script]
            assert pager_says == card == script_says == expect, ok


# ------------------------------------------------------------ the stand ------

MIHOMO = 'metacubex/mihomo:v1.19.33'
PYTHON = 'python:3.11-slim'
# TEST-NET-2: a "public" range the sshd's own egress rules let through (they
# refuse private, CGNAT, loopback, link-local), never routed on the internet.
STAND_NET, STAND_SUBNET = 'nekovpn-ssh-probe-stand', '198.51.100.0/24'
WEB_IP, SSHD_IP, MIHOMO_IP = '198.51.100.10', '198.51.100.11', '198.51.100.12'
STAND_CONTAINERS = ('e-probe-web', 'e-probe-sshd', 'e-probe-mihomo')

# The targets: HTTPS, as in production — the prober then goes through a
# CONNECT, which mihomo answers before it dials (plain http would come back
# as mihomo's 502 page, a row with a latency, i.e. "alive").
WEB_SERVER = r'''
import http.server, ssl
from urllib.parse import parse_qs, urlsplit


class Handler(http.server.BaseHTTPRequestHandler):
    def _head(self, length):
        self.send_response(200)
        self.send_header('Content-Length', str(length))
        self.end_headers()

    def do_HEAD(self):
        self._head(0)

    def do_GET(self):
        want = parse_qs(urlsplit(self.path).query).get('bytes', ['0'])[0]
        body = b'x' * int(want)
        self._head(len(body))
        self.wfile.write(body)

    def log_message(self, *args):
        pass


server = http.server.ThreadingHTTPServer(('0.0.0.0', 443), Handler)
ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
ctx.load_cert_chain('/srv/cert.pem', '/srv/key.pem')
server.socket = ctx.wrap_socket(server.socket, server_side=True)
server.serve_forever()
'''


def _docker(*args, check=True):
    r = subprocess.run(['docker', *args], capture_output=True, text=True)
    if check and r.returncode:
        raise AssertionError(f"docker {' '.join(args[:3])}: {r.stderr.strip()[-300:]}")
    return r.stdout.strip()


def _have(image):
    return subprocess.run(['docker', 'image', 'inspect', image],
                          capture_output=True).returncode == 0


def _stand_tls(tmp_path):
    """A stand CA and a certificate for the web server's address."""
    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import ec
    from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID
    import ipaddress
    now = datetime.utcnow()
    ca_key = ec.generate_private_key(ec.SECP256R1())
    ca_name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, 'ssh probe stand CA')])
    ca = (x509.CertificateBuilder().subject_name(ca_name).issuer_name(ca_name)
          .public_key(ca_key.public_key()).serial_number(x509.random_serial_number())
          .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=1))
          .add_extension(x509.BasicConstraints(ca=True, path_length=0), critical=True)
          .add_extension(x509.KeyUsage(digital_signature=True, key_cert_sign=True, crl_sign=True,
                                       content_commitment=False, key_encipherment=False,
                                       data_encipherment=False, key_agreement=False,
                                       encipher_only=False, decipher_only=False), critical=True)
          .add_extension(x509.SubjectKeyIdentifier.from_public_key(ca_key.public_key()),
                         critical=False)
          .sign(ca_key, hashes.SHA256()))
    key = ec.generate_private_key(ec.SECP256R1())
    cert = (x509.CertificateBuilder()
            .subject_name(x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, WEB_IP)]))
            .issuer_name(ca_name).public_key(key.public_key())
            .serial_number(x509.random_serial_number())
            .not_valid_before(now - timedelta(hours=1)).not_valid_after(now + timedelta(days=1))
            .add_extension(x509.SubjectAlternativeName(
                [x509.IPAddress(ipaddress.ip_address(WEB_IP))]), critical=False)
            .add_extension(x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
                           critical=False)
            .add_extension(x509.AuthorityKeyIdentifier.from_issuer_public_key(
                ca_key.public_key()), critical=False)
            .sign(ca_key, hashes.SHA256()))
    web = tmp_path / 'web'
    web.mkdir()
    (web / 'cert.pem').write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    (web / 'key.pem').write_bytes(key.private_bytes(
        serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption()))
    (web / 'server.py').write_text(WEB_SERVER)
    for f in web.iterdir():
        f.chmod(0o644)
    ca_pem = tmp_path / 'ca.pem'
    ca_pem.write_bytes(ca.public_bytes(serialization.Encoding.PEM))
    return web, ca_pem


def _sshd_image(tmp_path):
    """The sshd the way scripts/setup_ssh_exit.sh builds and configures it."""
    script = os.path.join(REPO, 'scripts', 'setup_ssh_exit.sh')

    def printed(flag):
        return subprocess.run(['bash', script, flag], capture_output=True, text=True,
                              check=True).stdout
    sshd = tmp_path / 'sshd'
    sshd.mkdir()
    (sshd / 'sshd_config').write_text(printed('--print-config'))
    (sshd / 'entrypoint.sh').write_text(printed('--print-entrypoint'))
    (sshd / 'entrypoint.sh').chmod(0o755)
    (sshd / 'keys').mkdir()
    base = next(line.split('=', 1)[1] for line in open(script).read().splitlines()
                if line.startswith('BASE_IMAGE='))
    image = 'nekovpn-ssh-stand:local'
    build = subprocess.run(['docker', 'build', '-q', '-t', image, '-'], input=(
        f'FROM {base}\nRUN apk add --no-cache openssh-server shadow iptables procps && addgroup vpn\n'
        'CMD ["/entrypoint.sh"]\n'), capture_output=True, text=True)
    if build.returncode:
        pytest.skip(f'could not build the sshd image: {build.stderr[-200:]}')
    return image, sshd


def _host_key(image, sshd, name):
    _docker('run', '--rm', '-v', f'{sshd}/keys:/k', image, 'ssh-keygen', '-q', '-t', 'ed25519',
            '-N', '', '-f', f'/k/{name}')
    return ' '.join((sshd / 'keys' / f'{name}.pub').read_text().split()[:2])


def _wait_port(host, port, timeout=40.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return True
        except OSError:
            time.sleep(0.3)
    return False


def _run_sidecar(tmp_path, config):
    """The probe sidecar as compose runs it, with ``config`` as its config.yaml."""
    _docker('rm', '-f', 'e-probe-mihomo', check=False)
    conf = tmp_path / 'mihomo'
    conf.mkdir(exist_ok=True)
    (conf / 'config.yaml').write_text(json.dumps(config, indent=2))
    _docker('run', '-d', '--name', 'e-probe-mihomo', '--network', STAND_NET, '--ip', MIHOMO_IP,
            '-v', f'{conf}:/root/.config/mihomo', MIHOMO, '-d', '/root/.config/mihomo')


@pytest.mark.requires_docker
@pytest.mark.asyncio
async def test_stand_the_generated_sidecar_config_probes_the_scripts_sshd(tmp_path, monkeypatch):
    """Offline, end to end: the probe user in a real sqlite; ssh-exit's users
    from the bot's own feed (users_payload), in the sshd setup_ssh_exit.sh
    configures (its sshd_config, its entrypoint with the egress rules);
    the sidecar config from gen_probe_config_mihomo.build_config in mihomo
    1.19.33; the real HealthChecker against it.

    * the path works: ten ok rows and a full bulk download;
    * ssh-exit restarts under the same sidecar: the next run is ok again;
    * the bot's secret no longer matches the server's hashes (a stuck sync
      after a rotation), or the pinned host key is not the server's: dark
      rows — three runs of them page protocol_down:ssh;
    * the sidecar runs a config without the ssh listener (the rollout,
      before its regeneration): no rows at all."""
    import aiohttp.connector
    import ssl as ssl_mod
    if not (_have(MIHOMO) and _have(PYTHON)):
        pytest.skip('stand images not pulled')
    web, ca_pem = _stand_tls(tmp_path)
    image, sshd = _sshd_image(tmp_path)
    host_key = _host_key(image, sshd, 'ssh_host_ed25519_key')
    other_key = _host_key(image, sshd, 'other_key')
    cfg = _config(SSH_EXIT_HOST=SSHD_IP, SSH_EXIT_PORT='22', SSH_HOST_KEY=host_key,
                  SSH_USER_SECRET='stand-secret', PROBE_PROXY_MIHOMO_HOST=MIHOMO_IP)
    db = Database(str(tmp_path / 'bot.db'))
    user = add_probe_user(db)
    # what the exit's sync writes from GET /api/ssh/users (name<TAB>hash)
    (sshd / 'users.plan').write_text(''.join(
        f"{u['user']}\t{u['hash']}\n" for u in ssh_exit.users_payload(db, cfg)))
    gen = _load('gen_probe_config_mihomo.py')

    # the prober trusts the stand CA (aiohttp's verified context, restored after)
    ctx = ssl_mod.create_default_context(cafile=str(ca_pem))
    monkeypatch.setattr(aiohttp.connector, '_SSL_CONTEXT_VERIFIED', ctx)

    def checker(config):
        hc = HealthChecker(db, config)
        # ten distinct targets (rows are keyed by target), all on the web server
        hc.TARGET_DOMAINS = [f'{WEB_IP}/{t}' for t in TARGETS]
        hc.BULK_URL = f'https://{WEB_IP}/bulk?bytes={{bytes}}'
        return hc

    def ssh_rows():
        return [r for r in health_rows(db) if r[0] == 'ssh']

    for c in STAND_CONTAINERS:
        _docker('rm', '-f', c, check=False)
    _docker('network', 'rm', STAND_NET, check=False)
    if subprocess.run(['docker', 'network', 'create', '--subnet', STAND_SUBNET, STAND_NET],
                      capture_output=True).returncode:
        pytest.skip(f'could not create the stand network {STAND_SUBNET}')
    try:
        _docker('run', '-d', '--name', 'e-probe-web', '--network', STAND_NET, '--ip', WEB_IP,
                '-v', f'{web}:/srv:ro', PYTHON, 'python', '/srv/server.py')
        _docker('run', '-d', '--name', 'e-probe-sshd', '--network', STAND_NET, '--ip', SSHD_IP,
                '--cap-add', 'NET_ADMIN',
                '-v', f'{sshd}/sshd_config:/etc/ssh/sshd_config:ro',
                '-v', f'{sshd}/keys:/etc/ssh/keys:ro',
                '-v', f'{sshd}/entrypoint.sh:/entrypoint.sh:ro',
                '-v', f'{sshd}/users.plan:/etc/ssh/users.plan:ro', image)

        # --- the path works
        mih = gen.build_config(cfg, user)
        assert [lst['name'] for lst in mih['listeners']] == ['probe-in-ssh']
        _run_sidecar(tmp_path, mih)
        for name, ip, port in (('e-probe-web', WEB_IP, 443), ('e-probe-sshd', SSHD_IP, 22),
                               ('e-probe-mihomo', MIHOMO_IP, 18088)):
            if not _wait_port(ip, port):
                # a host that cannot route to container addresses (Docker
                # Desktop): no stand here, rather than a false failure
                pytest.skip(f'{name} ({ip}:{port}) not reachable from this host: '
                            + _docker('logs', name, check=False)[-300:])
        res = await checker(cfg)._check_protocol('ssh')
        assert {d: r['status'] for d, r in res.items()} == {
            f'{WEB_IP}/{t}': 'ok' for t in TARGETS}, res
        assert [(st, lat is not None) for _t, _d, st, lat in ssh_rows()] == [('ok', True)] * 10
        assert bulk_rows(db) == [('ssh', 'ok', HealthChecker.BULK_BYTES)]

        # --- ssh-exit restarts under the same sidecar (a setup re-run, an exit
        # reboot): mihomo holds one ssh connection per proxy, and must not
        # keep the dead one — the next run is ok again, nothing pages
        _docker('restart', '-t', '1', 'e-probe-sshd')
        assert _wait_port(SSHD_IP, 22)
        res = await checker(cfg)._check_protocol('ssh')
        assert {r['status'] for r in res.values()} == {'ok'}, res
        assert bulk_rows(db)[-1] == ('ssh', 'ok', HealthChecker.BULK_BYTES)
        with db._connect() as conn:                 # the rest wants one ok run only
            conn.execute("DELETE FROM outbound_health WHERE id > 10")
            conn.execute("DELETE FROM outbound_bulk WHERE id > 1")
            conn.commit()

        # --- the bot's secret moved on, the server still has the old hashes
        rotated = _config(**{**vars(cfg), 'SSH_USER_SECRET': 'rotated-secret'})
        _run_sidecar(tmp_path, gen.build_config(rotated, user))
        assert _wait_port(MIHOMO_IP, 18088)
        for _ in range(pr.PROBE_RUNS):
            assert await checker(rotated)._check_protocol('ssh') is not None
        dark = ssh_rows()[10:]
        assert len(dark) == 30 and all(st != 'ok' and lat is None for _t, _d, st, lat in dark)
        assert not any(st == 'proxy_down' for _t, _d, st, _l in dark)
        assert [b[1:] for b in bulk_rows(db)[1:]] == [('error', 0)] * pr.PROBE_RUNS
        for tag in CASCADE:
            seed_window(db, tag, 10)
        alerts = pager(db)
        assert list(alerts) == ['protocol_down:ssh'], alerts
        assert alerts['protocol_down:ssh'].title.startswith('Аварийный SSH-выход не пускает')

        # --- the pinned host key is not the server's
        before = len(ssh_rows())
        wrong_key = _config(**{**vars(cfg), 'SSH_HOST_KEY': other_key})
        _run_sidecar(tmp_path, gen.build_config(wrong_key, user))
        assert _wait_port(MIHOMO_IP, 18088)
        await checker(wrong_key)._check_protocol('ssh')
        rows = ssh_rows()[before:]
        assert len(rows) == 10 and all(lat is None for _t, _d, _s, lat in rows)

        # --- a sidecar config without the ssh listener: nothing written
        before, before_bulk = len(health_rows(db)), len(bulk_rows(db))
        _run_sidecar(tmp_path, gen.build_config(_config(), user))
        time.sleep(3)
        assert 'running' in _docker('inspect', '-f', '{{.State.Status}}', 'e-probe-mihomo')
        assert await checker(cfg)._check_protocol('ssh') is None
        assert (len(health_rows(db)), len(bulk_rows(db))) == (before, before_bulk)
    finally:
        for c in STAND_CONTAINERS:
            _docker('rm', '-f', c, check=False)
        _docker('network', 'rm', STAND_NET, check=False)
