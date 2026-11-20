"""The NekoVPN app's network diagnostics: POST /api/neko/netstat
(docs/NEKO6_PLAN.md, contract D2; bot/services/netstat.py).

Level 2: the real WebAppServer on the runner the bot serves it with, on
127.0.0.1 and a free port, over a REAL sqlite (Database in tmp_path), and
aiohttp's own client. Nothing is faked.

What is pinned: the answers (204 / 400 / 401 / 405 / 413 / 429), the
strict reading of every field, the 10-minute floor of ``ts``, that nothing
stored or logged names the user (no token, chat_id or address: the table's
columns and values, every log record, the access log included), and the
daily cleanup.
"""

import asyncio
import json
import logging
import re
import socket
import sqlite3
import time
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import MagicMock, Mock, patch

import aiohttp
import pytest
from aiohttp import web

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.models.user import User
from bot.services import netstat
from bot.services.notifications import NotificationService
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

PATH = '/api/neko/netstat'
CID, UUID = '52291265', '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
CID2, UUID2 = '7700770', '0f9e8d7c-6b5a-4c3d-8e2f-1a0b9c8d7e6f'
USERNAME, EMAIL = 'ziriki', f'user_ziriki_{CID}@nekovo.ru'
CONTACT = 'ziriki.home@example.org'
# What a proxy in front would add; the handler must never read it.
FORWARDED = '198.51.100.23'
DAY = 86400


def make_config():
    return SimpleNamespace(BOT_TOKEN='netstat-test-bot-token',
                           WEBAPP_URL='https://dash.example.com',
                           is_admin=lambda user_id: False)


def token(uuid=UUID):
    return SubscriptionService(make_config()).derive_token(uuid)


def bucket(ts=None):
    ts = int(time.time()) if ts is None else ts
    return ts - ts % 600


def event(**over):
    """D2's example event, an hour ago; ``None`` in ``over`` drops a field."""
    ev = {'ts': bucket() - 3600, 'kind': 'drop', 'net': 'cell', 'mcc': '250',
          'mnc': '01', 'rat': 'lte', 'level': 2, 'ru': True, 'ours': False,
          'proto': {'hy2': False, 'stls': False},
          'cell': {'tac': 7701, 'cid': 123456789, 'pci': 301, 'earfcn': 1850},
          'area': 'ucfv0'}
    for key, value in over.items():
        if value is None:
            ev.pop(key, None)
        else:
            ev[key] = value
    return ev


def body(*events, **over):
    doc = {'v': 1, 'app': '0.9.2-neko.6', 'events': list(events) or [event()]}
    doc.update(over)
    return doc


@pytest.fixture
def db(tmp_path):
    database = Database(str(tmp_path / 'bot.db'))
    for chat_id, uuid, status in ((CID, UUID, 'paid'), (CID2, UUID2, 'demo')):
        database._users.save(User(
            chat_id=chat_id, username=USERNAME if chat_id == CID else 'u2',
            status=status, uuid=uuid,
            email=EMAIL if chat_id == CID else f'u2_{chat_id}@nekovo.ru',
            lang='ru', platform='android', contact_email=CONTACT,
        ))
    return database


@pytest.fixture
def srv(db):
    return WebAppServer(make_config(), db, xui_service=Mock())


def set_status(db, chat_id, status):
    user = db._users.get_by_id(chat_id)
    user.status = status
    db._users.save(user)


def stored(db):
    """(the table's column names, its rows as dicts)."""
    netstat.ensure_table(db)
    conn = db._connect()
    try:
        names = [r[1] for r in conn.execute('PRAGMA table_info(netstat_events)')]
        rows = [dict(r) for r in conn.execute('SELECT * FROM netstat_events')]
    finally:
        conn.close()
    return names, rows


def rows_of(db):
    return stored(db)[1]


@asynccontextmanager
async def listening(server):
    """The server on the runner the bot serves it with (``make_runner`` once
    §65's redacting access log is in, else run_in_thread's bare AppRunner),
    on 127.0.0.1 and a free port; yields the port."""
    make = getattr(server, 'make_runner', None)
    runner = make() if make else web.AppRunner(server.app)
    await runner.setup()
    sock = socket.socket()
    sock.bind(('127.0.0.1', 0))
    site = web.SockSite(runner, sock)
    await site.start()
    try:
        yield sock.getsockname()[1]
    finally:
        await runner.cleanup()


@asynccontextmanager
async def serve(server):
    """``listening`` with aiohttp's client on it."""
    async with listening(server) as port:
        async with aiohttp.ClientSession(f'http://127.0.0.1:{port}') as session:
            yield session


def auth(tok):
    """The header the app sends the token in."""
    return {'Authorization': f'NekoToken {tok}'}


async def post(session, payload, tok=None, headers=None, method='POST'):
    """(status, text, headers) of one request to the endpoint, the token
    (when given) in ``Authorization: NekoToken``."""
    hdrs = dict(headers or {})
    if tok is not None:
        hdrs.update(auth(tok))
    if isinstance(payload, (bytes, str)) or payload is None:
        data = payload
    else:
        data = json.dumps(payload)
    async with session.request(method, PATH, data=data, headers=hdrs) as resp:
        return resp.status, await resp.text(), resp.headers


# ---------------------------------------------------------------- accepted --

class TestAccepted:

    @pytest.mark.asyncio
    async def test_a_valid_batch_is_stored(self, srv, db):
        sent = event()
        async with serve(srv) as s:
            status, text, _ = await post(s, body(sent), token())
        assert (status, text) == (204, '')
        [row] = rows_of(db)
        assert {k: v for k, v in row.items() if k != 'id'} == {
            'ts': sent['ts'], 'kind': 'drop', 'net': 'cell', 'mcc': '250',
            'mnc': '01', 'rat': 'lte', 'level': 2, 'ru': 1, 'ours': 0,
            'proto': '{"hy2":false,"stls":false}',
            'cell_tac': 7701, 'cell_cid': 123456789, 'cell_pci': 301,
            'cell_earfcn': 1850, 'area': 'ucfv0', 'app': '0.9.2-neko.6',
        }

    @pytest.mark.asyncio
    async def test_a_stage_one_event_and_nulls(self, srv, db):
        # no cell, no area, nothing optional; then every optional one null
        bare = {'ts': bucket() - 600, 'kind': 'ok', 'net': 'wifi', 'ours': True}
        nulls = dict(bare, mcc=None, mnc=None, rat=None, level=None, ru=None,
                     proto=None, cell=None, area=None)
        # an empty proto / cell is no result either
        empty = dict(bare, proto={}, cell={}, ru=False, level=0, rat='unknown')
        async with serve(srv) as s:
            assert (await post(s, body(bare, nulls, empty), token(UUID2)))[0] == 204
        rows = sorted(rows_of(db), key=lambda r: (r['rat'] or '', r['ru'] is None))
        for row in rows:
            assert (row['kind'], row['net'], row['ours']) == ('ok', 'wifi', 1)
            assert row['proto'] is None and row['area'] is None
            assert (row['cell_tac'], row['cell_cid'], row['cell_pci'],
                    row['cell_earfcn']) == (None, None, None, None)
        assert [(r['rat'], r['ru'], r['level']) for r in rows] == [
            (None, None, None), (None, None, None), ('unknown', 0, 0)]

    @pytest.mark.asyncio
    async def test_ts_is_floored_to_ten_minutes(self, srv, db):
        b = bucket() - 1200
        async with serve(srv) as s:
            status, _, _ = await post(s, body(event(ts=b), event(ts=b + 1),
                                              event(ts=b + 599), event(ts=b + 600)), token())
        assert status == 204
        assert sorted(r['ts'] for r in rows_of(db)) == [b, b, b, b + 600]

    def test_the_floor_and_the_window_without_a_server(self):
        now = 1795190999                       # 1795190400 + 599
        rows = netstat.validate(body(event(ts=now)), now)
        assert [r[0] for r in rows] == [1795190400]
        assert len(netstat.COLUMNS) == len(rows[0]) + 1     # all but the id

    @pytest.mark.asyncio
    async def test_events_outside_the_14_days_are_valid_but_not_kept(self, srv, db):
        now = bucket()
        events = [
            event(ts=now - 14 * DAY - 600),   # older than the retention
            event(ts=0),                      # a phone whose clock was never set
            # inside it (a whole 10 minutes inside: the server reads the
            # clock a moment later, maybe in the next 10 minutes)
            event(ts=now - 14 * DAY + 1200),
            event(ts=now),
            event(ts=now + 1800),             # a clock half an hour ahead
            event(ts=now + 2 * 3600),         # far ahead: not kept
        ]
        async with serve(srv) as s:
            assert (await post(s, body(*events), token()))[0] == 204
        assert sorted(r['ts'] for r in rows_of(db)) == [now - 14 * DAY + 1200, now, now + 1800]

    @pytest.mark.asyncio
    async def test_a_hundred_events_fit(self, srv, db):
        async with serve(srv) as s:
            assert (await post(s, body(*[event()] * 100), token()))[0] == 204
        assert len(rows_of(db)) == 100

    @pytest.mark.asyncio
    async def test_a_body_that_arrives_in_pieces_is_read_whole(self, srv, db):
        raw = json.dumps(body(*[event()] * 100)).encode()
        assert len(raw) > 8 * 1024

        async def pieces():
            for i in range(0, len(raw), 1024):
                yield raw[i:i + 1024]
                await asyncio.sleep(0.002)

        async with serve(srv) as s:
            # chunked: no Content-Length, the body in 1 KB pieces
            async with s.post(PATH, data=pieces(),
                              headers=auth(token())) as resp:
                assert resp.status == 204, await resp.text()
        assert len(rows_of(db)) == 100

    @pytest.mark.asyncio
    async def test_every_active_status(self, srv, db):
        async with serve(srv) as s:
            for status in ('demo', 'paid', 'support_topic'):
                set_status(db, CID, status)
                assert (await post(s, body(), token()))[0] == 204, status
        assert len(rows_of(db)) == 3

    @pytest.mark.asyncio
    async def test_ids_are_random_not_a_sequence(self, srv, db):
        # one request's rows must not be a run of ids tying them together
        async with serve(srv) as s:
            assert (await post(s, body(*[event()] * 50), token()))[0] == 204
        ids = sorted(r['id'] for r in rows_of(db))
        assert len(set(ids)) == 50
        assert max(b - a for a, b in zip(ids, ids[1:])) > 2 ** 40


# ----------------------------------------------------------------- refused --

class TestRefused:

    @pytest.mark.asyncio
    async def test_401_without_an_active_token(self, srv, db):
        async with serve(srv) as s:
            status, _, headers = await post(s, body())                      # no header
            assert (status, headers.get('WWW-Authenticate')) == (401, 'NekoToken')
            assert (await post(s, body(), ''))[0] == 401                    # empty
            assert (await post(s, body(), '0123456789abcdef' * 2))[0] == 401  # nobody's
            assert (await post(s, body(), token().upper()))[0] == 401       # not its shape
            assert (await post(s, body(), token()[:-1]))[0] == 401
            # the /sub token in the path is no token here
            async with s.post(f'{PATH}?token={token()}', data=json.dumps(body())) as resp:
                assert resp.status == 401
            for status in ('banned', 'rejected', 'new', 'pending_demo', 'platform_select'):
                set_status(db, CID, status)                  # a stale uuid
                assert (await post(s, body(), token()))[0] == 401, status
        assert rows_of(db) == []

    @pytest.mark.asyncio
    @pytest.mark.parametrize('value', [
        'NekoToken',                       # the scheme alone
        '{tok}',                           # the token alone
        'Bearer {tok}',                    # another scheme
        'Basic {tok}',
        'NekoAdmin {tok}',                 # the dashboard's
        'NekoTokens {tok}',
        'NekoToken: {tok}',
        'NekoToken={tok}',
        'NekoToken {tok} {tok}',           # more than the token
        'NekoToken {tok} x',
        'NekoToken {tok},NekoToken {tok}',
    ])
    async def test_401_for_any_other_authorization(self, srv, db, value):
        async with serve(srv) as s:
            hdrs = {'Authorization': value.format(tok=token())}
            async with s.post(PATH, data=json.dumps(body()), headers=hdrs) as resp:
                assert resp.status == 401
        assert rows_of(db) == []

    @pytest.mark.asyncio
    async def test_401_for_the_header_twice_or_the_old_one(self, srv, db):
        async with serve(srv) as s:
            async with s.post(PATH, data=json.dumps(body()), headers=[
                    ('Authorization', f'NekoToken {token()}'),
                    ('Authorization', f'NekoToken {token()}')]) as resp:
                assert resp.status == 401                                   # said twice
            # the contract's first draft: no longer read
            async with s.post(PATH, data=json.dumps(body()),
                              headers={'X-Neko-Token': token()}) as resp:
                assert resp.status == 401
            # nor in the way of the real one
            hdrs = {'X-Neko-Token': token(UUID2), **auth(token())}
            async with s.post(PATH, data=json.dumps(body()), headers=hdrs) as resp:
                assert resp.status == 204
        assert len(rows_of(db)) == 1

    @pytest.mark.asyncio
    @pytest.mark.parametrize('value', [
        'NekoToken {tok}', 'nekotoken {tok}', 'NEKOTOKEN {tok}',   # RFC 7235: any case
        'NekoToken   {tok}', '  NekoToken {tok}  ',
    ])
    async def test_the_scheme_in_any_case_and_spacing(self, srv, db, value):
        async with serve(srv) as s:
            hdrs = {'Authorization': value.format(tok=token())}
            async with s.post(PATH, data=json.dumps(body()), headers=hdrs) as resp:
                assert resp.status == 204
        assert len(rows_of(db)) == 1

    @pytest.mark.asyncio
    async def test_401_before_the_body_is_read(self, srv, db):
        # no token: a body over the limit is not even looked at
        async with serve(srv) as s:
            assert (await post(s, b'x' * (netstat.MAX_BODY + 1)))[0] == 401

    @pytest.mark.asyncio
    async def test_405_for_any_other_method(self, srv, db):
        async with serve(srv) as s:
            for method in ('GET', 'PUT', 'DELETE'):
                status, _, headers = await post(s, None, token(), method=method)
                assert (status, headers.get('Allow')) == (405, 'POST'), method
        assert rows_of(db) == []

    @pytest.mark.asyncio
    async def test_413_over_64_kb(self, srv, db):
        doc = json.dumps(body()).encode()
        exact = doc + b' ' * (64 * 1024 - len(doc))           # JSON allows the spaces

        async def chunked(raw):
            for i in range(0, len(raw), 4096):
                yield raw[i:i + 4096]

        async with serve(srv) as s:
            assert (await post(s, exact, token()))[0] == 204
            assert (await post(s, exact + b' ', token()))[0] == 413        # by its length
            async with s.post(PATH, data=chunked(exact + b' '),
                              headers=auth(token())) as resp:
                assert resp.status == 413                                  # by what arrives
            async with s.post(PATH, data=chunked(exact),
                              headers=auth(token())) as resp:
                assert resp.status == 204
        assert len(rows_of(db)) == 2

    @pytest.mark.asyncio
    async def test_413_by_the_length_alone(self, srv, db):
        # a body announced over the limit is refused before the rest of it
        # is sent
        announced, first = 70000, b'{"v": 1'
        async with listening(srv) as port:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(b'POST ' + PATH.encode() + b' HTTP/1.1\r\nHost: t\r\n'
                         b'Connection: close\r\n'
                         b'Authorization: NekoToken ' + token().encode() + b'\r\n'
                         b'Content-Length: ' + str(announced).encode() + b'\r\n\r\n' + first)
            await writer.drain()
            head = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 5)
            # the rest, or the server waits 10 s for it before it closes
            writer.write(b' ' * (announced - len(first)))
            await writer.drain()
            await asyncio.wait_for(reader.read(), 5)
            writer.close()
        assert head.startswith(b'HTTP/1.1 413 ')

    @pytest.mark.asyncio
    async def test_429_after_30_an_hour_per_token(self, srv, db):
        clock = [1000.0]
        srv._netstat_limiter.clock = lambda: clock[0]
        async with serve(srv) as s:
            for i in range(29):
                assert (await post(s, body(), token()))[0] == 204, i
            # a bad body counts too: the request was made
            assert (await post(s, b'{', token()))[0] == 400
            clock[0] += 600.5
            status, _, headers = await post(s, body(), token())
            assert status == 429
            assert headers['Retry-After'] == '3000'          # 2999.5 s, rounded up
            assert (await post(s, body(), token()))[0] == 429            # not counted...
            assert (await post(s, body(), token(UUID2)))[0] == 204       # ...nor shared
            clock[0] += 3000                   # the first 29 are an hour old now
            assert (await post(s, body(), token()))[0] == 204
        assert len(rows_of(db)) == 29 + 1 + 1

    def test_the_limiter_forgets_idle_keys(self):
        clock = [0.0]
        limiter = netstat.RateLimiter(limit=2, window=100, clock=lambda: clock[0])
        assert limiter.check('a') == 0 and limiter.check('a') == 0
        assert limiter.check('a') == 100
        clock[0] = 40
        assert limiter.check('a') == 60 and limiter.check('b') == 0
        clock[0] = 120                         # a: idle a whole window; b: not
        assert limiter.check('c') == 0
        assert set(limiter._hits) == {'b', 'c'}
        clock[0] = 139.5
        assert limiter.check('b') == 0                       # b: 40, 139.5
        assert limiter.check('b') == pytest.approx(0.5)      # until 40 is a window old
        clock[0] = 140                         # 40 is a window old: gone
        assert limiter.check('b') == 0                       # b: 139.5, 140
        assert limiter.check('b') == pytest.approx(99.5)


# ---------------------------------------------------------------- the body --

# (what is wrong, the body as sent, the field the answer names)
BAD = [
    ('not JSON', b'{"v": 1,', 'not JSON'),
    ('not UTF-8', b'{"v": 1, "app": "\xff"}', 'not UTF-8'),
    ('NaN', b'{"v": 1, "app": "0.9.2-neko.6", "events": [{"ts": NaN}]}', 'NaN'),
    ('a field twice', b'{"v": 1, "v": 1, "app": "0.9.2-neko.6", "events": []}', 'duplicate'),
    ('nested deep', b'[' * 30000 + b']' * 30000, 'not JSON'),
    ('a list', json.dumps([body()]), 'the body'),
    ('an extra field', body(extra=1), 'unknown field'),
    ('v 2', body(v=2), 'v: 1'),
    ('v true', body(v=True), 'v: 1'),
    ('v "1"', body(v='1'), 'v: 1'),
    ('v 1.0', body(v=1.0), 'v: 1'),
    ('no v', {'app': '0.9.2-neko.6', 'events': [event()]}, 'v: 1'),
    ('no app', {'v': 1, 'events': [event()]}, 'app'),
    ('app junk', body(app='0.9.2 neko'), 'app'),
    ('app a number', body(app=6), 'app'),
    ('app with a newline', body(app='0.9.2-neko.6\n'), 'app'),
    ('no events', {'v': 1, 'app': '0.9.2-neko.6'}, 'events'),
    ('events empty', body(events=[]), 'events'),
    ('events an object', body(events={'0': event()}), 'events'),
    ('101 events', body(events=[event()] * 101), 'events'),
    ('an event a list', body(events=[[1]]), 'events[0]'),
    ('an extra event field', body(events=[event(), event(token='x')]), 'events[1]: unknown field'),
    ('no ts', body(events=[event(ts=None)]), 'events[0].ts: required'),
    ('no kind', body(events=[event(kind=None)]), 'events[0].kind: required'),
    ('no net', body(events=[event(net=None)]), 'events[0].net: required'),
    ('no ours', body(events=[event(ours=None)]), 'events[0].ours: required'),
    ('ts a string', body(events=[event(ts='1795190400')]), 'events[0].ts'),
    ('ts true', body(events=[event(ts=True)]), 'events[0].ts'),
    ('ts a float', body(events=[event(ts=1795190400.0)]), 'events[0].ts'),
    ('ts negative', body(events=[event(ts=-600)]), 'events[0].ts'),
    ('ts in milliseconds', body(events=[event(ts=1795190400000)]), 'events[0].ts'),
    ('kind unknown', body(events=[event(kind='slow')]), 'events[0].kind'),
    ('kind upper case', body(events=[event(kind='DROP')]), 'events[0].kind'),
    ('kind null', body(events=[dict(event(), kind=None)]), 'events[0].kind'),
    ('kind a list', body(events=[event(kind=['drop'])]), 'events[0].kind'),   # unhashable
    ('net an object', body(events=[event(net={'wifi': True})]), 'events[0].net'),
    ('rat a list', body(events=[event(rat=['lte'])]), 'events[0].rat'),
    ('net unknown', body(events=[event(net='ethernet')]), 'events[0].net'),
    ('rat unknown', body(events=[event(rat='5g')]), 'events[0].rat'),
    ('rat upper case', body(events=[event(rat='LTE')]), 'events[0].rat'),
    ('rat a number', body(events=[event(rat=4)]), 'events[0].rat'),
    ('mcc a number', body(events=[event(mcc=250)]), 'events[0].mcc'),
    ('mcc two digits', body(events=[event(mcc='25')]), 'events[0].mcc'),
    ('mcc four digits', body(events=[event(mcc='2500')]), 'events[0].mcc'),
    ('mcc a letter', body(events=[event(mcc='25a')]), 'events[0].mcc'),
    ('mcc other digits', body(events=[event(mcc='٢٥٠')]), 'events[0].mcc'),
    ('mcc with a newline', body(events=[event(mcc='250\n')]), 'events[0].mcc'),
    ('mnc one digit', body(events=[event(mnc='1')]), 'events[0].mnc'),
    ('mnc four digits', body(events=[event(mnc='0001')]), 'events[0].mnc'),
    ('mnc a number', body(events=[event(mnc=1)]), 'events[0].mnc'),
    ('level 5', body(events=[event(level=5)]), 'events[0].level'),
    ('level -1', body(events=[event(level=-1)]), 'events[0].level'),
    ('level true', body(events=[event(level=True)]), 'events[0].level'),
    ('level 2.0', body(events=[event(level=2.0)]), 'events[0].level'),
    ('ru a string', body(events=[event(ru='yes')]), 'events[0].ru'),
    ('ru 1', body(events=[event(ru=1)]), 'events[0].ru'),
    ('ours null', body(events=[dict(event(), ours=None)]), 'events[0].ours'),
    ('ours 0', body(events=[event(ours=0)]), 'events[0].ours'),
    ('proto a list', body(events=[event(proto=['hy2'])]), 'events[0].proto'),
    ('proto upper case', body(events=[event(proto={'HY2': True})]), 'events[0].proto'),
    ('proto a string result', body(events=[event(proto={'hy2': 'ok'})]), 'events[0].proto'),
    ('proto a null result', body(events=[event(proto={'hy2': None})]), 'events[0].proto'),
    ('proto 17 protocols', body(events=[event(proto={f'p{i}': True for i in range(17)})]),
     'events[0].proto'),
    ('proto a 17-character name', body(events=[event(proto={'p' * 17: True})]),
     'events[0].proto'),
    ('cell a list', body(events=[event(cell=[7701])]), 'events[0].cell'),
    ('cell lac', body(events=[event(cell={'lac': 7701})]), 'events[0].cell: unknown field'),
    ('cell tac negative', body(events=[event(cell={'tac': -1})]), 'events[0].cell.tac'),
    ('cell tac MAX_VALUE', body(events=[event(cell={'tac': 2147483647})]), 'events[0].cell.tac'),
    ('cell tac true', body(events=[event(cell={'tac': True})]), 'events[0].cell.tac'),
    ('cell cid over 36 bits', body(events=[event(cell={'cid': 2 ** 36})]), 'events[0].cell.cid'),
    ('cell pci 1008', body(events=[event(cell={'pci': 1008})]), 'events[0].cell.pci'),
    ('cell earfcn too high', body(events=[event(cell={'earfcn': 3279166})]), 'events[0].cell.earfcn'),
    ('cell earfcn a string', body(events=[event(cell={'earfcn': '1850'})]), 'events[0].cell.earfcn'),
    ('area 4 characters', body(events=[event(area='ucfv')]), 'events[0].area'),
    ('area 6 characters', body(events=[event(area='ucfv0d')]), 'events[0].area'),
    ('area with an a', body(events=[event(area='ucfva')]), 'events[0].area'),
    ('area upper case', body(events=[event(area='UCFV0')]), 'events[0].area'),
    ('area a number', body(events=[event(area=12345)]), 'events[0].area'),
    ('one bad event of three', body(events=[event(), event(), event(net='lan')]),
     'events[2].net'),
]


def _raw(payload):
    if isinstance(payload, bytes):
        return payload
    if isinstance(payload, str):
        return payload.encode()
    return json.dumps(payload, ensure_ascii=False).encode()


class TestStrictBody:

    @pytest.mark.parametrize('what,payload,names', BAD, ids=[b[0] for b in BAD])
    def test_rejects(self, what, payload, names):
        with pytest.raises(netstat.BadBody) as caught:
            netstat.validate(netstat.parse(_raw(payload)), time.time())
        assert names in str(caught.value)

    @pytest.mark.parametrize('field,value', [
        ('rat', 'gsm'), ('rat', 'umts'), ('rat', 'nr'), ('net', 'other'),
        ('level', 4), ('mnc', '001'), ('mcc', '001'), ('area', '0b9zz'),
        ('cell', {'tac': 0, 'cid': 2 ** 36 - 1, 'pci': 1007, 'earfcn': 3279165}),
        ('proto', {'reality': True, 'hy2': False, 'ws': True, 'stls': False,
                   'ss-2022': True, 'p_16_chars_long_': True}),
    ])
    def test_accepts_the_edges(self, field, value):
        rows = netstat.validate(body(event(**{field: value})), time.time())
        assert len(rows) == 1

    @pytest.mark.asyncio
    async def test_400_names_the_field_and_stores_nothing(self, srv, db):
        # more bad bodies than one token may send an hour
        srv._netstat_limiter = netstat.RateLimiter(limit=len(BAD))
        async with serve(srv) as s:
            for what, payload, names in BAD:
                status, text, _ = await post(s, _raw(payload), token(UUID2))
                assert status == 400, what
                assert text.startswith('Bad body: ') and names in text, (what, text)
        assert rows_of(db) == []


# ------------------------------------------------------ nothing names you --

class TestNothingNamesTheUser:

    @pytest.mark.asyncio
    async def test_the_table_has_no_token_chat_id_or_address(self, srv, db):
        headers = {'X-Forwarded-For': FORWARDED, 'X-Real-IP': FORWARDED,
                   'Forwarded': f'for={FORWARDED}', 'User-Agent': 'NekoVPN/0.9.2-neko.6'}
        async with serve(srv) as s:
            assert (await post(s, body(*[event()] * 3), token(), headers))[0] == 204
        names, rows = stored(db)
        assert tuple(names) == netstat.COLUMNS
        assert not [n for n in names if any(
            word in n for word in ('token', 'chat', 'user', 'uuid', 'mail', 'ip', 'addr'))]
        assert len(rows) == 3
        secrets = (token(), CID, UUID, USERNAME, EMAIL, CONTACT, FORWARDED,
                   '127.0.0.1', 'NekoVPN/')
        for row in rows:
            for value in row.values():
                for secret in secrets:
                    assert secret not in str(value), (secret, row)

    @pytest.mark.asyncio
    async def test_the_limiter_keeps_no_token(self, srv, db):
        async with serve(srv) as s:
            assert (await post(s, body(), token()))[0] == 204
        keys = list(srv._netstat_limiter._hits)
        assert len(keys) == 1 and token() not in keys[0]
        assert keys[0] != token() and keys[0] == netstat.key_of(token())

    @pytest.fixture
    def records(self, caplog):
        """Every record any logger wrote, at any level."""
        caplog.set_level(logging.DEBUG)
        return caplog

    @staticmethod
    def _held(record):
        """All a record holds: its message and every attribute."""
        return '\n'.join([record.getMessage()] + [str(v) for v in vars(record).values()])

    @pytest.mark.asyncio
    async def test_no_log_line_has_the_token_or_the_address(self, srv, db, records, monkeypatch):
        tok, headers = token(), {'X-Forwarded-For': FORWARDED}
        clock = [0.0]
        srv._netstat_limiter.clock = lambda: clock[0]
        async with serve(srv) as s:
            assert (await post(s, body(), tok, headers))[0] == 204
            assert (await post(s, b'{"v":', tok, headers))[0] == 400
            assert (await post(s, b' ' * (netstat.MAX_BODY + 1), tok, headers))[0] == 413
            assert (await post(s, None, tok, headers, method='GET'))[0] == 405
            assert (await post(s, body(), tok[::-1], headers))[0] == 401
            # the token under another scheme, and in the old header
            assert (await post(s, body(), None, {**headers, 'Authorization': f'Bearer {tok}'}))[0] == 401
            assert (await post(s, body(), None, {**headers, 'X-Neko-Token': tok}))[0] == 401
            set_status(db, CID, 'banned')
            assert (await post(s, body(), tok, headers))[0] == 401
            set_status(db, CID, 'paid')
            for _ in range(27):
                await post(s, body(), tok, headers)
            assert (await post(s, body(), tok, headers))[0] == 429
            clock[0] += 3600
            # a failing lookup whose error would carry the token
            def lookup(self, db_, t):
                raise RuntimeError(f'lookup of {t} broke')
            monkeypatch.setattr(SubscriptionService, 'find_user_by_token', lookup)
            assert (await post(s, body(), tok, headers))[0] == 500
            monkeypatch.undo()
            # a failing store
            monkeypatch.setattr(netstat, 'store', Mock(side_effect=sqlite3.OperationalError(
                'database is locked')))
            assert (await post(s, body(), tok, headers))[0] == 500
            async with s.get('/health') as resp:          # an ordinary request
                assert resp.status == 200
        everything = [(r, self._held(r)) for r in records.records]
        access = [held for r, held in everything if r.name == 'aiohttp.access']
        # the access log works, and wrote the ordinary request alone
        assert len(access) == 1 and 'GET /health' in access[0]
        for record, held in everything:
            for secret in (tok, FORWARDED):
                assert secret not in held, (secret, record.name, record.getMessage())
            if record.name != 'aiohttp.access':
                assert '127.0.0.1' not in held, (record.name, record.getMessage())
            assert 'Error handling request' not in held
        failures = [r.getMessage() for r in records.records
                    if r.getMessage().startswith('netstat: request failed')]
        assert failures == [
            'netstat: request failed: RuntimeError: lookup of [token] broke',
            'netstat: request failed: OperationalError: database is locked',
        ]

    @pytest.mark.asyncio
    async def test_the_next_request_on_the_connection_is_logged(self, srv, db, records):
        # one connection, netstat then /health then netstat again: the
        # netstat requests' mark must not reach the request after them
        raw = json.dumps(body()).encode()
        netstat_request = (
            b'POST ' + PATH.encode() + b' HTTP/1.1\r\nHost: t\r\n'
            b'Authorization: NekoToken ' + token().encode() + b'\r\n'
            b'Content-Type: application/json\r\n'
            b'Content-Length: ' + str(len(raw)).encode() + b'\r\n\r\n' + raw)
        async with listening(srv) as port:
            reader, writer = await asyncio.open_connection('127.0.0.1', port)
            writer.write(netstat_request + b'GET /health HTTP/1.1\r\nHost: t\r\n\r\n'
                         + netstat_request.replace(b'Host: t', b'Host: t\r\nConnection: close'))
            await writer.drain()
            answer = await reader.read()
            writer.close()
        # (a status line follows the previous body with no line break)
        assert re.findall(rb'HTTP/1\.1 (\d{3}) ', answer) == [b'204', b'200', b'204']
        access = [r.getMessage() for r in records.records if r.name == 'aiohttp.access']
        assert len(access) == 1 and 'GET /health' in access[0]

    def test_installed_once(self, db):
        WebAppServer(make_config(), db, xui_service=Mock())
        WebAppServer(make_config(), db, xui_service=Mock())
        for name in ('aiohttp.access', 'aiohttp.server'):
            mine = [f for f in logging.getLogger(name).filters
                    if type(f).__name__ == 'DropUnlogged']
            assert len(mine) == 1, name

    def test_route(self, srv):
        routes = {(r.method, r.resource.canonical) for r in srv.app.router.routes()}
        assert ('*', PATH) in routes


# ----------------------------------------------------------------- cleanup --

# Far from the clock of the day these tests were written (2026-11-20): a
# cleanup that read the clock instead of ``now`` would drop other rows.
NOW = 1800000000
CUTOFF = NOW - 14 * DAY              # literal 14 on purpose: the rule itself


def _seed(db, *stamps):
    netstat.store(db, [(ts, 'ok', 'wifi', None, None, None, None, None, 1,
                        None, None, None, None, None, None, '0.9.2-neko.6')
                       for ts in stamps])


class TestCleanup:

    def test_drops_rows_older_than_14_days_and_keeps_the_rest(self, db):
        _seed(db, CUTOFF - 600, CUTOFF - 1, CUTOFF, CUTOFF + 600, NOW)
        assert netstat.cleanup(db, now=NOW) == 2
        assert sorted(r['ts'] for r in rows_of(db)) == [CUTOFF, CUTOFF + 600, NOW]
        assert netstat.cleanup(db, now=NOW) == 0

    def test_without_the_table(self, db):
        assert netstat.cleanup(db, now=NOW) == 0
        assert stored(db)[0] == list(netstat.COLUMNS)

    def test_the_job_runs_it_and_never_raises(self, db, caplog):
        svc = NotificationService(MagicMock(), db, Mock())
        _seed(db, CUTOFF - 1, NOW)
        caplog.set_level(logging.INFO)
        assert svc._cleanup_netstat_sync(now=NOW) == 1
        assert [r['ts'] for r in rows_of(db)] == [NOW]
        assert 'netstat cleanup: dropped 1 rows older than 14d, 1 kept' in caplog.text
        broken = NotificationService(MagicMock(), Mock(_connect=Mock(
            side_effect=sqlite3.OperationalError('disk I/O error'))), Mock())
        assert broken._cleanup_netstat_sync(now=NOW) == 0

    def test_the_job_uses_the_clock(self, db):
        _seed(db, int(time.time()) - 15 * DAY, int(time.time()))
        NotificationService(MagicMock(), db, Mock())._cleanup_netstat_sync()
        assert len(rows_of(db)) == 1


class _FakeScheduler:

    def __init__(self, *args, **kwargs):
        self.calls = []

    def add_job(self, func, trigger=None, **kwargs):
        self.calls.append((func, trigger, kwargs))

    def start(self):
        pass


class _FakeTrigger:

    def __init__(self, *args, **kwargs):
        self.args, self.kwargs = args, kwargs


class TestJob:

    def test_daily_and_first_soon_after_a_start(self, db):
        svc = NotificationService(MagicMock(), db, Mock())
        with patch('bot.services.notifications.SCHEDULER_AVAILABLE', True), \
             patch('bot.services.notifications.BackgroundScheduler',
                   _FakeScheduler, create=True), \
             patch('bot.services.notifications.IntervalTrigger',
                   _FakeTrigger, create=True), \
             patch('bot.services.notifications.CronTrigger',
                   _FakeTrigger, create=True), \
             patch('bot.services.alert_manager.AlertManager',
                   side_effect=RuntimeError('not under test')):
            started = datetime.now(timezone.utc)
            svc.start_scheduler()
        calls = [c for c in svc.scheduler.calls if c[2].get('id') == 'netstat_cleanup']
        assert len(calls) == 1
        func, trigger, kwargs = calls[0]
        assert func == svc._cleanup_netstat_sync
        assert trigger.kwargs == {'hours': 24}
        assert kwargs['replace_existing'] is True
        # not a whole day after each deploy: the rows must not outlive 14 days
        first = kwargs['next_run_time'] - started
        assert timedelta(minutes=14) < first < timedelta(minutes=16)
