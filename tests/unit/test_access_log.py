"""bot/utils/access_log.py: the credentials URLs carry never reach the
access log (2026-11-20), and every other line stays aiohttp's own."""

import logging
import re
from unittest.mock import Mock

import pytest
from aiohttp import web
from aiohttp.test_utils import make_mocked_request
from aiohttp.web_log import AccessLogger

from bot.utils.access_log import REDACTED, RedactingAccessLogger, redact_url

R = REDACTED


@pytest.mark.parametrize('url,expected', [
    # the dashboard's two credentials, wherever they stand in the query
    ('/api/admin/stats?admin_token=TOK', f'/api/admin/stats?admin_token={R}'),
    ('/api/admin/ws?initData=query_id%3DAA%26hash%3Dabc&admin_token=TOK',
     f'/api/admin/ws?initData={R}&admin_token={R}'),
    ('/api/admin/users?status=paid&admin_token=TOK&search=ivan',
     f'/api/admin/users?status=paid&admin_token={R}&search=ivan'),
    ('/api/admin/bot_token?link=1;admin_token=TOK', f'/api/admin/bot_token?link=1;admin_token={R}'),
    # a name aiohttp would decode to one of them
    ('/?admin%5Ftoken=TOK', f'/?admin%5Ftoken={R}'),
    ('/?ADMIN_TOKEN=TOK&InitData=X', f'/?ADMIN_TOKEN={R}&InitData={R}'),
    # a parameter given twice: both
    ('/?admin_token=A&admin_token=B', f'/?admin_token={R}&admin_token={R}'),
    # the subscription token in the path
    ('/sub/0123456789abcdef0123456789abcdef', f'/sub/{R}'),
    ('/sub/TOK?format=clash&channel=emergency', f'/sub/{R}?format=clash&channel=emergency'),
    ('/open/TOK', f'/open/{R}'),
    ('/app/TOK', f'/app/{R}'),
    ('/probe/TOK/vpn', f'/probe/{R}/vpn'),
    ('/api/neko/sos/TOK', f'/api/neko/sos/{R}'),
    ('/%73ub/TOK', f'/%73ub/{R}'),                  # aiohttp routes this to /sub
    # a Referer: a whole URL
    ('https://dash.example:9443/?admin_token=TOK', f'https://dash.example:9443/?admin_token={R}'),
    ('https://sub.example:2096/sub/TOK?format=clash', f'https://sub.example:2096/sub/{R}?format=clash'),
])
def test_a_credential_is_redacted(url, expected):
    assert redact_url(url) == expected


@pytest.mark.parametrize('url', [
    '', '-', '/', '/health', '/app', '/app/', '/app.js', '/apk?abi=arm64-v8a', '/sub/',
    '/api/admin/users?status=paid&search=ivan', '/api/admin/users/42/detail',
    '/lists/clash/ru-direct.yaml', '/probe-big', '/neko/settings',
    '/api/admin/client_health?hours=6', '/?admin_token', '/?tokens=1&initDatum=2',
    'https://dash.example:9443/', 'https://sub.example:2096/',
])
def test_a_url_without_credentials_is_left_alone(url):
    assert redact_url(url) == url


def _line(logger_cls, request, status=200):
    log = Mock(spec=logging.Logger)
    logger_cls(log, AccessLogger.LOG_FORMAT).log(request, web.Response(status=status), 0.5)
    (fmt, *args), _ = log.info.call_args
    return fmt % tuple(args) if args else fmt     # as logging formats it


def _without_time(line):
    return re.sub(r'\[[^]]+\]', '[T]', line, count=1)


def test_a_line_without_credentials_is_aiohttps_own():
    request = make_mocked_request('GET', '/api/admin/users?status=paid', headers={
        'Referer': 'https://dash.example:9443/', 'User-Agent': 'Mozilla/5.0 test'})
    ours = _line(RedactingAccessLogger, request)
    assert _without_time(ours) == _without_time(_line(AccessLogger, request))
    assert '"GET /api/admin/users?status=paid HTTP/1.1" 200' in ours


def test_the_request_line_and_the_referer_are_redacted():
    request = make_mocked_request(
        'GET', '/api/admin/stats?initData=query_id%3DAA%26hash%3Dabc&admin_token=TOKEN',
        headers={'Referer': 'https://dash.example:9443/?admin_token=TOKEN',
                 'User-Agent': 'Mozilla/5.0 test'})
    line = _line(RedactingAccessLogger, request)
    assert 'TOKEN' not in line and 'hash' not in line
    assert f'"GET /api/admin/stats?initData={R}&admin_token={R} HTTP/1.1"' in line
    assert f'"https://dash.example:9443/?admin_token={R}" "Mozilla/5.0 test"' in line


def test_no_line_while_info_is_off():
    log = logging.getLogger('test_access_log.quiet')
    log.setLevel(logging.WARNING)
    assert RedactingAccessLogger(log, AccessLogger.LOG_FORMAT).enabled is False
    log.setLevel(logging.INFO)
    assert RedactingAccessLogger(log, AccessLogger.LOG_FORMAT).enabled is True
