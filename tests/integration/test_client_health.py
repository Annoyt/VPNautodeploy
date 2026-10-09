"""GET /api/admin/client_health — what works per protocol × operator, seen
from the users' own clients (IMPROVEMENT_PLAN C2; the data is E8's).

Level 2 (AGENTS.md §27): a REAL sqlite (``Database`` in tmp_path) seeded
with ``users`` + ``client_probe`` rows. The semantics run through
``client_health.collect`` on a fixed clock; the wiring (route, admin auth,
``hours`` validation, the real clock) through the aiohttp app.

Pinned:

* rate = alive / (alive + dead) — not over every client of the operator;
* dead = rows on OTHER protocols in the window, none on this one, and the
  client's profile carries the protocol: an active status, enabled by the
  operator, the tier (a demo profile has no Reality / Turbo Hy2 / DE);
  a success through the DE reserve counts as "the client is up";
* channels (emergency, mirror-<n>) make a client count, never a protocol
  and never evidence that one failed;
* NULL / blank ASN and clients without a users row → the unknown group,
  listed last;
* the window is ``ts >= now - hours`` in client_probe's own format;
* one statement over client_probe per request (plus the cascade setting),
  whatever the number of operators;
* hours ∈ {1, 6, 24, 168} (default 24), anything else → 400; 401 without
  an admin.
"""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import client_health as ch
from bot.utils.admin_token import make_admin_token

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

BOT_TOKEN = 'test_token'
ADMIN = '1652899'
NOW = datetime(2026, 10, 10, 12, 0, 0, tzinfo=timezone.utc)


def ago(now=NOW, **delta) -> str:
    """A client_probe.ts value: sqlite's CURRENT_TIMESTAMP format."""
    return (now - timedelta(**delta)).strftime('%Y-%m-%d %H:%M:%S')


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def user(db, chat_id, status='paid', asn='AS31133', country='RU'):
    db._users.save(User(chat_id=chat_id, username=f'u{chat_id}', status=status,
                        last_asn=asn, last_country=country))


def probes(db, *rows):
    """rows: (chat_id, grp, ts)."""
    conn = db._connect()
    try:
        conn.executemany(
            "INSERT INTO client_probe (chat_id, grp, ts) VALUES (?, ?, ?)", rows)
        conn.commit()
    finally:
        conn.close()


def matrix(db, hours=24, **kw):
    return ch.collect(db, hours, now=NOW, **kw)


def row(data, asn):
    found = [r for r in data['rows'] if r['asn'] == asn]
    assert len(found) == 1, (asn, [r['asn'] for r in data['rows']])
    return found[0]


def cell(data, asn, proto):
    return row(data, asn)['protocols'][proto]


def ad(c):
    return c['alive'], c['dead']


# ------------------------------------------------------ alive / dead / rate --

class TestRate:

    def test_rate_is_alive_over_alive_plus_dead(self, db):
        """Five clients on one operator: three paid ones checking
        protocols, one paid one alive through a channel only, one demo.
        The rate is over the clients that answered for the protocol, not
        over everyone the operator has."""
        for cid in ('1', '2', '3', '4'):
            user(db, cid)
        user(db, '5', status='demo')
        probes(db,
               ('1', 'p-reality', ago(hours=1)), ('1', 'p-ws', ago(hours=1)),
               ('2', 'p-ws', ago(hours=2)),
               ('3', 'p-reality', ago(hours=3)),
               ('4', 'emergency', ago(hours=1)),
               ('5', 'p-ws', ago(hours=1)))
        r = row(matrix(db), 'AS31133')
        assert (r['clients'], r['probed']) == (5, 4)
        # reality: 1 and 3 alive, 2 (paid, ws only) dead; 5 is demo
        assert ad(r['protocols']['reality']) == (2, 1)
        assert r['protocols']['reality']['rate'] == pytest.approx(2 / 3)
        # ws: 1, 2, 5 alive; 3 dead
        assert ad(r['protocols']['ws']) == (3, 1)
        assert r['protocols']['ws']['rate'] == 0.75

    def test_zero_rate_is_zero_not_no_data(self, db):
        user(db, '1', asn='AS8359')
        user(db, '2', asn='AS31133')
        probes(db, ('1', 'p-ws', ago(hours=1)), ('2', 'p-reality', ago(hours=1)))
        data = matrix(db)
        assert cell(data, 'AS8359', 'reality')['rate'] == 0.0
        assert ad(cell(data, 'AS8359', 'reality')) == (0, 1)

    def test_nobody_answered_is_no_rate(self, db):
        """A demo-only operator has nobody Reality is offered to."""
        user(db, '1', status='demo', asn='AS8359')
        user(db, '2', asn='AS31133')
        probes(db, ('1', 'p-ws', ago(hours=1)), ('2', 'p-reality', ago(hours=1)))
        data = matrix(db)
        assert cell(data, 'AS8359', 'reality') == {
            'alive': 0, 'dead': 0, 'rate': None, 'last_ts': None}
        assert cell(data, 'AS31133', 'reality')['rate'] == 1.0


class TestDead:

    def test_needs_rows_on_another_protocol(self, db):
        """Channel-only clients are alive but say nothing about protocols;
        a client checking only ws IS dead on the reality it is offered."""
        for cid in ('1', '2', '3'):
            user(db, cid)
        probes(db,
               ('1', 'p-reality', ago(hours=1)),
               ('2', 'emergency', ago(hours=1)), ('2', 'mirror-1', ago(hours=1)),
               ('3', 'p-ws', ago(hours=1)))
        r = row(matrix(db), 'AS31133')
        assert (r['clients'], r['probed']) == (3, 2)
        assert ad(r['protocols']['reality']) == (1, 1)
        assert ad(r['protocols']['ws']) == (1, 1)

    def test_tier_gate(self, db):
        """Each client checks ws only. Paid-only protocols (reality, hy2t)
        and the DE reserve are dead for paid / support_topic only — a demo
        profile never had them; the free hy2 is dead for every active
        client. A client without a users row gets no profile from /sub:
        alive counts, dead never does."""
        user(db, 'paid', status='paid')
        user(db, 'sup', status='support_topic')
        user(db, 'demo', status='demo')
        user(db, 'sees', status='paid', asn='AS8359')
        probes(db, *[(cid, 'p-ws', ago(hours=1))
                     for cid in ('paid', 'sup', 'demo', 'ghost')])
        probes(db, *[('sees', g, ago(hours=1))
                     for g in ('p-reality', 'p-hy2t', 'p-de', 'p-hy2', 'p-ws')])
        data = matrix(db)
        assert data['protocols'] == ['reality', 'hy2', 'hy2t', 'ws', 'de']
        r = row(data, 'AS31133')
        for proto in ('reality', 'hy2t', 'de'):
            assert ad(r['protocols'][proto]) == (0, 2), proto
        assert ad(r['protocols']['hy2']) == (0, 3)
        ghost = row(data, None)
        assert ad(ghost['protocols']['reality']) == (0, 0)
        assert ad(ghost['protocols']['de']) == (0, 0)
        assert ad(ghost['protocols']['hy2']) == (0, 0)
        assert ad(ghost['protocols']['ws']) == (1, 0)
        assert ad(data['total']['protocols']['reality']) == (1, 2)
        assert ad(data['total']['protocols']['hy2']) == (1, 3)

    @pytest.mark.parametrize('status', ['new', 'pending_demo', 'platform_select',
                                        'rejected', 'banned', None])
    def test_only_active_clients_can_be_dead(self, db, status):
        """/sub serves demo / paid / support_topic only (410 for the rest):
        a client in any other status keeps whatever profile it had, and
        its silence on a protocol says nothing."""
        user(db, '1', status=status)
        user(db, '2', status='paid')
        probes(db, ('1', 'p-ws', ago(hours=1)),
               ('2', 'p-ws', ago(hours=1)), ('2', 'p-hy2', ago(hours=1)))
        r = row(matrix(db), 'AS31133')
        assert (r['clients'], r['probed']) == (2, 2)
        assert ad(r['protocols']['ws']) == (2, 0)
        assert ad(r['protocols']['hy2']) == (1, 0)

    def test_protocol_switched_off_is_dead_for_nobody(self, db):
        """The operator disabled reality in the cascade: refreshed profiles
        no longer carry it. A stale profile still answering is alive; the
        others are not dead on it."""
        db.set_setting('cascade_protocol_order', json.dumps(
            [{'name': 'reality', 'enabled': False}, {'name': 'ws', 'enabled': True}]))
        for cid in ('1', '2', '3'):
            user(db, cid)
        probes(db, ('1', 'p-reality', ago(hours=1)), ('1', 'p-ws', ago(hours=1)),
               ('2', 'p-ws', ago(hours=1)), ('3', 'p-ws', ago(hours=1)))
        r = row(matrix(db), 'AS31133')
        assert ad(r['protocols']['reality']) == (1, 0)
        db.set_setting('cascade_protocol_order', json.dumps(
            [{'name': 'reality', 'enabled': True}]))
        assert ad(cell(matrix(db), 'AS31133', 'reality')) == (1, 2)

    def test_the_reserve_counts_as_up(self, db):
        """A paid client getting through ONLY via the DE reserve is dead on
        every main protocol its profile carries: the whole cascade is cut
        for it (an operator blocking the entry IP looks exactly so)."""
        user(db, '1')
        user(db, '2', asn='AS8359')
        probes(db, ('1', 'p-de', ago(hours=1)),
               *[('2', g, ago(hours=1)) for g in ('p-reality', 'p-ws', 'p-de')])
        r = row(matrix(db), 'AS31133')
        assert ad(r['protocols']['de']) == (1, 0)
        assert ad(r['protocols']['reality']) == (0, 1)
        assert ad(r['protocols']['ws']) == (0, 1)

    def test_unknown_protocol_is_carried_for_every_active_client(self, db):
        user(db, '1', status='demo')
        user(db, '3', status='banned')
        user(db, '2', asn='AS8359')
        probes(db, ('1', 'p-ws', ago(hours=1)), ('3', 'p-ws', ago(hours=1)),
               ('2', 'p-xhttp', ago(hours=1)))
        assert ad(cell(matrix(db), 'AS31133', 'xhttp')) == (0, 1)

    def test_old_success_does_not_count(self, db):
        """Only the window counts: a Reality success 30 h ago does not keep
        the client alive on it in the 24 h view."""
        user(db, '1')
        user(db, '2', asn='AS8359')
        probes(db,
               ('1', 'p-reality', ago(hours=30)), ('1', 'p-ws', ago(hours=1)),
               ('2', 'p-reality', ago(hours=1)))
        assert ad(cell(matrix(db), 'AS31133', 'reality')) == (0, 1)
        assert ad(cell(matrix(db, hours=168), 'AS31133', 'reality')) == (1, 0)


class TestChannels:

    def test_never_become_protocols(self, db):
        user(db, '1')
        probes(db, ('1', 'emergency', ago(hours=1)), ('1', 'mirror-1', ago(hours=1)),
               ('1', 'mirror-12', ago(hours=2)), ('1', 'cascade', ago(hours=2)))
        data = matrix(db)
        assert data['protocols'] == []
        r = row(data, 'AS31133')
        assert (r['clients'], r['probed'], r['protocols']) == (1, 0, {})
        assert r['last_ts'] == ago(hours=1)

    def test_mixed_with_protocols(self, db):
        user(db, '1')
        user(db, '2')
        probes(db, ('1', 'p-ws', ago(hours=1)), ('1', 'emergency', ago(hours=1)),
               ('2', 'mirror-2', ago(hours=1)))
        data = matrix(db)
        assert data['protocols'] == ['ws']
        assert set(row(data, 'AS31133')['protocols']) == {'ws'}
        assert ad(cell(data, 'AS31133', 'ws')) == (1, 0)

    @pytest.mark.parametrize('grp', ['p-', 'p-<b>x</b>', 'p-a b', 'P-ws'])
    def test_malformed_group_is_not_a_protocol(self, db, grp):
        user(db, '1')
        probes(db, ('1', grp, ago(hours=1)))
        data = matrix(db)
        assert data['protocols'] == []
        assert row(data, 'AS31133')['clients'] == 1

    def test_protocol_columns_in_contract_order(self, db):
        user(db, '1')
        probes(db, *[('1', g, ago(hours=1)) for g in
                     ('p-zz', 'p-de', 'p-ws', 'p-aa', 'p-stls', 'p-reality',
                      'p-hy2t', 'p-hy2')])
        assert matrix(db)['protocols'] == [
            'reality', 'hy2', 'hy2t', 'ws', 'stls', 'de', 'aa', 'zz']


# ------------------------------------------------------------- operators ----

class TestOperators:

    def test_null_blank_and_missing_users_are_the_unknown_group(self, db):
        user(db, '1', asn=None)
        user(db, '2', asn='')
        user(db, '3', asn='   ')
        user(db, '5')
        probes(db, *[(cid, 'p-ws', ago(hours=1)) for cid in ('1', '2', '3', '4', '5')])
        data = matrix(db)
        # the unknown group is the biggest and still listed last
        assert [r['asn'] for r in data['rows']] == ['AS31133', None]
        assert row(data, None)['clients'] == 4
        assert data['total']['clients'] == 5

    def test_asn_is_normalised(self, db):
        user(db, '1', asn=' as31133 ')
        user(db, '2', asn='AS31133')
        probes(db, ('1', 'p-ws', ago(hours=1)), ('2', 'p-ws', ago(hours=1)))
        data = matrix(db)
        assert [r['asn'] for r in data['rows']] == ['AS31133']
        assert row(data, 'AS31133')['clients'] == 2

    def test_country_is_the_most_common(self, db):
        user(db, '1', country='RU')
        user(db, '2', country='ru')
        user(db, '3', country='KZ')
        user(db, '4', asn='AS8359', country='KZ')
        user(db, '5', asn='AS8359', country='BY')
        probes(db, *[(cid, 'p-ws', ago(hours=1)) for cid in '12345'])
        data = matrix(db)
        assert row(data, 'AS31133')['country'] == 'RU'
        assert row(data, 'AS8359')['country'] == 'BY'      # a tie: alphabetical
        assert 'country' not in data['total'] and 'asn' not in data['total']

    def test_busiest_first_and_the_cap(self, db):
        layout = {'AS1': 1, 'AS2': 3, 'AS3': 2, None: 5}
        n = 0
        for asn, k in layout.items():
            for _ in range(k):
                n += 1
                user(db, str(n), asn=asn)
                probes(db, (str(n), 'p-ws', ago(hours=1)))
        data = matrix(db)
        assert [r['asn'] for r in data['rows']] == ['AS2', 'AS3', 'AS1', None]
        capped = matrix(db, max_rows=2)
        assert [r['asn'] for r in capped['rows']] == ['AS2', 'AS3']
        assert capped['rows_total'] == 4
        # the total is over every operator, not over the rows returned
        assert capped['total']['clients'] == 11
        assert ad(capped['total']['protocols']['ws']) == (11, 0)

    def test_last_ts(self, db):
        """Per operator: the newest row of any group (channels too); per
        cell: the newest success of that protocol. Rows outside the window
        do not count."""
        user(db, '1')
        user(db, '2')
        # the newest values sit on the client read FIRST, so "the last one
        # read" is not mistaken for "the newest"
        probes(db,
               ('1', 'emergency', ago(hours=1)),
               ('1', 'p-ws', ago(hours=5)), ('1', 'p-ws', ago(hours=2)),
               ('2', 'p-ws', ago(hours=4)),
               ('2', 'p-reality', ago(hours=3)), ('2', 'p-reality', ago(hours=40)))
        data = matrix(db)
        r = row(data, 'AS31133')
        assert r['last_ts'] == ago(hours=1)
        assert r['protocols']['ws']['last_ts'] == ago(hours=2)
        assert r['protocols']['reality']['last_ts'] == ago(hours=3)
        assert data['total']['last_ts'] == ago(hours=1)

    @pytest.mark.parametrize('order', [1, -1])
    def test_last_ts_does_not_depend_on_read_order(self, order):
        pairs = [('1', 'emergency', '2026-10-10 11:00:00', 'AS1', 'RU', 'paid'),
                 ('1', 'p-ws', '2026-10-10 10:00:00', 'AS1', 'RU', 'paid'),
                 ('2', 'p-ws', '2026-10-10 08:00:00', 'AS1', 'RU', 'paid'),
                 ('2', 'mirror-1', '2026-10-10 09:00:00', 'AS1', 'RU', 'paid')][::order]
        data = ch.summarize(pairs, rules={})
        r = row(data, 'AS1')
        assert r['last_ts'] == data['total']['last_ts'] == '2026-10-10 11:00:00'
        assert r['protocols']['ws']['last_ts'] == '2026-10-10 10:00:00'

    def test_too_many_pairs_is_flagged(self, db):
        user(db, '1')
        probes(db, ('1', 'p-ws', ago(hours=1)), ('1', 'p-hy2', ago(hours=1)),
               ('1', 'emergency', ago(hours=1)))
        assert matrix(db)['truncated'] is False
        assert matrix(db, max_pairs=3)['truncated'] is False
        assert matrix(db, max_pairs=2)['truncated'] is True


# ---------------------------------------------------------------- window ----

class TestWindow:

    def test_edge_of_the_window(self, db):
        user(db, '1')
        user(db, '2')
        probes(db, ('1', 'p-ws', ago(hours=24)), ('2', 'p-ws', ago(hours=24, seconds=1)))
        data = matrix(db)
        assert data['since'] == ago(hours=24)
        assert row(data, 'AS31133')['clients'] == 1

    def test_same_day_rows_count_in_the_hour_window(self, db):
        """client_probe.ts has a SPACE: an isoformat cutoff ('…T…') would
        sort after every row of its own day and empty the 1 h view."""
        user(db, '1')
        probes(db, ('1', 'p-ws', ago(minutes=30)))
        assert matrix(db, hours=1)['total']['clients'] == 1

    @pytest.mark.parametrize('hours,clients', [(1, 1), (6, 2), (24, 3), (168, 4)])
    def test_each_window(self, db, hours, clients):
        for cid, back in (('1', dict(minutes=30)), ('2', dict(hours=3)),
                          ('3', dict(hours=20)), ('4', dict(hours=100)),
                          ('5', dict(hours=200))):
            user(db, cid)
            probes(db, (cid, 'p-ws', ago(**back)))
        data = matrix(db, hours=hours)
        assert (data['hours'], data['since']) == (hours, ago(hours=hours))
        assert data['total']['clients'] == clients

    def test_parse_window(self):
        assert ch.parse_window(None) == 24
        for h in (1, 6, 24, 168):
            assert ch.parse_window(str(h)) == h
        for raw in ('', '0', '2', '720', '24.0', ' 24', '024', 'abc'):
            assert ch.parse_window(raw) is None, raw


class TestOneQuery:

    def test_one_statement_whatever_the_number_of_operators(self, db, monkeypatch):
        executed = []
        real_connect = db._connect

        def traced():
            conn = real_connect()
            conn.set_trace_callback(executed.append)
            return conn

        def selects(n_asn):
            for i in range(n_asn):
                cid = f'{n_asn}-{i}'
                user(db, cid, asn=f'AS{64500 + i}')
                probes(db, (cid, 'p-ws', ago(hours=1)), (cid, 'p-hy2', ago(hours=1)),
                       (cid, 'emergency', ago(hours=1)))
            monkeypatch.setattr(db, '_connect', traced)
            executed.clear()
            matrix(db)
            monkeypatch.setattr(db, '_connect', real_connect)
            return [s for s in executed if s.lstrip().upper().startswith('SELECT')]

        one, ten = selects(1), selects(10)
        assert len(one) == len(ten) == 2      # client_probe + the cascade setting
        assert sum('client_probe' in s for s in one) == 1
        assert sum('client_probe' in s for s in ten) == 1


# -------------------------------------------------------------- endpoint ----

@pytest.fixture
def srv(db):
    config = Mock()
    config.BOT_TOKEN = BOT_TOKEN
    config.is_admin = lambda uid: str(uid) == ADMIN
    return WebAppServer(config, db, xui_service=Mock())


async def get(srv, **query):
    async with TestClient(TestServer(srv.app)) as client:
        resp = await client.get('/api/admin/client_health', params=query)
        return resp.status, await resp.json()


def token(admin_id=ADMIN):
    return make_admin_token(BOT_TOKEN, admin_id)


class TestEndpoint:

    @pytest.mark.asyncio
    async def test_admin_only(self, srv):
        assert (await get(srv))[0] == 401
        assert (await get(srv, admin_token='garbage'))[0] == 401
        assert (await get(srv, admin_token=token('42')))[0] == 401
        status, data = await get(srv, admin_token=token())
        assert status == 200
        assert data['hours'] == 24 and data['rows'] == [] and data['protocols'] == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize('raw', ['abc', '0', '2', '12', '48', '720', '',
                                     '24.0', ' 24', '-1', '1e1'])
    async def test_other_windows_are_a_400(self, srv, raw):
        status, data = await get(srv, admin_token=token(), hours=raw)
        assert status == 400
        assert data['error'] == 'hours must be one of 1, 6, 24, 168'

    @pytest.mark.asyncio
    @pytest.mark.parametrize('raw', ['1', '6', '24', '168'])
    async def test_offered_windows(self, srv, raw):
        status, data = await get(srv, admin_token=token(), hours=raw)
        assert status == 200
        assert data['hours'] == int(raw)

    @pytest.mark.asyncio
    async def test_real_clock(self, srv, db):
        now = datetime.now(timezone.utc)
        user(db, '1')
        user(db, '2', asn=None)
        probes(db, ('1', 'p-ws', ago(now, minutes=10)), ('1', 'emergency', ago(now, minutes=5)),
               ('2', 'p-ws', ago(now, hours=3)), ('2', 'p-reality', ago(now, hours=3)))
        status, data = await get(srv, admin_token=token(), hours='1')
        assert status == 200
        assert [r['asn'] for r in data['rows']] == ['AS31133']
        assert data['protocols'] == ['ws']
        status, data = await get(srv, admin_token=token(), hours='6')
        assert [r['asn'] for r in data['rows']] == ['AS31133', None]
        assert data['protocols'] == ['reality', 'ws']
        assert ad(data['total']['protocols']['reality']) == (1, 1)
        assert data['truncated'] is False and data['rows_total'] == 2

    @pytest.mark.asyncio
    async def test_read_failure_is_a_500(self, srv, db):
        conn = db._connect()
        conn.execute("DROP TABLE client_probe")
        conn.commit()
        conn.close()
        status, data = await get(srv, admin_token=token())
        assert status == 500
        assert 'client_probe' in data['error']
