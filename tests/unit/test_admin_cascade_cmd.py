"""/cascade — the operator surface of the DPIMonitor feedback loop.

Why
---
IMPROVEMENT_PLAN A1: the monitor now reorders the user-facing cascade
on its own. An automatic actor the operator cannot see or undo in one
move is worse than no automation — the 2026-09-01 flow-wipe ran four
days precisely because nobody could see what the monthly job had done.
/cascade shows the effective order with every auto-demotion (reason,
since when, per ASN) and reverses them with ``reset``.

Real sqlite bot.db (Database creates app_settings / dpi_metrics /
admin_actions / users in its own migrations), keys seeded exactly as
the monitor writes them. bot/services/dpi_monitor.py is deliberately
NOT imported here: the app_settings keys ARE the contract, and
``reset`` is pinned with the module absent (ImportError path), present
(a recording fake), raising, and lying (leaves demotions behind).
"""

import json
import re
import sqlite3
import sys
import types
from datetime import datetime, timedelta
from unittest.mock import Mock, patch

import pytest

from bot.core.database import Database
from bot.handlers.admin.base import ADMIN_HELP_TEXT, AdminHandlerBase
from bot.handlers.admin.ops import (
    AdminOpsMixin, CASCADE_AUTO_KEY, DPI_MONITOR_ENABLED_KEY,
    DPI_MONITOR_STATE_KEY, load_cascade_auto, _partition_demoted,
)
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK


DEFAULT = list(MK.DEFAULT_CASCADE_ORDER)          # hy2t stls ws hy2 reality
DARK = 'пробы 0/30 за 45 мин (DARK)'
HSFAIL = 'reality hsfail 879 / conn 0 за 2 ч'


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


def iso_ago(**kw) -> str:
    return (datetime.utcnow() - timedelta(**kw)).replace(microsecond=0).isoformat()


def meta(reason=DARK, since=None) -> dict:
    return {'since': since or iso_ago(minutes=40), 'reason': reason}


def seed_auto(db, global_=None, asn=None) -> None:
    db.set_setting(CASCADE_AUTO_KEY,
                   json.dumps({'global': global_ or {}, 'asn': asn or {}}))


def seed_state(db, **fields) -> None:
    db.set_setting(DPI_MONITOR_STATE_KEY, json.dumps(dict({'targets': {}}, **fields)))


def seed_dpi_org(db, asn, org) -> None:
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO dpi_metrics (snapshot_at, country, asn, as_org, "
            "inbound_tag, conn_count) VALUES (?, 'RU', ?, ?, 'reality', 1)",
            (iso_ago(minutes=5), asn, org))
        conn.commit()


def seed_user(db, chat_id, asn) -> None:
    with db._connect() as conn:
        conn.execute("INSERT INTO users (chat_id, last_asn) VALUES (?, ?)",
                     (str(chat_id), asn))
        conn.commit()


def run(handler, *args) -> dict:
    """Invoke /cascade, return the send_message kwargs."""
    handler.bot.send_message.reset_mock()
    handler.show_cascade('chat', list(args))
    handler.bot.send_message.assert_called_once()
    return handler.bot.send_message.call_args.kwargs


def text_of(handler, *args) -> str:
    return run(handler, *args)['text']


def order_from(text) -> list:
    """Protocol names of the numbered lines, top to bottom."""
    return re.findall(r'^\d+\. <b>(\w+)</b>', text, re.M)


def line_for(text, proto) -> str:
    return next(ln for ln in text.splitlines()
                if re.match(rf'^\d+\. <b>{proto}</b>', ln))


def admin_actions(db) -> list:
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT admin_id, action, target_id, details FROM admin_actions "
            "ORDER BY id").fetchall()]


class TestOverview:

    def test_default_order_without_auto(self, handler):
        text = text_of(handler)
        assert order_from(text) == DEFAULT
        assert '⬇︎' not in text
        assert '<b>Авто по ASN:</b> нет' in text
        assert 'монитор: 🟢 вкл' in text
        assert 'ещё не запускался' in text

    def test_tier_tags(self, handler):
        text = text_of(handler)
        assert '1. <b>hy2t</b> (paid)' in text
        assert '2. <b>stls</b> (free)' in text
        assert line_for(text, 'reality').endswith('(paid)')

    def test_demoted_protocol_sinks_to_tail_with_reason_and_since(self, handler, db):
        since = iso_ago(minutes=40)
        seed_auto(db, global_={'ws': meta(since=since)})
        text = text_of(handler)
        assert order_from(text) == ['hy2t', 'stls', 'hy2', 'reality', 'ws']
        assert line_for(text, 'ws').endswith(
            f"⬇︎ авто: {DARK}, с {since[11:16]} UTC")
        # untouched protocols carry no mark
        assert '⬇︎' not in line_for(text, 'stls')

    def test_partition_is_stable_on_both_sides(self, handler, db):
        seed_auto(db, global_={'hy2t': meta(), 'stls': meta()})
        assert order_from(text_of(handler)) == ['ws', 'hy2', 'reality', 'hy2t', 'stls']

    def test_operator_order_is_the_base(self, handler, db):
        """Auto only reorders WITHIN what the operator chose."""
        db.set_setting(MK.SETTING_KEY,
                       json.dumps(['reality', 'ws', 'stls', 'hy2', 'hy2t']))
        seed_auto(db, global_={'reality': meta(reason=HSFAIL)})
        assert order_from(text_of(handler)) == ['ws', 'stls', 'hy2', 'hy2t', 'reality']

    def test_operator_disabled_protocol_is_listed_separately(self, handler, db):
        db.set_setting(MK.SETTING_KEY, json.dumps(
            [{'name': n, 'enabled': n != 'hy2'} for n in DEFAULT]))
        text = text_of(handler)
        assert 'hy2' not in order_from(text)
        assert 'выключены оператором: hy2' in text

    def test_asn_entries_resolve_org_from_dpi_metrics(self, handler, db):
        seed_auto(db, asn={'AS31133': {'reality': meta(reason=HSFAIL)}})
        seed_dpi_org(db, 'AS31133', 'MegaFon')
        text = text_of(handler)
        assert '<b>Авто по ASN</b> (1):' in text
        assert f'• AS31133 (MegaFon) — reality ⬇︎ {HSFAIL}, с ' in text

    def test_asn_entry_without_metrics_is_bare(self, handler, db):
        seed_auto(db, asn={'AS12389': {'hy2': meta(), 'hy2t': meta()}})
        text = text_of(handler)
        assert '• AS12389 — hy2 ⬇︎' in text
        assert '; hy2t ⬇︎' in text

    def test_asn_entries_are_sorted_and_empty_ones_dropped(self, handler, db):
        seed_auto(db, asn={'AS8359': {'reality': meta()}, 'AS99999': {},
                           'AS12389': {'hy2': meta()}})
        text = text_of(handler)
        assert text.index('• AS12389') < text.index('• AS8359')
        assert 'AS99999' not in text
        assert '<b>Авто по ASN</b> (2):' in text

    def test_monitor_off_flag_and_warning_when_demotions_persist(self, handler, db):
        db.set_setting(DPI_MONITOR_ENABLED_KEY, '0')
        seed_auto(db, global_={'ws': meta()})
        text = text_of(handler)
        assert 'монитор: ⏸ выкл' in text
        assert 'монитор выключен, но авто-понижения продолжают действовать' in text

    def test_monitor_off_without_demotions_has_no_warning(self, handler, db):
        db.set_setting(DPI_MONITOR_ENABLED_KEY, '0')
        text = text_of(handler)
        assert '⏸ выкл' in text
        assert 'продолжают действовать' not in text

    def test_env_default_applies_until_the_flag_is_written(self, handler, db):
        handler.config.DPI_MONITOR_ENABLED = '0'
        assert '⏸ выкл' in text_of(handler)
        db.set_setting(DPI_MONITOR_ENABLED_KEY, '1')      # /cascade on wins over env
        assert '🟢 вкл' in text_of(handler)

    def test_set_flag_is_on_only_when_exactly_1(self, handler, db):
        """Mirror of DPIMonitor.is_enabled: the job treats any set value
        other than '1' as off, so a hand-typed 'true' must read as off
        here too — otherwise the card says 🟢 while nothing runs."""
        for raw in ('true', 'yes', 'on', '2'):
            db.set_setting(DPI_MONITOR_ENABLED_KEY, raw)
            assert '⏸ выкл' in text_of(handler), raw
        db.set_setting(DPI_MONITOR_ENABLED_KEY, ' 1 ')
        assert '🟢 вкл' in text_of(handler)

    def test_last_run_rendered_with_age_and_runs(self, handler, db):
        last = iso_ago(minutes=7)
        seed_state(db, last_run=last, runs=12)
        text = text_of(handler)
        assert f'последний прогон {last[11:16]} UTC (7 мин назад), прогонов: 12' in text
        assert 'давно не запускался' not in text

    def test_stale_last_run_is_flagged_only_while_enabled(self, handler, db):
        seed_state(db, last_run=iso_ago(hours=3))
        assert '⚠️ давно не запускался' in text_of(handler)
        db.set_setting(DPI_MONITOR_ENABLED_KEY, '0')      # paused = expected silence
        assert 'давно не запускался' not in text_of(handler)

    def test_old_since_shows_the_date(self, handler, db):
        since = iso_ago(days=3)
        seed_auto(db, global_={'ws': meta(since=since)})
        assert f"с {since[8:10]}.{since[5:7]} {since[11:16]} UTC" in text_of(handler)

    def test_bad_json_is_treated_as_empty_not_an_error(self, handler, db):
        db.set_setting(CASCADE_AUTO_KEY, '{oops')
        db.set_setting(DPI_MONITOR_STATE_KEY, 'null')
        text = text_of(handler)
        assert '❌' not in text
        assert '⬇︎' not in text
        assert order_from(text) == DEFAULT
        assert 'ещё не запускался' in text

    def test_non_object_json_and_malformed_metas_are_tolerated(self, handler, db):
        db.set_setting(CASCADE_AUTO_KEY, json.dumps(['ws']))
        assert '⬇︎' not in text_of(handler)
        db.set_setting(CASCADE_AUTO_KEY, json.dumps(
            {'global': {'ws': 'not-a-dict'}, 'asn': ['AS1']}))
        text = text_of(handler)
        assert order_from(text)[-1] == 'ws'                # still demoted
        assert '⬇︎ авто: ?, с ?' in line_for(text, 'ws')  # unknown reason/since

    def test_evidence_is_shown_over_the_rule_id(self, handler, db):
        """The monitor stores reason=rule id (hysteresis key) and
        evidence=the human sentence; the card must show the sentence —
        'probe_dark' tells the operator nothing at 03:00."""
        m = {'since': iso_ago(minutes=5), 'reason': 'probe_dark',
             'evidence': 'пробы 0/30 за 3 прогона, ни одного живого ответа (DARK)'}
        seed_auto(db, global_={'ws': m},
                  asn={'AS31133': {'reality': dict(m, reason='reality_asn',
                                                    evidence='hsfail 879 / conn 0')}})
        text = text_of(handler)
        assert 'ни одного живого ответа (DARK)' in line_for(text, 'ws')
        assert 'probe_dark' not in text
        assert '• AS31133 — reality ⬇︎ hsfail 879 / conn 0' in text
        assert 'reality_asn' not in text
        asn_text = text_of(handler, 'AS31133')
        assert 'hsfail 879 / conn 0' in line_for(asn_text, 'reality')
        assert 'reality_asn' not in asn_text

    def test_reason_alone_is_the_fallback(self, handler, db):
        seed_auto(db, global_={'ws': {'since': iso_ago(minutes=5), 'reason': 'hand-written'}})
        assert '⬇︎ авто: hand-written, с' in line_for(text_of(handler), 'ws')

    def test_reason_html_is_escaped(self, handler, db):
        seed_auto(db, global_={'ws': meta(reason='<b>x</b> & y')})
        text = text_of(handler)
        assert '&lt;b&gt;x&lt;/b&gt; &amp; y' in text
        assert '<b>x</b>' not in text

    def test_footer_points_at_the_subcommands(self, handler):
        text = text_of(handler)
        assert '/cascade AS31133 · /cascade reset · /cascade off|on' in text


class TestAsnView:

    def test_operator_override_is_the_base_and_own_auto_sinks(self, handler, db):
        db.set_setting(MK.ASN_SETTING_KEY, json.dumps(
            {'AS31133': ['reality', 'hy2t', 'hy2', 'stls', 'ws']}))
        seed_auto(db, asn={'AS31133': {'reality': meta(reason=HSFAIL)}})
        text = text_of(handler, 'AS31133')
        assert '🔁 <b>Каскад для AS31133</b>' in text
        assert 'базовый порядок: оператор (cascade_by_asn)' in text
        assert order_from(text) == ['hy2t', 'hy2', 'stls', 'ws', 'reality']
        assert f"⬇︎ авто (AS31133): {HSFAIL}, с " in line_for(text, 'reality')
        assert 'авто для этого ASN: reality' in text

    def test_global_demotions_apply_in_the_asn_view_too(self, handler, db):
        seed_auto(db, global_={'ws': meta()},
                  asn={'AS31133': {'reality': meta(reason=HSFAIL)}})
        text = text_of(handler, 'AS31133')
        assert order_from(text) == ['hy2t', 'stls', 'hy2', 'ws', 'reality']
        assert '⬇︎ авто (глобально):' in line_for(text, 'ws')
        assert '⬇︎ авто (AS31133):' in line_for(text, 'reality')
        assert 'глобальные авто (действуют и здесь): ws' in text

    def test_other_asn_entries_do_not_leak(self, handler, db):
        seed_auto(db, asn={'AS8359': {'reality': meta()}})
        text = text_of(handler, 'AS31133')
        assert '⬇︎' not in text
        assert 'авто для этого ASN: нет' in text
        assert 'AS8359' not in text

    def test_no_override_says_global(self, handler):
        text = text_of(handler, 'AS31133')
        assert 'базовый порядок: глобальный (override для ASN не задан)' in text
        assert order_from(text) == DEFAULT

    def test_asn_arg_is_case_insensitive_and_accepts_a_bare_number(self, handler, db):
        seed_auto(db, asn={'AS31133': {'reality': meta()}})
        for arg in ('as31133', 'As31133', '31133'):
            text = text_of(handler, arg)
            assert '<b>Каскад для AS31133</b>' in text, arg
            assert 'авто для этого ASN: reality' in text, arg

    def test_org_and_user_count(self, handler, db):
        seed_dpi_org(db, 'AS31133', 'PJSC MegaFon')
        seed_user(db, 1, 'AS31133')
        seed_user(db, 2, 'as31133')
        seed_user(db, 3, 'AS8359')
        text = text_of(handler, 'AS31133')
        assert '<b>Каскад для AS31133</b> (PJSC MegaFon)' in text
        assert 'юзеров с этим ASN: 2' in text


class TestReset:

    @pytest.fixture
    def seeded(self, db):
        seed_auto(db, global_={'ws': meta()},
                  asn={'AS31133': {'reality': meta(reason=HSFAIL)}})
        seed_state(db, last_run=iso_ago(minutes=3), runs=9,
                   targets={'global:ws': {'bad': 5, 'good': 0}})

    @staticmethod
    def fake_monitor_module(reset_impl):
        """A stand-in bot.services.dpi_monitor exposing DPIMonitor."""
        mod = types.ModuleType('bot.services.dpi_monitor')
        calls = []

        class DPIMonitor:
            def __init__(self, db, config, bot=None):
                calls.append((db, config, bot))
                self.db = db

            def reset(self, actor='dpi_monitor'):
                actors.append(actor)
                reset_impl(self.db)

        actors = []
        mod.DPIMonitor = DPIMonitor
        mod.calls = calls
        mod.actors = actors
        return mod

    def test_reset_without_the_module_clears_both_keys_and_logs(self, handler, db, seeded):
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': None}):
            text = text_of(handler, 'reset')
        assert json.loads(db.get_setting(CASCADE_AUTO_KEY)) == {}
        assert json.loads(db.get_setting(DPI_MONITOR_STATE_KEY)) == {}
        assert '✅ <b>Авто-понижения сняты</b>' in text
        assert 'снято: ws (глобально), AS31133: reality' in text
        (admin_id, action, target, details), = admin_actions(db)
        assert (admin_id, action, target) == ('1652899', 'cascade_auto_reset', None)
        assert 'cleared: ws (глобально), AS31133: reality' in details
        assert '[direct]' in details

    def test_reset_prefers_the_module_reset(self, handler, db, seeded):
        def real_reset(d):
            d.set_setting(CASCADE_AUTO_KEY, '{}')
            d.set_setting(DPI_MONITOR_STATE_KEY, '{}')
        mod = self.fake_monitor_module(real_reset)
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': mod}):
            text_of(handler, 'reset')
        assert mod.calls == [(db, handler.config, None)]
        assert json.loads(db.get_setting(CASCADE_AUTO_KEY)) == {}
        assert '[DPIMonitor.reset]' in admin_actions(db)[0][3]

    def test_module_reset_is_told_who_typed_the_command(self, handler, db, seeded):
        """DPIMonitor.reset writes its own audit row under ``actor``;
        without the caller's id it reads as if the monitor reset
        itself (default actor 'dpi_monitor')."""
        handler._current_update = {'message': {'from': {'id': 42},
                                               'chat': {'id': 'chat'}}}
        mod = self.fake_monitor_module(lambda d: (
            d.set_setting(CASCADE_AUTO_KEY, '{}'),
            d.set_setting(DPI_MONITOR_STATE_KEY, '{}')))
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': mod}):
            text_of(handler, 'reset')
        assert mod.actors == ['42']
        assert admin_actions(db)[0][0] == '42'

    def test_reset_falls_back_when_the_module_reset_raises(self, handler, db, seeded):
        def boom(d):
            raise RuntimeError('schema drift')
        mod = self.fake_monitor_module(boom)
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': mod}):
            text = text_of(handler, 'reset')
        assert json.loads(db.get_setting(CASCADE_AUTO_KEY)) == {}
        assert json.loads(db.get_setting(DPI_MONITOR_STATE_KEY)) == {}
        assert '✅' in text
        assert '[direct]' in admin_actions(db)[0][3]

    def test_reset_falls_back_when_the_module_reset_leaves_demotions(self, handler, db, seeded):
        mod = self.fake_monitor_module(lambda d: None)   # contract drift: no-op
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': mod}):
            text_of(handler, 'reset')
        assert json.loads(db.get_setting(CASCADE_AUTO_KEY)) == {}
        assert json.loads(db.get_setting(DPI_MONITOR_STATE_KEY)) == {}
        assert '[direct]' in admin_actions(db)[0][3]

    def test_reset_with_nothing_demoted_still_zeroes_state(self, handler, db):
        seed_state(db, last_run=iso_ago(minutes=3), targets={'global:ws': {'bad': 1}})
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': None}):
            text = text_of(handler, 'reset')
        assert 'ℹ️ <b>Авто-понижений не было</b>' in text
        assert json.loads(db.get_setting(DPI_MONITOR_STATE_KEY)) == {}
        assert 'nothing was demoted' in admin_actions(db)[0][3]

    def test_reset_warns_that_an_enabled_monitor_may_redemote(self, handler, db, seeded):
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': None}):
            assert 'понизит снова через ~20 мин' in text_of(handler, 'reset')

    def test_reset_with_monitor_off_says_operator_only(self, handler, db, seeded):
        db.set_setting(DPI_MONITOR_ENABLED_KEY, '0')
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': None}):
            text = text_of(handler, 'reset')
        assert 'монитор выключен — порядок теперь чисто операторский' in text
        assert 'понизит снова' not in text

    def test_reset_is_effective_for_the_overview_immediately(self, handler, db, seeded):
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': None}):
            text_of(handler, 'reset')
        text = text_of(handler)
        assert order_from(text) == DEFAULT
        assert '⬇︎' not in text

    def test_audit_row_names_the_admin_who_typed_it(self, handler, db, seeded):
        handler._current_update = {'message': {'from': {'id': 42},
                                               'chat': {'id': 'chat'}}}
        with patch.dict(sys.modules, {'bot.services.dpi_monitor': None}):
            text_of(handler, 'reset')
        assert admin_actions(db)[0][0] == '42'


class TestToggle:

    def test_off_sets_the_flag_logs_and_reports_persisting_demotions(self, handler, db):
        seed_auto(db, global_={'ws': meta()},
                  asn={'AS31133': {'hy2': meta(), 'hy2t': meta()}})
        text = text_of(handler, 'off')
        assert db.get_setting(DPI_MONITOR_ENABLED_KEY) == '0'
        assert '⏸ <b>Монитор каскада выключен</b>' in text
        assert 'действующих авто-понижений: 3 — они остаются, снять: /cascade reset' in text
        assert admin_actions(db) == [('1652899', 'dpi_monitor_off', None, None)]

    def test_off_without_demotions(self, handler, db):
        text = text_of(handler, 'OFF')
        assert db.get_setting(DPI_MONITOR_ENABLED_KEY) == '0'
        assert 'авто-понижений сейчас нет' in text

    def test_on_sets_the_flag_and_logs(self, handler, db):
        db.set_setting(DPI_MONITOR_ENABLED_KEY, '0')
        text = text_of(handler, 'on')
        assert db.get_setting(DPI_MONITOR_ENABLED_KEY) == '1'
        assert '▶️ <b>Монитор каскада включён</b>' in text
        assert admin_actions(db) == [('1652899', 'dpi_monitor_on', None, None)]
        assert '🟢 вкл' in text_of(handler)

    def test_unknown_arg_shows_usage_not_the_card(self, handler):
        text = text_of(handler, 'foo')
        assert '<b>/cascade</b>' in text
        for sub in ('/cascade AS31133', '/cascade reset', '/cascade off', '/cascade on'):
            assert sub in text
        assert 'Каскад протоколов' not in text
        assert order_from(text) == []


class TestPlumbing:

    def test_registered_in_command_map_and_help(self):
        assert AdminHandlerBase.ADMIN_COMMANDS['/cascade'] == 'show_cascade'
        assert callable(getattr(AdminOpsMixin, 'show_cascade'))
        assert '<code>/cascade</code>' in ADMIN_HELP_TEXT
        assert '<code>/cascade reset</code>' in ADMIN_HELP_TEXT

    def test_reply_lands_in_the_calling_topic_as_html(self, handler):
        handler._get_thread_id = Mock(return_value=77)
        out = run(handler)
        assert out['parse_mode'] == 'HTML'
        assert out['message_thread_id'] == 77
        assert out['chat_id'] == 'chat'

    def test_answers_synchronously(self, handler):
        """No worker thread: the undo path must not race the next
        command for ``_current_update`` (unlike /protocols)."""
        assert handler.show_cascade('chat', []) is None
        handler.bot.send_message.assert_called_once()

    def test_db_failure_still_answers(self, handler):
        handler.db = Mock()
        handler.db.get_setting = Mock(side_effect=sqlite3.OperationalError('locked'))
        text = text_of(handler)
        assert text.startswith('❌ /cascade:')
        assert 'locked' in text

    def test_fits_telegram(self, handler, db):
        seed_auto(db, asn={f'AS{n}': {'reality': meta(reason='x' * 60)}
                           for n in range(1000, 1200)})
        text = text_of(handler)
        assert len(text) <= 4096
        assert text.endswith('…(обрезано)')
        # cut on a line boundary: a cut inside <b>… makes Telegram
        # reject the whole message (can't parse entities) — no card.
        body = text[:-len('\n…(обрезано)')]
        assert body == '\n'.join(body.split('\n'))          # sanity
        for tag in ('b', 'i', 'code'):
            assert body.count(f'<{tag}>') == body.count(f'</{tag}>'), tag
        assert not re.search(r'<[^>]*$', body)              # no half-open tag

    def test_truncation_never_splits_a_tag(self, handler):
        """Deterministic: 18-char lines put byte 3900 inside the ``x``
        run of a ``<b>…</b>`` (3900 % 18 == 12) — a raw slice would
        leave an unclosed <b> and Telegram would refuse the message."""
        crafted = '\n'.join('<b>' + 'x' * 10 + '</b>' for _ in range(300))
        assert crafted[3899] == 'x' and crafted[3900 - 12:3900 - 9] == '<b>'
        handler._cascade_overview = lambda: crafted
        text = text_of(handler)
        assert len(text) <= 4096 and text.endswith('…(обрезано)')
        body = text[:-len('\n…(обрезано)')]
        assert body.count('<b>') == body.count('</b>')
        assert body.endswith('</b>')


class TestHelpers:

    def test_partition_is_stable_and_idempotent(self):
        once = _partition_demoted(DEFAULT, {'stls', 'hy2t'})
        assert once == ['ws', 'hy2', 'reality', 'hy2t', 'stls']
        assert _partition_demoted(once, {'stls', 'hy2t'}) == once
        assert _partition_demoted(DEFAULT, set()) == DEFAULT
        assert sorted(once) == sorted(DEFAULT)          # never removes a protocol

    def test_load_cascade_auto_normalises(self, db):
        db.set_setting(CASCADE_AUTO_KEY, json.dumps({
            'global': {'ws': {'since': 's', 'reason': 'r'}},
            'asn': {'as31133': {'reality': {'since': 's'}}, 'AS1': {}, 'AS2': 'junk'},
        }))
        assert load_cascade_auto(db) == {
            'global': {'ws': {'since': 's', 'reason': 'r'}},
            'asn': {'AS31133': {'reality': {'since': 's'}}},
        }

    def test_load_cascade_auto_empty_shapes(self, db):
        assert load_cascade_auto(db) == {'global': {}, 'asn': {}}
        db.set_setting(CASCADE_AUTO_KEY, '{}')
        assert load_cascade_auto(db) == {'global': {}, 'asn': {}}
        db.set_setting(CASCADE_AUTO_KEY, 'garbage')
        assert load_cascade_auto(db) == {'global': {}, 'asn': {}}
