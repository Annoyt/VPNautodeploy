"""Tests for the /onlines protocol column.

Runs against a REAL sqlite file rather than mocks: the whole feature is
two SQL reads over a freshness window, so a mocked cursor would assert
nothing about the part that can actually break.

Why two sources (see AdminOpsMixin._protocol_by_email): the xray
inbounds come from ``user_presence``, posted by the exit node because
only its access.log carries the inbound tag; Hysteria2 is a separate
binary that never reaches that log, so recent ``hy2_auth_log`` rows are
the only "on hy2" signal there is.
"""

from datetime import datetime, timedelta

import pytest

from bot.core.database import Database
from bot.core.web_server import WebAppServer
from bot.handlers.admin.ops import AdminOpsMixin


def _ago(minutes: int) -> str:
    return (datetime.utcnow() - timedelta(minutes=minutes)).isoformat()


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def handler(db):
    h = AdminOpsMixin.__new__(AdminOpsMixin)
    h.db = db
    return h


def _add_user(db, chat_id, email):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO users (chat_id, username, email, status) "
            "VALUES (?, ?, ?, 'demo')",
            (chat_id, f'u{chat_id}', email),
        )


def _add_presence(db, email, tag, proto, minutes_ago=1):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO user_presence (email, inbound_tag, proto, conns, seen_at) "
            "VALUES (?, ?, ?, 3, ?)",
            (email, tag, proto, _ago(minutes_ago)),
        )


class TestProtocolByEmail:

    def test_fresh_presence_row_maps_to_a_label(self, db, handler):
        _add_presence(db, 'a@x', 'inbound-443', 'reality')
        assert handler._protocol_by_email() == {'a@x': 'Reality'}

    def test_every_known_proto_gets_a_readable_label(self, db, handler):
        for i, (proto, label) in enumerate([
            ('reality', 'Reality'), ('cf-ws', 'WS'),
            ('ss2022', 'ShadowTLS'), ('xhttp', 'XHTTP'),
        ]):
            _add_presence(db, f'{i}@x', f'inbound-{i}', proto)
        out = handler._protocol_by_email()
        assert sorted(out.values()) == ['Reality', 'ShadowTLS', 'WS', 'XHTTP']

    def test_unknown_proto_passes_through_raw(self, db, handler):
        """A newly added inbound must be visible before we name it,
        not silently blank."""
        _add_presence(db, 'a@x', 'inbound-9999', 'inbound-9999')
        assert handler._protocol_by_email() == {'a@x': 'inbound-9999'}

    def test_stale_presence_is_dropped(self, db, handler):
        _add_presence(db, 'old@x', 'inbound-443', 'reality',
                      minutes_ago=AdminOpsMixin.PRESENCE_WINDOW_MIN + 5)
        assert handler._protocol_by_email() == {}

    def test_row_just_inside_the_window_survives(self, db, handler):
        """One missed exit-reporter tick must not blank the column."""
        _add_presence(db, 'edge@x', 'inbound-443', 'reality',
                      minutes_ago=AdminOpsMixin.PRESENCE_WINDOW_MIN - 2)
        assert handler._protocol_by_email() == {'edge@x': 'Reality'}

    def test_recent_hy2_auth_marks_the_user(self, db, handler):
        _add_user(db, '555', 'h@x')
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO hy2_auth_log (ts, chat_id, decision) "
                "VALUES (datetime('now', '-2 minutes'), '555', 'allow')"
            )
        assert handler._protocol_by_email() == {'h@x': 'Hy2'}

    def test_hy2_wins_over_an_xray_row(self, db, handler):
        """A sing-box client keeps every outbound alive, so a user can
        hold both. UDP and calls only ride hy2, so that's the one worth
        showing."""
        _add_user(db, '555', 'both@x')
        _add_presence(db, 'both@x', 'inbound-2053', 'cf-ws')
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO hy2_auth_log (ts, chat_id, decision) "
                "VALUES (datetime('now', '-1 minutes'), '555', 'allow')"
            )
        assert handler._protocol_by_email() == {'both@x': 'Hy2'}

    def test_stale_hy2_auth_does_not_mark(self, db, handler):
        """hy2 auth fires on connect, not continuously — an old row must
        age out rather than pin the user to hy2 forever."""
        _add_user(db, '555', 'h@x')
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO hy2_auth_log (ts, chat_id, decision) VALUES "
                "(datetime('now', '-90 minutes'), '555', 'allow')"
            )
        assert handler._protocol_by_email() == {}

    def test_denied_hy2_auth_does_not_mark(self, db, handler):
        _add_user(db, '555', 'h@x')
        with db._connect() as conn:
            conn.execute(
                "INSERT INTO hy2_auth_log (ts, chat_id, decision) VALUES "
                "(datetime('now', '-1 minutes'), '555', 'deny')"
            )
        assert handler._protocol_by_email() == {}

    def test_db_error_degrades_to_empty_not_crash(self, handler):
        """/onlines must still render the rest of the row."""
        class Boom:
            def _connect(self):
                raise RuntimeError('db gone')
        handler.db = Boom()
        assert handler._protocol_by_email() == {}


class TestStorePresence:
    """WebAppServer._store_presence — ingest side."""

    @pytest.fixture
    def server(self, db):
        s = WebAppServer.__new__(WebAppServer)
        s.db = db
        return s

    def test_upsert_normalises_tag_to_proto(self, server, db):
        n = server._store_presence({'presence': [
            {'email': 'a@x', 'tag': 'inbound-443', 'conns': 4},
        ]})
        assert n == 1
        with db._connect() as conn:
            row = conn.execute(
                "SELECT inbound_tag, proto, conns FROM user_presence"
            ).fetchone()
        assert tuple(row) == ('inbound-443', 'reality', 4)

    def test_second_report_overwrites_not_duplicates(self, server, db):
        server._store_presence({'presence': [{'email': 'a@x', 'tag': 'inbound-443'}]})
        server._store_presence({'presence': [{'email': 'a@x', 'tag': 'inbound-2053'}]})
        with db._connect() as conn:
            rows = conn.execute(
                "SELECT email, proto FROM user_presence"
            ).fetchall()
        assert len(rows) == 1
        assert rows[0][1] == 'cf-ws'

    def test_malformed_items_are_skipped_not_fatal(self, server):
        n = server._store_presence({'presence': [
            {'email': '', 'tag': 'inbound-443'},      # no email
            {'email': 'b@x', 'tag': ''},              # no tag
            {'email': 'c@x', 'tag': 'inbound-443', 'conns': 'NaN'},
            'not-a-dict',
            {'email': 'd@x', 'tag': 'inbound-2053', 'conns': 2},
        ]})
        assert n == 2  # c@x (conns coerced to 0) and d@x

    def test_missing_or_wrong_presence_block_is_a_noop(self, server):
        assert server._store_presence({}) == 0
        assert server._store_presence({'presence': None}) == 0
        assert server._store_presence({'presence': 'nope'}) == 0
