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
"""

import pytest

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
        pytest.skip(f'chromium unavailable: {e}')

    def _new_page():
        page = browser.new_page()
        page.route(
            '**/*',
            lambda route: route.abort()
            if any(h in route.request.url for h in BLOCKED)
            else route.continue_(),
        )
        page.goto(e2e_stack.dashboard_url())
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
