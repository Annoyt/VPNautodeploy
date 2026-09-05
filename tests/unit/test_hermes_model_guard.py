"""Tests for scripts/hermes_model_guard.py — the free-model billing guard
that keeps the Hermes agent on $0 OpenRouter models.

No network anywhere: the pure core (classify / decide / dedupe / env
parser) is called directly and the one-cycle wrapper gets a fake GuardIO.
Pinned because a guard nobody tested is a guard that "promotes" onto a
paid model at 3 a.m. — or, worse, reads one proxy hiccup as "every model
is missing" and screams.
"""

import copy
import importlib.util
import json
import logging
import os
from datetime import datetime, timedelta, timezone

import pytest
import yaml

# Same loader shape as tests/unit/test_exit_dpi_reporter.py — scripts/ is
# not a package, so it is loaded by path.
_PATH = os.path.abspath(os.path.join(
    os.path.dirname(__file__), '..', '..', 'scripts', 'hermes_model_guard.py',
))
_spec = importlib.util.spec_from_file_location('hermes_model_guard', _PATH)
guard = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(guard)

REPO = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))

PRIMARY = 'minimax/minimax-m3:free'
FALLBACK = 'nvidia/nemotron-3-super-120b-a12b:free'
OTHER = 'qwen/qwen3-coder:free'

# Verbatim shape of entry:/root/.hermes/config.yaml (2026-09-05) — WITH its
# comments, because the round-trip test must prove the *values* survive
# even though PyYAML drops the comments (the backup keeps those).
REAL_CONFIG_TEXT = f'''model:
  # MiniMax M3 free: fast (~2-3s/turn), correct tool-calling, sane ops
  # judgment (probed 2026-08-30).
  default: "{PRIMARY}"
  provider: "openrouter"
  base_url: "https://openrouter.ai/api/v1"
# Tried on API failure of the primary, in order. Both :free.
fallback_providers:
  - provider: "openrouter"
    model: "{FALLBACK}"
    base_url: "https://openrouter.ai/api/v1"
max_turns: 90
terminal:
  backend: local
  working_dir: /root/hermes-work
  timeout: 300
hooks_auto_accept: true
# smart = auto-run safe commands, gate dangerous ones (rm, restart, down).
approvals:
  mode: "smart"
'''

NOW = datetime(2026, 9, 5, 10, 0, tzinfo=timezone.utc)


def real_config():
    return yaml.safe_load(REAL_CONFIG_TEXT)


def free():
    return {'pricing': {'prompt': '0', 'completion': '0'}}


def paid():
    # Real numbers for minimax/minimax-m3 (non-free id), 2026-09-05.
    return {'pricing': {'prompt': '0.0000003', 'completion': '0.0000012',
                        'input_cache_read': '0.00000006'}}


def make_index(**states):
    """{model_id: entry} big enough to pass MIN_MODELS_SANE.

    Defaults to the live chain (PRIMARY + FALLBACK) both free — the
    2026-09-05 reality; states override per id: 'free' | 'paid' | 'missing'.
    """
    index = {f'filler/model-{i}:free': free() for i in range(guard.MIN_MODELS_SANE + 5)}
    index[PRIMARY] = free()
    index[FALLBACK] = free()
    for mid, state in states.items():
        if state == 'free':
            index[mid] = free()
        elif state == 'paid':
            index[mid] = paid()
        elif state == 'missing':
            index.pop(mid, None)
        else:
            raise ValueError(state)
    return index


def keys(plan):
    return [n.key for n in plan.notifications]


# ---------------------------------------------------------------------------
# classify_model / is_free_price
# ---------------------------------------------------------------------------

class TestClassify:
    @pytest.mark.parametrize('value', ['0', '0.0', 0, 0.0, '0e0'])
    def test_zero_forms_are_free(self, value):
        assert guard.is_free_price(value) is True

    @pytest.mark.parametrize('value', ['0.0000003', 1, None, '', 'free', 'n/a'])
    def test_anything_else_is_not_free(self, value):
        assert guard.is_free_price(value) is False

    def test_free_model(self):
        st = guard.classify_model(PRIMARY, make_index(**{PRIMARY: 'free'}))
        assert st.state == guard.FREE
        assert 'prompt=0 completion=0' in st.pricing_text()

    def test_paid_model_names_the_prices(self):
        st = guard.classify_model(PRIMARY, make_index(**{PRIMARY: 'paid'}))
        assert st.state == guard.PAID
        assert 'prompt=0.0000003' in st.pricing_text()
        assert 'completion=0.0000012' in st.pricing_text()

    def test_missing_model(self):
        st = guard.classify_model(PRIMARY, make_index(**{PRIMARY: 'missing'}))
        assert st.state == guard.MISSING
        assert 'нет в списке' in st.pricing_text()

    def test_no_pricing_block_is_paid_not_free(self):
        # Fail closed: cannot prove it costs nothing -> treat as paid.
        index = make_index()
        index[PRIMARY] = {'id': PRIMARY}
        assert guard.classify_model(PRIMARY, index).state == guard.PAID

    def test_per_request_price_makes_it_paid(self):
        index = make_index()
        index[PRIMARY] = {'pricing': {'prompt': '0', 'completion': '0', 'request': '0.01'}}
        st = guard.classify_model(PRIMARY, index)
        assert st.state == guard.PAID
        assert 'request=0.01' in st.pricing_text()

    def test_tiny_catalogue_is_a_failed_fetch(self):
        # 3 models back from /models = truncated body, NOT "everything vanished".
        assert guard.sanitize_models_index({PRIMARY: free(), 'a': free(), 'b': free()}) is None
        assert guard.sanitize_models_index(None) is None
        assert guard.sanitize_models_index(make_index()) is not None


# ---------------------------------------------------------------------------
# decide(): pricing branch
# ---------------------------------------------------------------------------

class TestDecidePrimaryFree:
    def test_no_action_no_noise(self):
        cfg = real_config()
        plan = guard.decide(cfg, make_index(), 0.0152475, 0.0152475)
        assert plan.actions == []
        assert plan.notifications == []
        assert plan.new_config is None
        assert plan.primary.state == guard.FREE
        assert [st.state for st in plan.fallbacks] == [guard.FREE]

    def test_input_config_never_mutated(self):
        cfg = real_config()
        snapshot = copy.deepcopy(cfg)
        guard.decide(cfg, make_index(**{PRIMARY: 'paid'}), None, None)
        assert cfg == snapshot


class TestDecidePromote:
    def test_paid_primary_promotes_first_free_fallback(self):
        cfg = real_config()
        plan = guard.decide(cfg, make_index(**{PRIMARY: 'paid'}), None, None)

        assert plan.new_config is not None
        assert plan.new_config['model']['default'] == FALLBACK
        assert plan.new_config['model']['provider'] == 'openrouter'
        assert plan.new_config['model']['base_url'] == 'https://openrouter.ai/api/v1'
        # the PAID old primary is REMOVED, not demoted: the guard never
        # leaves a paid path in the chain it just cleaned
        assert plan.new_config['fallback_providers'] == []
        assert PRIMARY not in json.dumps(plan.new_config)
        assert f'promote:{FALLBACK}' in plan.actions
        assert f'write:drop:{PRIMARY}' in plan.actions
        assert f'restart:{guard.HERMES_UNIT}' in plan.actions

    def test_paid_primary_notification_is_critical_and_names_everything(self):
        plan = guard.decide(real_config(), make_index(**{PRIMARY: 'paid'}), None, None)
        [n] = [n for n in plan.notifications if n.key.startswith('promoted:')]
        assert n.severity == guard.CRITICAL   # money may already have moved
        assert PRIMARY in n.detail and FALLBACK in n.detail
        assert 'prompt=0.0000003' in n.detail   # pricing actually seen
        assert 'prompt=0 completion=0' in n.detail
        assert 'УБРАНА' in n.detail             # says where the old one went

    def test_missing_primary_first_sighting_warns_and_waits(self):
        # Hermes walks fallback_providers on 404 itself — nothing burns, so
        # one transient delisting must not rewrite the operator's config.
        plan = guard.decide(real_config(), make_index(**{PRIMARY: 'missing'}), None, None,
                            prev_states={PRIMARY: 'free', FALLBACK: 'free'})
        assert plan.new_config is None
        assert plan.actions == []
        assert keys(plan) == [f'primary_missing:{PRIMARY}']
        n = plan.notifications[0]
        assert n.severity == guard.WARN
        assert 'не трогал' in n.detail and FALLBACK in n.detail

    def test_missing_primary_confirmed_is_dropped_from_chain(self):
        plan = guard.decide(real_config(), make_index(**{PRIMARY: 'missing'}), None, None,
                            prev_states={PRIMARY: 'missing', FALLBACK: 'free'})
        assert plan.new_config['model']['default'] == FALLBACK
        assert plan.new_config['fallback_providers'] == []
        [n] = [n for n in plan.notifications if n.key.startswith('promoted:')]
        assert n.severity == guard.WARN         # 404s cost nothing
        assert 'пропала' in n.title
        assert any(a.startswith('write:drop:') for a in plan.actions)

    def test_paid_primary_is_promoted_at_once_no_confirmation(self):
        # money is moving: no second-sighting rule for PAID
        plan = guard.decide(real_config(), make_index(**{PRIMARY: 'paid'}), None, None,
                            prev_states={PRIMARY: 'free', FALLBACK: 'free'})
        assert plan.new_config is not None

    def test_first_free_fallback_wins_and_order_is_kept(self):
        cfg = real_config()
        cfg['fallback_providers'] = [
            {'provider': 'openrouter', 'model': 'x/paid-one', 'base_url': 'u'},
            {'provider': 'openrouter', 'model': FALLBACK, 'base_url': 'u'},
            {'provider': 'openrouter', 'model': OTHER, 'base_url': 'u'},
        ]
        index = make_index(**{PRIMARY: 'paid', 'x/paid-one': 'paid',
                              FALLBACK: 'free', OTHER: 'free'})
        plan = guard.decide(cfg, index, None, None)
        assert plan.new_config['model']['default'] == FALLBACK
        # untouched fallbacks keep their order; the old primary is gone
        assert [fb['model'] for fb in plan.new_config['fallback_providers']] == [
            'x/paid-one', OTHER,
        ]

    def test_winner_does_not_inherit_old_primary_endpoint_or_key(self):
        cfg = real_config()
        cfg['model']['api_key'] = 'sk-old-primary-key'
        cfg['fallback_providers'] = [{'provider': 'nous', 'model': FALLBACK}]
        plan = guard.decide(cfg, make_index(**{PRIMARY: 'paid'}), None, None)
        assert plan.new_config['model'] == {'default': FALLBACK, 'provider': 'nous'}

    def test_malformed_fallback_entries_survive_the_rewrite(self):
        cfg = real_config()
        cfg['fallback_providers'].append({'provider': 'openrouter'})   # no model
        plan = guard.decide(cfg, make_index(**{PRIMARY: 'paid'}), None, None)
        assert plan.new_config['fallback_providers'] == [{'provider': 'openrouter'}]

    def test_rendered_yaml_round_trips_other_keys(self):
        cfg = real_config()
        plan = guard.decide(cfg, make_index(**{PRIMARY: 'paid'}), None, None)
        text = guard.render_config(plan.new_config, 'promote:test',
                                   'config.yaml.bak-20260905-100000', NOW)
        back = yaml.safe_load(text)
        assert back == plan.new_config
        assert back['max_turns'] == 90
        assert back['terminal'] == {'backend': 'local',
                                    'working_dir': '/root/hermes-work',
                                    'timeout': 300}
        assert back['approvals'] == {'mode': 'smart'}
        assert back['hooks_auto_accept'] is True
        # key order preserved (Hermes doesn't care, humans diffing do)
        assert list(back) == list(cfg)
        # header points at the commented original
        assert 'config.yaml.bak-20260905-100000' in text.splitlines()[3]
        assert text.startswith('# Rewritten by hermes_model_guard.py')

    def test_tmp_file_is_private_from_the_first_byte(self, tmp_path, monkeypatch):
        # Even without the final copymode() the temp file must be 0600 —
        # an inline api_key must never sit world-readable for a moment.
        monkeypatch.setattr(guard.shutil, 'copymode', lambda *_a, **_k: None)
        cfg = tmp_path / 'config.yaml'
        cfg.write_text(REAL_CONFIG_TEXT)
        os.chmod(cfg, 0o644)
        guard.write_config(str(cfg), real_config(), 'r', NOW)
        assert oct(os.stat(cfg).st_mode & 0o777) == oct(0o600)

    def test_render_refuses_non_roundtrippable(self, monkeypatch):
        cfg = real_config()     # before the patch — same yaml module
        monkeypatch.setattr(guard.yaml, 'safe_load', lambda _t: {'nope': 1})
        with pytest.raises(guard.GuardError):
            guard.render_config(cfg, 'r', 'b', NOW)


class TestDecideNothingFree:
    def test_paid_primary_paid_fallback_is_critical_and_hands_off(self):
        plan = guard.decide(real_config(),
                            make_index(**{PRIMARY: 'paid', FALLBACK: 'paid'}), None, None)
        assert plan.new_config is None
        assert plan.actions == []
        assert 'no_free_model' in keys(plan)
        n = next(n for n in plan.notifications if n.key == 'no_free_model')
        assert n.severity == guard.CRITICAL
        assert 'не трогал' in n.detail
        assert PRIMARY in n.detail and FALLBACK in n.detail
        assert plan.critical is True

    def test_missing_primary_no_fallbacks_is_critical(self):
        cfg = real_config()
        cfg['fallback_providers'] = []
        plan = guard.decide(cfg, make_index(**{PRIMARY: 'missing'}), None, None)
        assert plan.new_config is None
        assert keys(plan) == ['no_free_model']

    def test_malformed_config_raises_instead_of_guessing(self):
        with pytest.raises(guard.GuardError):
            guard.decide({'model': {}}, make_index(), None, None)
        with pytest.raises(guard.GuardError):
            guard.decide({'model': {'default': PRIMARY}, 'fallback_providers': 'oops'},
                         make_index(), None, None)


# ---------------------------------------------------------------------------
# decide(): usage branch
# ---------------------------------------------------------------------------

class TestDecideUsage:
    def test_usage_grew_is_critical(self):
        plan = guard.decide(real_config(), make_index(), 0.0152475, 0.0252475)
        assert keys(plan) == ['usage_grew']
        n = plan.notifications[0]
        assert n.severity == guard.CRITICAL
        assert 'тратит деньги' in n.title
        assert '$0.0152' in n.detail and '$0.0252' in n.detail
        assert plan.new_config is None      # usage alone never rewrites
        assert plan.usage_delta == pytest.approx(0.01)

    def test_usage_flat_is_silent(self):
        plan = guard.decide(real_config(), make_index(), 0.0152475, 0.0152475)
        assert plan.notifications == []

    def test_growth_within_threshold_is_silent(self):
        plan = guard.decide(real_config(), make_index(), 0.0152475, 0.0152475 + 0.0009)
        assert plan.notifications == []

    def test_first_run_has_no_baseline(self):
        plan = guard.decide(real_config(), make_index(), None, 5.0)
        assert plan.notifications == []
        assert plan.usage_delta is None

    def test_usage_check_survives_models_fetch_failure(self):
        # /models down, /key up: still shout about money, decide nothing on pricing.
        plan = guard.decide(real_config(), None, 0.01, 0.05)
        assert keys(plan) == ['usage_grew']
        assert plan.primary is None
        assert plan.new_config is None
        assert plan.actions == []

    def test_models_fetch_failure_never_promotes(self):
        plan = guard.decide(real_config(), None, None, None)
        assert plan == guard.Plan()


# ---------------------------------------------------------------------------
# decide(): fallback degraded (transition-only)
# ---------------------------------------------------------------------------

class TestFallbackDegraded:
    def test_fallback_turned_paid_warns_once_on_transition(self):
        index = make_index(**{FALLBACK: 'paid'})
        first = guard.decide(real_config(), index, None, None, prev_states={})
        assert keys(first) == [f'fallback_degraded:{FALLBACK}']
        n = first.notifications[0]
        assert n.severity == guard.WARN
        assert 'ПЛАТНУЮ' in n.detail
        assert first.new_config is None       # primary is fine — hands off

        again = guard.decide(real_config(), index, None, None,
                             prev_states=first.model_states())
        assert again.notifications == []      # already known -> no nagging

    def test_fallback_vanished_warns(self):
        plan = guard.decide(real_config(), make_index(**{FALLBACK: 'missing'}), None, None,
                            prev_states={FALLBACK: 'free'})
        assert keys(plan) == [f'fallback_degraded:{FALLBACK}']
        assert 'пропала' in plan.notifications[0].title

    def test_paid_to_missing_is_a_new_transition(self):
        plan = guard.decide(real_config(), make_index(**{FALLBACK: 'missing'}), None, None,
                            prev_states={FALLBACK: 'paid'})
        assert keys(plan) == [f'fallback_degraded:{FALLBACK}']

    def test_model_states_snapshot(self):
        plan = guard.decide(real_config(), make_index(**{FALLBACK: 'paid'}), None, None)
        assert plan.model_states() == {PRIMARY: 'free', FALLBACK: 'paid'}


# ---------------------------------------------------------------------------
# dedupe
# ---------------------------------------------------------------------------

def _n(key, sev=guard.WARN):
    return guard.Notification(key=key, severity=sev, title='t', detail='d')


class TestDedupe:
    def test_same_key_within_window_suppressed(self):
        sent, notified = guard.filter_notifications([_n('a')], {}, NOW)
        assert [n.key for n in sent] == ['a']
        assert notified == {'a': NOW.isoformat()}

        later = NOW + timedelta(hours=5, minutes=59)
        sent2, notified2 = guard.filter_notifications([_n('a')], notified, later)
        assert sent2 == []
        assert notified2['a'] == NOW.isoformat()     # timestamp NOT refreshed

    def test_same_key_after_window_sent_again(self):
        notified = {'a': NOW.isoformat()}
        later = NOW + timedelta(hours=6, minutes=1)
        sent, notified2 = guard.filter_notifications([_n('a')], notified, later)
        assert [n.key for n in sent] == ['a']
        assert notified2['a'] == later.isoformat()

    def test_keys_are_independent(self):
        notified = {'a': NOW.isoformat()}
        sent, _ = guard.filter_notifications([_n('a'), _n('b', guard.CRITICAL)], notified, NOW)
        assert [n.key for n in sent] == ['b']

    def test_stale_keys_pruned(self):
        old = (NOW - timedelta(days=8)).isoformat()
        _, notified = guard.filter_notifications([], {'old': old, 'garbage': 'not-a-ts'}, NOW)
        assert 'old' not in notified
        assert 'garbage' in notified      # unparsable = keep, never crash

    def test_naive_timestamps_from_older_state_still_parse(self):
        # state.json written before tz-aware stamps existed
        notified = {'a': NOW.replace(tzinfo=None).isoformat()}
        sent, _ = guard.filter_notifications([_n('a')], notified, NOW + timedelta(hours=1))
        assert sent == []


class TestFormatMessage:
    def test_house_style(self):
        text = guard.format_message(_n('k', guard.CRITICAL), outcome='ok')
        assert text.startswith('🔥 <b>t</b>\nd')
        assert text.endswith('\nИтог: ok')
        assert guard.format_message(_n('k')).startswith('⚠️ <b>')

    def test_title_html_escaped(self):
        n = guard.Notification(key='k', severity=guard.WARN, title='a <b> & c', detail='')
        assert '&lt;b&gt; &amp; c' in guard.format_message(n)


# ---------------------------------------------------------------------------
# env parser + secrets hygiene
# ---------------------------------------------------------------------------

SECRET = 'sk-or-v1-SUPERSECRETVALUE-0123456789'
TOKEN = '123456789:AAF-telegram-bot-token-xyz'


class TestEnvParser:
    def test_dialects(self, tmp_path):
        env = tmp_path / '.env'
        env.write_text(
            '# hermes env\n'
            '\n'
            f'OPENROUTER_API_KEY={SECRET}\n'
            'HTTPS_PROXY="http://user:p#ss@proxy:3128"  \n'
            "export TOPIC_AI='42'\n"
            'FORUM_GROUP_ID=-1001234567890 # the forum\n'
            'NOT_ASSIGNMENT LINE\n'
            'IGNORED_KEY=whatever\n'
            f'BOT_TOKEN={TOKEN}\n'
        )
        got = guard.parse_env_file(str(env), ('OPENROUTER_API_KEY', 'HTTPS_PROXY',
                                              'TOPIC_AI', 'FORUM_GROUP_ID', 'BOT_TOKEN'))
        assert got == {
            'OPENROUTER_API_KEY': SECRET,
            'HTTPS_PROXY': 'http://user:p#ss@proxy:3128',   # '#' inside quotes is data
            'TOPIC_AI': '42',
            'FORUM_GROUP_ID': '-1001234567890',            # trailing comment dropped
            'BOT_TOKEN': TOKEN,
        }
        assert 'IGNORED_KEY' not in got

    def test_values_never_logged_or_printed(self, tmp_path, caplog, capsys):
        env = tmp_path / '.env'
        env.write_text(f'OPENROUTER_API_KEY={SECRET}\nBOT_TOKEN="{TOKEN}"\n')
        with caplog.at_level(logging.DEBUG):
            guard.parse_env_file(str(env), ('OPENROUTER_API_KEY', 'BOT_TOKEN'))
        out = capsys.readouterr()
        for blob in (caplog.text, out.out, out.err):
            assert SECRET not in blob
            assert TOKEN not in blob

    def test_missing_file_raises_oserror(self, tmp_path):
        with pytest.raises(OSError):
            guard.parse_env_file(str(tmp_path / 'nope'), ('X',))


class TestRedact:
    def test_registered_secrets_scrubbed_longest_first(self, monkeypatch):
        monkeypatch.setattr(guard, '_SECRETS', [])
        guard.register_secret('abc')
        guard.register_secret('abcdef')
        guard.register_secret(None)     # ignored
        guard.register_secret('')       # ignored
        msg = guard.redact('url https://api.telegram.org/botabcdef/sendMessage abc')
        assert msg == 'url https://api.telegram.org/bot***/sendMessage ***'

    def test_build_io_registers_key_token_and_proxies(self, tmp_path, monkeypatch):
        monkeypatch.setattr(guard, '_SECRETS', [])
        (tmp_path / 'h.env').write_text(f'OPENROUTER_API_KEY={SECRET}\n'
                                        'HTTPS_PROXY=http://u:pw@px:1\n')
        (tmp_path / 'b.env').write_text(f'BOT_TOKEN={TOKEN}\nFORUM_GROUP_ID=-100\n'
                                        'TOPIC_AI=7\nHTTPS_PROXY=http://u2:pw2@px:2\n')
        io = guard.build_io(str(tmp_path / 'h.env'), str(tmp_path / 'b.env'))
        assert set(guard._SECRETS) == {SECRET, TOKEN, 'http://u:pw@px:1', 'http://u2:pw2@px:2'}
        assert io.thread_id == 7 and io.chat_id == '-100'
        assert io.session.trust_env is False

    def test_build_io_without_key_is_a_guard_error(self, tmp_path):
        (tmp_path / 'h.env').write_text('HTTPS_PROXY=x\n')
        (tmp_path / 'b.env').write_text('')
        with pytest.raises(guard.GuardError):
            guard.build_io(str(tmp_path / 'h.env'), str(tmp_path / 'b.env'))


# ---------------------------------------------------------------------------
# run_once: the wrapper with a fake GuardIO
# ---------------------------------------------------------------------------

class FakeIO:
    def __init__(self, usage=0.0152475, index=None, now=NOW):
        self.usage = usage
        self.index = index if index is not None else make_index()
        self._now = now
        self.restarts = 0
        self.restart_ok = True
        self.sent = []
        self.send_ok = True
        self.inflight = 0

    def now(self):
        return self._now

    def inflight_requests(self):
        return self.inflight

    def fetch_usage(self):
        return self.usage

    def fetch_models(self):
        return guard.sanitize_models_index(self.index)

    def restart_hermes(self):
        self.restarts += 1
        return (True, '') if self.restart_ok else (False, 'Job failed. See journalctl')

    def send_telegram(self, text):
        self.sent.append(text)
        return self.send_ok


@pytest.fixture
def host(tmp_path):
    cfg = tmp_path / 'config.yaml'
    cfg.write_text(REAL_CONFIG_TEXT)
    os.chmod(cfg, 0o600)        # like entry:/root/.hermes/config.yaml
    return {'config': str(cfg), 'state': str(tmp_path / 'state')}


def state_of(host):
    with open(os.path.join(host['state'], 'state.json')) as fh:
        return json.load(fh)


class TestRunOnce:
    def test_all_free_writes_baseline_only(self, host):
        io = FakeIO()
        assert guard.run_once(io, host['config'], host['state'], dry_run=False) == 0
        assert io.sent == [] and io.restarts == 0
        st = state_of(host)
        assert st['last_usage'] == pytest.approx(0.0152475)
        assert st['model_states'] == {PRIMARY: 'free', FALLBACK: 'free'}
        assert st['api_failures'] == 0
        assert open(host['config']).read() == REAL_CONFIG_TEXT   # untouched

    def test_paid_primary_promotes_on_disk_restarts_and_notifies(self, host, tmp_path):
        io = FakeIO(index=make_index(**{PRIMARY: 'paid'}))
        rc = guard.run_once(io, host['config'], host['state'], dry_run=False)
        assert rc == 1     # paid primary = balance may have moved, flag it

        new = yaml.safe_load(open(host['config']).read())
        assert new['model']['default'] == FALLBACK
        assert new['fallback_providers'] == []          # paid old primary removed
        assert new['max_turns'] == 90 and new['approvals'] == {'mode': 'smart'}
        assert new['terminal']['working_dir'] == '/root/hermes-work'
        assert oct(os.stat(host['config']).st_mode & 0o777) == oct(0o600)   # mode kept

        backups = sorted(p for p in os.listdir(tmp_path) if p.startswith('config.yaml.bak-'))
        assert backups == ['config.yaml.bak-20260905-100000']
        assert (tmp_path / backups[0]).read_text() == REAL_CONFIG_TEXT  # comments intact
        assert not (tmp_path / 'config.yaml.tmp').exists()

        assert io.restarts == 1
        [msg] = io.sent
        assert msg.startswith('🔥 <b>')
        assert PRIMARY in msg and FALLBACK in msg
        assert 'prompt=0.0000003' in msg and 'prompt=0 completion=0' in msg
        assert 'config.yaml.bak-20260905-100000' in msg
        assert 'перезапущен' in msg
        assert 'УБРАНА' in msg

        st = state_of(host)
        assert f'promoted:{PRIMARY}->{FALLBACK}' in st['notified']
        assert st['pending'] == [] and st['restart_pending'] is None

    def test_second_run_after_promotion_is_quiet(self, host):
        index = make_index(**{PRIMARY: 'paid'})
        guard.run_once(FakeIO(index=index), host['config'], host['state'], dry_run=False)
        # Next tick: new primary is free, the old one is gone from the
        # chain -> nothing to say. Silence, not a 6-hourly nag.
        io2 = FakeIO(index=index, now=NOW + timedelta(minutes=30))
        assert guard.run_once(io2, host['config'], host['state'], dry_run=False) == 0
        assert io2.restarts == 0
        assert io2.sent == []
        assert state_of(host)['model_states'] == {FALLBACK: 'free'}
        io3 = FakeIO(index=index, now=NOW + timedelta(hours=7))
        guard.run_once(io3, host['config'], host['state'], dry_run=False)
        assert io3.sent == []

    def test_missing_primary_two_sightings_then_promote(self, host):
        index = make_index(**{PRIMARY: 'missing'})
        io1 = FakeIO(index=index)
        assert guard.run_once(io1, host['config'], host['state'], dry_run=False) == 0
        assert io1.restarts == 0
        assert open(host['config']).read() == REAL_CONFIG_TEXT
        [msg] = io1.sent
        assert 'жду подтверждения' in msg
        io2 = FakeIO(index=index, now=NOW + timedelta(minutes=30))
        guard.run_once(io2, host['config'], host['state'], dry_run=False)
        assert io2.restarts == 1
        assert yaml.safe_load(open(host['config']).read())['model']['default'] == FALLBACK
        [msg2] = io2.sent
        assert 'пропала' in msg2 and 'переключил' in msg2

    def test_restart_failure_is_reported_not_hidden(self, host):
        io = FakeIO(index=make_index(**{PRIMARY: 'paid'}))
        io.restart_ok = False
        guard.run_once(io, host['config'], host['state'], dry_run=False)
        [msg] = io.sent
        assert 'restart hermes-api.service упал' in msg
        assert 'Job failed' in msg

    def test_failed_write_never_restarts(self, host, monkeypatch):
        # The running process is on the old config: a restart would change
        # nothing and only kill an /ai request.
        def boom(*_a, **_k):
            raise OSError(28, 'No space left on device')
        monkeypatch.setattr(guard, 'write_config', boom)
        io = FakeIO(index=make_index(**{PRIMARY: 'paid'}))
        guard.run_once(io, host['config'], host['state'], dry_run=False)
        assert io.restarts == 0
        assert open(host['config']).read() == REAL_CONFIG_TEXT
        [msg] = io.sent
        assert 'НЕ удалось' in msg and 'OSError' in msg and 'не трогал' in msg

    def test_inflight_ai_request_defers_restart_then_catches_up(self, host):
        index = make_index(**{PRIMARY: 'paid'})
        io = FakeIO(index=index)
        io.inflight = 1
        guard.run_once(io, host['config'], host['state'], dry_run=False)
        assert io.restarts == 0                       # config written, process untouched
        assert yaml.safe_load(open(host['config']).read())['model']['default'] == FALLBACK
        [msg] = io.sent
        assert 'ОТЛОЖЕН' in msg
        st = state_of(host)
        assert st['restart_pending']['deferrals'] == 1

        io2 = FakeIO(index=index, now=NOW + timedelta(minutes=30))   # inflight 0
        guard.run_once(io2, host['config'], host['state'], dry_run=False)
        assert io2.restarts == 1
        [msg2] = io2.sent
        assert 'отложенный перезапуск' in msg2 and 'выполнен' in msg2
        assert state_of(host)['restart_pending'] is None

    def test_deferral_is_bounded(self, host):
        index = make_index(**{PRIMARY: 'paid'})
        restarts = 0
        for i in range(guard.RESTART_DEFER_MAX + 1):
            io = FakeIO(index=index, now=NOW + timedelta(minutes=30 * i))
            io.inflight = 3                           # permanently busy
            guard.run_once(io, host['config'], host['state'], dry_run=False)
            restarts += io.restarts
            if i < guard.RESTART_DEFER_MAX:
                assert restarts == 0
        assert restarts == 1                          # forced on the last one
        assert state_of(host)['restart_pending'] is None

    def test_dry_run_changes_nothing_and_sends_nothing(self, host, tmp_path, capsys):
        io = FakeIO(usage=1.0, index=make_index(**{PRIMARY: 'paid'}))
        rc = guard.run_once(io, host['config'], host['state'], dry_run=True)
        assert rc == 1
        assert open(host['config']).read() == REAL_CONFIG_TEXT
        assert not [p for p in os.listdir(tmp_path) if '.bak-' in p]
        assert io.restarts == 0 and io.sent == []
        assert not os.path.exists(host['state'])       # no state.json either
        out = capsys.readouterr().out
        assert '[dry-run]' in out
        assert f'promote:{FALLBACK}' in out
        assert 'new config.yaml would be:' in out

    def test_dry_run_masks_inline_api_keys(self, host, tmp_path, capsys, monkeypatch):
        monkeypatch.setattr(guard, '_SECRETS', [])
        cfg = real_config()
        cfg['fallback_providers'][0]['api_key'] = 'sk-inline-SECRET-KEY'
        with open(host['config'], 'w') as fh:
            yaml.safe_dump(cfg, fh, sort_keys=False)
        io = FakeIO(index=make_index(**{PRIMARY: 'paid'}))
        guard.run_once(io, host['config'], host['state'], dry_run=True)
        assert 'sk-inline-SECRET-KEY' not in capsys.readouterr().out
        assert 'sk-inline-SECRET-KEY' in guard._SECRETS

    def test_usage_growth_is_critical_and_baseline_advances(self, host):
        guard.run_once(FakeIO(usage=0.0152475), host['config'], host['state'], dry_run=False)
        io = FakeIO(usage=0.0452475, now=NOW + timedelta(minutes=30))
        assert guard.run_once(io, host['config'], host['state'], dry_run=False) == 1
        [msg] = io.sent
        assert 'тратит деньги' in msg and '$0.0452' in msg
        assert state_of(host)['last_usage'] == pytest.approx(0.0452475)
        # keeps growing within 6 h -> deduped, but baseline still moves
        io2 = FakeIO(usage=0.0952475, now=NOW + timedelta(hours=1))
        guard.run_once(io2, host['config'], host['state'], dry_run=False)
        assert io2.sent == []
        assert state_of(host)['last_usage'] == pytest.approx(0.0952475)

    def test_api_down_is_blind_not_trigger_happy(self, host):
        for i in range(guard.API_FAIL_NOTIFY_AFTER):
            io = FakeIO(usage=None, index={}, now=NOW + timedelta(minutes=30 * i))
            rc = guard.run_once(io, host['config'], host['state'], dry_run=False)
            assert rc == 2
            assert io.restarts == 0
            assert open(host['config']).read() == REAL_CONFIG_TEXT
            if i < guard.API_FAIL_NOTIFY_AFTER - 1:
                assert io.sent == []
        [msg] = io.sent
        assert 'guard' in msg.lower() and 'запусков подряд' in msg
        assert state_of(host)['api_failures'] == guard.API_FAIL_NOTIFY_AFTER
        # recovery resets the counter
        io_ok = FakeIO(now=NOW + timedelta(hours=2))
        guard.run_once(io_ok, host['config'], host['state'], dry_run=False)
        assert state_of(host)['api_failures'] == 0

    def test_failed_send_is_queued_and_delivered_next_run(self, host):
        io = FakeIO(index=make_index(**{PRIMARY: 'paid', FALLBACK: 'paid'}))
        io.send_ok = False
        guard.run_once(io, host['config'], host['state'], dry_run=False)
        st = state_of(host)
        assert [p['key'] for p in st['pending']] == [
            f'fallback_degraded:{FALLBACK}', 'no_free_model']
        # the dedupe stamp stays: the queued text IS the delivery
        assert 'no_free_model' in st['notified']
        io2 = FakeIO(index=io.index, now=NOW + timedelta(minutes=30))
        guard.run_once(io2, host['config'], host['state'], dry_run=False)
        assert len(io2.sent) == 2                     # queued ones only, no duplicate
        assert 'бесплатных моделей не осталось' in io2.sent[1]
        assert state_of(host)['pending'] == []

    def test_promote_message_survives_a_dead_telegram(self, host):
        # After the promotion the primary IS free -> the next run cannot
        # regenerate the message; only the queue can deliver it.
        index = make_index(**{PRIMARY: 'paid'})
        io = FakeIO(index=index)
        io.send_ok = False
        guard.run_once(io, host['config'], host['state'], dry_run=False)
        assert io.restarts == 1
        io2 = FakeIO(index=index, now=NOW + timedelta(minutes=30))
        guard.run_once(io2, host['config'], host['state'], dry_run=False)
        [msg] = io2.sent
        assert 'переключил на бесплатную' in msg and 'перезапущен' in msg

    def test_pending_queue_is_bounded(self, host):
        os.makedirs(host['state'])
        stale = [{'key': f'k{i}', 'text': f't{i}', 'queued_at': NOW.isoformat()}
                 for i in range(guard.PENDING_MAX + 10)]
        with open(os.path.join(host['state'], 'state.json'), 'w') as fh:
            json.dump({'pending': stale}, fh)
        io = FakeIO(index=make_index(**{PRIMARY: 'paid', FALLBACK: 'paid'}))
        io.send_ok = False
        guard.run_once(io, host['config'], host['state'], dry_run=False)
        assert len(state_of(host)['pending']) == guard.PENDING_MAX

    def test_unreadable_state_starts_fresh(self, host):
        os.makedirs(host['state'])
        with open(os.path.join(host['state'], 'state.json'), 'w') as fh:
            fh.write('{not json')
        assert guard.run_once(FakeIO(), host['config'], host['state'], dry_run=False) == 0
        assert state_of(host)['last_usage'] == pytest.approx(0.0152475)

    @pytest.mark.parametrize('broken', ['model: [not: valid: yaml\n  - x', 'just a string\n',
                                        'model:\n  provider: openrouter\n'])
    def test_broken_config_is_blind_not_silent(self, host, broken):
        # Was: yaml.YAMLError escaped as a traceback / GuardError -> rc 2 and
        # nobody ever hears. Now: counted as a blind run, topic told after 3.
        with open(host['config'], 'w') as fh:
            fh.write(broken)
        for i in range(guard.API_FAIL_NOTIFY_AFTER):
            io = FakeIO(index=make_index(**{PRIMARY: 'paid'}),
                        now=NOW + timedelta(minutes=30 * i))
            assert guard.run_once(io, host['config'], host['state'], dry_run=False) == 2
            assert io.restarts == 0
        [msg] = io.sent
        assert 'config.yaml не читается' in msg
        assert open(host['config']).read() == broken            # never "repaired"
        assert state_of(host)['last_usage'] == pytest.approx(0.0152475)  # usage still tracked

    def test_broken_config_dry_run_writes_nothing(self, host):
        with open(host['config'], 'w') as fh:
            fh.write('model: [')
        assert guard.run_once(FakeIO(), host['config'], host['state'], dry_run=True) == 2
        assert not os.path.exists(host['state'])

    def test_main_crash_is_rc2_not_rc1(self, host, monkeypatch, tmp_path):
        (tmp_path / 'h.env').write_text('OPENROUTER_API_KEY=k\n')
        (tmp_path / 'b.env').write_text('')
        monkeypatch.setattr(guard, 'run_once', lambda *a, **k: 1 / 0)
        rc = guard.main(['--hermes-env', str(tmp_path / 'h.env'), '--bot-env',
                         str(tmp_path / 'b.env'), '--hermes-config', host['config'],
                         '--state-dir', host['state']])
        assert rc == 2


class TestGuardIOSecrets:
    def test_telegram_exception_with_token_in_url_is_redacted(self, caplog, monkeypatch):
        monkeypatch.setattr(guard, '_SECRETS', [])
        io = guard.GuardIO('k', None, TOKEN, '-100', '7', None)
        guard.register_secret(TOKEN)

        def boom(*_a, **_k):
            raise guard.requests.ConnectionError(
                f'HTTPSConnectionPool: Max retries exceeded with url: /bot{TOKEN}/sendMessage')
        monkeypatch.setattr(io.session, 'post', boom)
        with caplog.at_level(logging.ERROR):
            assert io.send_telegram('x') is False
        assert TOKEN not in caplog.text
        assert 'bot***/sendMessage' in caplog.text

    def test_telegram_unconfigured_does_not_crash(self, caplog):
        io = guard.GuardIO('k', None, None, None, None, None)
        assert io.send_telegram('x') is False

    def test_telegram_payload_targets_topic_not_pm(self, monkeypatch):
        io = guard.GuardIO('k', None, TOKEN, '-1001', '55', 'http://px:1')
        seen = {}

        class R:
            status_code = 200
            text = ''

        def post(url, json=None, proxies=None, timeout=None):
            seen.update(url=url, json=json, proxies=proxies)
            return R()
        monkeypatch.setattr(io.session, 'post', post)
        assert io.send_telegram('<b>hi</b>') is True
        assert seen['json']['chat_id'] == '-1001'
        assert seen['json']['message_thread_id'] == 55
        assert seen['json']['parse_mode'] == 'HTML'
        assert seen['proxies'] == {'https': 'http://px:1', 'http': 'http://px:1'}
        assert seen['url'].endswith('/sendMessage')

    def test_non_numeric_topic_does_not_crash_or_pm(self, monkeypatch):
        # int(TOPIC_AI) used to happen inside send -> a ValueError after
        # the config rewrite and before state.json was saved.
        io = guard.GuardIO('k', None, TOKEN, '-1001', 'ai-topic', None)
        assert io.thread_id is None
        seen = {}

        class R:
            status_code = 200
            text = ''
        monkeypatch.setattr(io.session, 'post', lambda url, json=None, **_k: seen.update(json) or R())
        assert io.send_telegram('x') is True
        assert seen['chat_id'] == '-1001'                 # still the forum group, never a PM
        assert 'message_thread_id' not in seen

    def test_models_fetch_error_returns_none(self, monkeypatch):
        io = guard.GuardIO('k', None, None, None, None, None)

        def boom(*_a, **_k):
            raise guard.requests.Timeout('slow')
        monkeypatch.setattr(io.session, 'get', boom)
        assert io.fetch_models() is None
        assert io.fetch_usage() is None

    def test_inflight_counts_established_lines(self, monkeypatch):
        io = guard.GuardIO('k', None, None, None, None, None)
        calls = {}

        class P:
            returncode = 0
            stdout = '0  0  127.0.0.1:4097  172.18.0.5:51234\n\n0 0 127.0.0.1:4097 172.18.0.5:51240\n'
        def run(cmd, **kw):
            calls.update(cmd=cmd, timeout=kw.get('timeout'))
            return P()
        monkeypatch.setattr(guard.subprocess, 'run', run)
        assert io.inflight_requests() == 2
        assert calls['cmd'][0] == 'ss' and f':{guard.HERMES_API_PORT}' in calls['cmd'][-1]
        assert calls['timeout']                           # every subprocess has one

    def test_inflight_failure_means_zero_not_forever_deferred(self, monkeypatch):
        io = guard.GuardIO('k', None, None, None, None, None)
        def boom(*_a, **_k):
            raise FileNotFoundError('ss')
        monkeypatch.setattr(guard.subprocess, 'run', boom)
        assert io.inflight_requests() == 0

    def test_restart_has_timeout_and_is_the_hermes_unit(self, monkeypatch):
        io = guard.GuardIO('k', None, None, None, None, None)
        calls = {}

        class P:
            returncode = 0
            stdout = stderr = ''
        def run(cmd, **kw):
            calls.update(cmd=cmd, timeout=kw.get('timeout'))
            return P()
        monkeypatch.setattr(guard.subprocess, 'run', run)
        assert io.restart_hermes() == (True, '')
        assert calls['cmd'] == ['systemctl', 'restart', 'hermes-api.service']
        assert calls['timeout']


# ---------------------------------------------------------------------------
# drift: the units and the deploy script must agree with the script
# ---------------------------------------------------------------------------

class TestDeployArtifactsDrift:
    def _read(self, rel):
        with open(os.path.join(REPO, rel)) as fh:
            return fh.read()

    def test_service_unit(self):
        unit = self._read('systemd/hermes-model-guard.service')
        assert 'Type=oneshot' in unit
        assert 'ExecStart=/usr/local/bin/hermes_model_guard.py' in unit
        assert 'StateDirectory=hermes-guard' in unit       # == STATE_DIR
        assert guard.STATE_DIR == '/var/lib/hermes-guard'
        assert 'TimeoutStartSec=' in unit                  # oneshot default is infinity
        assert 'User=' not in unit                         # root: rewrites /root/.hermes

    def test_timer_unit(self):
        timer = self._read('systemd/hermes-model-guard.timer')
        assert 'OnBootSec=5min' in timer
        assert 'OnUnitActiveSec=30min' in timer
        assert 'WantedBy=timers.target' in timer

    def test_deploy_script_ships_everything(self):
        sh = self._read('scripts/deploy_hermes_host.sh')
        for name in ('scripts/hermes_api_watchdog.sh', 'scripts/hermes_model_guard.py',
                     'systemd/hermes-api-watchdog.service', 'systemd/hermes-api-watchdog.timer',
                     'systemd/hermes-model-guard.service', 'systemd/hermes-model-guard.timer'):
            assert name in sh, name
        assert '/usr/local/bin/hermes-api-watchdog.sh' in sh
        assert '/usr/local/bin/hermes_model_guard.py' in sh
        assert 'systemctl daemon-reload' in sh
        assert 'enable --now' in sh and 'hermes-model-guard.timer' in sh
        assert 'list-timers' in sh
        assert 'set -euo pipefail' in sh
        # never clobbers a host-edited newer copy silently
        assert 'check_drift scripts/hermes_api_watchdog.sh' in sh
        assert 'check_drift scripts/hermes_model_guard.py' in sh
        assert 'FORCE' in sh
        # units land 0644 whatever the repo checkout mode is
        assert '--chmod=F0644' in sh and '--chmod=F0755' in sh
        assert '--dry-run' in sh                           # ends with a read-only plan
        assert 'py_compile' in sh                          # never ship a syntax error

    def test_deploy_script_bash_syntax(self):
        import subprocess
        r = subprocess.run(['bash', '-n', os.path.join(REPO, 'scripts/deploy_hermes_host.sh')],
                           capture_output=True, text=True, timeout=30)
        assert r.returncode == 0, r.stderr

    def test_no_import_time_logging_config(self):
        # basicConfig lives in main(); importing must not attach a root handler
        import subprocess, sys
        code = ('import importlib.util,logging,sys;'
                f's=importlib.util.spec_from_file_location("g",{_PATH!r});'
                'm=importlib.util.module_from_spec(s);s.loader.exec_module(m);'
                'sys.exit(1 if logging.getLogger().handlers else 0)')
        r = subprocess.run([sys.executable, '-c', code], capture_output=True, timeout=60)
        assert r.returncode == 0, r.stderr

    def test_script_is_executable_with_python_shebang(self):
        assert os.access(_PATH, os.X_OK)
        with open(_PATH) as fh:
            assert fh.readline().startswith('#!/usr/bin/env python3')

    def test_cli_flags(self):
        # argparse contract the systemd unit / deploy script / operator rely on
        import io as _io
        import contextlib
        buf = _io.StringIO()
        with contextlib.redirect_stdout(buf), pytest.raises(SystemExit):
            guard.main(['--help'])
        text = buf.getvalue()
        for flag in ('--dry-run', '--once', '--state-dir', '--hermes-env',
                     '--hermes-config', '--bot-env'):
            assert flag in text
