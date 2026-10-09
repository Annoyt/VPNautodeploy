"""DPIMonitor R6 ``client_dark`` (IMPROVEMENT_PLAN E8) and the reverse SOS (E21).

* R6 — per (users.last_asn, protocol), from the ``client_probe`` rows the
  FlClash telemetry providers ``p-<proto>`` leave: ≥3 clients up through
  ANOTHER cascade protocol with no row for it, 0 clients with one, in 2 h
  → demote it for that ASN; hysteresis 2/6, cap 2 per run, ranked below
  the probe rules and above R3; silence (no rows, or rows through the DE
  reserve only) is never a signal; a client only counts on a protocol its
  profile holds (tier, the operator's enabled set, what this deployment
  builds).
* E21 — an applied R6 demotion writes once a day to the users of that ASN
  whose client used the protocol in the last 24 h (Telegram, or mail for
  ext_* users), logs ``admin_actions('dpi_monitor', 'reverse_sos')`` and
  posts one line to the AI topic. Only R6 triggers it.

Level 2: a REAL sqlite bot.db seeded with the rows production writes
(client_probe, users, app_settings, dpi_metrics, outbound_health) and the
real monitor; Telegram and SMTP are stubbed.
"""

import hashlib
import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot.core.database import Database
from bot.handlers.callbacks.user import MyKeyAnswerHandler
from bot.services import dpi_monitor as dm
from bot.services import reverse_sos
from bot.services.dpi_monitor import DPIMonitor
from bot.services.sos import match_sos_keywords
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

WEB = 'https://dash.example.com'
HY2T = dict(HY2T_PORT='8402', HY2T_HOP_PORTS='8402,40001:50000')
MEGAFON = 'AS31133'
MTS = 'AS8359'
T0 = datetime(2026, 10, 9, 12, 0, 0)
STEP = timedelta(minutes=10)
GROUP = -100123
TOPIC_AI = 55


def make_config(**over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL=WEB,
        ENTRY_NODE_IP='203.0.113.20', ENTRY_NODE_PORT=8443,
        REALITY_PUBLIC_KEY='reality-pbk', SNI_VALUE='www.bing.com',
        SID_VALUE='0123456789abcdef',
        HY2_HOST='203.0.113.20', HY2_PORT=8400, HY2_SNI='hy2.example.com',
        HY2_OBFS_PASSWORD='obfs-pw', HY2_HOP_PORTS='443,20000:40000', HY2T_PORT='',
        WS_HOST='cdn.example.com', WS_PORT=2053, WS_PATH='/api/v1/forecast',
        WS_SNI='cdn.example.com',
        STLS_HOST='203.0.113.20', STLS_PORT=443, STLS_SNI='www.microsoft.com',
        STLS_VERSION=3, STLS_PASSWORD='stls-pw',
        SS_METHOD='2022-blake3-aes-128-gcm', SS_SERVER_PASSWORD='srv-pw',
        SS_USER_SALT='salt', SUB_MIRROR_URLS='',
        FORUM_GROUP_ID=GROUP, TOPIC_AI=TOPIC_AI, SUPER_ADMIN_ID='1652899',
        DPI_MONITOR_ENABLED=True, DPI_MONITOR_INTERVAL_MIN=10,
        SMTP_HOST='',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def _rows(db, sql, *args):
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(sql, args).fetchall()]


# =====================================================================
# E8 → DPIMonitor R6 client_dark (real sqlite)
# =====================================================================

@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def bot():
    b = Mock()
    b.send_message.return_value = {'message_id': 1}
    return b


@pytest.fixture(autouse=True)
def fast_sends(monkeypatch):
    monkeypatch.setattr(reverse_sos, 'SEND_DELAY_S', 0)


def uuid_of(chat_id):
    h = hashlib.sha256(str(chat_id).encode()).hexdigest()
    return f'{h[:8]}-{h[8:12]}-4{h[13:16]}-8{h[17:20]}-{h[20:32]}'


def seed_user(db, chat_id, *, asn=MEGAFON, status='paid', lang='ru', contact=None):
    with db._connect() as c:
        c.execute(
            "INSERT OR REPLACE INTO users (chat_id, username, status, last_asn, uuid,"
            " email, lang, contact_email) VALUES (?,?,?,?,?,?,?,?)",
            (str(chat_id), f'u{chat_id}', status, asn, uuid_of(chat_id),
             f'user_{chat_id}@panel', lang, contact),
        )
        c.commit()


def seed_rows(db, chat_id, protos, *, now=T0, minutes_ago=5):
    ts = (now - timedelta(minutes=minutes_ago)).strftime('%Y-%m-%d %H:%M:%S')
    with db._connect() as c:
        c.executemany(
            "INSERT INTO client_probe (chat_id, grp, ts, src_ip) VALUES (?,?,?,?)",
            [(str(chat_id), f'p-{p}', ts, '203.0.113.9') for p in protos])
        c.commit()


def network(db, ids, *, asn=MEGAFON, status='paid', alive=('reality', 'ws', 'stls'),
            minutes_ago=5, now=T0):
    for cid in ids:
        seed_user(db, cid, asn=asn, status=status)
        seed_rows(db, cid, alive, now=now, minutes_ago=minutes_ago)


def auto(db):
    return json.loads(db.get_setting('cascade_auto') or '{}')


def mon_state(db):
    return json.loads(db.get_setting('dpi_monitor_state') or '{}')


def admin_rows(db, action=None):
    rows = _rows(db, "SELECT admin_id, action, target_id, details FROM admin_actions"
                     " ORDER BY id")
    return [r for r in rows if action is None or r[1] == action]


def ticks(monitor, n, *, start=T0, before=None):
    out, now = [], start
    for _ in range(n):
        if before:
            before(now)
        out.append(monitor.run_once(now=now))
        if monitor.reverse_sos_thread is not None:
            monitor.reverse_sos_thread.join(10)
        now += STEP
    return out


def user_on(asn, status='paid'):
    return SimpleNamespace(status=status, last_asn=asn, last_country=None)


@pytest.fixture
def monitor(db, bot):
    return DPIMonitor(db, make_config(), bot)


def r6(monitor, now=T0):
    return monitor.collect_signals(now)['client_dark']


class TestR6Collector:

    def test_three_clients_up_without_hy2_and_none_with_it(self, monitor, db):
        network(db, (1, 2, 3))
        assert r6(monitor) == {MEGAFON: {
            'hy2': 'клиенты AS31133: 0/3 по hy2 за 2 ч, другие протоколы живы'}}

    def test_two_dead_clients_are_not_enough(self, monitor, db):
        network(db, (1, 2))
        assert r6(monitor) == {}
        network(db, (3,))
        assert list(r6(monitor)[MEGAFON]) == ['hy2']

    def test_one_client_getting_through_vetoes(self, monitor, db):
        network(db, (1, 2, 3, 4))
        seed_rows(db, 5, ('hy2',))
        seed_user(db, 5)
        assert r6(monitor) == {}

    def test_the_window_is_two_hours(self, monitor, db):
        network(db, (1, 2, 3))
        seed_user(db, 4)
        seed_rows(db, 4, ('hy2',), minutes_ago=119)        # still in the window
        assert r6(monitor) == {}
        with db._connect() as c:
            c.execute("UPDATE client_probe SET ts = ? WHERE chat_id = '4'",
                      ((T0 - timedelta(minutes=121)).strftime('%Y-%m-%d %H:%M:%S'),))
            c.commit()
        assert list(r6(monitor)[MEGAFON]) == ['hy2']

    def test_silence_is_not_a_signal(self, monitor, db):
        # the app closed two hours ago: every row is outside the window
        network(db, (1, 2, 3), minutes_ago=125)
        assert r6(monitor) == {}

    def test_rows_through_the_reserve_alone_are_not_up(self, monitor, db):
        # exit unreachable, the DE node answers: no cascade protocol is up,
        # so nothing says the cascade protocols are dark per protocol
        network(db, (1, 2, 3), alive=('de',))
        assert r6(monitor) == {}

    def test_a_client_counts_for_every_protocol_it_lacks(self, monitor, db):
        network(db, (1, 2, 3), alive=('ws',))
        assert sorted(r6(monitor)[MEGAFON]) == ['hy2', 'reality', 'stls']

    def test_demo_clients_are_not_dead_on_paid_protocols(self, monitor, db):
        network(db, (1, 2, 3), status='demo', alive=('ws', 'stls', 'hy2'))
        assert r6(monitor) == {}
        network(db, (4, 5, 6), status='paid', alive=('ws', 'stls', 'hy2'))
        assert r6(monitor) == {MEGAFON: {
            'reality': 'клиенты AS31133: 0/3 по reality за 2 ч, другие протоколы живы'}}

    def test_support_topic_is_a_paid_status(self, monitor, db):
        network(db, (1, 2, 3), status='support_topic', alive=('ws', 'stls', 'hy2'))
        assert list(r6(monitor)[MEGAFON]) == ['reality']

    def test_inactive_users_do_not_count(self, monitor, db):
        network(db, (1, 2))
        network(db, (3,), status='banned')
        assert r6(monitor) == {}

    def test_a_protocol_the_operator_switched_off_is_not_judged(self, db, bot):
        # hy2t off since 2026-09-21: built (HY2T_PORT set) but disabled
        db.set_setting('cascade_protocol_order', json.dumps([
            {'name': 'hy2t', 'enabled': False}, {'name': 'stls', 'enabled': True},
            {'name': 'ws', 'enabled': True}, {'name': 'hy2', 'enabled': True},
            {'name': 'reality', 'enabled': True}]))
        mon = DPIMonitor(db, make_config(**HY2T), bot)
        network(db, (1, 2, 3), alive=('reality', 'ws', 'stls', 'hy2'))
        assert r6(mon) == {}
        db.set_setting('cascade_protocol_order', '')       # back to the default: on
        assert list(r6(mon)[MEGAFON]) == ['hy2t']

    def test_a_protocol_this_deployment_does_not_build_is_not_judged(self, monitor, db):
        # make_config(): HY2T_PORT empty — no p-hy2t provider exists anywhere
        network(db, (1, 2, 3), alive=('reality', 'ws', 'stls', 'hy2'))
        assert r6(monitor) == {}

    def test_per_network(self, monitor, db):
        network(db, (1, 2, 3), asn=MEGAFON)
        network(db, (4, 5, 6), asn=MTS, alive=('reality', 'ws', 'stls', 'hy2'))
        network(db, (7, 8, 9), asn=None)        # never fetched /sub since geo
        assert r6(monitor) == {MEGAFON: {
            'hy2': 'клиенты AS31133: 0/3 по hy2 за 2 ч, другие протоколы живы'}}

    def test_asn_is_normalised(self, monitor, db):
        network(db, (1, 2), asn=' as31133 ')
        network(db, (3,), asn='AS31133')
        assert list(r6(monitor)) == [MEGAFON]

    def test_the_channel_heartbeats_are_not_protocols(self, monitor, db):
        for cid in (1, 2, 3):
            seed_user(db, cid)
            with db._connect() as c:
                c.execute("INSERT INTO client_probe (chat_id, grp, ts) VALUES (?, ?, ?)",
                          (str(cid), 'emergency',
                           (T0 - timedelta(minutes=5)).strftime('%Y-%m-%d %H:%M:%S')))
                c.commit()
        assert r6(monitor) == {}

    def test_no_rows_at_all_does_not_read_the_config(self, db, bot):
        mon = DPIMonitor(db, Mock(), bot)        # a bare Mock config, like other tests
        assert r6(mon) == {}


class TestR6Monitor:

    def test_demoted_for_that_network_on_the_second_evaluation(self, monitor, db, bot):
        network(db, (1, 2, 3))
        [first] = ticks(monitor, 1)
        assert first == []
        assert mon_state(db)['targets'][f'asn:{MEGAFON}:hy2']['bad'] == 1
        [second] = ticks(monitor, 1, start=T0 + STEP)
        assert [c.to_dict() for c in second] == [{
            'scope': 'asn', 'target': MEGAFON, 'protocol': 'hy2', 'action': 'demote',
            'reason': 'client_dark',
            'evidence': 'клиенты AS31133: 0/3 по hy2 за 2 ч, другие протоколы живы'}]
        assert auto(db)['asn'][MEGAFON]['hy2']['reason'] == 'client_dark'
        assert MyKeyAnswerHandler.get_cascade_order(db, user=user_on(MEGAFON)) == (
            'hy2t', 'stls', 'ws', 'reality', 'hy2')
        assert MyKeyAnswerHandler.get_cascade_order(db, user=user_on(MTS)) == (
            'hy2t', 'stls', 'ws', 'hy2', 'reality')
        assert admin_rows(db, 'cascade_auto_demote') == [
            ('dpi_monitor', 'cascade_auto_demote', f'asn:{MEGAFON}:hy2',
             'client_dark: клиенты AS31133: 0/3 по hy2 за 2 ч, другие протоколы живы')]

    def test_restored_once_a_client_gets_through_again(self, monitor, db):
        network(db, (1, 2, 3))
        ticks(monitor, 2)
        assert 'hy2' in auto(db)['asn'][MEGAFON]
        seed_user(db, 4)
        seed_rows(db, 4, ('hy2',), now=T0 + 2 * STEP)
        good = ticks(monitor, 6, start=T0 + 2 * STEP)
        assert good[:5] == [[]] * 5
        assert [(c.action, c.protocol, c.reason) for c in good[5]] == [
            ('restore', 'hy2', 'client_dark')]
        assert auto(db) == {'asn': {}, 'global': {}}

    def test_judged_per_protocol_not_per_network(self):
        """hy2 demoted by R6; now the clients call REALITY dark there —
        hy2 is quiet and counts toward its restore."""
        state = dm.empty_state()
        state['auto']['asn'][MEGAFON] = {'hy2': {'since': T0.isoformat(),
                                                 'reason': 'client_dark', 'evidence': 'x'}}
        sig = dm.empty_signals()
        sig['client_dark'] = {MEGAFON: {'reality': 'reality dark'}}
        state, _ = DPIMonitor.evaluate(sig, state, T0 + STEP)
        assert state['targets'][f'asn:{MEGAFON}:hy2']['good'] == 1
        sig['client_dark'] = {MEGAFON: {'hy2': 'hy2 dark'}}
        state, _ = DPIMonitor.evaluate(sig, state, T0 + 2 * STEP)
        assert state['targets'][f'asn:{MEGAFON}:hy2']['good'] == 0

    def test_ranked_below_the_probe_rules_and_above_reality_asn(self):
        state = dm.empty_state()
        for key in ('global:ws', f'asn:{MEGAFON}:hy2', f'asn:{MTS}:reality'):
            state['targets'][key] = {'bad': 1, 'good': 0, 'rule': None, 'last_change': None}
        sig = dm.empty_signals()
        sig['probe'].update(stale=False, degraded={'ws': 'ws degraded'},
                            measured=['ws', 'hy2'])
        sig['client_dark'] = {MEGAFON: {'hy2': 'hy2 dark'}}
        sig['reality_asn'] = {MTS: 'reality hsfail'}
        state, changes = DPIMonitor.evaluate(sig, state, T0)
        assert [(c.reason, c.protocol) for c in changes] == [
            ('probe_degraded', 'ws'), ('client_dark', 'hy2')]
        state, changes = DPIMonitor.evaluate(sig, state, T0 + STEP)
        assert [(c.reason, c.protocol) for c in changes] == [('reality_asn', 'reality')]

    def test_outranks_reality_asn_on_the_same_target(self):
        state = dm.empty_state()
        sig = dm.empty_signals()
        sig['client_dark'] = {MEGAFON: {'reality': 'clients say reality is dark'}}
        sig['reality_asn'] = {MEGAFON: 'reality hsfail'}
        for i in range(2):
            state, changes = DPIMonitor.evaluate(sig, state, T0 + i * STEP)
        assert [(c.reason, c.evidence) for c in changes] == [
            ('client_dark', 'clients say reality is dark')]

    def test_counts_against_the_two_changes_cap(self, monitor, db):
        network(db, (1, 2, 3), alive=('ws',))         # hy2, reality, stls dark
        changes = ticks(monitor, 3)
        assert [c.protocol for c in changes[1]] == ['hy2', 'reality']
        assert [c.protocol for c in changes[2]] == ['stls']

    def test_a_broken_collector_freezes_its_demotions(self, monitor, db, caplog):
        network(db, (1, 2, 3))
        ticks(monitor, 2)
        with db._connect() as c:
            c.execute("DROP TABLE client_probe")
            c.commit()
        changes = ticks(monitor, 8, start=T0 + 2 * STEP)
        assert changes == [[]] * 8
        assert 'hy2' in auto(db)['asn'][MEGAFON]
        assert mon_state(db)['targets'][f'asn:{MEGAFON}:hy2']['good'] == 0
        assert 'collector client_dark failed' in caplog.text

    def test_frozen_by_an_upstream_outage(self, monitor, db):
        """Every entry probe dark = exit / link down: nothing moves."""
        network(db, (1, 2, 3))
        ts = (T0 - timedelta(minutes=5)).isoformat()
        with db._connect() as c:
            for proto in ('reality', 'ws', 'stls'):
                for run in range(3):
                    for i in range(10):
                        c.execute(
                            "INSERT INTO outbound_health (outbound_tag, target_domain, status,"
                            " latency_ms, error_msg, ts) VALUES (?,?,?,?,?,?)",
                            (proto, f'd{i}.example', 'timeout', None, 'timeout',
                             (T0 - timedelta(minutes=5 + run * 15)).isoformat()))
            c.commit()
        assert ts
        assert ticks(monitor, 3) == [[], [], []]
        assert auto(db) == {'asn': {}, 'global': {}}

    def test_dry_run_proposes_and_writes_nothing(self, monitor, db, bot):
        network(db, (1, 2, 3))
        monitor.run_once(now=T0)
        proposed = monitor.run_once(now=T0 + STEP, dry_run=True)
        assert [(c.reason, c.protocol) for c in proposed] == [('client_dark', 'hy2')]
        assert auto(db) == {'asn': {}, 'global': {}}
        assert monitor.reverse_sos_thread is None
        assert not [c for c in bot.send_message.call_args_list
                    if c.kwargs.get('chat_id') != GROUP]


# =====================================================================
# E21 — reverse SOS
# =====================================================================

def user_msgs(bot):
    return [c.kwargs for c in bot.send_message.call_args_list
            if c.kwargs.get('chat_id') != GROUP]


def topic_msgs(bot):
    return [c.kwargs for c in bot.send_message.call_args_list
            if c.kwargs.get('chat_id') == GROUP]


def hy2_dark_world(db, ids=(1, 2, 3), *, hy2_minutes_ago=300, asn=MEGAFON, now=T0, **kw):
    """R6 fires on hy2 for ``asn``; the clients used hy2 ``hy2_minutes_ago``
    before ``now`` (the reverse SOS itself runs a tick later, at now + STEP)."""
    network(db, ids, asn=asn, now=now, **kw)
    for cid in ids:
        seed_rows(db, cid, ('hy2',), now=now, minutes_ago=hy2_minutes_ago)


class TestReverseSos:

    def test_writes_to_the_networks_users_who_used_the_protocol(self, monitor, db, bot):
        hy2_dark_world(db)
        seed_user(db, 4)                                   # same network, no hy2 in 24 h
        seed_rows(db, 4, ('ws',))
        seed_rows(db, 4, ('hy2',), minutes_ago=25 * 60)
        seed_user(db, 5, asn=MTS)                           # other network
        seed_rows(db, 5, ('hy2',), minutes_ago=300)
        seed_user(db, 6, status='banned')                   # no active key
        seed_rows(db, 6, ('hy2',), minutes_ago=300)
        ticks(monitor, 2)
        assert sorted(m['chat_id'] for m in user_msgs(bot)) == ['1', '2', '3']
        text = user_msgs(bot)[0]['text']
        assert 'В вашей сети перестал работать Hysteria2' in text
        assert 'обновите профиль' in text and '/sos' in text
        tok = SubscriptionService(make_config()).derive_token(uuid_of(1))
        assert f'<code>{WEB}/sub/{tok}?format=clash&amp;emergency=1</code>' in \
            user_msgs(bot)[[m['chat_id'] for m in user_msgs(bot)].index('1')]['text']
        assert admin_rows(db, 'reverse_sos') == [(
            'dpi_monitor', 'reverse_sos', f'asn:{MEGAFON}:hy2',
            'recipients=3 telegram=3 mail=0 failed=0 cooldown=0 unreachable=0 selected=3')]

    def test_one_line_in_the_ai_topic(self, monitor, db, bot):
        hy2_dark_world(db)
        ticks(monitor, 2)
        lines = [m for m in topic_msgs(bot) if 'Обратный SOS' in m['text']]
        assert len(lines) == 1
        assert lines[0]['message_thread_id'] == TOPIC_AI
        assert lines[0]['text'] == ('📣 Обратный SOS: AS31133 · hy2 — написали 3 '
                                    '(Telegram 3, почта 0)')

    @pytest.mark.parametrize('hours,expected', [(23.9, ['1', '2', '3']), (24.1, [])])
    def test_the_recipient_window_is_a_day(self, monitor, db, bot, hours, expected):
        # measured from the run that demotes (the second tick, T0 + STEP)
        hy2_dark_world(db, hy2_minutes_ago=int(hours * 60) - 10)
        ticks(monitor, 2)
        assert sorted(m['chat_id'] for m in user_msgs(bot)) == expected
        assert len(admin_rows(db, 'reverse_sos')) == 1      # logged with zero too

    def test_once_a_day_per_user(self, db, bot):
        mon = DPIMonitor(db, make_config(), bot)
        hy2_dark_world(db)
        start = T0
        ticks(mon, 2, start=start)
        assert len(user_msgs(bot)) == 3
        # restored, then dark again 3 h later: no second message within 24 h
        db.set_setting('cascade_auto', '{}')
        db.set_setting('dpi_monitor_state', '{}')
        later = start + timedelta(hours=3)
        hy2_dark_world(db, now=later, hy2_minutes_ago=150)
        ticks(mon, 2, start=later)
        assert len(user_msgs(bot)) == 3
        assert admin_rows(db, 'reverse_sos')[-1][3].startswith('recipients=0 ')
        assert 'cooldown=3' in admin_rows(db, 'reverse_sos')[-1][3]
        # a day after the first message they may hear from us again
        db.set_setting('cascade_auto', '{}')
        db.set_setting('dpi_monitor_state', '{}')
        next_day = start + timedelta(hours=24, minutes=11)
        hy2_dark_world(db, now=next_day, hy2_minutes_ago=150)
        ticks(mon, 2, start=next_day)
        assert len(user_msgs(bot)) == 6

    def test_limit_survives_a_restart_and_prunes_old_entries(self, db, bot):
        hy2_dark_world(db)
        ticks(DPIMonitor(db, make_config(), bot), 2)
        sent = json.loads(db.get_setting('reverse_sos_sent'))
        assert sorted(sent) == ['1', '2', '3']
        db.set_setting('reverse_sos_sent', json.dumps(
            {**sent, 'gone': (T0 - timedelta(days=2)).isoformat()}))
        reverse_sos.claim(db, reverse_sos.load_sent(db), [], T0 + STEP)
        assert sorted(json.loads(db.get_setting('reverse_sos_sent'))) == ['1', '2', '3']

    def test_two_protocols_in_one_run_make_one_message(self, monitor, db, bot):
        network(db, (1, 2, 3), alive=('ws', 'stls'))
        for cid in (1, 2, 3):
            seed_rows(db, cid, ('hy2', 'reality'), minutes_ago=300)
        ticks(monitor, 2)
        msgs = user_msgs(bot)
        assert sorted(m['chat_id'] for m in msgs) == ['1', '2', '3']
        assert 'перестали работать Hysteria2 и Reality' in msgs[0]['text']
        assert [r[2] for r in admin_rows(db, 'reverse_sos')] == [
            f'asn:{MEGAFON}:hy2', f'asn:{MEGAFON}:reality']

    def test_ext_users_get_a_letter(self, db, bot):
        mailer = Mock()
        mailer.is_configured.return_value = True
        mailer.send_notice.return_value = True
        bot.services = {'email': mailer}
        mon = DPIMonitor(db, make_config(), bot)
        hy2_dark_world(db, ids=(1, 2))
        seed_user(db, 'ext_abc', contact='reader@example.org', lang='en')
        seed_rows(db, 'ext_abc', ('reality', 'ws', 'stls'))
        seed_rows(db, 'ext_abc', ('hy2',), minutes_ago=300)
        seed_user(db, 'ext_nomail', contact=None)
        seed_rows(db, 'ext_nomail', ('reality', 'ws', 'stls'))
        seed_rows(db, 'ext_nomail', ('hy2',), minutes_ago=300)
        ticks(mon, 2)
        assert sorted(m['chat_id'] for m in user_msgs(bot)) == ['1', '2']
        (to, subject, body), _kw = mailer.send_notice.call_args
        assert to == 'reader@example.org'
        assert subject == 'NekoVPN: Hysteria2 stopped working in your network'
        tok = SubscriptionService(make_config()).derive_token(uuid_of('ext_abc'))
        assert f'{WEB}/sub/{tok}?format=clash&emergency=1' in body
        assert not match_sos_keywords(subject) and not match_sos_keywords(body)
        assert admin_rows(db, 'reverse_sos')[0][3] == (
            'recipients=3 telegram=2 mail=1 failed=0 cooldown=0 unreachable=1 selected=4')
        assert 'ext_nomail' not in json.loads(db.get_setting('reverse_sos_sent'))

    def test_without_a_mailer_ext_users_are_unreachable(self, monitor, db, bot):
        # contact_email known, but SMTP is not configured (SMTP_HOST empty)
        hy2_dark_world(db, ids=('ext_a', 'ext_b', 'ext_c'))
        with db._connect() as c:
            c.execute("UPDATE users SET contact_email = chat_id || '@example.org'")
            c.commit()
        ticks(monitor, 2)
        assert user_msgs(bot) == []
        assert admin_rows(db, 'reverse_sos')[0][3] == (
            'recipients=0 telegram=0 mail=0 failed=0 cooldown=0 unreachable=3 selected=3')
        assert db.get_setting('reverse_sos_sent') is None      # nobody claimed

    @pytest.mark.parametrize('lang,first', [
        ('ru', '⚠️ <b>В вашей сети перестал работать Hysteria2.</b>'),
        ('en', '⚠️ <b>Hysteria2 stopped working in your network.</b>'),
    ])
    def test_texts(self, lang, first):
        text = reverse_sos.telegram_text(['hy2'], lang, f'{WEB}/sub/t?format=clash&emergency=1')
        assert text.splitlines()[0] == first
        assert '/sos' in text and f'{WEB}/sub/t?format=clash&amp;emergency=1' in text
        bare = reverse_sos.telegram_text(['hy2'], lang, None)
        assert '/sos' in bare and '<code>' not in bare
        for protos in (['hy2'], ['hy2', 'reality', 'ws']):
            subject, body = reverse_sos.mail_letter(protos, lang, f'{WEB}/sub/t')
            assert not match_sos_keywords(subject) and not match_sos_keywords(body)
            assert '/sos' not in body

    def test_only_client_dark_writes_to_users(self, monitor, db, bot):
        """R3 demotes reality for the network from server-side handshake
        failures — part of which are the operator's own scanners — so the
        users are NOT told their Reality broke."""
        network(db, (1, 2, 3), alive=('reality', 'hy2', 'ws', 'stls'))
        with db._connect() as c:
            c.execute(
                "INSERT INTO dpi_metrics (snapshot_at, country, asn, as_org, inbound_tag,"
                " conn_count, handshake_fail_count) VALUES (?,?,?,?,?,?,?)",
                ((T0 - timedelta(minutes=10)).isoformat(), 'RU', MEGAFON, 'MegaFon',
                 'reality', 0, 900))
            c.commit()
        changes = ticks(monitor, 2)
        assert [(c.reason, c.protocol) for c in changes[1]] == [('reality_asn', 'reality')]
        assert user_msgs(bot) == [] and admin_rows(db, 'reverse_sos') == []
        assert monitor.reverse_sos_thread is None

    def test_triggering_filter(self):
        C = dm.Change
        assert reverse_sos.triggering([
            C('asn', MEGAFON, 'hy2', 'demote', 'client_dark', 'e'),
            C('asn', MEGAFON, 'hy2', 'restore', 'client_dark', 'e'),
            C('global', None, 'ws', 'demote', 'client_dark', 'e'),
            C('asn', MEGAFON, 'reality', 'demote', 'reality_asn', 'e'),
            C('asn', MEGAFON, 'hy2', 'demote', 'udp_storm_asn', 'e'),
            C('asn', MEGAFON, 'stls', 'demote', 'user_reports_asn', 'e'),
        ]) == [C('asn', MEGAFON, 'hy2', 'demote', 'client_dark', 'e')]

    def test_without_a_bot_nobody_is_messaged(self, db):
        hy2_dark_world(db)
        mon = DPIMonitor(db, make_config(), None)
        ticks(mon, 2)
        assert 'hy2' in auto(db)['asn'][MEGAFON]
        assert admin_rows(db, 'reverse_sos') == [] and db.get_setting('reverse_sos_sent') is None

    def test_an_unreadable_limit_skips_the_event(self, monitor, db, bot, monkeypatch, caplog):
        hy2_dark_world(db)

        def locked(_db):
            raise sqlite3.OperationalError('database is locked')

        monkeypatch.setattr(reverse_sos, 'load_sent', locked)
        ticks(monitor, 2)
        assert 'hy2' in auto(db)['asn'][MEGAFON]            # the cascade still moved
        assert user_msgs(bot) == []
        assert 'unreadable' in caplog.text

    def test_a_failed_claim_sends_nothing(self, db, bot, monkeypatch, caplog):
        hy2_dark_world(db)
        real = db.set_setting
        monkeypatch.setattr(db, 'set_setting', lambda k, v: False if k == 'reverse_sos_sent'
                            else real(k, v))
        ticks(DPIMonitor(db, make_config(), bot), 2)
        assert user_msgs(bot) == []
        assert 'nothing sent' in caplog.text

    def test_a_failed_send_still_counts_against_the_day(self, monitor, db, bot):
        bot.send_message.side_effect = lambda **kw: None if kw['chat_id'] == '2' else {'ok': 1}
        hy2_dark_world(db)
        ticks(monitor, 2)
        assert 'failed=1' in admin_rows(db, 'reverse_sos')[0][3]
        assert sorted(json.loads(db.get_setting('reverse_sos_sent'))) == ['1', '2', '3']

    def test_a_crashing_planner_never_costs_the_cascade(self, monitor, db, bot, monkeypatch):
        hy2_dark_world(db)
        monkeypatch.setattr(reverse_sos, 'plan', Mock(side_effect=RuntimeError('boom')))
        changes = ticks(monitor, 2)
        assert [(c.reason, c.protocol) for c in changes[1]] == [('client_dark', 'hy2')]
        assert 'hy2' in auto(db)['asn'][MEGAFON]
