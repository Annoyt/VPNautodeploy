"""/lockdown — the operator's switch for the whitelist / shutdown mode.

Why
---
IMPROVEMENT_PLAN B1+B5: under a regional whitelist ("sovereign
internet") only allowed destinations pass — direct connections to our
entry IP die, the Cloudflare-fronted path (ws) survives. Lockdown flips
the user-facing cascade to fronted-first and sends DNS through the
tunnel; DPIMonitor's detector raises it on its own from the probe
signature. An automatic actor the operator cannot see or override in
one move is worse than none (the 2026-09-01 flow-wipe ran four days for
want of a one-command view and undo), so /lockdown shows the decision
and pins it on / off / hands it back to the detector.

Real sqlite bot.db (Database creates app_settings / admin_actions in
its own migrations). The app_settings JSON ``lockdown_mode`` IS the
contract with bot/services/lockdown.py and get_cascade_order, so the
transitions are pinned three ways: with the module absent (the command
writes the key itself), with a recording fake module (it defers to
set_mode / load_lockdown and never writes behind its back), and with
whatever is really importable (contract-level assertions only).
"""

import json
import sys
import types
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import pytest

from bot.core.database import Database
from bot.handlers.admin.base import ADMIN_HELP_TEXT, AdminHandlerBase
from bot.handlers.admin.ops import (
    AdminOpsMixin, LOCKDOWN_MODE_KEY, LOCKDOWN_ORDER_KEY, LOCKDOWN_ORDER_DEFAULT,
    _project_lockdown, load_lockdown_state, lockdown_order_preview,
)
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK


PAID_DEFAULT = 'hy2t → stls → ws → hy2 → reality'
DEMO_DEFAULT = 'stls → ws → hy2'
PAID_LOCKDOWN = 'ws → stls → reality → hy2 → hy2t'
DEMO_LOCKDOWN = 'ws → stls → hy2'
DEFAULT_STATE = {'mode': 'auto', 'active': False, 'since': None, 'by': 'system',
                 'reason': '', 'streak_on': 0, 'streak_off': 0, 'last_change': None}
SIGNATURE = ('прямые протоколы (reality, hy2, hy2t, stls) не отвечают 20+ мин, '
             'CF-фронт (ws) жив')


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def handler(db):
    h = AdminOpsMixin.__new__(AdminOpsMixin)
    h.bot = Mock()
    h.bot.send_message = Mock()
    h.bot.services = {}
    h.db = db
    cfg = Mock()
    cfg.FORUM_ENABLED = False
    cfg.FORUM_GROUP_ID = None
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.DPI_MONITOR_ENABLED = '1'
    h.config = cfg
    h._get_thread_id = Mock(return_value=None)
    return h


@pytest.fixture
def typed_by_42(handler):
    handler._current_update = {'message': {'from': {'id': 42}, 'chat': {'id': 'chat'}}}
    return handler


@pytest.fixture
def no_module():
    """bot.services.lockdown not importable → the command owns the key."""
    with patch.dict(sys.modules, {'bot.services.lockdown': None}):
        yield


def iso_ago(**kw) -> str:
    return (datetime.utcnow() - timedelta(**kw)).replace(microsecond=0).isoformat()


def seed(db, **fields) -> dict:
    st = dict(DEFAULT_STATE, **fields)
    db.set_setting(LOCKDOWN_MODE_KEY, json.dumps(st))
    return st


def state(db) -> dict:
    return json.loads(db.get_setting(LOCKDOWN_MODE_KEY))


def run(handler, *args) -> dict:
    handler.bot.send_message.reset_mock()
    handler.show_lockdown('chat', list(args))
    handler.bot.send_message.assert_called_once()
    return handler.bot.send_message.call_args.kwargs


def text_of(handler, *args) -> str:
    return run(handler, *args)['text']


def admin_actions(db) -> list:
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT admin_id, action, target_id, details FROM admin_actions "
            "ORDER BY id").fetchall()]


def fake_lockdown_module(order=LOCKDOWN_ORDER_DEFAULT, load_raises=False,
                         complete=True):
    """A stand-in bot.services.lockdown honouring the contract; every
    write it makes is stamped ``last_change='FAKE'`` so a test can tell
    the module wrote the key, not the command."""
    mod = types.ModuleType('bot.services.lockdown')
    mod.calls = []
    mod.LOCKDOWN_ORDER = tuple(order)

    def load_lockdown(db):
        if load_raises:
            raise RuntimeError('boom')
        raw = db.get_setting(LOCKDOWN_MODE_KEY)
        try:
            parsed = json.loads(raw) if raw else {}
        except ValueError:
            parsed = {}
        return dict(DEFAULT_STATE, **parsed) if isinstance(parsed, dict) else dict(DEFAULT_STATE)

    def is_lockdown_active(db):
        return bool(load_lockdown(db).get('active'))

    def set_mode(db, mode, *, by, reason, now=None):
        mod.calls.append((db, mode, by, reason))
        st = load_lockdown(db)
        st.update(mode=mode, last_change='FAKE')
        if mode == 'on':
            st.update(active=True, since=st['since'] or 'FAKE', by=by, reason=reason)
        elif mode == 'off':
            st.update(active=False, since=None, by=by, reason=reason)
        db.set_setting(LOCKDOWN_MODE_KEY, json.dumps(st))
        return st

    def apply_lockdown_order(ordered):
        ordered = list(ordered)
        return ([p for p in mod.LOCKDOWN_ORDER if p in ordered]
                + [p for p in ordered if p not in mod.LOCKDOWN_ORDER])

    mod.load_lockdown = load_lockdown
    mod.is_lockdown_active = is_lockdown_active
    mod.set_mode = set_mode
    if complete:
        mod.apply_lockdown_order = apply_lockdown_order
    return mod


class TestOverview:

    def test_default_is_auto_and_inactive(self, handler):
        text = text_of(handler)
        assert '🔒 <b>Lockdown</b>' in text
        assert 'режим: auto (решает детектор) · статус: ⚪️ не активен' in text
        assert f'• paid: {PAID_DEFAULT}' in text
        assert f'• demo: {DEMO_DEFAULT}' in text
        assert 'при включении наверх: ws, stls, reality, hy2, hy2t' in text
        assert 'сигнатура шатдауна 0/2 прогонов подряд · здоровых 0/12' in text
        assert 'последнее изменение: не было' in text
        assert '/lockdown on · /lockdown off · /lockdown auto · оповестить юзеров: /broadcast' in text
        assert 'причина:' not in text
        assert 'DNS через туннель' not in text

    def test_active_auto_shows_since_by_reason_and_fronted_orders(self, handler, db):
        since = iso_ago(minutes=30)
        seed(db, active=True, since=since, by='auto:probe_signature',
             reason=SIGNATURE, streak_on=2, last_change=since)
        text = text_of(handler)
        assert f'статус: 🔴 АКТИВЕН с {since[11:16]} UTC (auto:probe_signature)' in text
        assert f'причина: {SIGNATURE}' in text
        assert '(lockdown: CF-фронт первым, DNS через туннель)' in text
        assert f'• paid: {PAID_LOCKDOWN}' in text
        assert f'• demo: {DEMO_LOCKDOWN}' in text
        assert 'при включении наверх' not in text
        assert '⚠️' not in text                      # raised by the detector = it can lift it

    def test_streaks_and_last_change_are_rendered(self, handler, db):
        last = iso_ago(minutes=9)
        seed(db, streak_on=1, streak_off=7, last_change=last)
        text = text_of(handler)
        assert 'сигнатура шатдауна 1/2 прогонов подряд · здоровых 7/12' in text
        assert f'последнее изменение: {last[11:16]} UTC' in text

    def test_old_since_shows_the_date(self, handler, db):
        since = iso_ago(days=2)
        seed(db, active=True, since=since, by='auto:probe_signature')
        assert f'АКТИВЕН с {since[8:10]}.{since[5:7]} {since[11:16]} UTC' in text_of(handler)

    def test_mode_off_reads_as_pinned(self, handler, db):
        seed(db, mode='off', by='admin:7', reason='снято оператором (/lockdown off)')
        text = text_of(handler)
        assert 'режим: off (зафиксирован оператором) · статус: ⚪️ не активен' in text
        assert 'причина: снято оператором (/lockdown off)' in text

    def test_mode_on_reads_as_pinned_without_the_auto_warning(self, handler, db):
        seed(db, mode='on', active=True, since=iso_ago(minutes=5), by='admin:7')
        text = text_of(handler)
        assert 'режим: on (зафиксирован оператором) · статус: 🔴 АКТИВЕН' in text
        assert '(admin:7)' in text
        assert '⚠️' not in text

    def test_manual_owner_under_auto_warns_the_detector_will_not_lift(self, handler, db):
        """auto_off fires only for ``by=auto:…`` — an operator's ``on``
        followed by ``auto`` stays up until /lockdown off; say so."""
        seed(db, mode='auto', active=True, since=iso_ago(minutes=5), by='admin:7')
        assert ('⚠️ включён вручную при режиме auto — детектор сам не снимет, '
                'только /lockdown off') in text_of(handler)

    def test_operator_override_order_is_previewed(self, handler, db):
        db.set_setting(LOCKDOWN_ORDER_KEY, json.dumps(['hy2', 'ws', 'bogus', 'hy2']))
        assert 'при включении наверх: hy2, ws (override cascade_lockdown)' in text_of(handler)

    def test_empty_or_garbage_override_falls_back_to_default(self, handler, db):
        for raw in ('[]', '["bogus"]', '{oops', 'null', '{"a": 1}'):
            db.set_setting(LOCKDOWN_ORDER_KEY, raw)
            text = text_of(handler)
            assert 'при включении наверх: ws, stls, reality, hy2, hy2t' in text, raw
            assert 'override' not in text, raw

    def test_bad_json_is_auto_inactive_not_an_error(self, handler, db):
        for raw in ('{oops', 'null', '[]', '"on"', '{"mode": "banana", "active": "maybe"}'):
            db.set_setting(LOCKDOWN_MODE_KEY, raw)
            text = text_of(handler)
            assert '❌' not in text, raw
            assert 'режим: auto (решает детектор) · статус: ⚪️ не активен' in text, raw

    def test_string_flag_and_garbage_streaks_are_coerced(self, handler, db):
        seed(db, active='true', streak_on='x', streak_off=-3, since='')
        text = text_of(handler)
        assert '🔴 АКТИВЕН с ? (system)' in text
        assert 'сигнатура шатдауна 0/2 прогонов подряд · здоровых 0/12' in text

    def test_html_in_state_is_escaped(self, handler, db):
        seed(db, active=True, by='<b>x</b>', reason='a & b')
        text = text_of(handler)
        assert '&lt;b&gt;x&lt;/b&gt;' in text and '<b>x</b>' not in text
        assert 'причина: a &amp; b' in text

    def test_disabled_protocols_stay_out_under_lockdown(self, handler, db):
        db.set_setting(MK.SETTING_KEY, json.dumps(
            [{'name': n, 'enabled': n != 'stls'} for n in MK.DEFAULT_CASCADE_ORDER]))
        seed(db, active=True, by='auto:probe_signature')
        text = text_of(handler)
        assert '• paid: ws → reality → hy2 → hy2t' in text
        assert '• demo: ws → hy2' in text


class TestTransitions:
    """Against whatever bot.services.lockdown is really importable:
    contract-level assertions only (mode, active, owner, audit)."""

    def test_on_writes_state_audit_and_shows_fronted_orders(self, typed_by_42, db):
        text = text_of(typed_by_42, 'on')
        st = state(db)
        assert (st['mode'], st['active'], st['by']) == ('on', True, 'admin:42')
        assert st['since'] and st['last_change']
        assert admin_actions(db) == [('42', 'lockdown_set', 'on', 'auto/inactive -> on/active')]
        assert '🔒 <b>LOCKDOWN включён оператором</b>' in text
        assert f'• paid: {PAID_LOCKDOWN}' in text
        assert f'• demo: {DEMO_LOCKDOWN}' in text
        assert 'DNS клиентов — через туннель' in text
        assert 'снять: /lockdown off' in text
        assert 'оповестить юзеров: /broadcast' in text

    def test_on_over_an_auto_raised_lockdown_reads_as_pinned(self, typed_by_42, db):
        seed(db, active=True, since=iso_ago(minutes=30), by='auto:probe_signature')
        text = text_of(typed_by_42, 'on')
        st = state(db)
        assert (st['mode'], st['active'], st['by']) == ('on', True, 'admin:42')
        assert '🔒 <b>LOCKDOWN зафиксирован оператором</b> (был активен: auto:probe_signature)' in text
        assert admin_actions(db)[-1][3] == 'auto/active -> on/active'

    def test_off_from_active_restores_the_normal_orders(self, typed_by_42, db):
        seed(db, active=True, since=iso_ago(minutes=30), by='auto:probe_signature')
        text = text_of(typed_by_42, 'off')
        st = state(db)
        assert (st['mode'], st['active'], st['by']) == ('off', False, 'admin:42')
        assert admin_actions(db) == [('42', 'lockdown_set', 'off', 'auto/active -> off/inactive')]
        assert '🔓 <b>LOCKDOWN снят оператором</b>' in text
        assert f'• paid: {PAID_DEFAULT}' in text
        assert f'• demo: {DEMO_DEFAULT}' in text
        assert 'вернуть авто: /lockdown auto' in text

    def test_off_when_inactive_still_pins_and_logs(self, typed_by_42, db):
        text = text_of(typed_by_42, 'off')
        assert (state(db)['mode'], state(db)['active']) == ('off', False)
        assert admin_actions(db) == [('42', 'lockdown_set', 'off', 'auto/inactive -> off/inactive')]
        assert 'ℹ️ <b>Lockdown не был активен</b> — зафиксирован off' in text

    def test_auto_keeps_active_and_warns_when_the_owner_is_an_operator(self, typed_by_42, db):
        seed(db, mode='on', active=True, since=iso_ago(minutes=5), by='admin:7')
        text = text_of(typed_by_42, 'auto')
        st = state(db)
        assert (st['mode'], st['active']) == ('auto', True)
        assert admin_actions(db) == [('42', 'lockdown_set', 'auto', 'on/active -> auto/active')]
        assert '🔁 <b>Lockdown: auto</b>' in text
        assert 'сейчас: 🔴 активен' in text
        assert '⚠️ активен по решению оператора — детектор сам не снимет' in text
        assert f'• paid: {PAID_LOCKDOWN}' in text

    def test_auto_from_pinned_off_stays_inactive(self, typed_by_42, db):
        seed(db, mode='off')
        text = text_of(typed_by_42, 'auto')
        assert (state(db)['mode'], state(db)['active']) == ('auto', False)
        assert 'сейчас: ⚪️ не активен' in text
        assert '⚠️' not in text
        assert f'• paid: {PAID_DEFAULT}' in text

    def test_args_are_case_insensitive(self, typed_by_42, db):
        text_of(typed_by_42, 'ON')
        assert state(db)['mode'] == 'on'
        text_of(typed_by_42, 'Off')
        assert state(db)['mode'] == 'off'
        assert [a[2] for a in admin_actions(db)] == ['on', 'off']

    def test_unknown_arg_is_usage_and_writes_nothing(self, typed_by_42, db):
        for arg in ('banana', 'reset', '1'):
            assert text_of(typed_by_42, arg) == AdminOpsMixin.LOCKDOWN_USAGE, arg
        assert db.get_setting(LOCKDOWN_MODE_KEY) is None
        assert admin_actions(db) == []

    def test_usage_names_every_subcommand(self):
        u = AdminOpsMixin.LOCKDOWN_USAGE
        for sub in ('/lockdown on', '/lockdown off', '/lockdown auto'):
            assert f'<code>{sub}</code>' in u

    def test_audit_falls_back_to_super_admin_without_an_update(self, handler, db):
        text_of(handler, 'on')
        assert admin_actions(db)[0][0] == '1652899'
        assert state(db)['by'] == 'admin:1652899'

    def test_swallowed_write_failure_is_reported_not_hidden(self, typed_by_42, db):
        """Database.set_setting returns False on sqlite errors instead
        of raising; the operator must not read "включён" over an
        unchanged key — and no audit row may claim it happened."""
        with patch.object(db, 'set_setting', return_value=False):
            text = text_of(typed_by_42, 'on')
        assert text.startswith('❌ /lockdown:')
        assert 'не записался' in text
        assert db.get_setting(LOCKDOWN_MODE_KEY) is None
        assert admin_actions(db) == []

    def test_routed_through_handle_with_the_typing_admin(self, handler, db):
        handler.handle({'message': {'text': '/lockdown on',
                                    'from': {'id': 42}, 'chat': {'id': 'chat'}}})
        assert state(db)['by'] == 'admin:42'
        assert admin_actions(db)[0][:3] == ('42', 'lockdown_set', 'on')


class TestWithoutModule:
    """The documented JSON shape, written by the command itself."""

    def test_on_writes_the_documented_shape(self, typed_by_42, db, no_module):
        text_of(typed_by_42, 'on')
        st = state(db)
        assert set(st) == set(DEFAULT_STATE)
        assert st['mode'] == 'on' and st['active'] is True
        assert st['by'] == 'admin:42'
        assert st['reason'] == 'включено оператором (/lockdown on)'
        assert st['streak_on'] == 0 and st['streak_off'] == 0
        assert st['since'] == st['last_change']
        datetime.fromisoformat(st['since'])

    def test_on_keeps_since_when_already_active(self, typed_by_42, db, no_module):
        since = iso_ago(minutes=30)
        seed(db, active=True, since=since, by='auto:probe_signature', reason=SIGNATURE)
        text_of(typed_by_42, 'on')
        st = state(db)
        assert st['since'] == since
        assert st['by'] == 'admin:42'
        assert st['reason'] == 'включено оператором (/lockdown on)'

    def test_off_clears_since_and_keeps_streaks(self, typed_by_42, db, no_module):
        seed(db, active=True, since=iso_ago(minutes=30), by='auto:probe_signature',
             streak_on=2, streak_off=5)
        text_of(typed_by_42, 'off')
        st = state(db)
        assert (st['mode'], st['active'], st['since']) == ('off', False, None)
        assert (st['streak_on'], st['streak_off']) == (2, 5)
        assert st['reason'] == 'снято оператором (/lockdown off)'

    def test_auto_moves_only_the_mode(self, typed_by_42, db, no_module):
        """Re-affirming auto over an auto-raised lockdown must not
        re-own it as the operator's — the detector lifts only
        ``by=auto:…``, so that would silently pin it."""
        since = iso_ago(minutes=30)
        seed(db, mode='on', active=True, since=since, by='auto:probe_signature',
             reason=SIGNATURE, streak_on=2, streak_off=3)
        text = text_of(typed_by_42, 'auto')
        st = state(db)
        assert st['mode'] == 'auto'
        assert (st['active'], st['since'], st['by'], st['reason']) == (
            True, since, 'auto:probe_signature', SIGNATURE)
        assert (st['streak_on'], st['streak_off']) == (2, 3)
        assert st['last_change'] != since
        assert '⚠️' not in text                      # auto-owned: the detector can lift it

    def test_operator_owned_then_auto_warns(self, typed_by_42, db, no_module):
        text_of(typed_by_42, 'on')
        text = text_of(typed_by_42, 'auto')
        assert state(db)['by'] == 'admin:42'
        assert '⚠️ активен по решению оператора — детектор сам не снимет' in text

    def test_card_renders_from_the_key_alone(self, handler, db, no_module):
        seed(db, active=True, since=iso_ago(minutes=3), by='auto:probe_signature',
             reason=SIGNATURE)
        text = text_of(handler)
        assert '❌' not in text
        assert '🔴 АКТИВЕН' in text
        assert 'при включении наверх' not in text


class TestWithFakeModule:
    """The module owns the transitions when present: the command calls
    set_mode with the typing admin and does not write behind it."""

    def test_set_mode_is_called_and_owns_the_write(self, typed_by_42, db):
        mod = fake_lockdown_module()
        with patch.dict(sys.modules, {'bot.services.lockdown': mod}):
            text = text_of(typed_by_42, 'on')
        assert mod.calls == [(db, 'on', 'admin:42', 'включено оператором (/lockdown on)')]
        assert state(db)['last_change'] == 'FAKE'           # the fake wrote it, not us
        assert admin_actions(db) == [('42', 'lockdown_set', 'on', 'auto/inactive -> on/active')]
        assert '🔒 <b>LOCKDOWN включён оператором</b>' in text

    def test_module_order_is_the_preview(self, handler, db):
        mod = fake_lockdown_module(order=('stls', 'ws'))
        with patch.dict(sys.modules, {'bot.services.lockdown': mod}):
            text = text_of(handler)
        assert 'при включении наверх: stls, ws' in text
        assert 'override' not in text

    def test_load_failure_falls_back_to_the_key(self, handler, db):
        seed(db, active=True, since=iso_ago(minutes=3), by='auto:probe_signature')
        mod = fake_lockdown_module(load_raises=True)
        with patch.dict(sys.modules, {'bot.services.lockdown': mod}):
            text = text_of(handler)
        assert '❌' not in text
        assert '🔴 АКТИВЕН' in text

    def test_incomplete_module_is_ignored(self, typed_by_42, db):
        mod = fake_lockdown_module(complete=False)
        with patch.dict(sys.modules, {'bot.services.lockdown': mod}):
            text_of(typed_by_42, 'on')
        assert mod.calls == []
        st = state(db)
        assert st['mode'] == 'on' and st['last_change'] != 'FAKE'
        datetime.fromisoformat(st['last_change'])

    def test_module_that_ignores_the_write_is_reported(self, typed_by_42, db):
        mod = fake_lockdown_module()
        mod.set_mode = lambda db, mode, *, by, reason, now=None: None   # writes nothing
        with patch.dict(sys.modules, {'bot.services.lockdown': mod}):
            text = text_of(typed_by_42, 'on')
        assert text.startswith('❌ /lockdown:')
        assert admin_actions(db) == []


class TestHelpers:

    def test_project_lockdown_is_a_stable_projection(self):
        top = LOCKDOWN_ORDER_DEFAULT
        assert _project_lockdown(['hy2t', 'stls', 'ws', 'hy2', 'reality'], top) == \
            ['ws', 'stls', 'reality', 'hy2', 'hy2t']
        assert _project_lockdown(['stls', 'ws', 'hy2'], top) == ['ws', 'stls', 'hy2']
        assert _project_lockdown(['hy2t', 'ws', 'xhttp'], ('ws',)) == ['ws', 'hy2t', 'xhttp']
        assert _project_lockdown([], top) == []
        once = _project_lockdown(['hy2t', 'ws', 'stls'], top)
        assert _project_lockdown(once, top) == once

    def test_load_lockdown_state_never_raises(self):
        db = Mock()
        db.get_setting.side_effect = RuntimeError('db gone')
        with patch.dict(sys.modules, {'bot.services.lockdown': None}):
            assert load_lockdown_state(db) == DEFAULT_STATE

    def test_order_preview_projects_onto_known_names(self, db):
        db.set_setting(LOCKDOWN_ORDER_KEY, json.dumps(['ws', 'nope', 'hy2t', 'ws']))
        with patch.dict(sys.modules, {'bot.services.lockdown': None}):
            assert lockdown_order_preview(db, MK.PROTOCOL_METHOD_MAP) == (('ws', 'hy2t'), True)
            db.set_setting(LOCKDOWN_ORDER_KEY, '["nope"]')
            assert lockdown_order_preview(db, MK.PROTOCOL_METHOD_MAP) == (
                LOCKDOWN_ORDER_DEFAULT, False)


class TestRegistration:

    def test_routed_and_advertised(self):
        assert AdminHandlerBase.ADMIN_COMMANDS['/lockdown'] == 'show_lockdown'
        assert '<code>/lockdown</code>' in ADMIN_HELP_TEXT
        assert '<code>/lockdown on</code>' in ADMIN_HELP_TEXT

    def test_assembled_handler_has_the_method(self):
        from bot.handlers.admin import AdminHandler
        assert callable(getattr(AdminHandler, 'show_lockdown', None))

    def test_dashboard_reads_the_lockdown_block(self):
        """app.js has no test runner here; pin the contract strings so
        a rename of the API block or the banner text is caught."""
        js = (Path(__file__).resolve().parents[2] / 'bot' / 'webapp' / 'app.js').read_text()
        assert 'data.lockdown' in js
        assert 'lockdown.active === true' in js
        assert 'LOCKDOWN активен' in js
        assert '<code>/lockdown off</code>' in js
        assert 'детектор следит' in js
        assert "['ws', 'stls', 'reality', 'hy2', 'hy2t']" in js
