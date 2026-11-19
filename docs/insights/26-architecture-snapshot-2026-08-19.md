# §26. Architecture snapshot — 2026-08-19 (большой ремонт учёта/биллинга)

(Перенесено из удалённого PROJECT_CONTEXT.md; полная хроника — в памяти агента.)

**Тарифы.** Демо = freemium навсегда: 10 ГБ/мес с приложением NekoVPN, 5 ГБ/мес с другими клиентами (с 2026-11-01, §43; DEMO_DAYS=30); paid = 100 ГБ/мес (PAID_TRAFFIC_GB, floor) до даты `users.subscription_expiry`. Единственное определение paid-тира — `bot/services/billing.py:grant_paid_access()` — через него ходят Stars-оплата, /approve_payment и дашборд. Месячный сброс счётчиков (демо+paid) — ботовская джоба 1-го числа 00:00 UTC; панельный rolling reset отключён.

**Учёт трафика.** Панель exit считает все xray-протоколы в одну строку client_traffics на email (держится на api-инбаунде dokodemo 127.0.0.1:62789 в xrayTemplateConfig). Hy2 доливает systemd-мост `hy2-traffic-collector` на exit (hysteria trafficStats API), он же кикает over-quota live-сессии и бампает last_online. Бот зеркалирует цифры в users.traffic_* каждые 10 мин и шлёт предупреждения на 80%/100% квоты (ext-юзерам — письмом).

**Почта.** Исходящие (ключи, уведомления) — Gmail SMTP-релей; входящие заявки на ключ — IMAP-поллер каждые 3 мин → карточка с кнопками «Выдать (демо)/Отклонить» в топик заявок; карточки самообновляются.

**Мониторинг.** probe-proxy сайдкар (sing-box, конфиг генерится scripts/gen_probe_config.py) — HealthChecker ходит через реальные туннели per-protocol; /onlines читает clientStats.lastOnline; DPI-алерты гейтятся на реальные когорты и тренд (2 цикла).

**Команды.** Ответы админ-команд всегда в топик источника (AdminHandlerBase._send); в группах CommandHandler заявляет только /admin; все панельные чтения в командах — через API-aware методы XUIService (xui.db на entry = None). Дрифт карт команд/справки ловят тесты TestCommandMapIntegrity.
