"""The SSH emergency path through entry (AGENTS.md §51; 2026-11-16).

The same server as ``ssh`` (ssh-exit on exit), reached at entry's Russian
address through a TCP relay (scripts/setup_ssh_entry_relay.sh): no TLS name,
no UDP, no foreign hosting on the client's side. The owner asked for it to
take over «самым последним, когда всё блокируется».

* the server: entry's address and the relay's port, exit's credentials and
  pinned host key; only with SSH_ENTRY_PORT and ENTRY_NODE_IP, only for
  eligible payers, only in the emergency profiles;
* the order: the very last server, after ssh-exit, in Clash's Cascade and
  VPN and sing-box's proxy selector; never in the speed-test groups (Auto,
  auto) nor in the calls group;
* without SSH_ENTRY_PORT every profile is what it was;
* the probe (``ssh-entry``, the mihomo sidecar's :18089), its warn alerts,
  the healthcheck's relay finding and suspect.

Level 2: real sqlite where a db is read, the real builders, the real pager
check and the healthcheck script loaded by its path. Two stands at the
bottom (``requires_docker``): the relay script's own haproxy in front of
setup_ssh_exit.sh's sshd under the real prober, and real sshds (OpenSSH 9.9
and 9.7) showing the per-address penalties SSH_RELAY_IPS exempts the relay
from. The script's flows, with fakes: tests/unit/test_setup_ssh_entry_relay_sh.py.
"""

import json
import os
import socket
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

from bot.core.database import Database
from bot.services import probe_rules as pr
from bot.services import sos, ssh_exit
from bot.services.agent_client import _detect_skill_domains
from bot.services.alert_manager import _protocol_agent_prompt
from bot.services.health_checker import HealthChecker
from bot.services.subscription import SubscriptionService

from .test_ssh_probe import (CASCADE, MIHOMO, MIHOMO_IP, PYTHON, SSH, SSHD_IP,
                             STAND_CONTAINERS, STAND_NET, STAND_SUBNET, TARGETS, WEB_IP,
                             _config, _docker, _hc_script, _have, _host_key, _load,
                             _run_sidecar, _sshd_image, _stand_tls, _wait_port,
                             add_probe_user, bulk_rows, health_rows, pager, pager_run,
                             seed_window)

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
# A port of its own, not exit's 22022: a server that kept exit's port would
# pass otherwise.
ENTRY = dict(ENTRY_NODE_IP='203.0.113.20', SSH_ENTRY_PORT='2222')


def cfg(**over):
    return _config(**{**SSH, **ENTRY, **over})


def user(status='paid', expiry='2099-12-31T23:59:59', uid=UUID):
    return SimpleNamespace(uuid=uid, email='u1@x', status=status, lang='ru', chat_id='1',
                           subscription_expiry=expiry, previous_state=None)


def clash(config, *, lockdown=True, u=None, protocols=CASCADE):
    svc = SubscriptionService(config)
    return json.loads(svc.build_clash_config(u or user(), protocols, lockdown=lockdown))


def singbox(config, *, lockdown=True, u=None, protocols=CASCADE, **kw):
    return SubscriptionService(config).build_singbox_config(u or user(), protocols,
                                                            lockdown=lockdown, **kw)


# ------------------------------------------------------------------ server ---

class TestServer:

    def test_exits_server_at_entrys_address(self):
        c = cfg()
        direct, relayed = ssh_exit.outbound(c, user()), ssh_exit.entry_outbound(c, user())
        assert relayed == {**direct, 'tag': 'u1-ssh-entry', 'server': '203.0.113.20',
                           'server_port': 2222}
        assert direct['server_port'] == 22022
        # the relay only copies bytes: exit's user, password and host key
        assert (relayed['user'], relayed['password'], relayed['host_key']) == (
            direct['user'], direct['password'], [SSH['SSH_HOST_KEY']])
        assert direct['server'] == SSH['SSH_EXIT_HOST']

    def test_the_relays_port_and_entrys_address_are_both_needed(self):
        assert ssh_exit.entry_port(cfg()) == 2222
        assert ssh_exit.entry_port(cfg(SSH_ENTRY_PORT=' 22023 ')) == 22023
        assert ssh_exit.entry_port(cfg(SSH_ENTRY_PORT=443)) == 443
        for port in ('', 'x', '0', '65536', '-1', '22022a', None, True, 2.5):
            assert ssh_exit.entry_port(cfg(SSH_ENTRY_PORT=port)) is None, port
            assert ssh_exit.entry_outbound(cfg(SSH_ENTRY_PORT=port), user()) is None
        for host in ('', '  ', None, object()):
            assert ssh_exit.entry_port(cfg(ENTRY_NODE_IP=host)) is None, host
        assert ssh_exit.entry_port(cfg(SSH_ENTRY_PORT='65535')) == 65535
        assert ssh_exit.entry_port(cfg(SSH_ENTRY_PORT='1')) == 1
        # a config that has never heard of it (every config before this)
        old = _config(**SSH)
        assert not hasattr(old, 'SSH_ENTRY_PORT')
        assert ssh_exit.entry_port(old) is None and not ssh_exit.entry_enabled(old)

    def test_the_ssh_path_itself_is_needed(self):
        for key in ('SSH_EXIT_HOST', 'SSH_USER_SECRET', 'SSH_HOST_KEY'):
            off = cfg(**{key: ''})
            assert ssh_exit.entry_port(off) == 2222
            assert not ssh_exit.entry_enabled(off)
            assert ssh_exit.entry_outbound(off, user()) is None
        assert ssh_exit.entry_enabled(cfg())

    def test_settings_and_compose_carry_it(self, monkeypatch):
        # the container's environment, not the project .env, is what
        # Settings reads (AGENTS.md §6): compose must pass it
        from bot.config.settings import Settings
        monkeypatch.setenv('SSH_ENTRY_PORT', ' 22022 ')
        assert Settings().SSH_ENTRY_PORT == '22022'
        monkeypatch.delenv('SSH_ENTRY_PORT')
        assert Settings().SSH_ENTRY_PORT == ''
        compose = (Path(__file__).resolve().parents[2] / 'docker-compose.yml').read_text()
        assert '- SSH_ENTRY_PORT=${SSH_ENTRY_PORT:-}' in compose

    def test_payers_only(self):
        assert ssh_exit.entry_outbound(cfg(), user('demo')) is None
        assert ssh_exit.entry_outbound(cfg(), user(expiry='2000-01-01T00:00:00')) is None
        assert ssh_exit.entry_outbound(cfg(), user(uid='')) is None
        assert ssh_exit.entry_outbound(cfg(), user('support_topic', expiry=None)) is None


# ---------------------------------------------------------------- profiles ---

class TestTheVeryLast:

    def test_clash_cascade_and_pick_end_with_it_auto_and_calls_never(self):
        prof = clash(cfg())
        names = [p['name'] for p in prof['proxies']]
        assert names[-2:] == ['u1-ssh', 'u1-ssh-entry']
        entry = prof['proxies'][-1]
        assert (entry['type'], entry['server'], entry['port']) == ('ssh', '203.0.113.20', 2222)
        groups = {g['name']: g for g in prof['proxy-groups']}
        assert groups['Cascade']['type'] == 'fallback'
        assert groups['Cascade']['proxies'][-2:] == ['u1-ssh', 'u1-ssh-entry']
        assert groups['VPN']['proxies'][-2:] == ['u1-ssh', 'u1-ssh-entry']
        for name in ('Auto', 'Calls'):
            assert not {'u1-ssh', 'u1-ssh-entry'} & set(groups[name]['proxies']), name

    def test_singbox_pick_ends_with_it_auto_and_calls_never(self):
        prof = singbox(cfg())
        ob = {o['tag']: o for o in prof['outbounds']}
        assert ob['proxy']['outbounds'][-2:] == ['u1-ssh', 'u1-ssh-entry']
        assert ob['u1-ssh-entry']['server'] == '203.0.113.20'
        assert ob['u1-ssh-entry']['server_port'] == 2222
        for name in ('auto', 'calls'):
            assert not {'u1-ssh', 'u1-ssh-entry'} & set(ob[name]['outbounds']), name

    def test_hiddify_balances_over_it_like_over_ssh_exit(self):
        # Hiddify has no groups to put anything last in; it sees both, as it
        # has seen ssh-exit since E4 (§hide§ would take them away entirely).
        tags = [o['tag'] for o in singbox(cfg(), hiddify=True)['outbounds']]
        assert 'u1-ssh' in tags and 'u1-ssh-entry' in tags

    def test_only_in_the_emergency_profiles(self):
        assert not any(p['type'] == 'ssh' for p in clash(cfg(), lockdown=False)['proxies'])
        assert not any(o['type'] == 'ssh'
                       for o in singbox(cfg(), lockdown=False)['outbounds'])
        provider = json.loads(SubscriptionService(cfg()).build_clash_proxies(user(), CASCADE))
        assert not any(p['type'] == 'ssh' for p in provider['proxies'])

    def test_not_for_demo_or_a_lapsed_payer(self):
        for u in (user('demo'), user(expiry='2000-01-01T00:00:00')):
            assert not any(p['type'] == 'ssh' for p in clash(cfg(), u=u)['proxies'])

    def test_without_the_port_every_profile_is_what_it_was(self):
        before = _config(**SSH)          # no SSH_ENTRY_PORT at all
        after = cfg(SSH_ENTRY_PORT='')
        for lockdown in (True, False):
            assert (SubscriptionService(after).build_clash_config(user(), CASCADE,
                                                                  lockdown=lockdown)
                    == SubscriptionService(before).build_clash_config(user(), CASCADE,
                                                                      lockdown=lockdown))
            for hiddify in (True, False):
                assert (singbox(after, lockdown=lockdown, hiddify=hiddify)
                        == singbox(before, lockdown=lockdown, hiddify=hiddify))
        assert [p['name'] for p in clash(after)['proxies'] if p['type'] == 'ssh'] == ['u1-ssh']

    def test_the_sos_kit_carries_it_last(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        files = dict(sos.build_kit(db, cfg(), user()))
        prof = json.loads(files[sos.KIT_YAML_NAME])
        groups = {g['name']: g for g in prof['proxy-groups']}
        assert groups['Cascade']['proxies'][-1] == 'u1-ssh-entry'
        assert 'u1-ssh-entry' not in groups['Auto']['proxies']
        tags = [o['tag'] for o in json.loads(files[sos.KIT_JSON_NAME])['outbounds']]
        assert 'u1-ssh-entry' in tags


# ------------------------------------------------------------------- probe ---

class TestProbe:

    def test_probed_wherever_the_bot_hands_it_out(self):
        ports = HealthChecker.probe_ports_for(cfg())
        assert ports['ssh'] == 18088 and ports['ssh-entry'] == 18089
        assert list(ports)[-2:] == ['ssh', 'ssh-entry']
        assert 'ssh-entry' not in HealthChecker.probe_ports_for(cfg(SSH_ENTRY_PORT=''))
        assert 'ssh-entry' not in HealthChecker.probe_ports_for(cfg(SSH_USER_SECRET=''))
        assert 'ssh-entry' in HealthChecker.MIHOMO_TAGS
        assert 'ssh-entry' in HealthChecker.OPTIONAL_SIDECAR_TAGS
        hc = HealthChecker(None, cfg(PROBE_PROXY_MIHOMO_HOST='10.9.9.9'))
        assert hc._proxy_url('ssh-entry') == 'http://10.9.9.9:18089'

    def test_each_listener_is_bound_to_its_own_server(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        probe = add_probe_user(db)
        gen = _load('gen_probe_config_mihomo.py')
        c = cfg()
        out = gen.build_config(c, probe)
        bound = {lst['port']: lst['proxy'] for lst in out['listeners']}
        by_name = {p['name']: p for p in out['proxies']}
        assert set(bound) == {18088, 18089}
        relayed, direct = by_name[bound[18089]], by_name[bound[18088]]
        assert (relayed['server'], relayed['port']) == ('203.0.113.20', 2222)
        assert (direct['server'], direct['port']) == (SSH['SSH_EXIT_HOST'], 22022)
        # exactly what the probe user's own emergency profile carries
        own = {p['name']: p for p in json.loads(SubscriptionService(c).build_clash_config(
            probe, (), lockdown=True))['proxies']}
        assert relayed == own[bound[18089]] and direct == own[bound[18088]]
        assert bound[18089].endswith('-ssh-entry') and bound[18088].endswith('-ssh')

    def test_a_probe_user_who_is_no_payer_gets_no_listener_and_a_line(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        probe = add_probe_user(db, status='demo')
        lines = []
        out = _load('gen_probe_config_mihomo.py').build_config(cfg(), probe, log=lines.append)
        assert out['listeners'] == []
        assert [line.split(':')[0] for line in lines] == ['skip ssh', 'skip ssh-entry']
        assert 'eligible payer' in lines[1]


# ------------------------------------------------------------------- pager ---

class TestPager:

    def _seed(self, db, entry_ok, *, ssh_ok=10, cascade_ok=10, alive_fail=False):
        """Three runs per tag; a tag at 0 ok is dark (no latency at all)
        unless ``alive_fail`` keeps the failures of ssh-entry alive."""
        for tag in CASCADE:
            seed_window(db, tag, cascade_ok, alive_fail=cascade_ok != 0)
        seed_window(db, 'ssh', ssh_ok, alive_fail=ssh_ok != 0)
        seed_window(db, 'ssh-entry', entry_ok, alive_fail=alive_fail)

    def test_dark_warns_as_itself(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        self._seed(db, 0)
        alerts, judged = pager_run(db)
        assert list(alerts) == ['protocol_down:ssh-entry']
        alert = alerts['protocol_down:ssh-entry']
        assert alert.severity == 'warn' and alert.min_cycles == 2
        assert alert.title.startswith('Аварийный SSH через entry не пускает (лежит ')
        assert 'ssh-relay' in alert.detail and 'entry_relay:ssh' in alert.detail
        assert 'ssh-entry ok 0/30 (живых 0)' in alert.detail.rsplit('Сейчас по пробам: ', 1)[1]
        assert {'protocol_down:ssh-entry', 'protocol_down:ssh-entry:degraded'} <= judged
        domains = _detect_skill_domains(_protocol_agent_prompt(alert))
        assert 'vpn-ops' in domains
        assert 'incident-response' not in domains and 'code-review' not in domains

    def test_degraded_warns_as_itself(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        self._seed(db, 2, alive_fail=True)
        alerts = pager(db)
        assert list(alerts) == ['protocol_down:ssh-entry:degraded']
        alert = alerts['protocol_down:ssh-entry:degraded']
        assert alert.severity == 'warn' and alert.min_cycles == 3
        assert alert.title == 'Аварийный SSH через entry деградировал: ok 6/30'
        assert 'ssh-relay' in alert.detail

    def test_answering_heals(self, tmp_path):
        db = Database(str(tmp_path / 'bot.db'))
        self._seed(db, 10)
        alerts, judged = pager_run(db)
        assert alerts == {}
        assert {'protocol_down:ssh-entry', 'protocol_down:ssh-entry:degraded'} <= judged

    def test_no_cascade_protocol(self, tmp_path):
        # every cascade protocol dark: one upstream incident, whatever the
        # SSH paths say (they skip the sing-box sidecar)
        db = Database(str(tmp_path / 'bot.db'))
        self._seed(db, 10, cascade_ok=0)
        assert 'protocol_down:all' in pager(db)
        # the cascade alive, both SSH paths dark: two warns, no "all"
        db2 = Database(str(tmp_path / 'bot2.db'))
        self._seed(db2, 0, ssh_ok=0)
        assert sorted(pager(db2)) == ['protocol_down:ssh', 'protocol_down:ssh-entry']


class TestMonitor:
    """DPIMonitor leaves it alone, as it leaves ssh: never measured, never
    demoted, never the proof the channel is up, never the bulk rule's control."""

    def test_never_measured_never_demoted(self, tmp_path):
        from unittest.mock import Mock
        from bot.services.dpi_monitor import DPIMonitor
        db = Database(str(tmp_path / 'bot.db'))
        for tag in CASCADE:
            seed_window(db, tag, 10)
        seed_window(db, 'ssh-entry', 0, alive_fail=False)
        mon = DPIMonitor(db, Mock(), None)
        probe = mon.collect_signals()['probe']
        assert 'ssh-entry' not in probe['measured'] and not probe['dark']
        for _ in range(3):
            assert mon.run_once() == []
        assert 'ssh-entry' not in (db.get_setting('cascade_auto') or '')

    def test_alive_it_does_not_break_the_all_dark_guard(self, tmp_path):
        from unittest.mock import Mock
        from bot.services.dpi_monitor import DPIMonitor
        db = Database(str(tmp_path / 'bot.db'))
        for tag in CASCADE:
            seed_window(db, tag, 0, alive_fail=False)
        seed_window(db, 'ssh-entry', 10)
        probe = DPIMonitor(db, Mock(), None).collect_signals()['probe']
        assert probe['all_dark'] is True and 'ssh-entry' not in probe['measured']


# --------------------------------------------------------------- healthcheck --

class TestHealthcheck:

    NOW = datetime(2026, 11, 16, 12, 0, 0)

    def _rows(self, ok, dark=0):
        out = []
        for run in range(3):
            ts = (self.NOW - timedelta(minutes=5 + 15 * run)).isoformat()
            out += [[ts, 'ok', 120] for _ in range(ok)]
            out += [[ts, 'error', 300] for _ in range(10 - ok - dark)]
            out += [[ts, 'timeout', None] for _ in range(dark)]
        return out

    def _layers(self, hc, *, ssh_ok=10, entry_ok=None, relay=None):
        alive = (self.NOW - timedelta(minutes=5)).isoformat()
        since = (self.NOW - timedelta(hours=1)).isoformat()
        probes = {t: {'rows': self._rows(10), 'last_alive': alive} for t in hc.PROBED}
        probes['ssh'] = {'rows': self._rows(ssh_ok, dark=10 - ssh_ok),
                         'last_alive': alive if ssh_ok else since}
        if entry_ok is not None:
            probes['ssh-entry'] = {'rows': self._rows(entry_ok, dark=10 - entry_ok),
                                   'last_alive': alive if entry_ok else since}
        a = {'ok': True, 'probes': probes, 'alerts': [],
             'newest_ts': alive, 'audit': {'rc': 0, 'lines': ['OK']},
             'health': {'status': 'healthy', 'version': 'x'},
             # hy2t off, as on prod since 2026-09-21 (no layer C here)
             'cascade_order': json.dumps([{'name': n, 'enabled': n != 'hy2t'}
                                          for n in ('reality', 'hy2', 'ws', 'stls', 'hy2t')])}
        b = {'ok': True, 'services': {'shadow-tls': 'active', 'haproxy': 'active'},
             'containers': {'probe-proxy': 'running', 'probe-proxy-mihomo': 'running',
                            'vpn-bot': 'running'},
             'nat_prerouting': [], 'ssh_relay': relay or {'port': None, 'state': None}}
        return {'a': a, 'b': b, 'c': {'ok': False, 'error': 'not collected here'}}

    def test_the_tag_rides_the_mihomo_sidecar_and_the_report(self):
        hc = _hc_script()
        assert 'ssh-entry' in hc.PROBED_OPTIONAL and hc.REPORT_ORDER[-2:] == ['ssh', 'ssh-entry']
        assert hc.SIDECAR_FOR['ssh-entry'] == hc.MIHOMO_SIDECAR

    def test_a_dark_relay_with_ssh_exit_answering_is_blamed_on_entry(self):
        hc = _hc_script()
        report = hc.assess(self._layers(hc, entry_ok=0), now=self.NOW)
        assert report['protocols']['ssh-entry']['state'] == 'DOWN'
        assert report['exit_code'] == 1
        assert report['verdict'].startswith('ИТОГ: ssh-entry DOWN')
        s = next(s for s in report['suspects'] if s['protocol'] == 'ssh-entry')
        assert 'ssh-relay' in s['title'] and 'напрямую на exit пускает' in s['title']
        assert s['next'].startswith('docker ps -a --filter name=ssh-relay;')

    def test_both_dark_is_ssh_exits_suspect_only(self):
        hc = _hc_script()
        report = hc.assess(self._layers(hc, ssh_ok=0, entry_ok=0), now=self.NOW)
        assert {report['protocols'][t]['state'] for t in ('ssh', 'ssh-entry')} == {'DOWN'}
        protocols = [s['protocol'] for s in report['suspects']]
        assert 'ssh' in protocols and 'ssh-entry' not in protocols

    def test_answering_is_ok_and_named(self):
        hc = _hc_script()
        report = hc.assess(self._layers(hc, entry_ok=10), now=self.NOW)
        assert report['protocols']['ssh-entry']['state'] == 'OK'
        assert 'ssh-entry 30/30' in report['verdict']
        assert report['exit_code'] == 0

    def test_the_relay_finding(self):
        hc = _hc_script()
        assert hc.entry_relay({'ssh_relay': {'port': None, 'state': None}}) == ([], [])
        assert hc.entry_relay({'ssh_relay': {'port': '22022', 'state': 'running'}}) == ([], [])
        found, unjudged = hc.entry_relay({'ssh_relay': {'port': '22022', 'state': 'exited'}})
        assert unjudged == [] and len(found) == 1
        f = found[0]
        assert (f['key'], f['layer'], f['severity']) == ('entry_relay:ssh', 'b', 'warn')
        assert 'контейнер exited' in f['title'] and '22022' in f['title']
        assert 'setup_ssh_entry_relay.sh' in f['next']
        found, _ = hc.entry_relay({'ssh_relay': {'port': '22022', 'state': 'missing'}})
        assert 'нет контейнера' in found[0]['title']
        found, _ = hc.entry_relay({'ssh_relay': {'port': '22022', 'state': 'restarting'}})
        assert 'контейнер restarting' in found[0]['title']
        # docker could not be asked, or a collector without the fact: no
        # verdict, the alert stays as it was
        assert hc.entry_relay({'ssh_relay': {'port': '22022', 'state': None}}) == (
            [], ['entry_relay:ssh'])
        assert hc.entry_relay({}) == ([], ['entry_relay:ssh'])

    def test_findings_and_unjudged_carry_it_while_layer_b_looked(self):
        hc = _hc_script()
        layers = self._layers(hc, relay={'port': '22022', 'state': 'missing'})
        assert [f['key'] for f in hc.findings_for(layers)
                if f['key'].startswith('entry_relay')] == ['entry_relay:ssh']
        layers = self._layers(hc, relay={'port': '22022', 'state': None})
        assert 'entry_relay:ssh' in hc.unjudged_for(layers)
        layers['b']['ok'] = False
        assert 'entry_relay:ssh' not in hc.unjudged_for(layers)
        assert not [f for f in hc.findings_for(layers) if f['key'] == 'entry_relay:ssh']

    def test_the_bot_keeps_the_relay_alert_while_entry_could_not_look(self, tmp_path):
        """The bot judges a finding by its key's layer: entry_relay:ssh is
        entry's (B). With entry unreadable it stays open; with exit
        unreadable it is still judged — the relay is on entry."""
        from bot.services.alert_manager import RECOVERY_TICKS
        from .test_adviser import _judged, _manager, _report, _texts
        db = Database(str(tmp_path / 'bot.db'))
        relay = {'key': 'entry_relay:ssh', 'severity': 'warn', 'layer': 'b',
                 'title': 'SSH через entry: ретранслятор ssh-relay не работает', 'next': 'x'}
        mgr = _manager(db)
        _report(db, findings=[relay])
        mgr.run_once()
        assert mgr._state['healthcheck:entry_relay:ssh'].open
        no_entry = {'a': 'ok', 'b': 'FAILED: docker: timed out', 'c': 'ok'}
        _report(db, minutes_ago=0, findings=[], layers=no_entry)
        for _ in range(RECOVERY_TICKS + 1):
            mgr.run_once()
        assert not any('Восстановилось' in t for t in _texts(mgr))
        assert mgr._state['healthcheck:entry_relay:ssh'].open
        assert not _judged(db, 'healthcheck:entry_relay:ssh')
        no_exit = {'a': 'ok', 'b': 'ok', 'c': 'FAILED: ssh: timed out'}
        _report(db, minutes_ago=0, findings=[], layers=no_exit)
        assert _judged(db, 'healthcheck:entry_relay:ssh')

    def test_the_collector_reads_one_key_and_asks_docker(self, tmp_path, monkeypatch):
        hc = _hc_script()
        env = tmp_path / '.env'
        env.write_text('BOT_TOKEN=never-read\nSSH_ENTRY_PORT="22022"\n')
        monkeypatch.setattr(hc, 'BOT_ENV', str(env))
        calls = []

        def fake_run(argv, timeout, stdin=None):
            calls.append(argv)
            return answers.pop(0)
        monkeypatch.setattr(hc, '_run', fake_run)
        answers = [(0, 'running\n', '')]
        assert hc.collect_ssh_relay() == {'port': '22022', 'state': 'running'}
        assert calls[-1] == ['docker', 'inspect', '-f', '{{.State.Status}}', 'ssh-relay']
        answers = [(1, '', 'Error: No such object: ssh-relay\n')]
        assert hc.collect_ssh_relay()['state'] == 'missing'
        answers = [(1, '', 'Cannot connect to the Docker daemon\n')]
        assert hc.collect_ssh_relay()['state'] is None
        env.write_text('BOT_TOKEN=never-read\n')
        n = len(calls)
        assert hc.collect_ssh_relay() == {'port': None, 'state': None}
        assert len(calls) == n                 # nothing to ask without the port


# ------------------------------------------------------------ the stands -----

REPO = Path(__file__).resolve().parents[2]
RELAY_SCRIPT = REPO / 'scripts' / 'setup_ssh_entry_relay.sh'
EXIT_SCRIPT = REPO / 'scripts' / 'setup_ssh_exit.sh'
HAPROXY = ('haproxy:2.8-alpine'
           '@sha256:fce63f5dde742cd1f2b883ab39f7d4f9f88788827dd54fcfa864f63b9eec3bc8')
ALPINE = 'alpine:3.20@sha256:d9e853e87e55526f6b2917df91a2115c36dd7c696a35be12163d44e6e2a4b6bc'
# The script runs as root on entry; here only `id` says so. Tripwires first
# in PATH: nothing it runs may reach the host's power state, firewall or
# services (test payloads only ever touch a file in the test's own
# directory: a ";reboot" rebooted the developer's laptop on 2026-11-11).
STAND_BIN = {'id': '#!/bin/sh\necho 0\n',
             'ufw': '#!/bin/sh\nexit 1\n',
             **{t: '#!/bin/sh\necho "$(basename "$0") $*" >> "$TRIPWIRE_LOG"\nexit 1\n'
                for t in ('reboot', 'shutdown', 'poweroff', 'halt', 'init', 'telinit',
                          'systemctl', 'iptables', 'ip6tables', 'nft', 'pkill', 'killall')}}


def _free_port():
    with socket.socket() as s:
        s.bind(('0.0.0.0', 0))
        return s.getsockname()[1]


def _relay_script(tmp_path, *args, bot_env):
    """setup_ssh_entry_relay.sh for real (docker, ss, timeout), as root by
    the fake `id` only."""
    fake = tmp_path / 'relay-bin'
    if not fake.exists():
        fake.mkdir()
        for name, body in STAND_BIN.items():
            (fake / name).write_text(body)
            (fake / name).chmod(0o755)
    env = {k: v for k, v in os.environ.items() if not k.startswith(('SSH_', 'ENTRY_NODE'))}
    env.update(PATH=f"{fake}:{env['PATH']}", SSH_RELAY_DIR=str(tmp_path / 'etc-ssh-relay'),
               SSH_RELAY_ENV=str(bot_env), SSH_RELAY_SETTLE_S='2',
               TRIPWIRE_LOG=str(tmp_path / 'tripwire.log'))
    r = subprocess.run(['bash', str(RELAY_SCRIPT), *args], env=env, capture_output=True,
                       text=True, timeout=180)
    assert not (tmp_path / 'tripwire.log').exists(), (tmp_path / 'tripwire.log').read_text()
    return r


@pytest.mark.requires_docker
@pytest.mark.asyncio
async def test_stand_the_relay_carries_the_probe_to_the_scripts_sshd(tmp_path, monkeypatch):
    """Offline, end to end, with the script's own container:

    * setup_ssh_entry_relay.sh (only `id` faked: it wants root) runs its
      haproxy on this host's network, in front of the sshd setup_ssh_exit.sh
      configures, on a docker network — and reads ssh-exit's banner through it;
    * the generated sidecar config in mihomo 1.19.33 binds both SSH
      listeners; the real HealthChecker through the relay gets ten ok rows
      and a full bulk download, under exit's pinned host key;
    * the relay stopped: dark rows through it (never proxy_down) while
      ssh-exit answers directly; three runs page protocol_down:ssh-entry
      alone, as a warn;
    * setup re-run brings it back; --remove takes it."""
    import aiohttp.connector
    import ssl as ssl_mod
    if not (_have(MIHOMO) and _have(PYTHON) and _have(HAPROXY) and _have(ALPINE)):
        pytest.skip('stand images not pulled')
    web, ca_pem = _stand_tls(tmp_path)
    image, sshd = _sshd_image(tmp_path)
    host_key = _host_key(image, sshd, 'ssh_host_ed25519_key')
    port = _free_port()
    if port < 1024:
        pytest.skip('no unprivileged port')
    db = Database(str(tmp_path / 'bot.db'))
    probe = add_probe_user(db)
    gen = _load('gen_probe_config_mihomo.py')
    ctx = ssl_mod.create_default_context(cafile=str(ca_pem))
    monkeypatch.setattr(aiohttp.connector, '_SSL_CONTEXT_VERIFIED', ctx)

    def checker(config):
        hc = HealthChecker(db, config)
        hc.TARGET_DOMAINS = [f'{WEB_IP}/{t}' for t in TARGETS]
        hc.BULK_URL = f'https://{WEB_IP}/bulk?bytes={{bytes}}'
        return hc

    def rows(tag):
        return [r for r in health_rows(db) if r[0] == tag]

    for c in STAND_CONTAINERS + ('ssh-relay',):
        _docker('rm', '-f', c, check=False)
    _docker('network', 'rm', STAND_NET, check=False)
    if subprocess.run(['docker', 'network', 'create', '--subnet', STAND_SUBNET, STAND_NET],
                      capture_output=True).returncode:
        pytest.skip(f'could not create the stand network {STAND_SUBNET}')
    try:
        gateway = _docker('network', 'inspect', '-f', '{{(index .IPAM.Config 0).Gateway}}',
                          STAND_NET)
        cfg_ = _config(SSH_EXIT_HOST=SSHD_IP, SSH_EXIT_PORT='22', SSH_HOST_KEY=host_key,
                       SSH_USER_SECRET='stand-secret', ENTRY_NODE_IP=gateway,
                       SSH_ENTRY_PORT=str(port), PROBE_PROXY_MIHOMO_HOST=MIHOMO_IP)
        (sshd / 'users.plan').write_text(''.join(
            f"{u['user']}\t{u['hash']}\n" for u in ssh_exit.users_payload(db, cfg_)))
        _docker('run', '-d', '--name', 'e-probe-web', '--network', STAND_NET, '--ip', WEB_IP,
                '-v', f'{web}:/srv:ro', PYTHON, 'python', '/srv/server.py')
        _docker('run', '-d', '--name', 'e-probe-sshd', '--network', STAND_NET, '--ip', SSHD_IP,
                '--cap-add', 'NET_ADMIN',
                '-v', f'{sshd}/sshd_config:/etc/ssh/sshd_config:ro',
                '-v', f'{sshd}/keys:/etc/ssh/keys:ro',
                '-v', f'{sshd}/entrypoint.sh:/entrypoint.sh:ro',
                '-v', f'{sshd}/users.plan:/etc/ssh/users.plan:ro', image)
        for name, ip, p in (('e-probe-web', WEB_IP, 443), ('e-probe-sshd', SSHD_IP, 22)):
            if not _wait_port(ip, p):
                pytest.skip(f'{name} ({ip}:{p}) not reachable from this host')

        # --- the script, for real: its container on the host network
        bot_env = tmp_path / 'bot.env'
        bot_env.write_text(f'BOT_TOKEN=never-printed\nENTRY_NODE_IP={gateway}\n'
                           f'SSH_EXIT_HOST={SSHD_IP}\nSSH_EXIT_PORT=22\n'
                           f'SSH_ENTRY_PORT={port}\n')
        r = _relay_script(tmp_path, bot_env=bot_env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert f'ssh-relay on :{port} -> {SSHD_IP}:22' in r.stdout
        assert 'through the relay: SSH-2.0-OpenSSH_' in r.stdout
        assert f'SSH_RELAY_IPS={gateway}' in r.stdout and 'never-printed' not in r.stdout
        state = _docker('inspect', '-f', '{{.HostConfig.NetworkMode}} {{.HostConfig.ReadonlyRootfs}} '
                        '{{.HostConfig.CapDrop}} {{.Config.User}}', 'ssh-relay')
        assert state == 'host true [ALL] haproxy', state
        # can a container reach this host's own port at all?
        seen = subprocess.run(['docker', 'run', '--rm', '--network', STAND_NET, ALPINE, 'sh',
                               '-c', f'nc -w 3 {gateway} {port} </dev/null | head -c 8'],
                              capture_output=True, text=True, timeout=60).stdout
        if seen != 'SSH-2.0-':
            pytest.skip(f'containers cannot reach this host on :{port} (a host firewall?)')

        # --- the probe through the relay
        mih = gen.build_config(cfg_, probe)
        assert [lst['port'] for lst in mih['listeners']] == [18088, 18089]
        _run_sidecar(tmp_path, mih)
        assert _wait_port(MIHOMO_IP, 18089)
        res = await checker(cfg_)._check_protocol('ssh-entry')
        assert {d: v['status'] for d, v in res.items()} == {
            f'{WEB_IP}/{t}': 'ok' for t in TARGETS}, res
        assert [(st, lat is not None) for _t, _d, st, lat in rows('ssh-entry')] == [('ok', True)] * 10
        assert bulk_rows(db) == [('ssh-entry', 'ok', HealthChecker.BULK_BYTES)]

        # --- the relay down, ssh-exit up: the path through entry is dark
        _docker('stop', '-t', '1', 'ssh-relay')
        for _ in range(pr.PROBE_RUNS):
            assert await checker(cfg_)._check_protocol('ssh-entry') is not None
        dark = rows('ssh-entry')[10:]
        assert len(dark) == 30 and all(st != 'ok' and lat is None for _t, _d, st, lat in dark)
        assert not any(st == 'proxy_down' for _t, _d, st, _l in dark)
        direct = await checker(cfg_)._check_protocol('ssh')
        assert {v['status'] for v in direct.values()} == {'ok'}, direct
        with db._connect() as conn:            # the window the pager reads
            conn.execute("DELETE FROM outbound_health WHERE outbound_tag = 'ssh-entry' AND id <= 10")
            conn.commit()
        for tag in CASCADE:
            seed_window(db, tag, 10)
        seed_window(db, 'ssh', 10)
        alerts = pager(db)
        assert list(alerts) == ['protocol_down:ssh-entry'], alerts
        assert alerts['protocol_down:ssh-entry'].severity == 'warn'

        # --- setup again: the relay is back
        r = _relay_script(tmp_path, bot_env=bot_env)
        assert r.returncode == 0, r.stdout + r.stderr
        assert 'through the relay: SSH-2.0-OpenSSH_' in r.stdout
        res = await checker(cfg_)._check_protocol('ssh-entry')
        assert {v['status'] for v in res.values()} == {'ok'}, res

        # --- --remove
        r = _relay_script(tmp_path, '--remove', bot_env=bot_env)
        assert r.returncode == 0 and 'SSH_ENTRY_PORT is still set' in r.stdout
        assert _docker('ps', '-aq', '--filter', 'name=^ssh-relay$') == ''
        assert (tmp_path / 'etc-ssh-relay' / 'haproxy.cfg').exists()
    finally:
        for c in STAND_CONTAINERS + ('ssh-relay',):
            _docker('rm', '-f', c, check=False)
        _docker('network', 'rm', STAND_NET, check=False)


PENALTY_NET, PENALTY_SUBNET = 'nekovpn-ssh-penalty-stand', '192.0.2.0/24'
P_SSHD, P_RELAY, P_OTHER = '192.0.2.10', '192.0.2.20', '192.0.2.30'


def _sshd_on(tmp_path, base, tag):
    """The sshd of setup_ssh_exit.sh (its config and entrypoint) on ``base``."""
    def printed(flag):
        return subprocess.run(['bash', str(EXIT_SCRIPT), flag], capture_output=True, text=True,
                              check=True).stdout
    d = tmp_path / tag.split(':')[0]
    d.mkdir()
    (d / 'sshd_config').write_text(printed('--print-config'))
    (d / 'entrypoint.sh').write_text(printed('--print-entrypoint'))
    (d / 'entrypoint.sh').chmod(0o755)
    (d / 'keys').mkdir()
    (d / 'users.plan').write_text('')
    build = subprocess.run(['docker', 'build', '-q', '-t', tag, '-'], input=(
        f'FROM {base}\nRUN apk add --no-cache openssh-server shadow iptables procps && addgroup vpn\n'
        'CMD ["/entrypoint.sh"]\n'), capture_output=True, text=True, timeout=600)
    if build.returncode:
        pytest.skip(f'could not build {tag}: {build.stderr[-200:]}')
    _docker('run', '--rm', '-v', f'{d}/keys:/k', tag, 'ssh-keygen', '-q', '-t', 'ed25519',
            '-N', '', '-f', '/k/ssh_host_ed25519_key')
    return d


def _run_sshd(name, tag, d, relays):
    _docker('run', '-d', '--name', name, '--network', PENALTY_NET, '--ip', P_SSHD,
            '--cap-add', 'NET_ADMIN', '-e', f'SSH_RELAY_IPS={relays}',
            '-v', f'{d}/sshd_config:/etc/ssh/sshd_config:ro',
            '-v', f'{d}/keys:/etc/ssh/keys:ro',
            '-v', f'{d}/entrypoint.sh:/entrypoint.sh:ro',
            '-v', f'{d}/users.plan:/etc/ssh/users.plan:ro', tag)


def _burst_then_banner(ip, n=40):
    """From ``ip``: ``n`` connections that never authenticate, then one more
    — the first line it reads back: the banner, or the refusal sshd 9.8+
    sends a penalised address."""
    script = (f'for i in $(seq {n}); do nc -w 2 {P_SSHD} 22 </dev/null >/dev/null 2>&1; done; '
              f'nc -w 3 {P_SSHD} 22 </dev/null | head -n 1')
    return subprocess.run(['docker', 'run', '--rm', '--network', PENALTY_NET, '--ip', ip,
                           ALPINE, 'sh', '-c', script],
                          capture_output=True, text=True, timeout=180).stdout.strip()


@pytest.mark.requires_docker
@pytest.mark.requires_network
def test_stand_sshd_exempts_the_relay_from_its_penalties(tmp_path):
    """The trap SSH_RELAY_IPS closes, on real sshds with setup_ssh_exit.sh's
    own config and entrypoint (the images are built with apk, hence the
    network):

    * OpenSSH 9.9 (alpine 3.21): a burst of connections that never
      authenticate gets an address refused («Not allowed at this time») —
      but not the relay's address; without SSH_RELAY_IPS the relay too;
    * OpenSSH 9.7 (alpine 3.20, the image ssh-exit runs): no penalties, and
      it starts with SSH_RELAY_IPS set all the same."""
    if not _have(ALPINE):
        pytest.skip('alpine 3.20 not pulled')
    new = _sshd_on(tmp_path, 'alpine:3.21', 'nekovpn-sshd-99:local')
    base = next(line.split('=', 1)[1] for line in EXIT_SCRIPT.read_text().splitlines()
                if line.startswith('BASE_IMAGE='))
    old = _sshd_on(tmp_path, base, 'nekovpn-sshd-97:local')
    _docker('rm', '-f', 'e-sshd-penalty', check=False)
    _docker('network', 'rm', PENALTY_NET, check=False)
    if subprocess.run(['docker', 'network', 'create', '--subnet', PENALTY_SUBNET, PENALTY_NET],
                      capture_output=True).returncode:
        pytest.skip(f'could not create the stand network {PENALTY_SUBNET}')
    try:
        _run_sshd('e-sshd-penalty', 'nekovpn-sshd-99:local', new, P_RELAY)
        if not _wait_port(P_SSHD, 22):
            pytest.skip('the sshd is not reachable from this host: '
                        + _docker('logs', 'e-sshd-penalty', check=False)[-300:])
        assert 'PerSourcePenaltyExemptList' in _docker(
            'exec', 'e-sshd-penalty', 'sh', '-c', 'cat /proc/1/cmdline | tr "\\0" " "')
        assert _burst_then_banner(P_OTHER) == 'Not allowed at this time'
        assert _burst_then_banner(P_RELAY).startswith('SSH-2.0-OpenSSH_9.9'), 'relay refused'

        # the control: the same sshd without the exemption refuses the relay too
        _docker('rm', '-f', 'e-sshd-penalty')
        _run_sshd('e-sshd-penalty', 'nekovpn-sshd-99:local', new, '')
        assert _wait_port(P_SSHD, 22), _docker('logs', 'e-sshd-penalty', check=False)[-300:]
        assert _burst_then_banner(P_RELAY) == 'Not allowed at this time'

        _docker('rm', '-f', 'e-sshd-penalty')
        _run_sshd('e-sshd-penalty', 'nekovpn-sshd-97:local', old, P_RELAY)
        assert _wait_port(P_SSHD, 22), _docker('logs', 'e-sshd-penalty', check=False)[-300:]
        assert 'PerSourcePenaltyExemptList' not in _docker(
            'exec', 'e-sshd-penalty', 'sh', '-c', 'cat /proc/1/cmdline | tr "\\0" " "')
        assert _burst_then_banner(P_OTHER).startswith('SSH-2.0-OpenSSH_9.7')
    finally:
        _docker('rm', '-f', 'e-sshd-penalty', check=False)
        _docker('network', 'rm', PENALTY_NET, check=False)
