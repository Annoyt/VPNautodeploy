# §46. The NekoVPN app's quota card: the `nekovpn-plan` header and `/start buy` (2026-11-01)

The app shows a card above the dashboard (client `lib/neko/quota.dart`): used / total, what the allowance means, and «учитывается ТОЛЬКО трафик вне РФ». It warns at 80% and at 100%, and offers «Купить подписку».

- **`nekovpn-plan`**, on every Clash-format profile next to `nekovpn-mirrors` (`demo_tiers.plan_header`):
  - `plan=paid` for a payer: status `paid`, or a payer's open ticket, told apart by the paid-until date or `previous_state='paid'`;
  - `plan=demo; app=<DEMO_TRAFFIC_GB>; other=<DEMO_OTHER_TRAFFIC_GB>` for everyone else.

  The bytes stay in `subscription-userinfo`, and the paid-until date is its `expire`. The sing-box profile does not carry the header.
- **`t.me/<bot>?start=buy`** (`/start buy`) opens the `/buy` plan menu. The card links to it when the traffic runs out or the paid period ends.
- **Tests:** `TestPlanHeader` and `TestStartBuy` in `tests/integration/test_demo_tiers.py`; 5 mutants killed.
