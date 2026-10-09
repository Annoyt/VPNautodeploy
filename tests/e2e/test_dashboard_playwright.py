"""Browser E2E smoke for the admin dashboard (Playwright + Chromium).

Pins the FRONTEND layer the python suites can't see: the 2026-08-30
dead-confirm bug (hideModal nulled the callback before it ran) left
every confirm-gated button doing nothing while 1789 unit tests stayed
green. Each test drives the real page served by the real WebAppServer
(tests/e2e/conftest.py) and asserts the persisted DB row.

Runs as a SEPARATE pytest stage (`pytest tests/e2e -q`): playwright's
sync API keeps an event loop running on the main thread, which poisons
pytest-asyncio tests collected after it — so pytest.ini norecursedirs
excludes e2e from the default `pytest tests/` run.

Skipped automatically when playwright (or its chromium) is missing:
    pip install -r requirements-dev.txt && playwright install chromium
unless E2E_REQUIRE_BROWSER=1 (CI sets it): there a missing browser is a
broken runner, and seven skips must not read as a green E2E stage.
"""

import json
import os
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlparse

import pytest

REQUIRE_BROWSER = os.environ.get('E2E_REQUIRE_BROWSER') == '1'

if REQUIRE_BROWSER:
    import playwright.sync_api as pw_sync
else:
    pw_sync = pytest.importorskip(
        'playwright.sync_api',
        reason='playwright not installed (pip install playwright; '
               'playwright install chromium)',
    )

# External CDNs referenced by index.html — blocked so the suite is
# hermetic and doesn't hang offline.
BLOCKED = ('telegram.org', 'cdn.jsdelivr.net', 'unpkg.com')


@pytest.fixture(scope='session')
def page_factory(e2e_stack):
    try:
        pw = pw_sync.sync_playwright().start()
        browser = pw.chromium.launch()
    except Exception as e:  # chromium not downloaded
        if REQUIRE_BROWSER:
            raise
        pytest.skip(f'chromium unavailable: {e}')

    def _new_page(users_tab=True):
        page = browser.new_page()
        page.route(
            '**/*',
            lambda route: route.abort()
            if any(h in route.request.url for h in BLOCKED)
            else route.continue_(),
        )
        page.goto(e2e_stack.dashboard_url())
        if users_tab:
            # Users tab; cards render after the API fetch.
            page.click('[data-tab="users"]')
            page.wait_for_selector('.user-card')
        return page

    yield _new_page
    browser.close()
    pw.stop()


def _card(page, chat_id):
    return page.locator(f'.user-card[data-chat-id="{chat_id}"]')


def _find_user_card(page, stack, chat_id):
    """Search for the seeded user so its card is on screen."""
    page.fill('#search-input', chat_id)
    page.wait_for_selector(f'.user-card[data-chat-id="{chat_id}"]')


class TestDashboardSmoke:

    def test_users_list_renders_seeded_user(self, page_factory, e2e_stack):
        cid = e2e_stack.seed_user('demo')
        page = page_factory()
        _find_user_card(page, e2e_stack, cid)
        card = _card(page, cid)
        assert 'demo' in card.inner_text().lower()
        page.close()

    def test_confirm_flow_grant_paid(self, page_factory, e2e_stack):
        """THE dead-callback regression: card button → confirm modal →
        Подтвердить → the action must actually execute."""
        cid = e2e_stack.seed_user('demo')
        page = page_factory()
        _find_user_card(page, e2e_stack, cid)
        _card(page, cid).get_by_role('button', name='⭐ Paid').click()

        modal = page.locator('#modal-overlay')
        assert not modal.get_attribute('class').count('hidden')
        page.click('#modal-confirm')

        u = e2e_stack.wait_status(cid, 'paid')
        assert u.quota_gb == 100.0
        page.close()

    def test_confirm_flow_ban(self, page_factory, e2e_stack):
        cid = e2e_stack.seed_user('demo')
        page = page_factory()
        _find_user_card(page, e2e_stack, cid)
        _card(page, cid).get_by_role('button', name='⛔ Ban').click()
        page.click('#modal-confirm')
        e2e_stack.wait_status(cid, 'banned')
        page.close()

    def test_confirm_flow_reject_persists(self, page_factory, e2e_stack):
        """Pairs with the stale-snapshot fix: the UI reject must land as
        'rejected' in the DB, not roll back to pending_demo."""
        cid = e2e_stack.seed_user('pending_demo')
        page = page_factory()
        _find_user_card(page, e2e_stack, cid)
        _card(page, cid).get_by_role('button', name='🚫 Reject').click()
        page.click('#modal-confirm')
        u = e2e_stack.wait_status(cid, 'rejected')
        assert u.reject_count == 1
        page.close()

    def test_detail_modal_grant_paid_button(self, page_factory, e2e_stack):
        """The modal-only path mobile admins use."""
        cid = e2e_stack.seed_user('demo')
        page = page_factory()
        _find_user_card(page, e2e_stack, cid)
        _card(page, cid).click()
        page.wait_for_selector('#detail-grant-paid')
        page.click('#detail-grant-paid')
        page.click('#modal-confirm')
        e2e_stack.wait_status(cid, 'paid')
        page.close()

    def test_detail_modal_set_quota(self, page_factory, e2e_stack):
        """Direct-apiPost path (no confirm modal)."""
        cid = e2e_stack.seed_user('demo')
        page = page_factory()
        _find_user_card(page, e2e_stack, cid)
        _card(page, cid).click()
        page.wait_for_selector('#edit-quota')
        page.fill('#edit-quota', '42')
        page.click('[data-edit-action="set_quota"]')
        deadline_ok = False
        import time
        for _ in range(25):
            if e2e_stack.db.get_user(cid).quota_gb == 42.0:
                deadline_ok = True
                break
            time.sleep(0.2)
        assert deadline_ok, 'set_quota never persisted'
        page.close()


class TestSignalsFailureReports:

    def test_report_row_shows_last_traffic_and_sub_fetch(self, page_factory, e2e_stack):
        """The Signals triage row prints both stored facts. Until 2026-09-30
        last_sub_fetch_ts was always NULL and never rendered, and
        last_traffic_ts held the traffic mirror's clock, not the user's."""
        cid = e2e_stack.seed_user('paid')
        with e2e_stack.db._connect() as conn:
            rid = conn.execute(
                "INSERT INTO user_failure_reports (chat_id, country, asn, "
                " last_sub_fetch_ts, last_traffic_ts, target_domain) "
                "VALUES (?, 'RU', 'AS31133', '2026-09-26 08:18:18', "
                "'2026-09-30 15:27:20', 'nothing_loads')", (cid,)).lastrowid
            conn.commit()
        page = page_factory()
        page.click('[data-tab="signals"]')
        row = page.locator('#signals-reports-list .alert-row', has_text=f'#{rid} ')
        row.wait_for()
        text = row.inner_text()
        assert 'Последний трафик: 2026-09-30 15:27:20' in text
        assert '/sub: 2026-09-26 08:18:18' in text
        page.close()


# ------------------------------------------ Signals: clients by operator ----

def _ch_cell(alive, dead):
    n = alive + dead
    return {'alive': alive, 'dead': dead, 'rate': alive / n if n else None,
            'last_ts': '2026-10-10 11:58:00' if alive else None}


# What the colour test is served: a cell on each side of both edges, a 0
# that must read red (not "no data") and an empty cell that must read grey.
CLIENT_HEALTH = {
    'hours': 24, 'since': '2026-10-09 12:00:00',
    'protocols': ['reality', 'hy2', 'ws', 'stls', 'de'],
    'rows': [
        {'asn': 'AS31133', 'country': 'RU', 'clients': 120, 'probed': 100,
         'last_ts': '2026-10-10 11:58:00',
         'protocols': {
             'reality': _ch_cell(3, 1),     # 0.75 — green, the edge
             'hy2': _ch_cell(74, 26),       # 0.74 — yellow
             'ws': _ch_cell(1, 3),          # 0.25 — yellow, the edge
             'stls': _ch_cell(24, 76),      # 0.24 — red
             'de': _ch_cell(0, 0),          # nobody answered — grey
         }},
        {'asn': None, 'country': 'KZ', 'clients': 3, 'probed': 2,
         'last_ts': '2026-10-10 10:00:00',
         'protocols': {
             'reality': _ch_cell(0, 2),     # 0 — red
             'hy2': _ch_cell(2, 0),         # 1 — green
             'ws': _ch_cell(0, 0), 'stls': _ch_cell(0, 0), 'de': _ch_cell(0, 0),
         }},
    ],
    'rows_total': 2,
    'total': {'clients': 123, 'probed': 102, 'last_ts': '2026-10-10 11:58:00',
              'protocols': {
                  'reality': _ch_cell(3, 3), 'hy2': _ch_cell(76, 26),
                  'ws': _ch_cell(1, 3), 'stls': _ch_cell(24, 76),
                  'de': _ch_cell(0, 0)}},
    'truncated': False,
}


def _mock_client_health(page, answer, hold=()):
    """Serve /api/admin/client_health from ``answer(hours)``. Registered
    after page_factory's catch-all, so it wins (Playwright runs the newest
    matching route first). Requests for a window in ``hold`` are parked in
    the returned ``held`` list instead of answered. Returns (asked, held):
    the requested windows in order, the parked routes."""
    asked, held = [], []

    def handle(route):
        hours = parse_qs(urlparse(route.request.url).query).get('hours', [None])[0]
        asked.append(hours)
        if hours in hold:
            held.append(route)
            return
        route.fulfill(status=200, content_type='application/json',
                      body=json.dumps(answer(hours)))

    page.route(lambda url: '/api/admin/client_health' in url, handle)
    return asked, held


def _ch_row(page, asn):
    return page.locator(f'#signals-clients tr[data-asn="{asn}"]')


def _ch_band(page, asn, proto):
    cls = _ch_row(page, asn).locator(f'td[data-proto="{proto}"] .heat-cell') \
        .get_attribute('class').split()
    return next(c for c in cls if c != 'heat-cell')


def _per_window(hours):
    """Each window paints its own operator, so the table shows which
    answer it rendered."""
    data = json.loads(json.dumps(CLIENT_HEALTH))
    data['hours'] = int(hours)
    data['rows'][0]['asn'] = f'AS{hours}'
    return data


class TestSignalsClientHealth:

    def test_renders_and_colours_follow_rate(self, page_factory):
        page = page_factory(users_tab=False)
        asked, _ = _mock_client_health(page, lambda hours: CLIENT_HEALTH)
        page.click('[data-tab="signals"]')
        _ch_row(page, 'AS31133').wait_for()
        assert asked == ['24']                   # the selected default window
        heads = page.locator('#signals-clients thead th').all_inner_texts()
        assert heads[3:8] == ['REALITY', 'HY2', 'WS', 'STLS', 'DE']
        expected = {
            ('AS31133', 'reality'): 'heat-good',
            ('AS31133', 'hy2'): 'heat-warn',
            ('AS31133', 'ws'): 'heat-warn',
            ('AS31133', 'stls'): 'heat-bad',
            ('AS31133', 'de'): 'heat-none',
            ('?', 'reality'): 'heat-bad',
            ('?', 'hy2'): 'heat-good',
            ('?', 'ws'): 'heat-none',
            ('*', 'reality'): 'heat-warn',
            ('*', 'stls'): 'heat-bad',
        }
        assert {k: _ch_band(page, *k) for k in expected} == expected
        reality = _ch_row(page, 'AS31133').locator('td[data-proto="reality"]').inner_text()
        assert '75%' in reality and '3/4' in reality
        rows = page.locator('#signals-clients tbody tr')
        assert rows.nth(0).get_attribute('data-asn') == '*'       # the total first
        assert 'Σ все операторы' in rows.nth(0).inner_text()
        assert 'неизвестно' in _ch_row(page, '?').inner_text()
        page.close()

    def test_window_switch_asks_for_that_window(self, page_factory):
        from bot.services.client_health import WINDOWS_H
        page = page_factory(users_tab=False)
        asked, _ = _mock_client_health(page, _per_window)
        page.click('[data-tab="signals"]')
        _ch_row(page, 'AS24').wait_for()
        values = page.locator('#signals-clients-hours option').evaluate_all(
            'os => os.map(o => o.value)')
        assert values == [str(h) for h in WINDOWS_H]   # what the API accepts
        page.select_option('#signals-clients-hours', '168')
        _ch_row(page, 'AS168').wait_for()
        page.select_option('#signals-clients-hours', '1')
        _ch_row(page, 'AS1').wait_for()
        assert _ch_row(page, 'AS168').count() == 0
        with page.expect_response(lambda r: '/api/admin/client_health' in r.url):
            page.click('#signals-clients-reload')        # ⟳ re-asks the same window
        assert asked == ['24', '168', '1', '1']
        page.close()

    def test_late_answer_for_an_old_window_is_dropped(self, page_factory):
        page = page_factory(users_tab=False)
        asked, held = _mock_client_health(page, _per_window, hold=('24',))
        page.click('[data-tab="signals"]')
        page.wait_for_timeout(200)
        page.select_option('#signals-clients-hours', '168')
        _ch_row(page, 'AS168').wait_for()
        assert len(held) == 1
        held[0].fulfill(status=200, content_type='application/json',
                        body=json.dumps(_per_window('24')))
        page.wait_for_timeout(500)
        assert _ch_row(page, 'AS24').count() == 0
        assert _ch_row(page, 'AS168').count() == 1
        page.close()

    def test_real_endpoint_end_to_end(self, page_factory, e2e_stack):
        """No mock: client_probe rows → the real endpoint → the table. Pins
        the contract between the API's shape and the renderer."""
        now = datetime.now(timezone.utc)

        def ago(**d):
            return (now - timedelta(**d)).strftime('%Y-%m-%d %H:%M:%S')

        kw = dict(last_asn='AS64501', last_country='RU')
        a = e2e_stack.seed_user('paid', **kw)
        b = e2e_stack.seed_user('paid', **kw)
        c = e2e_stack.seed_user('demo', **kw)
        with e2e_stack.db._connect() as conn:
            conn.executemany(
                "INSERT INTO client_probe (chat_id, grp, ts) VALUES (?, ?, ?)",
                [(a, 'p-reality', ago(minutes=10)), (a, 'p-ws', ago(minutes=10)),
                 (b, 'p-ws', ago(minutes=20)), (b, 'emergency', ago(minutes=5)),
                 (c, 'p-ws', ago(minutes=30))])
            conn.commit()
        page = page_factory(users_tab=False)
        page.click('[data-tab="signals"]')
        _ch_row(page, 'AS64501').wait_for()
        # reality: a alive, b dead, c is demo (not offered); ws: all three
        assert _ch_band(page, 'AS64501', 'reality') == 'heat-warn'
        assert '1/2' in _ch_row(page, 'AS64501').locator('td[data-proto="reality"]').inner_text()
        assert _ch_band(page, 'AS64501', 'ws') == 'heat-good'
        assert '3/3' in _ch_row(page, 'AS64501').locator('td[data-proto="ws"]').inner_text()
        heads = page.locator('#signals-clients thead th').all_inner_texts()
        assert 'EMERGENCY' not in heads and 'REALITY' in heads
        page.close()
