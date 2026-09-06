"""cascade_auto merge in MyKeyAnswerHandler.get_cascade_order.

DPIMonitor (bot/services/dpi_monitor.py) writes ``app_settings.cascade_auto``;
this is the read side: ``get_auto_demotions`` tolerates anything that
lands in that key, and ``get_cascade_order`` applies the result as a
STABLE PARTITION on top of the operator's order — demoted protocols to
the tail, relative order kept, nothing dropped, tier filter still last,
``apply_auto=False`` for the dashboard editor which must show what the
operator saved rather than what the monitor did to it this hour.

Pure unit level (dict-backed fake db); the sqlite end-to-end path is in
tests/integration/test_dpi_monitor.py.
"""

import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot.handlers.callbacks.user import MyKeyAnswerHandler as H


class FakeDB:
    def __init__(self, **settings):
        self.settings = dict(settings)

    def get_setting(self, key, default=None):
        return self.settings.get(key, default)


def auto_json(global_=(), **asn):
    return json.dumps({
        'global': {p: {'since': 't', 'reason': 'probe_dark'} for p in global_},
        'asn': {a: {p: {'since': 't', 'reason': 'reality_asn'} for p in ps}
                for a, ps in asn.items()},
    })


def user(status='paid', asn=None, country=None):
    return SimpleNamespace(status=status, last_asn=asn, last_country=country)


class TestGetAutoDemotions:

    def test_nothing_stored_is_an_empty_set(self):
        assert H.get_auto_demotions(FakeDB()) == set()
        assert H.get_auto_demotions(FakeDB(), 'AS31133') == set()

    def test_none_db_is_an_empty_set(self):
        assert H.get_auto_demotions(None, 'AS31133') == set()

    def test_global_entries_apply_to_everyone(self):
        db = FakeDB(cascade_auto=auto_json(global_=('ws',)))
        assert H.get_auto_demotions(db) == {'ws'}
        assert H.get_auto_demotions(db, 'AS31133') == {'ws'}

    def test_asn_entries_apply_to_that_asn_only_union_with_global(self):
        db = FakeDB(cascade_auto=auto_json(global_=('ws',), AS31133=('reality', 'hy2')))
        assert H.get_auto_demotions(db, 'AS31133') == {'ws', 'reality', 'hy2'}
        assert H.get_auto_demotions(db, 'AS8359') == {'ws'}
        assert H.get_auto_demotions(db, None) == {'ws'}

    def test_asn_lookup_is_case_and_whitespace_insensitive(self):
        db = FakeDB(cascade_auto=auto_json(AS31133=('reality',)))
        assert H.get_auto_demotions(db, ' as31133 ') == {'reality'}

    def test_unknown_protocol_names_are_ignored(self):
        db = FakeDB(cascade_auto=auto_json(global_=('xhttp', 'ws', 'trojan')))
        assert H.get_auto_demotions(db) == {'ws'}

    @pytest.mark.parametrize('raw', [
        '{not json', '[]', '"ws"', '42', '', '   ',
        json.dumps({'global': ['ws']}),                # wrong container type
        json.dumps({'asn': {'AS31133': ['reality']}}),
        json.dumps({'global': None, 'asn': None}),
    ])
    def test_bad_or_odd_json_collapses_to_empty(self, raw):
        db = FakeDB(cascade_auto=raw)
        assert H.get_auto_demotions(db, 'AS31133') == set()

    def test_mock_db_returning_a_mock_is_empty(self):
        """Many handler tests hand in a bare Mock db; get_setting then
        returns a Mock, which must not blow up json.loads in /sub."""
        assert H.get_auto_demotions(Mock(), 'AS31133') == set()

    def test_non_string_asn_is_treated_as_no_asn(self):
        """user.last_asn from a Mock user (or a stray int) must not turn
        the /sub path into an AttributeError on .strip()."""
        db = FakeDB(cascade_auto=auto_json(global_=('ws',), AS31133=('reality',)))
        assert H.get_auto_demotions(db, 31133) == {'ws'}
        assert H.get_auto_demotions(db, Mock()) == {'ws'}
        assert H.get_cascade_order(db, user=SimpleNamespace(
            status='paid', last_asn=Mock(), last_country=None))[-1] == 'ws'

    def test_a_raising_get_setting_is_an_empty_set(self):
        db = Mock()
        db.get_setting = Mock(side_effect=RuntimeError('locked'))
        assert H.get_auto_demotions(db, 'AS31133') == set()


class TestMerge:

    def test_default_order_with_one_global_demotion(self):
        db = FakeDB(cascade_auto=auto_json(global_=('ws',)))
        assert H.get_cascade_order(db, user=user()) == ('hy2t', 'stls', 'hy2', 'reality', 'ws')

    def test_no_demotions_leaves_the_order_alone(self):
        assert H.get_cascade_order(FakeDB(), user=user()) == H.DEFAULT_CASCADE_ORDER
        assert H.get_cascade_order(None, user=user()) == H.DEFAULT_CASCADE_ORDER

    def test_partition_is_stable_for_several_demoted(self):
        db = FakeDB(cascade_auto=auto_json(global_=('stls', 'ws')))
        assert H.get_cascade_order(db, user=user()) == ('hy2t', 'hy2', 'reality', 'stls', 'ws')
        db = FakeDB(cascade_auto=auto_json(global_=('hy2t', 'hy2')))
        assert H.get_cascade_order(db, user=user()) == ('stls', 'ws', 'reality', 'hy2t', 'hy2')

    def test_nothing_is_ever_removed(self):
        db = FakeDB(cascade_auto=auto_json(global_=H.DEFAULT_CASCADE_ORDER))
        assert H.get_cascade_order(db, user=user()) == H.DEFAULT_CASCADE_ORDER
        db = FakeDB(cascade_auto=auto_json(global_=('reality', 'ws')))
        assert set(H.get_cascade_order(db, user=user())) == set(H.DEFAULT_CASCADE_ORDER)

    def test_tier_filter_runs_after_the_merge(self):
        """A demo user sees the same demotion a paid one does — ws to the
        tail of the free ladder — and still never sees hy2t/reality."""
        db = FakeDB(cascade_auto=auto_json(global_=('ws',)))
        assert H.get_cascade_order(db, user=user('demo')) == ('stls', 'hy2', 'ws')
        db = FakeDB(cascade_auto=auto_json(global_=('hy2t', 'reality')))
        assert H.get_cascade_order(db, user=user('demo')) == ('stls', 'ws', 'hy2')

    def test_operator_asn_override_stays_the_base_order(self):
        """cascade_by_asn decides the ladder; auto only moves within it."""
        db = FakeDB(
            cascade_by_asn=json.dumps({'AS31133': ['reality', 'hy2', 'stls', 'ws', 'hy2t']}),
            cascade_auto=auto_json(AS31133=('reality',)),
        )
        assert H.get_cascade_order(db, asn='AS31133', user=user()) == (
            'hy2', 'stls', 'ws', 'hy2t', 'reality')
        # The override is still what the operator wrote.
        assert H.get_asn_cascade(db, 'AS31133') == ('reality', 'hy2', 'stls', 'ws', 'hy2t')

    def test_asn_demotion_does_not_leak_to_other_asns_or_global(self):
        db = FakeDB(cascade_auto=auto_json(AS31133=('hy2t',)))
        assert H.get_cascade_order(db, asn='AS31133', user=user())[0] == 'stls'
        assert H.get_cascade_order(db, asn='AS8359', user=user())[0] == 'hy2t'
        assert H.get_cascade_order(db, user=user())[0] == 'hy2t'

    def test_asn_comes_from_the_user_when_not_passed(self):
        db = FakeDB(cascade_auto=auto_json(AS31133=('hy2t',)))
        assert H.get_cascade_order(db, user=user(asn='AS31133'))[0] == 'stls'
        assert H.get_cascade_order(db, user=user(asn=None))[0] == 'hy2t'

    def test_explicit_asn_wins_over_the_users_stored_one(self):
        """/sub knows the request-time IP; the stored last_asn may be
        from a previous network."""
        db = FakeDB(cascade_auto=auto_json(AS31133=('hy2t',)))
        assert H.get_cascade_order(db, user=user(asn='AS31133'), asn='AS8359')[0] == 'hy2t'
        assert H.get_cascade_order(db, user=user(asn='AS8359'), asn='AS31133')[0] == 'stls'

    def test_country_ladder_plus_global_demotion(self):
        db = FakeDB(cascade_auto=auto_json(global_=('reality',)))
        # KZ is direct-first: reality second by default, tail once demoted.
        assert H.get_cascade_order(db, country='KZ', user=user()) == (
            'hy2t', 'hy2', 'stls', 'ws', 'reality')

    def test_global_and_asn_demotions_combine(self):
        db = FakeDB(cascade_auto=auto_json(global_=('ws',), AS31133=('hy2t',)))
        assert H.get_cascade_order(db, asn='AS31133', user=user()) == (
            'stls', 'hy2', 'reality', 'hy2t', 'ws')

    def test_apply_auto_false_returns_the_raw_operator_order(self):
        db = FakeDB(
            cascade_by_asn=json.dumps({'AS31133': ['reality', 'hy2']}),
            cascade_auto=auto_json(global_=('ws',), AS31133=('reality',)),
        )
        assert H.get_cascade_order(db, asn='AS31133', apply_auto=False) == (
            'reality', 'hy2', 'hy2t', 'stls', 'ws')
        assert H.get_cascade_order(db, apply_auto=False) == H.DEFAULT_CASCADE_ORDER
        # and it is keyword-only, so no positional caller can flip it by accident
        with pytest.raises(TypeError):
            H.get_cascade_order(db, None, None, None, False)   # type: ignore[misc]

    def test_disabled_protocols_stay_disabled_regardless_of_auto(self):
        """A demotion of something the operator disabled must not
        resurrect it — the enabled set is decided before the merge."""
        db = FakeDB(
            cascade_protocol_order=json.dumps([
                {'name': 'hy2t', 'enabled': True}, {'name': 'stls', 'enabled': True},
                {'name': 'ws', 'enabled': False}, {'name': 'hy2', 'enabled': True},
                {'name': 'reality', 'enabled': True}]),
            cascade_auto=auto_json(global_=('ws', 'stls')),
        )
        assert H.get_cascade_order(db, user=user()) == ('hy2t', 'hy2', 'reality', 'stls')

    def test_user_none_preview_gets_the_merged_unfiltered_order(self):
        db = FakeDB(cascade_auto=auto_json(global_=('hy2t',)))
        assert H.get_cascade_order(db) == ('stls', 'ws', 'hy2', 'reality', 'hy2t')
