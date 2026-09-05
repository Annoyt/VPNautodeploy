"""The DPI agent follow-up runs off the alert tick, bounded per key and
by a global slot pool; the protocol follow-up is untouched by that pool.

Why
---
``AlertManager.run_once`` is one APScheduler job (60 s, default
max_instances=1). Until 2026-09-05 ``_kick_dpi_agent`` ran INSIDE it,
synchronously, with a 600-s budget — so every DPI bucket that fired
could park the whole tick (CPU/RAM/disk, Telegram egress, and the
protocol_down pager added after the four-day Reality outage) for ten
minutes, several times an hour. The follow-up now goes through the same
``_spawn_agent_worker`` path as protocol_down, with one extra rule: at
most ``DPI_AGENT_MAX_CONCURRENT`` DPI turns at once, and a fire beyond
that is skipped (INFO), not queued.

What these tests pin
--------------------
* ``_fire`` returns in milliseconds while the agent is blocked;
* the analysis still lands in ``alert_history.kimi_analysis`` once the
  worker finishes;
* per-key dedupe, the slot cap, and that both are released after every
  worker outcome — success, ``ask()`` raising, ``_kick_agent`` itself
  raising, a thread that would not start;
* the protocol kick neither draws from nor waits on the DPI pool;
* DPI still never posts to chat.

Mutation checks that must go red: drop the semaphore (the third key
runs), drop the ``finally`` release (slot/key never freed), make the
worker non-daemon.

Real sqlite bot.db, like the hook tests, so the attach UPDATE is
exercised rather than mocked. The agent client is stubbed the way
``test_alert_protocol_agent_hook.py`` does it, with ``ask()`` parked on
a per-alert-key ``threading.Event`` so a test can hold two turns open
and release exactly one.
"""

import logging
import threading
import time
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pytest

import bot.services.alert_manager as alert_manager_module
from bot.core.database import Database
from bot.services.alert_manager import (
    Alert,
    AlertManager,
    DPI_AGENT_MAX_CONCURRENT,
    DPI_AGENT_TIMEOUT_S,
    _Tracker,
)


REPLY = '<b>RU/AS8402</b>: 55% коротких сессий против 12% за 7д — похоже на DPI.'
LOGGER = 'bot.services.alert_manager'

KEY_A = 'dpi_short:RU:AS8402'
KEY_B = 'dpi_hsfail:RU:AS12389'
KEY_C = 'dpi_rst:global'
KEY_D = 'dpi_short:KZ:AS9198'
PROTO_KEY = 'protocol_down:reality'


def _alert_key(session_key):
    """session_key = f"{prefix}:{alert.key}:{epoch}" (see _kick_agent)."""
    return session_key.split(':', 1)[1].rsplit(':', 1)[0]


class BlockingAgent:
    """Stand-in for the Hermes client. ``ask()`` records that it was
    entered for an alert key, then parks on that key's gate until the
    test opens it — or raises, for keys listed in ``fail_keys``."""

    def __init__(self, client):
        self.client = client
        self.fail_keys = set()
        self._lock = threading.Lock()
        self._gates = {}
        self._entered = {}
        self._all_open = False

    def _event(self, table, key):
        with self._lock:
            return table.setdefault(key, threading.Event())

    def entered(self, key):
        return self._event(self._entered, key)

    def ask(self, session_key, prompt, **kw):
        key = _alert_key(session_key)
        gate = self._event(self._gates, key)
        self.entered(key).set()
        if key in self.fail_keys:
            raise RuntimeError(f'boom for {key}')
        if not self._all_open:
            assert gate.wait(timeout=10), f'gate for {key} never opened'
        return REPLY, 10

    def release(self, key):
        self._event(self._gates, key).set()

    def release_all(self):
        with self._lock:
            self._all_open = True
            gates = list(self._gates.values())
        for g in gates:
            g.set()


@pytest.fixture
def db(tmp_path):
    return Database(str(tmp_path / 'bot.db'))


@pytest.fixture
def config(tmp_path):
    cfg = Mock()
    cfg.DB_PATH = str(tmp_path / 'bot.db')
    cfg.TOPIC_AI = 55
    cfg.FORUM_GROUP_ID = -100123
    cfg.SUPER_ADMIN_ID = '1652899'
    cfg.ALERT_TG_MIN_SEVERITY = 'critical'
    # Explicit strings: a bare Mock attr is truthy and would make
    # get_agent_url() believe an agent is configured.
    cfg.AGENT_BACKEND = 'hermes'
    cfg.HERMES_URL = 'http://hermes:4097'
    return cfg


@pytest.fixture
def agent():
    client = Mock()
    harness = BlockingAgent(client)
    client.ask.side_effect = harness.ask
    with patch('bot.services.agent_factory.build_agent_client',
               return_value=client):
        yield harness
    harness.release_all()


def _join_workers(timeout=10):
    """Every agent worker this module can have started, by thread name
    (``_spawn_agent_worker`` names them ``<prefix>-<alert key>``)."""
    for t in threading.enumerate():
        if t.name.startswith(('dpi-alert-', 'proto-alert-')):
            t.join(timeout=timeout)
            assert not t.is_alive(), f'{t.name} did not finish'


@pytest.fixture
def mgr(db, config, agent):
    m = AlertManager(Mock(), config, db=db)
    yield m
    # Never leave a parked worker behind: it would wake up after the
    # tmp DB is gone and the next test's caplog would inherit its log.
    agent.release_all()
    _join_workers()


def dpi_alert(key=KEY_A, severity='warn'):
    return Alert(
        key=key, severity=severity, min_cycles=2,
        title=f'DPI? {key} 55% short sessions',
        detail='Rostelecom · 55/100 коротких сессий за час.',
    )


def proto_alert(key=PROTO_KEY):
    return Alert(
        key=key, severity='critical', min_cycles=2,
        title='Протокол reality мёртв (лежит 4 д 3 ч)',
        detail='За последние 3 прогона через reality не вернулся НИ ОДИН ответ.',
    )


def fire(mgr, alert):
    """Drive ``_fire`` (tracker bookkeeping is covered elsewhere) and
    return the worker it spawned — None when nothing was started."""
    before = mgr._last_agent_thread
    mgr._fire(alert, _Tracker(consecutive_fails=2))
    after = mgr._last_agent_thread
    return after if after is not before else None


def wait_entered(agent, key, timeout=5):
    assert agent.entered(key).wait(timeout=timeout), f'{key} never reached ask()'


def history(db):
    with db._connect() as conn:
        return [dict(r) for r in conn.execute(
            "SELECT key, kimi_analysis FROM alert_history ORDER BY id"
        ).fetchall()]


def inflight(mgr):
    with mgr._agent_lock:
        return set(mgr._agent_inflight)


class TestDpiFireIsOffTheTick:

    def test_fire_returns_while_the_agent_is_still_thinking(self, db, mgr, agent):
        t0 = time.monotonic()
        worker = fire(mgr, dpi_alert())
        assert time.monotonic() - t0 < 2.0
        assert worker is not None and worker.is_alive()
        wait_entered(agent, KEY_A)
        # The alert row is already persisted; the analysis is not there yet.
        assert history(db) == [{'key': KEY_A, 'kimi_analysis': None}]

        agent.release(KEY_A)
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert history(db) == [{'key': KEY_A, 'kimi_analysis': REPLY}]
        # Budget and session naming survived the move off the tick.
        agent.client.ask.assert_called_once()
        assert agent.client.ask.call_args.kwargs['timeout'] == DPI_AGENT_TIMEOUT_S == 600
        assert agent.client.ask.call_args.args[0].startswith(f'dpi-alert:{KEY_A}:')

    def test_worker_is_a_daemon_thread(self, mgr, agent):
        """A parked agent turn must never keep the container from
        exiting on SIGTERM (docker stop grace is 10 s)."""
        worker = fire(mgr, dpi_alert())
        assert worker.daemon is True

    def test_run_once_tick_is_not_held_by_a_dpi_turn(self, db, mgr, agent):
        """Through the real tick: multi-bucket check shape, min_cycles=2,
        the second run_once must come back at once with the worker
        still parked — that is the whole point of the change."""
        mgr.register(lambda: ('dpi_short:', [dpi_alert()]))
        mgr.run_once()
        agent.client.ask.assert_not_called()

        t0 = time.monotonic()
        mgr.run_once()
        assert time.monotonic() - t0 < 2.0
        wait_entered(agent, KEY_A)
        assert mgr._last_agent_thread.is_alive()

        agent.release(KEY_A)
        mgr._last_agent_thread.join(timeout=10)
        assert history(db) == [{'key': KEY_A, 'kimi_analysis': REPLY}]

    @pytest.mark.parametrize('severity', ['warn', 'critical'])
    def test_dpi_never_posts_to_the_topic(self, db, mgr, agent, severity):
        """Dashboard-only contract, unchanged: neither the alert nor the
        diagnosis reaches chat, whatever the severity."""
        worker = fire(mgr, dpi_alert(severity=severity))
        agent.release(KEY_A)
        worker.join(timeout=10)
        assert history(db)[0]['kimi_analysis'] == REPLY
        mgr.bot.send_message.assert_not_called()


class TestDpiPerKeyDedupe:

    def test_same_key_is_not_rekicked_while_in_flight(self, db, mgr, agent):
        first = fire(mgr, dpi_alert())
        wait_entered(agent, KEY_A)
        assert fire(mgr, dpi_alert()) is None          # same key, still parked
        assert agent.client.ask.call_count == 1
        assert inflight(mgr) == {KEY_A}

        agent.release(KEY_A)
        first.join(timeout=10)
        assert inflight(mgr) == set()
        # Once the turn is over the key is free again.
        again = fire(mgr, dpi_alert())
        assert again is not None and again is not first
        again.join(timeout=10)
        assert agent.client.ask.call_count == 2
        assert [r['kimi_analysis'] for r in history(db)] == [REPLY, None, REPLY]

    def test_two_different_keys_run_concurrently(self, db, mgr, agent):
        first = fire(mgr, dpi_alert(KEY_A))
        second = fire(mgr, dpi_alert(KEY_B))
        assert first is not None and second is not None and first is not second
        # Both reached ask() while neither gate is open = truly parallel.
        wait_entered(agent, KEY_A)
        wait_entered(agent, KEY_B)
        assert first.is_alive() and second.is_alive()
        assert inflight(mgr) == {KEY_A, KEY_B}

        agent.release_all()
        first.join(timeout=10)
        second.join(timeout=10)
        assert history(db) == [
            {'key': KEY_A, 'kimi_analysis': REPLY},
            {'key': KEY_B, 'kimi_analysis': REPLY},
        ]


class TestDpiSlotPool:

    def test_cap_is_two(self):
        assert DPI_AGENT_MAX_CONCURRENT == 2

    def test_third_key_is_skipped_then_runs_once_a_slot_frees(self, db, mgr, agent, caplog):
        """Skip, don't queue: a dashboard annotation is not worth a
        backlog of 10-min turns. The skipped key's row still exists (the
        alert itself fired) and the key retries on its next fire."""
        first = fire(mgr, dpi_alert(KEY_A))
        fire(mgr, dpi_alert(KEY_B))
        wait_entered(agent, KEY_A)
        wait_entered(agent, KEY_B)

        with caplog.at_level(logging.INFO, logger=LOGGER):
            assert fire(mgr, dpi_alert(KEY_C)) is None
        assert agent.client.ask.call_count == 2
        assert KEY_C not in inflight(mgr)
        infos = [r.getMessage() for r in caplog.records
                 if r.name == LOGGER and r.levelno == logging.INFO]
        assert any('no free agent slot' in m and KEY_C in m for m in infos), infos
        # The throttle doing its job is not a fault.
        assert not [r for r in caplog.records
                    if r.name == LOGGER and r.levelno >= logging.WARNING]

        agent.release(KEY_A)
        first.join(timeout=10)
        third = fire(mgr, dpi_alert(KEY_C))
        assert third is not None
        wait_entered(agent, KEY_C)
        agent.release_all()
        third.join(timeout=10)
        _join_workers()
        assert [(r['key'], r['kimi_analysis'] is not None) for r in history(db)] == [
            (KEY_A, True), (KEY_B, True), (KEY_C, False), (KEY_C, True),
        ]

    def test_slots_are_released_after_failing_workers(self, db, mgr, agent):
        """ask() raising is the everyday failure (quota, 504, Hermes
        down). Two such turns must hand both slots back."""
        agent.fail_keys = {KEY_A, KEY_B}
        first = fire(mgr, dpi_alert(KEY_A))
        second = fire(mgr, dpi_alert(KEY_B))
        first.join(timeout=10)
        second.join(timeout=10)
        assert inflight(mgr) == set()
        assert [r['kimi_analysis'] for r in history(db)] == [None, None]

        # Both slots free again: two new keys both reach the agent.
        assert fire(mgr, dpi_alert(KEY_C)) is not None
        assert fire(mgr, dpi_alert(KEY_D)) is not None
        wait_entered(agent, KEY_C)
        wait_entered(agent, KEY_D)
        assert inflight(mgr) == {KEY_C, KEY_D}

    def test_slot_and_key_are_released_when_kick_agent_itself_raises(self, mgr, agent, caplog):
        """_kick_agent never raises by contract; the worker's finally is
        the belt for that brace — without it one bug there would silence
        the key until the next deploy and burn a slot for good."""
        with patch.object(mgr, '_kick_agent', side_effect=RuntimeError('unexpected')), \
                caplog.at_level(logging.WARNING, logger=LOGGER):
            first = fire(mgr, dpi_alert(KEY_A))
            second = fire(mgr, dpi_alert(KEY_B))
            first.join(timeout=10)
            second.join(timeout=10)
        assert inflight(mgr) == set()
        assert sum('worker failed' in r.getMessage() for r in caplog.records
                   if r.name == LOGGER) == 2

        assert fire(mgr, dpi_alert(KEY_C)) is not None
        assert fire(mgr, dpi_alert(KEY_D)) is not None
        wait_entered(agent, KEY_C)
        wait_entered(agent, KEY_D)

    def test_thread_start_failure_is_contained_and_releases(self, db, mgr, agent, caplog):
        """RuntimeError("can't start new thread") under memory pressure:
        _fire must still return, the row is persisted, and neither the
        key nor the slot is leaked (the worker's finally never ran)."""
        class _Unstartable(threading.Thread):
            def start(self):
                raise RuntimeError("can't start new thread")

        fake_threading = SimpleNamespace(Thread=_Unstartable)
        with patch.object(alert_manager_module, 'threading', fake_threading), \
                caplog.at_level(logging.WARNING, logger=LOGGER):
            assert fire(mgr, dpi_alert(KEY_A)) is None      # did not raise
        assert history(db) == [{'key': KEY_A, 'kimi_analysis': None}]
        assert inflight(mgr) == set()
        assert any('could not start worker' in r.getMessage() for r in caplog.records)

        # Same key runs on the next fire and both slots are still there.
        assert fire(mgr, dpi_alert(KEY_A)) is not None
        assert fire(mgr, dpi_alert(KEY_B)) is not None
        wait_entered(agent, KEY_A)
        wait_entered(agent, KEY_B)


class TestProtocolKickIsUnaffected:

    def test_protocol_turn_runs_while_both_dpi_slots_are_taken(self, db, mgr, agent):
        """A DPI storm holding every slot must not postpone the diagnosis
        of a dead protocol — protocol_down is bounded per key only."""
        fire(mgr, dpi_alert(KEY_A))
        fire(mgr, dpi_alert(KEY_B))
        wait_entered(agent, KEY_A)
        wait_entered(agent, KEY_B)
        assert fire(mgr, dpi_alert(KEY_C)) is None          # pool is full…

        proto = fire(mgr, proto_alert())                     # …and this still runs
        assert proto is not None
        wait_entered(agent, PROTO_KEY)
        assert inflight(mgr) == {KEY_A, KEY_B, PROTO_KEY}

        agent.release_all()
        proto.join(timeout=10)
        _join_workers()
        # The protocol contract: alert + diagnosis in the topic; DPI posted nothing.
        assert mgr.bot.send_message.call_count == 2
        texts = [c.kwargs['text'] for c in mgr.bot.send_message.call_args_list]
        assert 'reality' in texts[0]
        assert texts[1].startswith('🤖 <b>Диагностика по алерту</b>')
        assert history(db)[-1] == {'key': PROTO_KEY, 'kimi_analysis': REPLY}

    def test_protocol_turn_does_not_consume_a_dpi_slot(self, mgr, agent):
        proto = fire(mgr, proto_alert())
        wait_entered(agent, PROTO_KEY)
        # Both DPI slots are still available with a protocol turn parked.
        assert fire(mgr, dpi_alert(KEY_A)) is not None
        assert fire(mgr, dpi_alert(KEY_B)) is not None
        wait_entered(agent, KEY_A)
        wait_entered(agent, KEY_B)
        assert proto.is_alive()


class TestFactoryFailureIsContained:

    def test_client_factory_raising_frees_key_and_slot(self, db, mgr, agent):
        with patch('bot.services.agent_factory.build_agent_client',
                   side_effect=ImportError('no hermes client')):
            first = fire(mgr, dpi_alert(KEY_A))
            second = fire(mgr, dpi_alert(KEY_B))
            first.join(timeout=10)
            second.join(timeout=10)
        assert [r['kimi_analysis'] for r in history(db)] == [None, None]
        assert inflight(mgr) == set()
        assert fire(mgr, dpi_alert(KEY_C)) is not None
        assert fire(mgr, dpi_alert(KEY_D)) is not None
        wait_entered(agent, KEY_C)
        wait_entered(agent, KEY_D)

    def test_agent_url_lookup_raising_spawns_nothing(self, db, mgr, agent, caplog):
        with patch('bot.services.agent_factory.get_agent_url',
                   side_effect=RuntimeError('settings broken')), \
                caplog.at_level(logging.WARNING, logger=LOGGER):
            assert fire(mgr, dpi_alert()) is None
        assert history(db) == [{'key': KEY_A, 'kimi_analysis': None}]
        assert inflight(mgr) == set()
        agent.client.ask.assert_not_called()
        assert any('agent factory unavailable' in r.getMessage() for r in caplog.records)

    def test_agent_not_configured_spawns_nothing(self, config, db, mgr, agent):
        config.HERMES_URL = ''
        assert fire(mgr, dpi_alert()) is None
        agent.client.ask.assert_not_called()
        assert history(db) == [{'key': KEY_A, 'kimi_analysis': None}]
