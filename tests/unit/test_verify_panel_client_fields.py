"""The post-deploy panel audit must recognise the 2026-09-01 damage.

A guard nobody tested is a guard that reports OK on a dead panel. These
pin the two directions that matter: a Reality client without ``flow``
is broken, and a websocket client WITH one is broken too (the mistake a
naive "always set flow" fix would make).
"""

import importlib.util
import json
import os

# Same loader shape as tests/unit/test_exit_dpi_reporter.py — scripts/ is
# not a package, so it is loaded by path.
_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'scripts',
    'verify_panel_client_fields.py',
))
_spec = importlib.util.spec_from_file_location('verify_panel_client_fields',
                                               _PATH)
_audit = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_audit)

audit_all = _audit.audit_all
audit_inbound = _audit.audit_inbound
required_fields = _audit.required_fields


VISION = 'xtls-rprx-vision'


def inbound(iid, protocol, clients, *, network='tcp', security='none',
            enable=True, tag=None, settings_as_dict=False):
    settings = {'clients': clients}
    stream = {'network': network, 'security': security}
    return {
        'id': iid,
        'tag': tag or f'inbound-{iid}',
        'protocol': protocol,
        'enable': enable,
        'settings': settings if settings_as_dict else json.dumps(settings),
        'streamSettings': json.dumps(stream),
    }


def reality(clients, **kw):
    return inbound(1, 'vless', clients, network='tcp', security='reality',
                   tag='inbound-443', **kw)


def shadowsocks(clients, **kw):
    return inbound(8, 'shadowsocks', clients, tag='inbound-8444', **kw)


def ws(clients, **kw):
    return inbound(4, 'vmess', clients, network='ws', security='none',
                   tag='inbound-2053', **kw)


class TestRequiredFields:

    def test_vless_reality_over_tcp_needs_flow(self):
        must, _ = required_fields(reality([]))
        assert 'flow' in must

    def test_vless_over_websocket_must_not_have_flow(self):
        """xtls-rprx-vision is only valid over raw TCP."""
        must, forbidden = required_fields(
            inbound(9, 'vless', [], network='ws', security='tls'))
        assert 'flow' not in must
        assert 'flow' in forbidden

    def test_plain_vless_tls_over_tcp_is_not_policed_for_flow(self):
        """Vision is optional on plain VLESS+TLS. Requiring it there
        would make the audit cry wolf on a future inbound — and an
        audit operators learn to ignore is worse than none."""
        must, forbidden = required_fields(
            inbound(9, 'vless', [], network='tcp', security='tls'))
        assert 'flow' not in must
        assert 'flow' not in forbidden

    def test_shadowsocks_needs_a_per_user_password(self):
        must, _ = required_fields(shadowsocks([]))
        assert 'password' in must


class TestAudit:

    def test_the_incident_is_reported(self):
        """80 of 81 flowless on the Reality inbound."""
        clients = [{'email': f'u{i}@x', 'id': f'uuid{i}'} for i in range(80)]
        clients.append({'email': 'u80@x', 'id': 'uuid80', 'flow': VISION})

        problems = audit_inbound(reality(clients))

        assert len(problems) == 80
        assert "empty 'flow'" in problems[0]
        assert 'inbound-443' in problems[0]

    def test_a_healthy_panel_is_silent(self):
        problems = audit_all([
            reality([{'email': 'u@x', 'id': 'uuid', 'flow': VISION}]),
            shadowsocks([{'email': 'u@x', 'id': 'uuid', 'password': 'pw'}]),
            ws([{'email': 'u@x', 'id': 'uuid'}]),
        ])
        assert problems == []

    def test_flow_leaking_onto_the_websocket_mirror_is_reported(self):
        """The inverse failure: a fix that hardcodes flow everywhere
        would break the CDN mirrors instead. The guard must catch both
        directions or it just moves the outage."""
        problems = audit_inbound(ws([
            {'email': 'u@x', 'id': 'uuid', 'flow': VISION}]))

        assert len(problems) == 1
        assert 'must NOT carry' in problems[0]

    def test_missing_ss_password_is_reported(self):
        problems = audit_inbound(shadowsocks([{'email': 'u@x', 'id': 'uuid'}]))
        assert len(problems) == 1
        assert "empty 'password'" in problems[0]

    def test_disabled_inbounds_are_skipped(self):
        """A disabled inbound serves nobody; failing the deploy on it
        would train people to ignore this check."""
        assert audit_inbound(reality([{'email': 'u@x', 'id': 'u'}],
                                     enable=False)) == []

    def test_settings_already_parsed_as_dict_is_handled(self):
        """Some panel routes hand back settings as a dict, some as a
        JSON string. Guessing wrong makes the audit crash instead of
        reporting — which is how scripts/restore_reality_flow.py fails."""
        ib = reality([{'email': 'u@x', 'id': 'uuid'}], settings_as_dict=True)
        assert len(audit_inbound(ib)) == 1

    def test_empty_client_list_is_silent(self):
        assert audit_inbound(reality([])) == []
