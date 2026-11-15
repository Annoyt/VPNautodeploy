"""The journal of demotions for everyone (bot/services/demotion_spans.py),
kept for the inbound-drop alert: sampling, the tail, reading it back."""

import json
from datetime import datetime, timedelta, timezone

from bot.services import demotion_spans as ds

NOW = datetime(2026, 11, 15, 12, 0, 0)
H = timedelta(hours=1)


def at(h):
    """``h`` hours before NOW."""
    return NOW - h * H


def epoch(ts):
    return ts.replace(tzinfo=timezone.utc).timestamp()


class TestUpdate:

    def test_a_new_demotion_opens_a_span_from_its_since(self):
        spans, changed = ds.update({}, {'ws': at(5)}, NOW)
        assert changed and spans == {'ws': [[at(5), None]]}

    def test_an_open_span_is_left_as_it_is(self):
        before = {'ws': [[at(5), None]]}
        spans, changed = ds.update(before, {'ws': at(1)}, NOW)    # extended: since moved
        assert not changed and spans == before

    def test_a_restore_ends_the_span_now(self):
        spans, changed = ds.update({'ws': [[at(5), None]]}, {}, NOW)
        assert changed and spans == {'ws': [[at(5), NOW]]}

    def test_a_since_before_the_last_end_reopens_it(self):
        # the bot was down while the demotion went on
        spans, changed = ds.update({'ws': [[at(9), at(2)]]}, {'ws': at(3)}, NOW)
        assert changed and spans == {'ws': [[at(9), None]]}
        spans, _ = ds.update({'ws': [[at(4), at(2)]]}, {'ws': at(6)}, NOW)
        assert spans == {'ws': [[at(6), None]]}

    def test_a_later_demotion_gets_a_span_of_its_own(self):
        spans, changed = ds.update({'ws': [[at(9), at(5)]]}, {'ws': at(2)}, NOW)
        assert changed and spans == {'ws': [[at(9), at(5)], [at(2), None]]}

    def test_a_since_ahead_of_now_starts_now(self):
        spans, _ = ds.update({}, {'ws': NOW + H}, NOW)
        assert spans == {'ws': [[NOW, None]]}

    def test_old_spans_go_open_ones_stay(self):
        old = NOW - timedelta(days=ds.KEEP_DAYS, hours=1)
        kept = NOW - timedelta(days=ds.KEEP_DAYS) + H
        spans, changed = ds.update(
            {'ws': [[old - H, old], [kept - H, kept]], 'reality': [[old - H, None]]},
            {'reality': old - H}, NOW)
        assert changed
        assert spans == {'ws': [[kept - H, kept]], 'reality': [[old - H, None]]}
        spans, changed = ds.update({'stls': [[old - H, old]]}, {}, NOW)
        assert changed and spans == {}

    def test_nothing_to_do_changes_nothing(self):
        before = {'ws': [[at(9), at(5)]]}
        assert ds.update(before, {}, NOW) == (before, False)


class TestCovers:

    def covers(self, spans, a_h, b_h, tail_h=24):
        """Does it cover the interval from ``a_h`` to ``b_h`` hours ago?"""
        return ds.covers(spans, 'ws', epoch(at(a_h)), epoch(at(b_h)), epoch(NOW),
                         tail_h * 3600)

    def test_inside_and_before(self):
        spans = {'ws': [[at(30), at(24)]]}
        assert self.covers(spans, 28, 26)
        assert not self.covers(spans, 32, 30)            # ends where the span starts

    def test_the_tail_lasts_as_long_as_the_demotion(self):
        spans = {'ws': [[at(30), at(24)]]}               # six hours
        assert self.covers(spans, 19, 18)
        assert not self.covers(spans, 18, 17)

    def test_at_most_the_cap(self):
        spans = {'ws': [[at(100), at(50)]]}              # 50 h, cap a day
        assert self.covers(spans, 27, 26)
        assert not self.covers(spans, 26, 25)

    def test_an_open_span_runs_to_now(self):
        spans = {'ws': [[at(5), None]]}
        assert self.covers(spans, 1, 0)
        assert not self.covers(spans, 7, 6)

    def test_only_its_own_protocol(self):
        assert not self.covers({'reality': [[at(5), None]]}, 1, 0)


class TestReadBack:

    def test_round_trip(self):
        spans = {'ws': [[at(5), None]], 'reality': [[at(9), at(8)]]}
        raw = ds.dump(spans)
        assert json.loads(raw)['ws'] == [['2026-11-15T07:00:00', None]]
        assert ds.load_spans(raw) == spans

    def test_bad_json_or_none_is_an_empty_journal(self):
        assert ds.load_spans('{not json') == {}
        assert ds.load_spans(None) == {}
        assert ds.load_spans('[]') == {}

    def test_malformed_spans_are_left_out_the_rest_sorted(self):
        raw = json.dumps({
            'ws': [['2026-11-15T10:00:00', None],
                   ['2026-11-15T01:00:00', '2026-11-15T02:00:00'],
                   ['nonsense', None],
                   ['2026-11-15T10:00:00'],
                   ['2026-11-15T10:00:00', '2026-11-15T09:00:00'],
                   ['2026-11-15T08:00:00', 'bad'],
                   'x'],
            'reality': 'y',
            'stls': [],
        })
        assert ds.load_spans(raw) == {'ws': [[datetime(2026, 11, 15, 1), datetime(2026, 11, 15, 2)],
                                             [datetime(2026, 11, 15, 10), None]]}


class TestGlobalDemotions:

    def test_both_stores_their_global_part_only(self):
        auto = json.dumps({
            'global': {'ws': {'since': '2026-11-15T06:00:00', 'reason': 'probe_bulk'}},
            'asn': {'AS31133': {'reality': {'since': '2026-11-15T01:00:00'}}}})
        manual = json.dumps({
            'global': {'hy2': {'since': '2026-11-15T07:00:00', 'until': '2026-11-16T00:00:00'}},
            'asn': {'AS1': {'stls': {'since': '2026-11-15T07:00:00',
                                     'until': '2026-11-16T00:00:00'}}}})
        assert ds.global_demotions(auto, manual, NOW) == {
            'ws': datetime(2026, 11, 15, 6), 'hy2': datetime(2026, 11, 15, 7)}

    def test_the_earliest_since_of_the_two(self):
        auto = json.dumps({'global': {'ws': {'since': '2026-11-15T06:00:00'}}})
        manual = json.dumps({'global': {'ws': {'since': '2026-11-15T03:00:00',
                                               'until': '2026-11-16T00:00:00'}}})
        assert ds.global_demotions(auto, manual, NOW) == {'ws': datetime(2026, 11, 15, 3)}
        assert ds.global_demotions(auto, manual.replace('03:00', '09:00'), NOW) == {
            'ws': datetime(2026, 11, 15, 6)}

    def test_an_expired_hand_entry_is_none(self):
        manual = json.dumps({'global': {'ws': {'since': '2026-11-14T03:00:00',
                                               'until': '2026-11-15T11:59:00'}}})
        assert ds.global_demotions(None, manual, NOW) == {}

    def test_an_unreadable_since_or_one_ahead_is_now(self):
        auto = json.dumps({'global': {'ws': {'since': 'soon'}, 'hy2': {},
                                      'stls': {'since': '2026-11-15T13:00:00'}}})
        assert ds.global_demotions(auto, None, NOW) == {'ws': NOW, 'hy2': NOW, 'stls': NOW}

    def test_bad_json_is_nothing(self):
        assert ds.global_demotions('{x', '{y', NOW) == {}
        assert ds.global_demotions(None, None, NOW) == {}
