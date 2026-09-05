"""gen_probe_config must emit the hy2t inbound (:18085) ONLY where HY2T_PORT is set.

Why this matters: the sidecar config is what HealthChecker's rows mean.
A deployment without turbo must not get a probe-in-hy2t inbound (a curl
through it would "work" against a tunnel nobody sells), and a deployment
WITH turbo must get one whose outbound is the very same hysteria2 block
SubscriptionService hands to paying users — otherwise hy2t stays the one
protocol nobody watches (it had no probe rows until 2026-09-05).

The port table is HealthChecker.probe_ports_for(config) — one source for
the generator and the checker — so a test here also guards the checker's
side of the contract. The REAL SubscriptionService builds the outbounds
(no stubs there); only Settings/Database are replaced for main().
"""

import importlib.util
import json
import os
from types import SimpleNamespace

import pytest

from bot.services.health_checker import HealthChecker
from bot.services.subscription import SubscriptionService

# scripts/ is not a package — load by path (same shape as
# tests/unit/test_protocol_healthcheck.py).
_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'scripts', 'gen_probe_config.py',
))
_spec = importlib.util.spec_from_file_location('gen_probe_config', _PATH)
gen = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(gen)


UUID = 'aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee'
# The dedicated panel client (email prefix is what names the outbounds:
# 'probe_monitor-reality' etc. in the live config).
PROBE_USER = SimpleNamespace(uuid=UUID, email='probe_monitor@nekovo.ru',
                             status='paid', chat_id='probe_internal')


def make_config(**over):
    """Same shape as tests/unit/test_hy2_turbo.py: reality + hy2 (+ hy2t)
    configured, ws/stls deliberately absent so their 'skip' path runs."""
    base = dict(
        BOT_TOKEN='tok',
        DB_PATH='/tmp/does-not-matter.db',
        ENTRY_NODE_IP='10.0.0.1',
        ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='pbk-main',
        SNI_VALUE='www.bing.com',
        SID_VALUE='037a08d118bfaafd',
        HY2_HOST='hy.example.com',
        HY2_PORT=8400,
        HY2_SNI='hy.example.com',
        HY2_OBFS_PASSWORD='obfs-pw',
        HY2_HOP_PORTS='20000:40000',
        HY2T_PORT='8402',
        HY2T_HOP_PORTS='40001:50000',
        HY2T_UP_MBPS=20,
        HY2T_DOWN_MBPS=60,
        WS_HOST='',
        WS2_HOST='',
        STLS_HOST='',
        FALLBACK_NODE_HOST='',
    )
    base.update(over)
    return SimpleNamespace(**base)


def inbound_ports(cfg):
    return {i['tag']: i['listen_port'] for i in cfg['inbounds']}


def outbound(cfg, tag):
    return next(o for o in cfg['outbounds'] if o.get('tag') == tag)


class TestHy2tByConfig:

    def test_enabled_emits_inbound_outbound_and_route_on_18085(self):
        cfg = gen.build_config(make_config(HY2T_PORT='8402'), PROBE_USER)

        assert inbound_ports(cfg)['probe-in-hy2t'] == 18085
        ib = next(i for i in cfg['inbounds'] if i['tag'] == 'probe-in-hy2t')
        assert ib['type'] == 'http' and ib['listen'] == '0.0.0.0'
        ob = outbound(cfg, 'probe_monitor-hy2t')
        # The paid users' turbo block, verbatim: second instance port +
        # Brutal hints + salamander obfs + the turbo hop range.
        assert ob['type'] == 'hysteria2'
        assert ob['server_port'] == 8402
        assert ob['password'] == UUID
        assert ob['up_mbps'] == 20 and ob['down_mbps'] == 60
        assert ob['obfs'] == {'type': 'salamander', 'password': 'obfs-pw'}
        assert ob['server_ports'] == ['40001:50000']
        assert {'inbound': ['probe-in-hy2t'], 'outbound': 'probe_monitor-hy2t'} in cfg['route']['rules']

    def test_enabled_keeps_plain_hy2_on_its_own_port(self):
        cfg = gen.build_config(make_config(HY2T_PORT='8402'), PROBE_USER)
        assert inbound_ports(cfg)['probe-in-hy2'] == 18082
        assert outbound(cfg, 'probe_monitor-hy2')['server_port'] == 8400
        assert 'up_mbps' not in outbound(cfg, 'probe_monitor-hy2')

    @pytest.mark.parametrize('raw', ['', '   ', 'oops', '0'])
    def test_disabled_emits_no_hy2t_anywhere(self, raw):
        """No dead inbound: a listener with no tunnel behind it would let a
        deploy smoke 'pass' and give the checker a port to write
        proxy_down rows against."""
        cfg = gen.build_config(make_config(HY2T_PORT=raw), PROBE_USER)

        assert 'probe-in-hy2t' not in inbound_ports(cfg)
        assert 18085 not in inbound_ports(cfg).values()
        assert 'hy2t' not in json.dumps(cfg)
        # …while everything else is untouched.
        assert inbound_ports(cfg)['probe-in-reality'] == 18081
        assert inbound_ports(cfg)['probe-in-hy2'] == 18082

    def test_ports_are_health_checkers_table(self):
        """Single source of truth: the generator must listen exactly where
        the checker will knock."""
        conf = make_config(HY2T_PORT='8402')
        cfg = gen.build_config(conf, PROBE_USER)
        expected = HealthChecker.probe_ports_for(conf)
        emitted = {tag.replace('probe-in-', ''): port for tag, port in inbound_ports(cfg).items()}
        # ws/stls are skipped (unconfigured here); whatever IS emitted sits
        # on the checker's port for that tag, and hy2t is in both.
        for tag, port in emitted.items():
            assert expected[tag] == port, tag
        assert emitted['hy2t'] == HealthChecker.HY2T_PROBE_PORT == 18085

    def test_tls_fragment_is_stripped_from_every_outbound(self):
        """sing-box 1.11 rejects tls.fragment as an unknown field; the
        subscription builders add it for client apps."""
        assert 'fragment' in SubscriptionService._TLS_FRAGMENT      # guard: still emitted upstream
        cfg = gen.build_config(make_config(), PROBE_USER)
        with_tls = [o for o in cfg['outbounds'] if isinstance(o.get('tls'), dict)]
        assert with_tls                                             # reality + hysteria2 at least
        assert all('fragment' not in o['tls'] for o in with_tls)

    def test_unconfigured_protocols_are_logged_as_skipped(self):
        log = []
        cfg = gen.build_config(make_config(HY2T_PORT=''), PROBE_USER, log=log.append)
        assert 'skip ws: not configured' in log
        assert 'skip stls: not configured' in log
        # hy2t is not "skipped" — it is not in the table at all when disabled.
        assert not any('hy2t' in line for line in log)
        assert cfg['route']['final'] == 'direct'
        assert cfg['outbounds'][-1] == {'type': 'direct', 'tag': 'direct'}


class TestMain:

    def _wire(self, monkeypatch, user, conf=None):
        monkeypatch.setattr(gen, 'Settings', lambda: conf or make_config())
        monkeypatch.setattr(gen, 'Database',
                            lambda path: SimpleNamespace(get_user=lambda chat_id: user))

    def test_prints_the_config_with_hy2t_for_a_turbo_deployment(self, monkeypatch, capsys):
        self._wire(monkeypatch, PROBE_USER)
        assert gen.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert inbound_ports(out)['probe-in-hy2t'] == 18085

    def test_omits_hy2t_when_the_env_has_no_turbo(self, monkeypatch, capsys):
        self._wire(monkeypatch, PROBE_USER, make_config(HY2T_PORT=''))
        assert gen.main() == 0
        out = json.loads(capsys.readouterr().out)
        assert 'probe-in-hy2t' not in inbound_ports(out)

    def test_missing_probe_user_is_rc_1_and_no_stdout(self, monkeypatch, capsys):
        """stdout is redirected straight into config.json — a failure must
        never write a partial/empty config there."""
        self._wire(monkeypatch, None)
        assert gen.main() == 1
        captured = capsys.readouterr()
        assert captured.out == ''
        assert 'probe_internal' in captured.err

    def test_probe_user_without_uuid_is_rc_1(self, monkeypatch, capsys):
        self._wire(monkeypatch, SimpleNamespace(uuid='', email='probe_monitor@x'))
        assert gen.main() == 1
        assert capsys.readouterr().out == ''
