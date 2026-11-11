"""bot/utils/admin_token.py — the signed token in the dashboard's URL."""

import base64
import time

import pytest

from bot.utils.admin_token import make_admin_token, verify_admin_token

BOT_TOKEN = 'bot-token'


def _forge(text: str) -> str:
    """A token the way make_admin_token encodes one, around any text."""
    return base64.urlsafe_b64encode(text.encode()).rstrip(b'=').decode()


def _parts(token: str):
    return base64.urlsafe_b64decode(token + '=' * (-len(token) % 4)).decode().split(':')


def test_round_trip():
    assert verify_admin_token(BOT_TOKEN, make_admin_token(BOT_TOKEN, '42')) == '42'


def test_another_bot_token():
    assert verify_admin_token('other', make_admin_token(BOT_TOKEN, '42')) is None


def test_expired():
    assert verify_admin_token(BOT_TOKEN, make_admin_token(BOT_TOKEN, '42', ttl_seconds=-5)) is None


def test_another_admin_id_under_the_same_mac():
    _, expiry, mac = _parts(make_admin_token(BOT_TOKEN, '42'))
    assert verify_admin_token(BOT_TOKEN, _forge(f'43:{expiry}:{mac}')) is None


@pytest.mark.parametrize('mac', ['тук', 'é' * 64, '١' * 64])
def test_a_non_ascii_mac_is_refused_not_raised(mac):
    # compared as str, it made compare_digest raise: a 500 on the dashboard
    expiry = int(time.time()) + 600
    assert verify_admin_token(BOT_TOKEN, _forge(f'42:{expiry}:{mac}')) is None


@pytest.mark.parametrize('token', ['', 'garbage', '%%%', 'тук', _forge('no colons at all'),
                                   _forge('42:soon:abc')])
def test_junk(token):
    assert verify_admin_token(BOT_TOKEN, token) is None


def test_no_bot_token():
    assert verify_admin_token('', make_admin_token(BOT_TOKEN, '42')) is None
