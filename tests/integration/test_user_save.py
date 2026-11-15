"""users.save() never deletes another user's row (2026-11-15).

It was an INSERT OR REPLACE. users.uuid and users.email are UNIQUE, so a
save that carried a uuid or panel email another row held deleted that
row, and every save reset the columns it does not write to their
defaults. Now it updates the user's own row in place, or inserts it, and
refuses a clash.

Real sqlite: Database() builds the schema the bot runs on. An older
table is built by hand where a case needs one (prod's users table is
older than the repo's schema and has columns the repo does not create).
"""
import asyncio
import logging
import sqlite3
import threading

import pytest

from bot.core.database import Database
from bot.core.repositories.async_adapter import AsyncUserRepository
from bot.core.repositories.user import SAVE_COLUMNS, UserRepository
from bot.models import User

pytestmark = pytest.mark.filterwarnings(
    "ignore:Database\\..*is deprecated:DeprecationWarning"
)

UUID_A = '11111111-1111-4111-8111-111111111111'
UUID_B = '22222222-2222-4222-8222-222222222222'
EMAIL_A = 'user_alice_100@nekovo.ru'
EMAIL_B = 'user_bob_200@nekovo.ru'


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def repo(db):
    return UserRepository(db.db_path)


def alice(**kw) -> User:
    fields = dict(
        chat_id='100', username='alice', uuid=UUID_A, email=EMAIL_A,
        status='paid', platform='android',
        subscription_expiry='2026-12-31T00:00:00', limit_ip=5, quota_gb=100.0,
        last_asn='AS31133', contact_email='alice@example.com',
    )
    fields.update(kw)
    return User(**fields)


def bob(**kw) -> User:
    fields = dict(chat_id='200', username='bob', uuid=UUID_B, email=EMAIL_B,
                  status='demo', platform='ios')
    fields.update(kw)
    return User(**fields)


def rows(path) -> dict:
    """Every users row as stored, by chat_id."""
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        return {r['chat_id']: dict(r) for r in conn.execute('SELECT * FROM users')}
    finally:
        conn.close()


def sql(path, statement, params=()):
    conn = sqlite3.connect(path)
    try:
        out = conn.execute(statement, params).fetchall()
        conn.commit()
        return out
    finally:
        conn.close()


class TestNoOtherRowIsTouched:
    def test_the_schema_makes_both_keys_unique(self, db):
        """Without this the clash tests below would prove nothing."""
        for column, value in (('uuid', UUID_A), ('email', EMAIL_A)):
            sql(db.db_path, 'DELETE FROM users')
            sql(db.db_path, f'INSERT INTO users (chat_id, {column}) VALUES (?, ?)',
                ('1', value))
            with pytest.raises(sqlite3.IntegrityError, match=f'users.{column}'):
                sql(db.db_path, f'INSERT INTO users (chat_id, {column}) VALUES (?, ?)',
                    ('2', value))

    @pytest.mark.parametrize('clash', [{'uuid': UUID_A}, {'email': EMAIL_A},
                                       {'uuid': UUID_A, 'email': EMAIL_A}])
    def test_a_new_user_with_a_held_key_is_refused(self, repo, db, clash):
        assert repo.save(alice())
        before = rows(db.db_path)

        assert repo.save(bob(**clash)) is False

        assert rows(db.db_path) == before

    @pytest.mark.parametrize('clash', [{'uuid': UUID_A}, {'email': EMAIL_A}])
    def test_an_existing_user_taking_a_held_key_is_refused_whole(self, repo, db, clash):
        assert repo.save(alice())
        assert repo.save(bob())
        before = rows(db.db_path)

        user = repo.get_by_id('200')
        user.username = 'bobby'
        user.status = 'paid'
        for column, value in clash.items():
            setattr(user, column, value)
        assert repo.save(user) is False

        assert rows(db.db_path) == before

    def test_the_old_statement_deleted_the_holder(self, repo, db):
        """The class of loss this closes, reproduced with the statement
        save() used to run."""
        assert repo.save(alice())
        sql(db.db_path, 'INSERT OR REPLACE INTO users (chat_id, uuid) VALUES (?, ?)',
            ('200', UUID_A))
        assert set(rows(db.db_path)) == {'200'}

    def test_a_users_own_keys_are_no_clash(self, repo, db):
        assert repo.save(alice())
        user = repo.get_by_id('100')
        user.status = 'demo'

        assert repo.save(user)

        assert rows(db.db_path)['100']['status'] == 'demo'
        assert rows(db.db_path)['100']['uuid'] == UUID_A

    def test_the_log_names_the_holder_never_the_key(self, repo, caplog):
        assert repo.save(alice())
        with caplog.at_level(logging.ERROR, logger='bot.core.repositories.user'):
            assert repo.save(bob(uuid=UUID_A, email=EMAIL_A)) is False

        refusal = [r.getMessage() for r in caplog.records
                   if r.name == 'bot.core.repositories.user']
        assert refusal == [
            'Refused to save user 200: its uuid belongs to user 100; '
            'its email belongs to user 100. Nothing was written.'
        ]
        logged = '\n'.join(r.getMessage() for r in caplog.records)
        assert UUID_A not in logged
        assert EMAIL_A not in logged

    def test_the_log_names_only_the_column_that_clashes(self, repo, caplog):
        assert repo.save(alice())
        assert repo.save(bob())
        with caplog.at_level(logging.ERROR, logger='bot.core.repositories.user'):
            assert repo.save(bob(email=EMAIL_A)) is False

        assert [r.getMessage() for r in caplog.records
                if r.name == 'bot.core.repositories.user'] == [
            'Refused to save user 200: its email belongs to user 100. '
            'Nothing was written.'
        ]

    def test_any_other_refusal_keeps_the_plain_message(self, tmp_path, caplog):
        path = str(tmp_path / 'old.db')
        build_older_table(path, platform='TEXT NOT NULL')
        repo = UserRepository(path)
        with caplog.at_level(logging.ERROR, logger='bot.core.repositories.user'):
            assert repo.save(User(chat_id='1', platform=None)) is False

        assert [r.getMessage() for r in caplog.records
                if r.name == 'bot.core.repositories.user'] == [
            'Failed to save user 1: NOT NULL constraint failed: users.platform'
        ]

    def test_a_holder_that_cannot_be_read_still_gives_false(self, repo, db, caplog):
        """The holder is looked up after the refusal, on a connection of its
        own; when that read fails too, save() still answers False."""
        assert repo.save(alice())
        real_connect = repo._connect
        calls = []

        def connect():
            calls.append(1)
            if len(calls) > 1:
                raise sqlite3.OperationalError('database is locked')
            return real_connect()

        repo._connect = connect
        with caplog.at_level(logging.ERROR, logger='bot.core.repositories.user'):
            assert repo.save(bob(uuid=UUID_A)) is False

        assert len(calls) == 2
        assert [r.getMessage() for r in caplog.records
                if r.name == 'bot.core.repositories.user'] == [
            'Failed to save user 200: UNIQUE constraint failed: users.uuid'
        ]

    def test_the_facade_and_the_async_adapter_refuse_too(self, db):
        assert db.save_user(alice())
        before = rows(db.db_path)

        assert db.save_user(bob(uuid=UUID_A)) is False
        assert asyncio.run(AsyncUserRepository(db.db_path).save(bob(email=EMAIL_A))) is False

        assert rows(db.db_path) == before


class TestTheUsersOwnRow:
    def test_a_new_user_is_inserted(self, repo):
        assert repo.save(alice())

        user = repo.get_by_id('100')
        assert (user.username, user.uuid, user.email, user.status, user.quota_gb,
                user.limit_ip, user.contact_email) == (
            'alice', UUID_A, EMAIL_A, 'paid', 100.0, 5, 'alice@example.com')

    def test_every_column_round_trips_on_insert_and_on_update(self, repo, db):
        """Each column gets its own value on both paths: a value bound to
        the wrong column, or one left out, shows here."""
        def user_with(tag: int) -> User:
            user = User(chat_id='100')
            for n, column in enumerate(SAVE_COLUMNS[1:]):
                if column in ('reject_count', 'support_topic_id', 'limit_ip',
                              'next_protocol_idx'):
                    value = tag * 100 + n
                elif column in ('quota_gb', 'last_lat', 'last_lon'):
                    value = tag * 100 + n + 0.5
                else:
                    value = f'{column}-{tag}'
                setattr(user, column, value)
            return user

        for tag in (1, 2):
            expected = user_with(tag)
            assert repo.save(expected)
            stored = rows(db.db_path)['100']
            assert {c: stored[c] for c in SAVE_COLUMNS} == {
                c: getattr(expected, c) for c in SAVE_COLUMNS}
        assert len(rows(db.db_path)) == 1

    @pytest.mark.parametrize('chat_id', [None, ''])
    def test_a_user_without_a_chat_id_is_refused(self, repo, db, chat_id, caplog):
        """Such a row is nobody's (AGENTS.md §27's zombie), and with a NULL
        every save added another."""
        with caplog.at_level(logging.ERROR, logger='bot.core.repositories.user'):
            assert repo.save(User(chat_id=chat_id, uuid=UUID_B)) is False
            assert repo.save(User(chat_id=chat_id, uuid=UUID_B)) is False

        assert rows(db.db_path) == {}
        assert sql(db.db_path, 'SELECT COUNT(*) FROM users') == [(0,)]
        assert [r.getMessage() for r in caplog.records
                if r.name == 'bot.core.repositories.user'] == [
            'Refused to save a user without a chat_id. Nothing was written.'] * 2

    def test_no_protocol_index_is_stored_as_zero(self, repo, db):
        assert repo.save(alice(next_protocol_idx=None))
        assert rows(db.db_path)['100']['next_protocol_idx'] == 0

    def test_the_row_is_updated_in_place(self, repo, db):
        assert repo.save(alice())
        (rowid_before,), = sql(db.db_path, "SELECT rowid FROM users WHERE chat_id = '100'")

        assert repo.save(alice(status='demo'))

        assert sql(db.db_path, "SELECT rowid FROM users WHERE chat_id = '100'") == [
            (rowid_before,)]

    def test_columns_save_does_not_write_keep_their_values(self, repo, db):
        """traffic_up / traffic_down: the traffic mirror writes them, and
        prod's older table has them. REPLACE reset them on every save."""
        sql(db.db_path, 'ALTER TABLE users ADD COLUMN traffic_up REAL')
        sql(db.db_path, 'ALTER TABLE users ADD COLUMN traffic_down REAL')
        assert repo.save(alice())
        sql(db.db_path,
            "UPDATE users SET traffic_up = 1.5, traffic_down = 2.5, xui_synced = 1 "
            "WHERE chat_id = '100'")

        user = repo.get_by_id('100')
        user.status = 'demo'
        assert repo.save(user)

        stored = rows(db.db_path)['100']
        assert (stored['status'], stored['traffic_up'], stored['traffic_down'],
                stored['xui_synced']) == ('demo', 1.5, 2.5, 1)

    def test_reset_user_data_clears_the_key_and_keeps_the_rest(self, db):
        sql(db.db_path, 'ALTER TABLE users ADD COLUMN traffic_up REAL')
        assert db.save_user(alice())
        sql(db.db_path, "UPDATE users SET traffic_up = 3.0 WHERE chat_id = '100'")

        assert db.reset_user_data('100')

        stored = rows(db.db_path)['100']
        assert (stored['uuid'], stored['email'], stored['traffic_up'],
                stored['contact_email']) == (None, None, 3.0, 'alice@example.com')

    def test_two_saves_of_one_new_user_at_once(self, db):
        """The UPDATE takes the write lock even when it matches nothing,
        so the other save waits and then updates: no chat_id clash."""
        start = threading.Barrier(2)
        results = []

        def save(name):
            start.wait()
            results.append(UserRepository(db.db_path).save(User(chat_id='500', username=name)))

        threads = [threading.Thread(target=save, args=(n,)) for n in ('one', 'two')]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        assert results == [True, True]
        assert [r['username'] in ('one', 'two') for r in rows(db.db_path).values()] == [True]


class TestEmptyKeys:
    def test_an_empty_key_is_stored_as_none(self, repo, db):
        assert repo.save(bob(uuid='', email=''))

        stored = rows(db.db_path)['200']
        assert (stored['uuid'], stored['email']) == (None, None)

    def test_two_users_with_empty_keys_both_save(self, repo, db):
        """'' is a value: stored, the second '' clashed with the first
        (and REPLACE deleted it)."""
        assert repo.save(alice(uuid='', email=''))
        assert repo.save(bob(uuid='', email=''))

        assert set(rows(db.db_path)) == {'100', '200'}

    def test_a_row_that_already_holds_an_empty_key_heals(self, repo, db):
        sql(db.db_path, "INSERT INTO users (chat_id, uuid, email) VALUES ('300', '', '')")

        assert repo.save(bob(uuid='', email=''))
        assert set(rows(db.db_path)) == {'200', '300'}

        assert repo.save(repo.get_by_id('300'))
        assert (rows(db.db_path)['300']['uuid'], rows(db.db_path)['300']['email']) == (
            None, None)

    def test_the_users_object_is_left_as_it_was(self, repo):
        user = bob(uuid='', email='')
        assert repo.save(user)
        assert (user.uuid, user.email) == ('', '')


def build_older_table(path, **overrides):
    """A users table of another shape: every column save() writes, plus
    the given column definitions (NOT NULL, defaults) and traffic_up."""
    columns = {
        'chat_id': 'TEXT PRIMARY KEY', 'username': 'TEXT', 'previous_state': 'TEXT',
        'reject_count': 'INTEGER', 'uuid': 'TEXT UNIQUE', 'email': 'TEXT UNIQUE',
        'status': 'TEXT', 'lang': 'TEXT', 'platform': 'TEXT',
        'support_topic_id': 'INTEGER', 'created_at': 'TEXT',
        'subscription_expiry': 'TEXT', 'limit_ip': 'INTEGER', 'quota_gb': 'REAL',
        'last_traffic_update': 'TEXT', 'next_protocol_idx': 'INTEGER',
        'last_country': 'TEXT', 'last_asn': 'TEXT', 'last_city': 'TEXT',
        'last_lat': 'REAL', 'last_lon': 'REAL', 'contact_email': 'TEXT',
        'traffic_up': 'REAL',
    }
    columns.update(overrides)
    sql(path, 'CREATE TABLE users (' + ', '.join(
        f'{name} {kind}' for name, kind in columns.items()) + ')')


class TestOlderSchema:
    def test_a_null_for_a_not_null_column_with_a_default_writes_the_default(self, tmp_path):
        """What REPLACE did; a plain INSERT or UPDATE refuses the NULL."""
        path = str(tmp_path / 'old.db')
        build_older_table(path, lang="TEXT NOT NULL DEFAULT 'ru'",
                          status="TEXT NOT NULL DEFAULT 'new'",
                          created_at='TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP')
        repo = UserRepository(path)

        assert repo.save(User(chat_id='1', lang=None, status=None))
        first = rows(path)['1']
        assert (first['lang'], first['status']) == ('ru', 'new')

        user = repo.get_by_id('1')
        user.lang, user.status, user.created_at = None, None, None
        user.username = 'renamed'
        assert repo.save(user)
        second = rows(path)['1']
        assert (second['lang'], second['status'], second['username']) == (
            'ru', 'new', 'renamed')
        assert second['created_at']

    def test_a_null_stays_null_where_the_column_allows_it(self, repo, db):
        """The repo's lang has a default but no NOT NULL: REPLACE stored the
        NULL, and so does save()."""
        assert repo.save(alice(lang=None))
        assert rows(db.db_path)['100']['lang'] is None

    def test_not_null_without_a_default_still_refuses(self, tmp_path):
        path = str(tmp_path / 'old.db')
        build_older_table(path, platform='TEXT NOT NULL')
        repo = UserRepository(path)

        assert repo.save(User(chat_id='1', platform=None)) is False
        assert rows(path) == {}

    def test_a_clash_on_the_older_table_is_refused_too(self, tmp_path):
        path = str(tmp_path / 'old.db')
        build_older_table(path, lang="TEXT NOT NULL DEFAULT 'ru'")
        repo = UserRepository(path)
        assert repo.save(alice())
        sql(path, "UPDATE users SET traffic_up = 4.0 WHERE chat_id = '100'")
        before = rows(path)

        assert repo.save(bob(uuid=UUID_A, lang=None)) is False
        assert rows(path) == before
