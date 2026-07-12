"""Self-serve subscriptions via Telegram Stars (XTR).

Why Stars
---------
- No external payment provider, no KYC, no API key dance — Telegram
  Stars are sold in-app and tradeable for our service.
- ``send_invoice(currency="XTR", provider_token="")`` is the
  documented way; ``pre_checkout_query`` + ``successful_payment`` are
  the two callbacks we need to handle.

Flow
----
1. User types ``/buy`` (or taps a button from /start). PaymentHandler
   replies with an inline keyboard listing PLANS (1 mo / 3 mo / 12 mo).
2. User taps a plan → callback ``buy_plan:<months>``.
   PaymentHandler sends an invoice with that plan's Stars price.
3. Telegram shows the payment UI. User confirms.
4. Telegram pushes ``pre_checkout_query`` to the bot. We answer
   ``ok=True`` (no inventory / fraud checks for now).
5. Telegram pushes ``successful_payment`` (as a regular message
   field). We extract the payload, look up the user, bump their
   subscription_expiry by ``months × 30 days``, log the payment.

Config
------
Prices come from env vars (defaults in PLANS below) — operator can
override without code changes::

    PLAN_1M_STARS=100
    PLAN_3M_STARS=270
    PLAN_6M_STARS=500
    PLAN_12M_STARS=900
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta

from bot.config.constants import BYTES_PER_GB
from bot.handlers.base import BaseHandler

logger = logging.getLogger(__name__)


# (months, label, factory_default_stars)
# Resolution order at runtime:
#   1. app_settings table  (dashboard-editable, persistent)
#   2. env var PLAN_<N>M_STARS  (deploy-time override)
#   3. factory default from this list  (last resort)
# The label is always rendered from this list (admin can't rename plans
# from the dashboard, only set the price).
PLAN_DEFINITIONS = [
    (1,  '1 месяц',     100),
    (3,  '3 месяца',    270),
    (6,  '6 месяцев',   500),
    (12, '12 месяцев',  900),
]


def _setting_key(months: int) -> str:
    return f"plan_{months}m_stars"


def _env_key(months: int) -> str:
    return f"PLAN_{months}M_STARS"


def get_active_plans(db) -> list[tuple[int, str, int]]:
    """Return [(months, label, stars)] using DB → env → factory order.

    Called on every /buy and every plan tap, so any dashboard edit is
    picked up immediately without a bot restart. DB read is one SELECT
    per plan — negligible.
    """
    out: list[tuple[int, str, int]] = []
    for months, label, factory in PLAN_DEFINITIONS:
        price = None
        try:
            raw = db.get_setting(_setting_key(months)) if db else None
            if raw is not None:
                price = int(raw)
        except (ValueError, TypeError):
            price = None
        if price is None:
            env = os.environ.get(_env_key(months))
            if env:
                try:
                    price = int(env)
                except ValueError:
                    price = None
        if price is None:
            price = factory
        out.append((months, label, price))
    return out


class PaymentHandler(BaseHandler):
    """Processes /buy commands, plan-pick callbacks, and the two
    Telegram Stars payment events.
    """

    CALLBACK_PREFIX = 'buy_plan:'
    # Open-the-menu callback. Wired from notify_main_menu's
    # "💳 Купить подписку" button so DEMO users can reach /buy
    # without typing — they just tap.
    CALLBACK_MENU = 'buy_menu'

    # ----- routing -----

    def can_handle(self, update: dict) -> bool:
        # /buy command in a regular message
        msg = update.get('message') or {}
        text = msg.get('text', '') or ''
        if text.startswith('/buy'):
            return True
        # successful_payment field on a message
        if msg.get('successful_payment'):
            return True
        # pre_checkout_query — top-level update field
        if update.get('pre_checkout_query'):
            return True
        # buy_plan:<n> / buy_menu callbacks
        cb = update.get('callback_query') or {}
        data = cb.get('data') or ''
        if data.startswith(self.CALLBACK_PREFIX) or data == self.CALLBACK_MENU:
            return True
        return False

    def handle(self, update: dict) -> None:
        msg = update.get('message') or {}
        if msg.get('successful_payment'):
            self._on_successful_payment(msg)
            return
        if update.get('pre_checkout_query'):
            self._on_pre_checkout(update['pre_checkout_query'])
            return
        cb = update.get('callback_query')
        if cb:
            data = cb.get('data') or ''
            if data.startswith(self.CALLBACK_PREFIX):
                self._on_plan_selected(cb)
                return
            if data == self.CALLBACK_MENU:
                chat_id = str((cb.get('from') or {}).get('id') or '')
                if chat_id:
                    self._show_plan_menu(chat_id)
                cb_id = cb.get('id')
                if cb_id:
                    try:
                        self.bot.answer_callback_query(cb_id)
                    except Exception:
                        pass
                return
        if (msg.get('text') or '').startswith('/buy'):
            chat_id = str((msg.get('chat') or {}).get('id') or '')
            if chat_id:
                self._show_plan_menu(chat_id)
            return

    # ----- /buy → plan menu -----

    def _show_plan_menu(self, chat_id: str) -> None:
        if not chat_id:
            return
        rows = []
        for months, label, stars in get_active_plans(self.db):
            # Примерный курс: 1 Star ≈ 1.5₽
            rub_price = stars * 1.5
            rows.append([{
                'text': f"{label} — ⭐ {stars} (~{int(rub_price)}₽)",
                'callback_data': f"{self.CALLBACK_PREFIX}{months}",
            }])
        self.bot.send_message(
            chat_id=chat_id,
            text=(
                "💳 <b>Подписка NekoVPN</b>\n\n"
                "💰 Оплата через Telegram Stars\n"
                "📱 Купи в: Настройки → Звёзды (Telegram Desktop)\n"
                "   или в профиле (Telegram mobile)\n\n"
                "Выбери срок:"
            ),
            parse_mode='HTML',
            reply_markup={'inline_keyboard': rows},
        )

    # ----- plan tap → send invoice -----

    def _on_plan_selected(self, cb: dict) -> None:
        data = cb.get('data') or ''
        try:
            months = int(data.split(':', 1)[1])
        except (IndexError, ValueError):
            return
        plan = next((p for p in get_active_plans(self.db) if p[0] == months), None)
        if not plan:
            return
        months, label, stars = plan

        from_user = cb.get('from') or {}
        chat_id = str(from_user.get('id') or '')
        if not chat_id:
            return

        # Payload echoes back in successful_payment so we know which
        # plan was bought and for whom (defensive — Telegram only ever
        # delivers the event to the same chat the invoice was for).
        payload = f"sub:{chat_id}:{months}"

        ok = self.bot.client.send_invoice(
            chat_id=chat_id,
            title=f"NekoVPN — {label}",
            description=(
                f"Продление подписки на {months} мес. "
                "Оплата через Telegram Stars."
            ),
            payload=payload,
            currency="XTR",  # Telegram Stars
            prices=[{"label": label, "amount": stars}],
            provider_token="",
        )
        cb_id = cb.get('id')
        if cb_id:
            try:
                self.bot.answer_callback_query(cb_id)
            except Exception:
                pass
        if not ok:
            self.bot.send_message(
                chat_id=chat_id,
                text="⚠️ Не удалось создать счёт. Попробуйте позже.",
            )

    # ----- pre_checkout_query -----

    def _on_pre_checkout(self, pcq: dict) -> None:
        """Confirm within 10 seconds, otherwise Telegram cancels.

        We don't do server-side inventory or fraud checks yet, so we
        just sanity-check the payload format and answer ok=True. If
        the payload is malformed (shouldn't happen — we own it), we
        reject with a helpful message so Telegram can refund.
        """
        pcq_id = pcq.get('id')
        payload = pcq.get('invoice_payload') or ''
        if not pcq_id:
            return
        if not payload.startswith('sub:') or payload.count(':') < 2:
            self.bot.client.answer_pre_checkout_query(
                pcq_id, ok=False,
                error_message="Не распознан тип подписки.",
            )
            return
        self.bot.client.answer_pre_checkout_query(pcq_id, ok=True)

    # ----- successful_payment -----

    def _on_successful_payment(self, msg: dict) -> None:
        sp = msg.get('successful_payment') or {}
        payload = sp.get('invoice_payload') or ''
        total_stars = sp.get('total_amount') or 0
        tx_id = sp.get('telegram_payment_charge_id') or ''
        chat_id = str((msg.get('chat') or {}).get('id'))

        # Re-parse payload "sub:<chat_id>:<months>"
        parts = payload.split(':')
        if len(parts) < 3 or parts[0] != 'sub':
            logger.warning(f"successful_payment: unknown payload {payload!r}")
            return
        target_chat = parts[1]
        try:
            months = int(parts[2])
        except ValueError:
            logger.warning(f"successful_payment: bad months in {payload!r}")
            return
        # Defensive: chat_id must match the payer; Stars can't be
        # gifted to a third party via /buy.
        if target_chat != chat_id:
            logger.warning(
                f"successful_payment: mismatch payload chat {target_chat} != "
                f"event chat {chat_id}; honouring event chat"
            )

        user = self.db.get_user(chat_id)
        if not user:
            logger.warning(f"successful_payment: user {chat_id} not found")
            return

        # Extend subscription_expiry by months * 30 days, starting
        # from MAX(now, current expiry) so a stacked purchase doesn't
        # collapse to N months from today.
        try:
            cur_dt = (
                datetime.fromisoformat(user.subscription_expiry)
                if user.subscription_expiry else datetime.utcnow()
            )
        except ValueError:
            cur_dt = datetime.utcnow()
        base = max(cur_dt, datetime.utcnow())
        new_dt = base + timedelta(days=30 * months)
        user.subscription_expiry = new_dt.isoformat()
        # If we have an x-ui email already, also update the subscription
        # row so the dashboard "subs" bucket reflects the renewal.
        self.db.save_user(user)
        try:
            with self.db._connect() as conn:
                conn.execute(
                    "UPDATE subscriptions SET expires_at = ?, is_active = 1 "
                    "WHERE chat_id = ? AND is_active = 1",
                    (new_dt.isoformat(), str(chat_id)),
                )
        except Exception as e:
            logger.warning(f"successful_payment: subscriptions update failed: {e}")

        # Propagate the new expiry (and current quota) to X-UI so the
        # client isn't auto-removed by 3x-ui's expiration cleanup while
        # the subscription is still active. Re-adds the client if it was
        # previously removed (e.g. due to an expired prior period).
        try:
            if user.uuid and user.email:
                xui = self.bot.services.get('xui')
                if xui and xui.db:
                    expiry_ts = 0
                    if user.subscription_expiry:
                        try:
                            expiry_ts = int(
                                datetime.fromisoformat(user.subscription_expiry).timestamp() * 1000
                            )
                        except ValueError:
                            pass
                    client_config = {
                        "id": user.uuid,
                        "flow": "xtls-rprx-vision",
                        "email": user.email,
                        "limitIp": getattr(user, 'limit_ip', 1),
                        "totalGB": int((getattr(user, 'quota_gb', 5.0) or 5.0) * BYTES_PER_GB),
                        "expiryTime": expiry_ts,
                        "enable": True,
                    }
                    if xui.add_client_sync(client_config, 1):
                        logger.info(
                            f"successful_payment: synced {user.email} to X-UI "
                            f"until {user.subscription_expiry}"
                        )
                    else:
                        logger.warning(
                            f"successful_payment: X-UI sync returned False for {user.email}"
                        )
        except Exception as e:
            logger.warning(f"successful_payment: X-UI sync failed: {e}")

        # Audit
        try:
            self.db.log_admin_action(
                'self-serve',
                'payment_stars',
                str(chat_id),
                f"+{months} мес, {total_stars} ⭐, tx={tx_id}",
            )
        except Exception:
            pass

        self.bot.send_message(
            chat_id=chat_id,
            text=(
                f"🎉 <b>Подписка активирована!</b>\n\n"
                f"Теперь у тебя <b>{months} мес.</b> доступа\n"
                f"Действует до <b>{new_dt.strftime('%d.%m.%Y')}</b>\n\n"
                f"💡 Продолжай пользоваться тем же ключом —\n"
                f"ничего менять не нужно\n\n"
                f"<i>{total_stars} ⭐ списано</i>"
            ),
            parse_mode='HTML',
        )

        # Ping the admin so they can see the revenue live. Goes to the
        # payments topic; the personal chat is only a fallback (house
        # rule: no PM while the forum group works).
        uname = user.username or str(chat_id)
        revenue_text = (
            f"💰 @{uname} продлил подписку на {months} мес "
            f"за {total_stars} ⭐ (до {new_dt.strftime('%Y-%m-%d')})"
        )
        sent = False
        topic_payments = getattr(self.config, 'TOPIC_PAYMENTS', 0) or 0
        if getattr(self.config, 'FORUM_ENABLED', False) and topic_payments:
            try:
                self.bot.send_message_to_topic(
                    chat_id=self.config.FORUM_GROUP_ID,
                    message_thread_id=topic_payments,
                    text=revenue_text,
                )
                sent = True
            except Exception:
                pass
        if not sent:
            admin = getattr(self.config, 'SUPER_ADMIN_ID', None)
            if admin:
                try:
                    self.bot.send_message(chat_id=str(admin), text=revenue_text)
                except Exception:
                    pass
