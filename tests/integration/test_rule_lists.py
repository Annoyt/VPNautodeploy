"""FlClash rule lists — storage, the provider endpoint, the Clash profile
and the E9 auto rule, on a REAL sqlite bot.db (IMPROVEMENT_PLAN E1/E9).

Why
---
Hiddify drops a sing-box profile's rules; FlClash applies a Clash
profile as is but refreshes it only once a day — while it honours the
hourly ``interval`` of every rule-provider. So urgent routing (a site
blocked yesterday, a RU site that refuses the exit IP) lives in four
lists the profile points at and the bot serves at
``/lists/clash/<name>.yaml``. A provider mihomo cannot parse breaks the
whole profile, so everything that reaches the YAML is validated, and an
unreadable store answers 503 (the client keeps its cached copy) instead
of an empty list (which would wipe it for an hour).

The app_settings JSON (``rule_lists``, ``rule_list_queue``,
``rule_list_auto_threshold``) and the ``admin_actions`` rows ARE the
contract, so the assertions are on what is persisted.
"""

import json
import sqlite3
from datetime import datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.services import rule_lists as rl
from bot.services.subscription import SubscriptionService

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

NOW = datetime(2026, 10, 8, 12, 0, 0)
ADMIN = '1652899'


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


def stored(db) -> dict:
    return json.loads(db.get_setting(rl.SETTING_KEY))


def audit(db) -> list:
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT admin_id, action, target_id, details FROM admin_actions "
            "ORDER BY id").fetchall()]


def queue(db) -> dict:
    return rl.parse_queue(db.get_setting(rl.QUEUE_KEY))


def iso(hours_ago: float = 0, base: datetime = NOW) -> str:
    return (base - timedelta(hours=hours_ago)).replace(microsecond=0).isoformat()


# ---------- validation ----------

class TestValidation:

    @pytest.mark.parametrize('raw,expected', [
        ('example.com', 'example.com'),
        ('  Example.COM  ', 'example.com'),
        ('https://www.example.com/path?q=1#frag', 'example.com'),
        ('http://user@sub.example.com:8080/x', 'sub.example.com'),
        ('*.example.com', 'example.com'),
        ('+.example.com', 'example.com'),
        ('example.com.', 'example.com'),
        ('www.com', 'www.com'),                       # www IS the name here
        ('Президент.РФ', 'xn--d1abbgf6aiiy.xn--p1ai'),
        ('rutracker.org,', 'rutracker.org'),
        ('«youtube.com»', 'youtube.com'),
    ])
    def test_domain_normalised(self, raw, expected):
        assert rl.normalize_domain(raw) == expected

    @pytest.mark.parametrize('raw', [
        'localhost', 'com', '1.2.3.4', '10.0.0.1:80', 'exa mple.com', 'a_b.com',
        '-bad.com', 'bad-.com', 'a..b.com', '', '   ', 'x' * 64 + '.com',
        'т.е.', 'example.c', 'example.123', '[::1]', 'http://', None, 42,
        'ex"ample.com', "ex'ample.com", 'a.b.c.d.' + 'x' * 250,
    ])
    def test_domain_rejected(self, raw):
        assert rl.normalize_domain(raw) is None

    @pytest.mark.parametrize('raw,expected', [
        ('1.2.3.4', '1.2.3.4/32'),
        ('1.2.3.5/24', '1.2.3.0/24'),
        (' 95.161.64.0/20 ', '95.161.64.0/20'),
        ('10.0.0.0/8', '10.0.0.0/8'),
        ('2001:db8::1/32', '2001:db8::/32'),
        ('::1', '::1/128'),
    ])
    def test_cidr_normalised(self, raw, expected):
        assert rl.normalize_cidr(raw) == expected

    @pytest.mark.parametrize('raw', [
        '0.0.0.0/0', '1.0.0.0/7', '2001::/15', '::/0', 'example.com',
        '1.2.3.4/33', '300.1.1.1', '', None,
    ])
    def test_cidr_rejected(self, raw):
        assert rl.normalize_cidr(raw) is None

    def test_too_broad_net_says_why(self):
        value, err = rl.validate_entry('ru-direct-ip', '0.0.0.0/0')
        assert value is None and 'слишком широкая' in err

    def test_ip_in_a_domain_list_points_at_the_ip_list(self):
        value, err = rl.validate_entry('ru-direct', '1.2.3.4')
        assert value is None and 'ru-direct-ip' in err

    def test_domain_in_the_ip_list_is_rejected(self):
        assert rl.validate_entry('ru-direct-ip', 'example.com')[0] is None

    def test_unknown_list(self):
        assert rl.validate_entry('nope', 'example.com') == (None, 'нет такого списка: nope')

    def test_extract_from_free_text(self):
        text = 'не открывается https://rutracker.org/forum и youtube.com, т.е. всё; ещё a.b.c.org'
        assert rl.extract_domains(text) == ['rutracker.org', 'youtube.com', 'a.b.c.org']
        assert rl.extract_domains(text, limit=1) == ['rutracker.org']
        assert rl.extract_domains('привет, ничего не работает') == []


# ---------- storage ----------

class TestStorage:

    def test_missing_key_is_four_empty_lists(self, db):
        assert rl.load_lists(db) == {n: {} for n in rl.LIST_NAMES}
        assert rl.lists_health(db) is None

    @pytest.mark.parametrize('raw', ['{not json', '[1, 2]', '"text"', '42'])
    def test_broken_json_never_raises(self, db, raw):
        db.set_setting(rl.SETTING_KEY, raw)
        assert rl.load_lists(db) == {n: {} for n in rl.LIST_NAMES}
        assert rl.lists_health(db)
        db.set_setting(rl.QUEUE_KEY, raw)
        assert rl.load_queue(db) == {'next_id': 1, 'items': [], 'ignored': {}}

    def test_wrong_shapes_inside_are_dropped(self, db):
        db.set_setting(rl.SETTING_KEY, json.dumps({
            'blocked-recent': 5, 'ru-direct': 'x', 'always-proxy': None, 'other': ['a.com']}))
        assert rl.load_lists(db) == {n: {} for n in rl.LIST_NAMES}

    def test_hand_edited_strings_are_validated_on_read(self, db):
        db.set_setting(rl.SETTING_KEY, json.dumps({
            'blocked-recent': ['Example.COM', 'bad entry', '1.2.3.4', 'example.com'],
            'ru-direct-ip': ['1.2.3.4', '0.0.0.0/0'],
            'ru-direct': {'Ok.ru': 'not-a-dict'},
        }))
        lists = rl.load_lists(db)
        assert list(lists['blocked-recent']) == ['example.com']
        assert list(lists['ru-direct-ip']) == ['1.2.3.4/32']
        assert lists['ru-direct'] == {'ok.ru': {}}

    def test_add_normalises_persists_and_audits(self, db):
        ch = rl.add_entry(db, 'blocked-recent', 'https://www.Example.com/x',
                          actor=ADMIN, note='/list add', now=NOW)
        assert (ch.status, ch.entry, ch.changed) == ('added', 'example.com', True)
        assert stored(db)['blocked-recent'] == {
            'example.com': {'ts': '2026-10-08T12:00:00', 'by': ADMIN, 'note': '/list add'}}
        assert audit(db) == [(ADMIN, 'rule_list_add', 'blocked-recent:example.com', '/list add')]

    def test_exact_duplicate_is_a_no_op(self, db):
        rl.add_entry(db, 'blocked-recent', 'example.com', actor=ADMIN)
        ch = rl.add_entry(db, 'blocked-recent', 'EXAMPLE.com.', actor=ADMIN)
        assert ch.status == 'exists' and not ch.changed
        assert len(audit(db)) == 1

    def test_subdomain_is_covered_by_parent(self, db):
        rl.add_entry(db, 'always-proxy', 'example.com', actor=ADMIN)
        ch = rl.add_entry(db, 'always-proxy', 'cdn.example.com', actor=ADMIN)
        assert (ch.status, ch.detail) == ('covered', 'example.com')
        assert list(stored(db)['always-proxy']) == ['example.com']
        # the parent added after a child is NOT covered by it
        rl.add_entry(db, 'ru-direct', 'a.example.org', actor=ADMIN)
        assert rl.add_entry(db, 'ru-direct', 'example.org', actor=ADMIN).status == 'added'

    def test_cidr_covered_by_supernet(self, db):
        rl.add_entry(db, 'ru-direct-ip', '95.161.64.0/20', actor=ADMIN)
        ch = rl.add_entry(db, 'ru-direct-ip', '95.161.65.7', actor=ADMIN)
        assert (ch.status, ch.detail) == ('covered', '95.161.64.0/20')
        assert rl.add_entry(db, 'ru-direct-ip', '2001:db8::/32', actor=ADMIN).status == 'added'

    def test_size_limit(self, db, monkeypatch):
        monkeypatch.setattr(rl, 'MAX_ENTRIES', 2)
        for d in ('a.com', 'b.com'):
            assert rl.add_entry(db, 'ru-direct', d, actor=ADMIN).status == 'added'
        assert rl.add_entry(db, 'ru-direct', 'c.com', actor=ADMIN).status == 'full'
        assert list(stored(db)['ru-direct']) == ['a.com', 'b.com']

    def test_invalid_and_unknown_write_nothing(self, db):
        assert rl.add_entry(db, 'ru-direct-ip', '0.0.0.0/0', actor=ADMIN).status == 'invalid'
        assert rl.add_entry(db, 'blocked-recent', 'no spaces.com', actor=ADMIN).status == 'invalid'
        assert rl.add_entry(db, 'nope', 'a.com', actor=ADMIN).status == 'unknown_list'
        assert db.get_setting(rl.SETTING_KEY) is None and audit(db) == []

    def test_remove(self, db):
        rl.add_entry(db, 'ru-direct', 'example.com', actor=ADMIN)
        ch = rl.remove_entry(db, 'ru-direct', 'https://example.com/', actor=ADMIN, note='/list rm')
        assert (ch.status, ch.entry) == ('removed', 'example.com')
        assert stored(db)['ru-direct'] == {}
        assert audit(db)[-1] == (ADMIN, 'rule_list_rm', 'ru-direct:example.com', '/list rm')
        assert rl.remove_entry(db, 'ru-direct', 'example.com', actor=ADMIN).status == 'absent'
        assert len(audit(db)) == 2

    def test_remove_names_the_covering_parent(self, db):
        rl.add_entry(db, 'blocked-recent', 'example.com', actor=ADMIN)
        ch = rl.remove_entry(db, 'blocked-recent', 'a.example.com', actor=ADMIN)
        assert (ch.status, ch.detail) == ('absent', 'example.com')

    def test_removing_from_a_vpn_list_holds_auto_off(self, db):
        rl.add_entry(db, 'blocked-recent', 'example.com', actor=ADMIN, now=NOW)
        rl.remove_entry(db, 'blocked-recent', 'example.com', actor=ADMIN, now=NOW)
        assert queue(db)['ignored'] == {'example.com': {'ts': iso(0), 'by': ADMIN}}
        # a DIRECT list removal says nothing about the VPN route
        rl.add_entry(db, 'ru-direct', 'other.com', actor=ADMIN, now=NOW)
        rl.remove_entry(db, 'ru-direct', 'other.com', actor=ADMIN, now=NOW)
        assert 'other.com' not in queue(db)['ignored']

    def test_conflicting_lists_warn(self, db):
        rl.add_entry(db, 'blocked-recent', 'example.com', actor=ADMIN)
        ch = rl.add_entry(db, 'ru-direct', 'a.example.com', actor=ADMIN)
        assert ch.status == 'added' and 'blocked-recent (example.com)' in ch.warning

    def test_broken_json_is_backed_up_before_a_write(self, db):
        db.set_setting(rl.SETTING_KEY, '{oops')
        ch = rl.add_entry(db, 'ru-direct', 'example.com', actor=ADMIN)
        assert ch.status == 'added' and rl.BACKUP_KEY in ch.warning
        assert db.get_setting(rl.BACKUP_KEY) == '{oops'
        assert list(stored(db)['ru-direct']) == ['example.com']

    def test_unreadable_db_never_clobbers_the_lists(self, db, monkeypatch):
        """Only the READ fails (a locked SELECT) while writes would go
        through: Database.get_setting folds that into "no row", so a
        tolerant read-modify-write would replace every list with the
        one new entry."""
        rl.add_entry(db, 'blocked-recent', 'keep.com', actor=ADMIN)
        rl.record_complaint(db, 'queued.com', '9')
        before = (db.get_setting(rl.SETTING_KEY), db.get_setting(rl.QUEUE_KEY))
        real_connect = db._connect

        class SelectLocked:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, *args):
                if sql.lstrip().upper().startswith('SELECT VALUE FROM APP_SETTINGS'):
                    raise sqlite3.OperationalError('database is locked')
                return self._conn.execute(sql, *args)

            def __enter__(self):
                self._conn.__enter__()
                return self

            def __exit__(self, *exc):
                return self._conn.__exit__(*exc)

            def __getattr__(self, name):
                return getattr(self._conn, name)

        monkeypatch.setattr(db, '_connect', lambda: SelectLocked(real_connect()))
        assert rl.add_entry(db, 'blocked-recent', 'new.com', actor=ADMIN).status == 'error'
        assert rl.remove_entry(db, 'blocked-recent', 'keep.com', actor=ADMIN).status == 'error'
        assert rl.serve_provider(db, 'blocked-recent')[0] == 503
        assert rl.record_complaint(db, 'x.com', '1')[0] == 'error'
        assert rl.ignore_domain(db, 'queued.com', actor=ADMIN) == 0
        monkeypatch.setattr(db, '_connect', real_connect)
        assert (db.get_setting(rl.SETTING_KEY), db.get_setting(rl.QUEUE_KEY)) == before
        assert [r[1] for r in audit(db)] == ['rule_list_add']

    def test_connection_failure_is_an_error_too(self, db, monkeypatch):
        def refused():
            raise sqlite3.OperationalError('unable to open database file')
        monkeypatch.setattr(db, '_connect', refused)
        assert rl.add_entry(db, 'blocked-recent', 'new.com', actor=ADMIN).status == 'error'
        assert rl.serve_provider(db, 'blocked-recent')[0] == 503
        assert rl.load_lists(db) == {n: {} for n in rl.LIST_NAMES}


# ---------- GET /lists/clash/<name>.yaml ----------

class TestProviderEndpoint:

    @pytest.fixture
    def server(self, db):
        cfg = Mock()
        cfg.BOT_TOKEN = 'test_token'
        return WebAppServer(cfg, db, xui_service=Mock())

    async def _get(self, server, path):
        async with TestClient(TestServer(server.app)) as client:
            resp = await client.get(path)
            return resp.status, resp.headers, await resp.text()

    @pytest.mark.asyncio
    async def test_domain_list(self, server, db):
        rl.add_entry(db, 'blocked-recent', 'rutracker.org', actor=ADMIN)
        rl.add_entry(db, 'blocked-recent', 'президент.рф', actor=ADMIN)
        status, headers, body = await self._get(server, '/lists/clash/blocked-recent.yaml')
        assert status == 200
        assert headers['Content-Type'].startswith('text/yaml')
        assert headers['Cache-Control'] == 'max-age=300'
        assert body == ("# NekoVPN rule list: blocked-recent (2)\npayload:\n"
                        "  - '+.rutracker.org'\n  - '+.xn--d1abbgf6aiiy.xn--p1ai'\n")

    @pytest.mark.asyncio
    async def test_ipcidr_list_has_no_domain_prefix(self, server, db):
        rl.add_entry(db, 'ru-direct-ip', '95.161.64.0/20', actor=ADMIN)
        rl.add_entry(db, 'ru-direct-ip', '2001:db8::/32', actor=ADMIN)
        status, _, body = await self._get(server, '/lists/clash/ru-direct-ip.yaml')
        assert status == 200
        assert body.endswith("payload:\n  - '95.161.64.0/20'\n  - '2001:db8::/32'\n")

    @pytest.mark.asyncio
    @pytest.mark.parametrize('name', rl.LIST_NAMES)
    async def test_every_list_is_served_empty_as_valid_yaml(self, server, name):
        status, headers, body = await self._get(server, f'/lists/clash/{name}.yaml')
        assert status == 200 and headers['Cache-Control'] == 'max-age=300'
        assert body == f"# NekoVPN rule list: {name} (0)\npayload: []\n"

    @pytest.mark.asyncio
    @pytest.mark.parametrize('path', [
        '/lists/clash/nope.yaml', '/lists/clash/blocked-recent.yml',
        '/lists/clash/..%2Fbot.yaml', '/lists/clash/a.b.yaml', '/lists/clash/.yaml',
    ])
    async def test_unknown_name_is_404(self, server, path):
        status, headers, _ = await self._get(server, path)
        assert status == 404 and 'Cache-Control' not in headers

    @pytest.mark.asyncio
    async def test_unreadable_store_is_503_not_an_empty_list(self, server, db):
        db.set_setting(rl.SETTING_KEY, '{oops')
        status, headers, body = await self._get(server, '/lists/clash/ru-direct.yaml')
        assert status == 503 and 'payload' not in body

    def test_yaml_parses(self, db):
        yaml = pytest.importorskip('yaml')
        rl.add_entry(db, 'always-proxy', 'example.com', actor=ADMIN)
        assert yaml.safe_load(rl.serve_provider(db, 'always-proxy')[1]) == {
            'payload': ['+.example.com']}
        assert yaml.safe_load(rl.serve_provider(db, 'ru-direct')[1]) == {'payload': []}

    def test_no_quote_can_reach_the_yaml(self):
        body = rl.provider_yaml('blocked-recent', ["ev'il.com", 'ok.com', 'a b.com'])
        assert body == "# NekoVPN rule list: blocked-recent (1)\npayload:\n  - '+.ok.com'\n"


# ---------- the Clash profile ----------

UUID = '8c2f6a3e-1d4b-4c5e-9f7a-2b3c4d5e6f70'
ALL = ('reality', 'hy2', 'ws', 'stls')


def _config(**over):
    cfg = dict(
        BOT_TOKEN='test_token', WEBAPP_URL='https://dash.example.com',
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
        SS_USER_SALT='salt',
    )
    cfg.update(over)
    return SimpleNamespace(**cfg)


def _clash(config=None, protocols=ALL, **kw) -> dict:
    user = SimpleNamespace(uuid=UUID, email='u1@x', status='demo', lang='ru', chat_id='1')
    return json.loads(SubscriptionService(config or _config()).build_clash_config(
        user, protocols, **kw))


class TestClashProfile:

    def test_providers_point_at_the_bot(self):
        providers = _clash()['rule-providers']
        assert providers == {
            name: {'type': 'http', 'behavior': spec.behavior, 'format': 'yaml',
                   'url': f'https://dash.example.com/lists/clash/{name}.yaml',
                   'path': f'./lists/{name}.yaml', 'interval': 3600}
            for name, spec in rl.LISTS.items()
        }
        assert providers['ru-direct-ip']['behavior'] == 'ipcidr'

    def test_trailing_slash_in_webapp_url(self):
        url = _clash(_config(WEBAPP_URL='https://dash.example.com/'))[
            'rule-providers']['ru-direct']['url']
        assert url == 'https://dash.example.com/lists/clash/ru-direct.yaml'

    def test_rule_order(self):
        rules = _clash()['rules']
        at = rules.index
        quic_lists = at('AND,((NETWORK,UDP),(DST-PORT,443),(RULE-SET,ru-direct)),DIRECT')
        first_geo_vpn = at('GEOSITE,youtube,VPN')
        last_geo_vpn = at('GEOSITE,google,VPN')
        assert quic_lists < at('NETWORK,UDP,Calls')
        assert (at('DOMAIN-SUFFIX,max.ru,DIRECT') < at('RULE-SET,always-proxy,VPN')
                < at('RULE-SET,blocked-recent,VPN') < first_geo_vpn)
        assert (last_geo_vpn < at('RULE-SET,ru-direct,DIRECT')
                < at('RULE-SET,ru-direct-ip,DIRECT,no-resolve')
                < at('GEOSITE,category-ru,DIRECT'))
        assert rules[-1] == 'MATCH,VPN'

    def test_every_rule_set_has_its_provider(self):
        cfg = _clash()
        used = {r.split('RULE-SET,', 1)[1].split(')')[0].split(',')[0]
                for r in cfg['rules'] if 'RULE-SET,' in r}
        assert used == set(cfg['rule-providers'])

    @pytest.mark.parametrize('url', ['', None])
    def test_no_webapp_url_means_no_lists(self, url):
        cfg = _clash(_config(WEBAPP_URL=url))
        assert 'rule-providers' not in cfg
        assert not [r for r in cfg['rules'] if 'RULE-SET' in r]
        assert cfg['rules'] == SubscriptionService(_config())._clash_rules('Calls')

    def test_lists_ride_along_in_lockdown_and_without_calls(self):
        assert 'rule-providers' in _clash(lockdown=True)
        rules = _clash(protocols=('ws', 'stls'))['rules']
        assert 'NETWORK,UDP,VPN' in rules and 'RULE-SET,blocked-recent,VPN' in rules

    def test_singbox_profile_does_not_point_at_the_lists(self):
        user = SimpleNamespace(uuid=UUID, email='u1@x', status='paid', lang='ru', chat_id='1')
        txt = json.dumps(SubscriptionService(_config()).build_singbox_config(user, ALL))
        assert 'lists/clash' not in txt and 'blocked-recent' not in txt


# ---------- E9: the decision (pure) ----------

def item(domain, chat_id, hours_ago=1.0, status='pending', item_id=1):
    return {'id': item_id, 'domain': domain, 'chat_id': chat_id,
            'ts': iso(hours_ago), 'status': status}


def decide(items, lists=None, ignored=None, threshold=2, only=None):
    lists = lists or {n: {} for n in rl.LIST_NAMES}
    return rl.decide_auto_adds(items, lists, ignored or {}, threshold=threshold,
                               now=NOW, only=only)


class TestAutoDecision:

    def test_two_distinct_users_in_a_day(self):
        out = decide([item('a.com', '1'), item('a.com', '2', 20)])
        assert [(a.domain, a.chat_ids) for a in out] == [('a.com', ('1', '2'))]
        assert '2 жалобы от разных юзеров за 24 ч (порог 2)' == out[0].evidence

    def test_one_user_is_not_enough(self):
        assert decide([item('a.com', '1')]) == []

    def test_the_same_user_twice_counts_once(self):
        assert decide([item('a.com', '1'), item('a.com', '1', 2)]) == []

    def test_window_is_24h(self):
        assert decide([item('a.com', '1'), item('a.com', '2', 25)]) == []
        assert decide([item('a.com', '1'), item('a.com', '2', 23.9)])

    def test_only_pending_complaints_count(self):
        assert decide([item('a.com', '1'), item('a.com', '2', status='ignored')]) == []
        assert decide([item('a.com', '1'), item('a.com', '2', status='added')]) == []

    def test_threshold(self):
        items = [item('a.com', '1'), item('a.com', '2')]
        assert decide(items, threshold=3) == []
        assert decide(items + [item('a.com', '3')], threshold=3)
        assert decide(items, threshold=1)
        assert decide(items, threshold=0) == []         # off

    def test_only_restricts_the_look(self):
        items = [item('a.com', '1'), item('a.com', '2'), item('b.com', '1'), item('b.com', '2')]
        assert [a.domain for a in decide(items)] == ['a.com', 'b.com']
        assert [a.domain for a in decide(items, only={'b.com'})] == ['b.com']

    @pytest.mark.parametrize('lists,why', [
        ({'blocked-recent': {'a.com': {}}}, 'уже в blocked-recent'),
        ({'blocked-recent': {'com.a.com': {}}}, None),                  # not a parent
        ({'always-proxy': {'a.com': {}}}, 'уже в always-proxy'),
        ({'ru-direct': {'a.com': {}}}, 'в ru-direct'),
    ])
    def test_guard_lists(self, lists, why):
        full = {n: {} for n in rl.LIST_NAMES}
        full.update(lists)
        items = [item('x.a.com', '1'), item('x.a.com', '2')]
        out = decide(items, lists=full)
        if why is None:
            assert out
        else:
            assert out == [] and why in rl.auto_guard('x.a.com', full, {}, NOW)

    def test_guard_admin_ignore_holds_30_days(self):
        items = [item('a.com', '1'), item('a.com', '2')]
        assert decide(items, ignored={'a.com': {'ts': iso(24 * 29), 'by': ADMIN}}) == []
        assert decide(items, ignored={'a.com': {'ts': iso(24 * 31), 'by': ADMIN}})

    @pytest.mark.parametrize('domain', ['site.ru', 'gosuslugi.ru', 'x.su', 'xn--d1abbgf6aiiy.xn--p1ai'])
    def test_guard_ru_zone(self, domain):
        assert decide([item(domain, '1'), item(domain, '2')]) == []

    @pytest.mark.parametrize('raw,expected', [
        (None, 2), ('3', 3), (' 5 ', 5), ('0', 0), ('off', 0), ('OFF', 0),
        ('abc', 2), ('-1', 2), ('', 2),
    ])
    def test_threshold_setting(self, raw, expected):
        assert rl.parse_threshold(raw) == expected


# ---------- E9 on sqlite: queue → list → audit ----------

class TestAutoOnSqlite:

    def test_two_users_add_audit_and_settle(self, db):
        assert rl.record_complaint(db, 'https://rutracker.org/x', '111', now=NOW)[0] == 'queued'
        assert rl.run_auto(db, only={'rutracker.org'}, now=NOW) == []
        assert rl.record_complaint(db, 'rutracker.org', '222', now=NOW)[0] == 'queued'
        added = rl.run_auto(db, only={'rutracker.org'}, now=NOW)
        assert [a.domain for a in added] == ['rutracker.org']
        meta = stored(db)['blocked-recent']['rutracker.org']
        assert meta['by'] == 'rule_lists' and '2 жалобы' in meta['note']
        row = audit(db)[-1]
        assert row[:3] == ('rule_lists', 'auto_add', 'blocked-recent:rutracker.org')
        assert '2 жалобы' in row[3] and '111, 222' in row[3]
        assert {it['status'] for it in queue(db)['items']} == {'added'}
        assert "'+.rutracker.org'" in rl.serve_provider(db, 'blocked-recent')[1]

    def test_threshold_from_app_settings(self, db):
        db.set_setting(rl.THRESHOLD_KEY, '3')
        for cid in ('1', '2'):
            rl.record_complaint(db, 'a.com', cid, now=NOW)
        assert rl.run_auto(db, now=NOW) == []
        rl.record_complaint(db, 'a.com', '3', now=NOW)
        assert [a.domain for a in rl.run_auto(db, now=NOW)] == ['a.com']

    def test_off_switch(self, db):
        db.set_setting(rl.THRESHOLD_KEY, 'off')
        for cid in ('1', '2', '3'):
            rl.record_complaint(db, 'a.com', cid, now=NOW)
        assert rl.run_auto(db, now=NOW) == []
        assert 'blocked-recent' not in (db.get_setting(rl.SETTING_KEY) or '')

    def test_admin_removal_is_not_undone_by_new_complaints(self, db):
        for cid in ('1', '2'):
            rl.record_complaint(db, 'a.com', cid, now=NOW)
        assert rl.run_auto(db, now=NOW)
        rl.remove_entry(db, 'blocked-recent', 'a.com', actor=ADMIN, now=NOW)
        for cid in ('3', '4'):
            rl.record_complaint(db, 'a.com', cid, now=NOW)
        assert rl.run_auto(db, now=NOW) == []
        assert stored(db)['blocked-recent'] == {}

    def test_operator_ru_direct_wins(self, db):
        rl.add_entry(db, 'ru-direct', 'a.com', actor=ADMIN)
        for cid in ('1', '2'):
            rl.record_complaint(db, 'a.com', cid, now=NOW)
        assert rl.run_auto(db, now=NOW) == []

    def test_unreadable_lists_decide_nothing(self, db):
        for cid in ('1', '2'):
            rl.record_complaint(db, 'a.com', cid, now=NOW)
        db.set_setting(rl.SETTING_KEY, '{oops')
        assert rl.run_auto(db, now=NOW) == []
        assert db.get_setting(rl.SETTING_KEY) == '{oops'


class TestQueue:

    def test_item_shape(self, db):
        status, it = rl.record_complaint(db, 'WWW.Example.com', 111, now=NOW)
        assert status == 'queued'
        assert it == {'id': 1, 'domain': 'example.com', 'chat_id': '111',
                      'ts': '2026-10-08T12:00:00', 'status': 'pending'}
        assert queue(db)['items'] == [it] and queue(db)['next_id'] == 2

    def test_duplicate_from_the_same_user(self, db):
        rl.record_complaint(db, 'a.com', '1', now=NOW)
        status, it = rl.record_complaint(db, 'a.com', '1', now=NOW)
        assert status == 'duplicate' and it['id'] == 1
        assert len(queue(db)['items']) == 1

    def test_daily_limit_per_user(self, db):
        for i in range(rl.USER_DAILY_LIMIT):
            assert rl.record_complaint(db, f's{i}.com', '1', now=NOW)[0] == 'queued'
        assert rl.record_complaint(db, 'more.com', '1', now=NOW) == ('limit', None)
        assert rl.record_complaint(db, 'more.com', '2', now=NOW)[0] == 'queued'
        later = NOW + timedelta(hours=25)
        assert rl.record_complaint(db, 'more.com', '1', now=later)[0] == 'queued'

    def test_retention_and_cap(self, db, monkeypatch):
        rl.record_complaint(db, 'old.com', '1', now=NOW - timedelta(days=8))
        rl.record_complaint(db, 'new.com', '2', now=NOW)
        assert [it['domain'] for it in queue(db)['items']] == ['new.com']
        monkeypatch.setattr(rl, 'QUEUE_MAX_ITEMS', 2)
        rl.record_complaint(db, 'b.com', '3', now=NOW)
        rl.record_complaint(db, 'c.com', '4', now=NOW)
        assert [it['domain'] for it in queue(db)['items']] == ['b.com', 'c.com']
        assert queue(db)['next_id'] == 5           # ids are never reused

    def test_ignore_settles_and_audits(self, db):
        rl.record_complaint(db, 'a.com', '1', now=NOW)
        assert rl.ignore_domain(db, 'a.com', actor=ADMIN, now=NOW, note='жалоба #1') == 1
        q = queue(db)
        assert q['items'][0]['status'] == 'ignored'
        assert q['ignored'] == {'a.com': {'ts': iso(0), 'by': ADMIN}}
        assert audit(db) == [(ADMIN, 'rule_list_ignore', 'blocked-recent:a.com', 'жалоба #1')]

    def test_manual_add_lifts_the_ignore_and_settles(self, db):
        rl.record_complaint(db, 'a.com', '1', now=NOW)
        rl.ignore_domain(db, 'a.com', actor=ADMIN, now=NOW)
        rl.record_complaint(db, 'a.com', '2', now=NOW)
        rl.add_entry(db, 'blocked-recent', 'a.com', actor=ADMIN, now=NOW)
        q = queue(db)
        assert q['ignored'] == {}
        assert [it['status'] for it in q['items']] == ['ignored', 'added']

    def test_summary_groups_by_domain(self, db):
        rl.record_complaint(db, 'a.com', '1', now=NOW - timedelta(hours=30))
        rl.record_complaint(db, 'a.com', '2', now=NOW)
        rl.record_complaint(db, 'b.com', '1', now=NOW)
        groups = rl.pending_summary(queue(db), NOW)
        assert [(g['domain'], len(g['users']), len(g['users_window'])) for g in groups] == [
            ('a.com', 2, 1), ('b.com', 1, 1)]
