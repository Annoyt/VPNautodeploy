"""Failure report: the operator ping and the row carry the USER's facts.

Regression (2026-09-30, report #10): "Last traffic: 14:32" was
users.last_traffic_update — the 10-minute traffic mirror stamps it on every
panel client each run — while the user's connections kept arriving until
15:27; "Network: RU / AS31133" came from a /sub fetch four days old with
nothing saying so; the row's last_sub_fetch_ts column was always NULL.

Level 2: real sqlite (Database creates sub_fetches / user_presence /
hy2_auth_log / user_failure_reports); only the panel read and Telegram
are faked.
"""

import asyncio
import threading
import time
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from bot.core.database import Database
from bot.handlers.callbacks.user import MyKeyAnswerHandler as MK, _ago

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

CID = '52291265'
EMAIL = 'user_ziriki_52291265@nekovo.ru'
GROUP, TOPIC = -1001234, 77


def utcnow():
    return datetime.now(timezone.utc).replace(tzinfo=None)


def sql_ts(dt):
    return dt.isoformat(sep=' ', timespec='seconds')


def ms(dt):
    return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)


@pytest.fixture(autouse=True)
def _class_state():
    MK._last_report_times.clear()
    MK._panel_skip_until = 0.0
    yield
    MK._last_report_times.clear()
    MK._panel_skip_until = 0.0


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def handler(db):
    bot = Mock()
    bot.send_message.return_value = {'message_id': 1}
    h = MK(bot, db, SimpleNamespace(FORUM_GROUP_ID=GROUP, TOPIC_SUPPORT=TOPIC))
    h.panel = 0            # what the fake panel read returns / raises / calls
    h.panel_calls = []

    def fake_fetch(email):
        h.panel_calls.append(email)
        value = h.panel
        if isinstance(value, Exception):
            raise value
        return value() if callable(value) else value

    h._fetch_panel_last_online_ms = fake_fetch
    return h


def user(**kw):
    fields = dict(
        chat_id=CID, username='ziriki', email=EMAIL, uuid='uuid-1',
        last_country='RU', last_asn='AS31133', last_city=None,
        last_lat=None, last_lon=None,
        # the mirror just ran — exactly the value that used to leak out
        last_traffic_update=utcnow().isoformat(),
    )
    fields.update(kw)
    return SimpleNamespace(**fields)


def report(h, u=None, category='nothing_loads'):
    u = u or user()
    h._handle_target_selection(f'report_target:{category}', u.chat_id,
                               u.chat_id, u, 'ru')
    pings = [c.kwargs['text'] for c in h.bot.send_message.call_args_list
             if c.kwargs.get('chat_id') == GROUP]
    assert pings, 'no operator ping'
    return pings[-1]


def last_row(db):
    with db._connect() as conn:
        return conn.execute(
            "SELECT chat_id, last_sub_fetch_ts, last_traffic_ts, target_domain "
            "FROM user_failure_reports ORDER BY id DESC LIMIT 1").fetchone()


def seed(db, sql, *args):
    with db._connect() as conn:
        conn.execute(sql, args)
        conn.commit()


def sub_fetch(db, at):
    seed(db, "INSERT INTO sub_fetches (ts, chat_id, country, asn) "
             "VALUES (?, ?, 'RU', 'AS31133')", sql_ts(at), CID)


def presence(db, proto, at, conns):
    seed(db, "INSERT INTO user_presence (email, inbound_tag, proto, conns, seen_at) "
             "VALUES (?, 'inbound-x', ?, ?, ?)", EMAIL, proto, conns, at.isoformat())


def hy2(db, at, decision='allow'):
    seed(db, "INSERT INTO hy2_auth_log (ts, chat_id, decision, addr_ip) "
             "VALUES (?, ?, ?, '130.49.146.10')", sql_ts(at), CID, decision)


class TestLastTraffic:

    def test_is_the_panels_last_online_not_the_mirror_clock(self, handler, db):
        u = user()
        seen = utcnow() - timedelta(days=3, minutes=5)
        handler.panel = ms(seen)
        text = report(handler, u)
        assert 'Last traffic: 3 дн назад' in text
        assert u.last_traffic_update[:16] not in text
        assert last_row(db)[2] == sql_ts(seen)

    def test_recent_traffic_shows_clock_time_to_match_logs(self, handler, db):
        seen = utcnow() - timedelta(minutes=5, seconds=10)
        handler.panel = ms(seen)
        text = report(handler)
        assert 'Last traffic: 5 мин назад, ' in text
        assert f'{seen:%H:%M} UTC' in text

    def test_client_missing_from_the_panel_is_flagged(self, handler, db):
        handler.panel = None
        text = report(handler)
        assert 'Last traffic: ⚠️ клиента нет в панели' in text
        assert last_row(db)[2] is None

    def test_client_that_never_had_traffic(self, handler, db):
        handler.panel = 0
        assert 'Last traffic: ни разу' in report(handler)
        assert last_row(db)[2] is None

    def test_panel_error_is_not_fatal(self, handler, db):
        handler.panel = RuntimeError('panel 502')
        text = report(handler)
        assert 'Last traffic: нет данных (панель не ответила)' in text
        assert last_row(db)[0] == CID and last_row(db)[3] == 'nothing_loads'
        to_user = [c for c in handler.bot.send_message.call_args_list
                   if c.kwargs.get('chat_id') == CID]
        assert to_user and 'Сигнал получили' in to_user[0].kwargs['text']


class TestDeadPanel:
    """Updates are handled one at a time; a hung panel must not hold them."""

    def test_costs_at_most_the_cap_then_is_skipped(self, handler, monkeypatch):
        monkeypatch.setattr(MK, 'PANEL_LOOKUP_TIMEOUT_S', 0.2)
        release = threading.Event()
        handler.panel = lambda: (release.wait(10), 0)[1]
        try:
            t0 = time.monotonic()
            text = report(handler)
            assert time.monotonic() - t0 < 1.5
            assert 'Last traffic: нет данных (панель не ответила)' in text
            assert MK._panel_skip_until - time.monotonic() > 50

            # the next report inside the window doesn't even try
            t0 = time.monotonic()
            text = report(handler, user(chat_id='777', username='other'))
            assert time.monotonic() - t0 < 0.5
            assert len(handler.panel_calls) == 1
            assert 'нет данных (панель не ответила)' in text
        finally:
            release.set()

    def test_panel_is_asked_again_after_the_window(self, handler):
        MK._panel_skip_until = time.monotonic() - 1
        handler.panel = ms(utcnow())
        assert 'Last traffic: только что' in report(handler)
        assert handler.panel_calls == [EMAIL]


class TestNetworkAge:

    def test_network_carries_the_age_of_the_last_sub_fetch(self, handler, db):
        sub_fetch(db, utcnow() - timedelta(days=10))
        latest = utcnow() - timedelta(days=4, hours=1)
        sub_fetch(db, latest)
        text = report(handler)
        assert 'Network: RU / AS31133 (по /sub 4 дн назад)' in text
        assert last_row(db)[1] == sql_ts(latest)

    def test_other_users_fetches_do_not_count(self, handler, db):
        seed(db, "INSERT INTO sub_fetches (ts, chat_id) VALUES (?, '999')",
             sql_ts(utcnow()))
        assert 'Network: RU / AS31133 (давность неизвестна)' in report(handler)
        assert last_row(db)[1] is None

    def test_no_geo_at_all(self, handler):
        text = report(handler, user(last_country=None, last_asn=None))
        assert 'Network: unk\n' in text


class TestProtocols:

    def test_xray_feed_and_a_hy2_reconnect_storm(self, handler, db):
        now = utcnow()
        presence(db, 'cf-ws', now - timedelta(minutes=1, seconds=5), 62)
        for i in range(5):                                   # outside the hour
            hy2(db, now - timedelta(hours=3, minutes=i))
        for i in range(23):                                  # the storm
            hy2(db, now - timedelta(minutes=11, seconds=5 + 30 * i))
        text = report(handler)
        assert ('Protocols: WS 1 мин назад (соединений за 5 мин: 62)'
                ' · Hy2 11 мин назад (входов за час: 23)') in text

    def test_hy2_deny_is_flagged(self, handler, db):
        now = utcnow()
        hy2(db, now - timedelta(hours=2))
        hy2(db, now - timedelta(minutes=2, seconds=5), decision='deny')
        assert 'Protocols: Hy2 ОТКАЗ 2 мин назад' in report(handler)

    def test_other_users_rows_do_not_count(self, handler, db):
        seed(db, "INSERT INTO hy2_auth_log (ts, chat_id, decision) "
                 "VALUES (?, '999', 'allow')", sql_ts(utcnow()))
        seed(db, "INSERT INTO user_presence (email, proto, seen_at) "
                 "VALUES ('someone@else', 'reality', ?)", utcnow().isoformat())
        assert 'Protocols:' not in report(handler)


class TestRobustness:

    def test_missing_tables_do_not_block_the_report(self, handler, db):
        with db._connect() as conn:
            for table in ('sub_fetches', 'user_presence', 'hy2_auth_log'):
                conn.execute(f"DROP TABLE {table}")
            conn.commit()
        handler.panel = ms(utcnow() - timedelta(minutes=30))
        text = report(handler)
        assert 'Network: RU / AS31133 (давность неизвестна)' in text
        assert 'Last traffic: 30 мин назад' in text
        assert last_row(db)[0] == CID


class TestPanelRead:
    """The real _fetch_panel_last_online_ms against a fake XUIService."""

    @pytest.fixture
    def read(self, db, monkeypatch):
        state = {'api': True, 'inbounds': [], 'closed': 0, 'raise': None}

        class FakeXUI:
            def __init__(self, config):
                self.api = (SimpleNamespace(get_inbounds=self._inbounds,
                                            close=self._close)
                            if state['api'] else None)

            async def _inbounds(self):
                if state['raise']:
                    raise state['raise']
                return state['inbounds']

            async def _close(self):
                state['closed'] += 1

            def _run_sync(self, coro):
                return asyncio.run(coro)

        monkeypatch.setattr('bot.services.xui_service.XUIService', FakeXUI)
        h = MK(Mock(), db, SimpleNamespace())
        return state, lambda: h._fetch_panel_last_online_ms(EMAIL)

    def test_reads_the_clients_row(self, read):
        state, fetch = read
        state['inbounds'] = [
            {'clientStats': [{'email': 'a@x', 'lastOnline': 5}]},
            {'clientStats': [{'email': EMAIL, 'lastOnline': 1790000000000}]},
        ]
        assert fetch() == 1790000000000

    def test_client_not_in_the_panel(self, read):
        state, fetch = read
        state['inbounds'] = [{'clientStats': [{'email': 'a@x', 'lastOnline': 5}]}]
        assert fetch() is None

    def test_row_without_traffic(self, read):
        state, fetch = read
        state['inbounds'] = [{'clientStats': [{'email': EMAIL}]}]
        assert fetch() == 0

    def test_no_inbounds_means_the_panel_could_not_be_asked(self, read):
        state, fetch = read
        with pytest.raises(RuntimeError):
            fetch()

    def test_no_api(self, read):
        state, fetch = read
        state['api'] = False
        with pytest.raises(RuntimeError):
            fetch()

    def test_session_is_closed_after_a_read(self, read):
        state, fetch = read
        state['inbounds'] = [{'clientStats': [{'email': EMAIL, 'lastOnline': 7}]}]
        assert fetch() == 7
        assert state['closed'] == 1

    def test_session_is_closed_when_the_read_fails(self, read):
        state, fetch = read
        state['raise'] = ConnectionError('panel down')
        with pytest.raises(ConnectionError):
            fetch()
        assert state['closed'] == 1


@pytest.mark.parametrize('secs, text', [
    (0, 'только что'), (59, 'только что'), (60, '1 мин назад'),
    (59 * 60 + 59, '59 мин назад'), (3600, '1 ч назад'),
    (47 * 3600 + 3599, '47 ч назад'), (48 * 3600, '2 дн назад'),
    (-30, 'только что'),                   # clock skew never reads "in the future"
])
def test_ago(secs, text):
    now = datetime(2026, 9, 30, 15, 30)
    assert _ago(now - timedelta(seconds=secs), now) == text
