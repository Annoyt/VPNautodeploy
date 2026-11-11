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
from unittest.mock import Mock
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

    def _new_page(users_tab=True, url=None):
        page = browser.new_page()
        page.route(
            '**/*',
            lambda route: route.abort()
            if any(h in route.request.url for h in BLOCKED)
            else route.continue_(),
        )
        page.goto(url or e2e_stack.dashboard_url())
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


# ------------------------------- Settings saves, reminder sends, alert ack ----
#
# Until 2026-11-11 all five went through apiFetch(path), which took the path
# alone and always sent a GET: the {method: 'POST', ...} its callers passed
# was dropped. The plans and reminders saves read the current values back and
# printed «✅ сохранено» with nothing stored; the manual reminder sends and the
# alert ack got 405. Each test drives the button and reads the database.

def _open_tab(page_factory, tab, ready):
    page = page_factory(users_tab=False)
    page.click(f'[data-tab="{tab}"]')
    page.wait_for_selector(ready)
    return page


def _record_requests(page, path):
    """Every request the page makes to ``path`` from now on, as (method, JSON
    body or None), in order. The query string (the auth) is ignored."""
    seen = []

    def on_request(request):
        if urlparse(request.url).path == path:
            body = request.post_data
            seen.append((request.method, json.loads(body) if body else None))

    page.on('request', on_request)
    return seen


def _wait_until(page, js, arg=None):
    page.wait_for_function(js, arg=arg, timeout=5000)


def _editor_reloaded(page, editor):
    """Wait until ``editor`` has no edited row left: the page re-reads the
    server once a save has answered, and the re-render drops the marks.
    A failed save keeps them, so this times out on one."""
    _wait_until(page, 'sel => !document.querySelector(sel)', f'{editor} .dirty')


def _settled_status(page, selector):
    """(text, class) of a status line once its request has answered: ✅ is
    the page's success, ⚠ an error it caught; «отправляю…» is neither."""
    _wait_until(page, """sel => {
        const t = document.querySelector(sel).textContent;
        return t.startsWith('✅') || t.startsWith('⚠');
    }""", selector)
    status = page.locator(selector)
    return status.text_content(), status.get_attribute('class')


def _clear_settings(db, keys):
    with db._connect() as conn:
        conn.executemany("DELETE FROM app_settings WHERE key = ?",
                         [(k,) for k in keys])
        conn.commit()


def _plan_keys():
    from bot.handlers.payments import PLAN_DEFINITIONS, _setting_key
    return {months: _setting_key(months) for months, _, _ in PLAN_DEFINITIONS}


def _stored_plan_prices(db):
    """{months: the stored price or None}: what /buy reads before env and
    the factory price."""
    return {m: db.get_setting(key) for m, key in _plan_keys().items()}


def _store_plan_prices(db, prices):
    """Exactly ``prices`` ({months: stars}) stored, every other plan unset."""
    _clear_settings(db, _plan_keys().values())
    for months, stars in prices.items():
        db.set_setting(_plan_keys()[months], str(stars))


def _plan_row(page, months):
    return page.locator(f'#plans-editor .plan-row[data-months="{months}"]')


def _reminder_row(page, key):
    return page.locator(f'#reminders-editor .reminder-row[data-key="{key}"]')


def _notification_log(db, chat_id):
    with db._connect() as conn:
        return [r[0] for r in conn.execute(
            "SELECT notification_type FROM notification_log "
            "WHERE chat_id = ? ORDER BY id", (chat_id,))]


def _admin_actions(db, action):
    """(admin_id, target_id, details) of every ``action`` row, oldest first."""
    with db._connect() as conn:
        return [tuple(r) for r in conn.execute(
            "SELECT admin_id, target_id, details FROM admin_actions "
            "WHERE action = ? ORDER BY id", (action,))]


@pytest.fixture
def notifications(e2e_stack):
    """A real NotificationService on the stack's sqlite for one test, with
    Telegram faked by a Mock bot: its send_message calls are the sends. The
    stack's own service is a bare Mock, which GET /api/admin/reminders cannot
    serialize (500); it is put back afterwards."""
    from bot.services.notifications import NotificationService
    server = e2e_stack.server
    saved = server.notification_service
    service = NotificationService(Mock(), e2e_stack.db, server.config)
    server.notification_service = service
    yield service
    server.notification_service = saved


def _sent_to(service):
    return [c.kwargs['chat_id'] for c in service.bot.send_message.call_args_list]


class TestPlansSave:

    def test_stores_the_edited_price_and_nothing_else(self, page_factory, e2e_stack):
        db = e2e_stack.db
        _store_plan_prices(db, {12: 999})                    # an earlier edit
        page = _open_tab(page_factory, 'settings', '#plans-editor .plan-row')
        sent = _record_requests(page, '/api/admin/plans')
        _plan_row(page, 3).locator('.plan-input').fill('333')
        page.click('#plans-save')
        _editor_reloaded(page, '#plans-editor')
        # The server's shape, with the edited row only: a save never wipes
        # (or rewrites) the plans nobody touched.
        assert sent == [('POST', {'prices': {'3': 333}}), ('GET', None)]
        assert _stored_plan_prices(db) == {1: None, 3: '333', 6: None, 12: '999'}
        assert _plan_row(page, 3).get_attribute('data-original') == '333'
        assert _plan_row(page, 12).get_attribute('data-original') == '999'
        page.close()

    def test_reset_drops_the_stored_price(self, page_factory, e2e_stack):
        from bot.handlers.payments import PLAN_DEFINITIONS
        factory = {months: stars for months, _, stars in PLAN_DEFINITIONS}
        db = e2e_stack.db
        _store_plan_prices(db, {6: 555, 12: 999})
        page = _open_tab(page_factory, 'settings', '#plans-editor .plan-row')
        sent = _record_requests(page, '/api/admin/plans')
        _plan_row(page, 6).locator('.plan-reset').click()
        page.click('#plans-save')
        _editor_reloaded(page, '#plans-editor')
        assert sent == [('POST', {'prices': {'6': None}}), ('GET', None)]
        assert _stored_plan_prices(db) == {1: None, 3: None, 6: None, 12: '999'}
        assert _plan_row(page, 6).get_attribute('data-original') == str(factory[6])
        page.close()


class TestReminders:

    def test_save_stores_the_edited_settings(self, page_factory, e2e_stack,
                                             notifications):
        db = e2e_stack.db
        keys = list(notifications.REMINDER_DEFAULTS)
        _clear_settings(db, keys)
        page = _open_tab(page_factory, 'settings', '#reminders-editor .reminder-row')
        sent = _record_requests(page, '/api/admin/reminders')
        _reminder_row(page, 'reminder_max_per_user').locator('input').fill('5')
        _reminder_row(page, 'reminder_text_new_ru').locator('textarea').fill(
            'Привет из e2e')
        page.click('#reminders-save')
        _editor_reloaded(page, '#reminders-editor')
        edits = {'reminder_max_per_user': 5, 'reminder_text_new_ru': 'Привет из e2e'}
        assert sent == [('POST', edits), ('GET', None)]
        assert {k: db.get_setting(k) for k in keys} == {
            **{k: None for k in keys},
            'reminder_max_per_user': '5', 'reminder_text_new_ru': 'Привет из e2e'}
        # what the reminder job reads, and what the editor shows now
        assert notifications.get_reminder_setting('reminder_max_per_user') == 5
        assert _reminder_row(page, 'reminder_max_per_user').get_attribute(
            'data-original') == '5'
        assert _reminder_row(page, 'reminder_text_new_ru').locator(
            'textarea').input_value() == 'Привет из e2e'
        _clear_settings(db, keys)
        page.close()

    def test_cohort_send_reminds_every_stuck_user(self, page_factory, e2e_stack,
                                                  notifications):
        db = e2e_stack.db
        cid = e2e_stack.seed_user('platform_select')
        with db._connect() as conn:
            cohort = {r[0] for r in conn.execute(
                "SELECT chat_id FROM users WHERE status = 'platform_select' "
                "AND username IS NOT NULL AND username != ''")}
        page = _open_tab(page_factory, 'settings', '#reminders-editor .reminder-row')
        page.select_option('#manual-cohort', 'platform_select')
        page.once('dialog', lambda dialog: dialog.accept())   # «Отправить …?»
        page.click('#manual-cohort-send')
        assert _settled_status(page, '#manual-status') == (
            f'✅ отправлено: {len(cohort)}', 'plans-status ok')
        assert sorted(_sent_to(notifications)) == sorted(cohort)
        assert _notification_log(db, cid) == ['reminder_platform_select']
        assert _admin_actions(db, 'reminders_manual_cohort')[-1] == (
            e2e_stack.admin_id, None,
            f'cohort=platform_select, force=True, sent={len(cohort)}')
        page.close()

    def test_user_send_reminds_that_user(self, page_factory, e2e_stack,
                                         notifications):
        db = e2e_stack.db
        cid = e2e_stack.seed_user('new')
        page = _open_tab(page_factory, 'settings', '#reminders-editor .reminder-row')
        page.fill('#manual-chat-id', cid)
        page.click('#manual-user-send')
        assert _settled_status(page, '#manual-status') == (
            '✅ отправлено', 'plans-status ok')
        assert _sent_to(notifications) == [cid]
        assert _notification_log(db, cid) == ['reminder_new']
        assert _admin_actions(db, 'reminders_manual_user')[-1] == (
            e2e_stack.admin_id, cid, 'force=True, ok=True')
        page.close()

    def test_user_send_shows_the_servers_refusal(self, page_factory, e2e_stack,
                                                 notifications):
        """A user who is not stuck in NEW / PLATFORM_SELECT gets nothing, and
        the page prints the server's reason (its 400's {"error": ...}), not
        «HTTP 400»."""
        db = e2e_stack.db
        cid = e2e_stack.seed_user('demo')
        page = _open_tab(page_factory, 'settings', '#reminders-editor .reminder-row')
        page.fill('#manual-chat-id', cid)
        page.click('#manual-user-send')
        assert _settled_status(page, '#manual-status') == (
            '⚠️ user not stuck in NEW/PLATFORM_SELECT or send failed',
            'plans-status err')
        assert _sent_to(notifications) == []
        assert _notification_log(db, cid) == []
        assert _admin_actions(db, 'reminders_manual_user')[-1] == (
            e2e_stack.admin_id, cid, 'force=True, ok=False')
        page.close()


class TestAlertAck:

    def test_ack_button_acks_the_alert(self, page_factory, e2e_stack):
        db = e2e_stack.db
        with db._connect() as conn:
            aid = conn.execute(
                "INSERT INTO alert_history (key, severity, title, detail) "
                "VALUES ('e2e:ack', 'critical', 'E2E: ack me', 'detail')",
            ).lastrowid
            conn.commit()
        button = f'#alerts-list .btn-ack[data-id="{aid}"]'
        page = _open_tab(page_factory, 'alerts', button)
        sent = _record_requests(page, f'/api/admin/alerts/{aid}/ack')
        page.click(button)
        # The list re-reads the server once the ack has answered and drops
        # the row; a failed ack leaves its error on the button instead.
        _wait_until(page, """sel => {
            const b = document.querySelector(sel);
            return !b || b.textContent.startsWith('⚠');
        }""", button)
        assert page.locator(button).count() == 0, page.locator(button).text_content()
        assert sent == [('POST', None)]
        with db._connect() as conn:
            acked_at, acked_by = conn.execute(
                "SELECT acked_at, acked_by FROM alert_history WHERE id = ?",
                (aid,)).fetchone()
        assert acked_at is not None
        assert acked_by == e2e_stack.admin_id
        page.close()


# ---------------------------------------------------- the agent's replies ----
#
# alert_history.kimi_analysis and dpi_reports.kimi_analysis hold the agent's
# free reply: plain text from the protocol prompt, HTML from the DPI prompts.
# Until 2026-11-11 the Alerts tab put it into innerHTML as it came, and the
# dashboard URL carries the admin token: markup the model echoed from a log
# line or a user's text ran in the admin's session.

# What the old render ran: the image fails to load and its onerror fires.
XSS_IMG = '<img src="e2e-missing.png" onerror="window.__xss = \'img\'">'


def _seed_alert_reply(db, title, reply):
    with db._connect() as conn:
        conn.execute(
            "INSERT INTO alert_history (key, severity, title, detail, kimi_analysis) "
            "VALUES ('e2e:agent', 'warn', ?, 'detail', ?)", (title, reply))
        conn.commit()


def _alert_reply(page, title):
    """The «🤖» block under the alert titled ``title``."""
    row = page.locator('#alerts-list .alert-row',
                       has=page.locator('.alert-title', has_text=title))
    return row.locator('.alert-kimi')


def _reply_attributes(box):
    """Sorted «tag[attribute]» of every element inside a reply block, the
    page's own wrappers (the «🤖» head, the reply div) left out."""
    return sorted(set(box.evaluate("""el => [...el.querySelectorAll('*')]
        .filter(n => !n.matches('.alert-kimi-head, .agent-reply'))
        .flatMap(n => [...n.attributes].map(a => `${n.localName}[${a.name}]`))""")))


def _nothing_ran(page):
    """No payload ran: none set window.__xss, and no <style> got in."""
    page.wait_for_timeout(500)      # a failed image load has fired by now
    assert page.evaluate('window.__xss') is None
    assert page.evaluate(
        "getComputedStyle(document.body).getPropertyValue('--e2e-styled')") == ''


class TestAgentReplies:

    def test_html_reply_keeps_its_formatting_and_runs_nothing(
            self, page_factory, e2e_stack):
        reply = (
            '<b>ИТОГ</b>: ws лежит 40 мин, остальные живы\n'
            '<ul><li>reality ok</li><li>hy2 ok</li></ul>'
            '<p onclick="window.__xss = \'click\'">Подробнее: '
            '<a href="https://example.com/runbook">runbook</a> · '
            '<a href="javascript:window.__xss = \'href\'">локально</a></p>'
            '<code>systemctl status hysteria</code>'
            + XSS_IMG +
            '<script>window.__xss = "script"</script>'
            '<style>body { --e2e-styled: 1 }</style>'
        )
        _seed_alert_reply(e2e_stack.db, 'E2E: agent html', reply)
        page = _open_tab(page_factory, 'alerts', '#alerts-list .alert-kimi')
        _nothing_ran(page)
        box = _alert_reply(page, 'E2E: agent html')
        assert box.locator('b').inner_text() == 'ИТОГ'
        assert box.locator('li').all_inner_texts() == ['reality ok', 'hy2 ok']
        # block markup lays itself out: the source's line break adds no gap
        assert 'остальные живы\nreality ok\nhy2 ok' in box.inner_text()
        assert box.locator('code').inner_text() == 'systemctl status hysteria'
        # an http(s) link stays a link, one that leaks no Referer (the page
        # URL carries the token); a javascript: one is its text
        assert box.locator('a').evaluate_all(
            'as => as.map(a => [a.getAttribute("href"), a.rel, a.target])'
        ) == [['https://example.com/runbook', 'noopener noreferrer', '_blank']]
        assert 'локально' in box.inner_text()
        assert box.locator('img, script, style').count() == 0
        assert 'window.__xss' not in box.inner_text()   # dropped code is not shown
        assert _reply_attributes(box) == ['a[href]', 'a[rel]', 'a[target]']
        box.locator('p').click()
        assert page.evaluate('window.__xss') is None
        page.close()

    def test_plain_text_reply_is_shown_as_written(self, page_factory, e2e_stack):
        """The protocol prompt asks for plain text, and Telegram gets it
        escaped: the page shows it the same way, angle brackets and line
        breaks included."""
        reply = ('ИТОГ: reality жив, ws лежит <40 мин>\n'
                 'СЛЕДУЮЩАЯ КОМАНДА: journalctl -u xray --since <time> | tail\n'
                 + XSS_IMG)
        _seed_alert_reply(e2e_stack.db, 'E2E: agent text', reply)
        page = _open_tab(page_factory, 'alerts', '#alerts-list .alert-kimi')
        _nothing_ran(page)
        box = _alert_reply(page, 'E2E: agent text')
        assert reply in box.inner_text()
        assert box.locator('img').count() == 0
        page.close()

    def test_dpi_report_reply_is_cleaned_too(self, page_factory, e2e_stack):
        reply = ('<b>AS31133</b>: handshake-fail ×12 к базе\n'
                 '<b>AS12389</b>: в норме' + XSS_IMG
                 + '<style>body { --e2e-styled: 1 }</style>')
        with e2e_stack.db._connect() as conn:
            conn.execute(
                "INSERT INTO dpi_reports (kind, period_start, period_end, "
                "snapshot_json, kimi_analysis) VALUES ('daily', "
                "'2026-11-09T00:00:00', '2026-11-10T00:00:00', '{}', ?)", (reply,))
            conn.commit()
        page = _open_tab(page_factory, 'alerts', '#reports-list .report-row')
        _nothing_ran(page)          # a collapsed report's markup is live too
        report = page.locator('#reports-list .report-row', has_text='2026-11-09')
        report.locator('.report-head').click()               # expand it
        box = report.locator('.alert-kimi')
        assert box.locator('b').all_inner_texts() == ['AS31133', 'AS12389']
        # Telegram-style inline HTML: its line breaks are the layout
        assert 'к базе\nAS12389: в норме' in box.inner_text()
        assert box.locator('img, style').count() == 0


# ---------------------------------------------------------------------------
# Settings → «Токен бота»: the page seals the token in a real browser and the
# bot opens it (bot/utils/token_envelope.py ↔ app.js sealBotToken).
# ---------------------------------------------------------------------------

TB_ID = 7012345678
TB_OLD = f'{TB_ID}:AAE' + 'o' * 32
TB_NEW = f'{TB_ID}:AAE' + 'n' * 32


class TokenStack:
    """Its own server: a well-formed token, a data dir, faked getMe and a
    restart that does what the new process would (read the saved token)."""

    def __init__(self, tmp_path):
        import threading
        from unittest.mock import Mock

        from bot.config import secret_store
        from bot.core.database import Database
        from bot.core.web_server import WebAppServer
        from bot.services import bot_token as bt
        from .conftest import E2E_ADMIN_ID, serve_in_thread

        self.bt, self.secret_store, self.threading = bt, secret_store, threading
        self.admin = E2E_ADMIN_ID
        self.data = str(tmp_path)
        self.db = Database(str(tmp_path / 'bot.db'))
        secret_store.write_sub_secret(self.data, TB_OLD)   # what on_startup pins
        config = Mock()
        config.BOT_TOKEN = config.BOT_TOKEN_ENV = config.SUB_TOKEN_SECRET = TB_OLD
        config.BOT_TOKEN_SOURCE = 'env'
        config.WEBAPP_URL = ''
        config.PAID_TRAFFIC_GB = 100
        config.is_admin = lambda uid: str(uid) == E2E_ADMIN_ID
        self.config = config
        self.calls, self.answers = [], {}
        self.server = WebAppServer(config, self.db, xui_service=Mock())
        self.server.notification_service = Mock()
        self.server.bot_token = self._changer()
        self.base = serve_in_thread(self.server, name='e2e-bot-token')

    def _check(self, token):
        self.calls.append(token)
        if token in self.answers:
            return self.answers[token]
        return self.bt.TokenCheck('ok', {'id': TB_ID, 'username': 'nekotest_bot'})

    def _changer(self):
        return self.bt.BotTokenChanger(self.config, self.db, check=self._check,
                                       restart=self._restart)

    def _restart(self, delay):
        def new_process():
            rec = self.secret_store.read_bot_token_record(self.data)
            self.config.BOT_TOKEN = rec['token']
            self.config.BOT_TOKEN_SOURCE = 'dashboard'
            self.server.bot_token = self._changer()
        timer = self.threading.Timer(1.0, new_process)
        timer.daemon = True
        timer.start()

    def url(self):
        from bot.utils.admin_token import make_admin_token
        return f'{self.base}/?admin_token={make_admin_token(TB_OLD, self.admin)}'

    def saved_token(self):
        rec = self.secret_store.read_bot_token_record(self.data)
        return rec and rec['token']


@pytest.fixture
def token_stack(tmp_path):
    return TokenStack(tmp_path)


def _open_token_section(page_factory, stack):
    page = page_factory(users_tab=False, url=stack.url())
    page.click('[data-tab="settings"]')
    page.wait_for_selector('#bottoken-status .bottoken-line')
    return page


class TestBotTokenSettings:

    def test_the_browser_seals_and_the_bot_switches(self, page_factory, token_stack):
        page = _open_token_section(page_factory, token_stack)
        assert '@nekotest_bot' in page.inner_text('#bottoken-status')
        page.fill('#bottoken-input', TB_NEW)
        page.click('#bottoken-apply')
        page.click('#modal-confirm')
        page.wait_for_function(
            "document.getElementById('bottoken-result').textContent.includes('новом токене')",
            timeout=20000)
        # the bot opened what Chromium sealed: getMe saw exactly the new token
        assert token_stack.calls[-1] == TB_NEW and token_stack.saved_token() == TB_NEW
        assert page.input_value('#bottoken-input') == ''
        link = parse_qs(urlparse(page.url).query)['admin_token'][0]
        from bot.utils.admin_token import verify_admin_token
        assert verify_admin_token(TB_NEW, link) == token_stack.admin
        assert 'Токен задан здесь' in page.inner_text('#bottoken-status')
        page.close()

    def test_another_bots_token_never_leaves_the_page(self, page_factory, token_stack):
        page = _open_token_section(page_factory, token_stack)
        page.fill('#bottoken-input', '7099999999:AAE' + 'x' * 32)
        page.click('#bottoken-apply')
        assert 'другого бота' in page.inner_text('#bottoken-result')
        assert page.is_hidden('#modal-overlay')
        assert token_stack.calls == [TB_OLD] and token_stack.saved_token() is None
        page.close()

    def test_a_refusal_from_telegram_is_shown(self, page_factory, token_stack):
        token_stack.answers[TB_NEW] = token_stack.bt.TokenCheck('rejected', detail='Unauthorized')
        page = _open_token_section(page_factory, token_stack)
        page.fill('#bottoken-input', TB_NEW)
        page.click('#bottoken-apply')
        page.click('#modal-confirm')
        page.wait_for_function(
            "document.getElementById('bottoken-result').textContent.includes('Telegram не принял')",
            timeout=20000)
        assert token_stack.saved_token() is None
        assert page.input_value('#bottoken-input') == TB_NEW   # left to fix a typo
        page.close()
