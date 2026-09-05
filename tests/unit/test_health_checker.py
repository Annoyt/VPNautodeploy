"""HealthChecker's probe set is config-driven: hy2t (:18085) only with HY2T_PORT.

Why: a checker that probes a port the sidecar does not listen on writes
proxy_down rows (latency NULL) every 15 min, and check_protocol_probe_down
derives its tags from DISTINCT outbound_tag — so a deployment without
turbo would be paged protocol_down:hy2t forever for a protocol it does
not run. The four base tags stay class constants (the pager and the
card import them); hy2t joins ONLY through probe_tags_for(config).

The write path is exercised against a real sqlite bot.db with the
network call (_check_one) stubbed — the rows and their outbound_tag are
what every consumer keys on.
"""

import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import pytest

from bot.core.database import Database
from bot.services.health_checker import HealthChecker


BASE = ['reality', 'hy2', 'ws', 'stls']


def cfg(**over):
    return SimpleNamespace(**{'HY2T_PORT': '', **over})


class TestProbeSetByConfig:

    def test_base_four_stay_class_constants(self):
        assert HealthChecker.PROTOCOL_TAGS == BASE
        assert HealthChecker.PROBE_PORTS == {'reality': 18081, 'hy2': 18082,
                                             'ws': 18083, 'stls': 18084}
        assert 'hy2t' not in HealthChecker.PROBE_PORTS

    def test_hy2t_joins_with_a_numeric_port(self):
        assert HealthChecker.probe_tags_for(cfg(HY2T_PORT='8402')) == BASE + ['hy2t']
        assert HealthChecker.probe_ports_for(cfg(HY2T_PORT='8402'))['hy2t'] == 18085
        assert HealthChecker.probe_ports_for(cfg(HY2T_PORT=' 8402 '))['hy2t'] == 18085

    @pytest.mark.parametrize('raw', ['', '   ', 'oops', '0', None])
    def test_anything_else_keeps_hy2t_out(self, raw):
        """Same gate as subscription._build_hy2t: those values keep turbo
        out of subscriptions, so they must keep it out of the probes."""
        assert HealthChecker.probe_tags_for(cfg(HY2T_PORT=raw)) == BASE
        assert 'hy2t' not in HealthChecker.probe_ports_for(cfg(HY2T_PORT=raw))

    def test_no_config_or_a_mock_config_is_disabled(self):
        assert HealthChecker.probe_tags_for(None) == BASE
        assert HealthChecker.probe_tags_for(Mock()) == BASE          # Mock attrs are not ports

    def test_enabling_does_not_mutate_the_class_table(self):
        HealthChecker.probe_ports_for(cfg(HY2T_PORT='8402'))
        assert 'hy2t' not in HealthChecker.PROBE_PORTS
        assert HealthChecker.PROTOCOL_TAGS == BASE

    def test_instance_properties_and_proxy_url(self):
        on = HealthChecker(Mock(), cfg(HY2T_PORT='8402'))
        assert on.protocol_tags == BASE + ['hy2t']
        assert on.probe_ports['hy2t'] == 18085
        assert on._proxy_url('hy2t') == 'http://probe-proxy:18085'
        assert on._proxy_url('reality') == 'http://probe-proxy:18081'

        off = HealthChecker(Mock(), cfg())
        assert off.protocol_tags == BASE
        assert off._proxy_url('hy2t') is None

    def test_properties_follow_a_swapped_config(self):
        hc = HealthChecker(Mock(), cfg())
        assert 'hy2t' not in hc.protocol_tags
        hc.config = cfg(HY2T_PORT='8402')
        assert hc.protocol_tags[-1] == 'hy2t'

    def test_probe_proxy_host_override_applies_to_hy2t_too(self):
        hc = HealthChecker(Mock(), cfg(HY2T_PORT='8402', PROBE_PROXY_HOST='127.0.0.1'))
        assert hc._proxy_url('hy2t') == 'http://127.0.0.1:18085'


class TestRowsWritten:

    @pytest.fixture
    def db(self, tmp_path):
        return Database(str(tmp_path / 'bot.db'))

    OK = {'status': 'ok', 'latency_ms': 120, 'error': None}

    def _run(self, hc):
        with patch.object(hc, '_check_one', new=AsyncMock(return_value=self.OK)) as probe:
            results = asyncio.run(hc.check_all_outbounds())
        proxies = {c.args[2] for c in probe.call_args_list}
        return results, proxies

    def _counts(self, db):
        with db._connect() as conn:
            return dict(conn.execute(
                "SELECT outbound_tag, COUNT(*) FROM outbound_health GROUP BY outbound_tag"
            ).fetchall())

    def test_enabled_writes_hy2t_rows_through_18085(self, db):
        results, proxies = self._run(HealthChecker(db, cfg(HY2T_PORT='8402')))

        assert 'hy2t' in results
        assert 'http://probe-proxy:18085' in proxies
        counts = self._counts(db)
        # One row per target domain per tag, tagged with the protocol
        # short-name — the key the pager/card/healthcheck look up.
        assert counts['hy2t'] == len(HealthChecker.TARGET_DOMAINS)
        assert set(counts) == set(BASE + ['hy2t'])

    def test_disabled_writes_no_hy2t_rows_and_never_knocks_on_18085(self, db):
        results, proxies = self._run(HealthChecker(db, cfg()))

        assert 'hy2t' not in results
        assert not any(p and p.endswith(':18085') for p in proxies)
        counts = self._counts(db)
        assert 'hy2t' not in counts
        assert set(counts) == set(BASE)
        assert all(n == len(HealthChecker.TARGET_DOMAINS) for n in counts.values())
