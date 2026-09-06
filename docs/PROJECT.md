# NekoVPN — обзор проекта

**Last updated: 2026-09-06.** Этот файл — человекочитаемый обзор «как всё
устроено сейчас». Парный документ — **AGENTS.md** в корне: гайд для агентов,
секция Deployment и хроника граблей (§8–29) — при конфликте прав AGENTS.md.
Живая операционная память — в memory-файлах агентских сессий.

---

## 1. Топология

Три узла. Бот живёт на **entry** (с 2026-07-19; в ранних доках было наоборот).

| Узел | SSH-алиас | Что там |
|---|---|---|
| **Entry** (РФ-facing) | `entry` | контейнер `vpn-bot` (`/opt/vpn-bot`, rsync-деплой), локальный `3x-ui` для CF-fronted инбаундов, ingress :443 (форвард на exit), dashboard API :8080, Hermes-агент `/ai` :4097 |
| **Exit** (за границей) | `vpn-exit` | боевая панель 3x-ui :2026 (форк v3.4) + xray :443, два инстанса hysteria2 (hy2 + hy2t Turbo :8402), Caddy :9443 → entry:8080 (TLS дашборда), tinyproxy :8888 (Telegram-egress бота), hy2-traffic-collector, probe-раннеры |
| **Reserve DE** | — | запасная VLESS-Reality нода для paid (панель x-ui 2.8.11), lazy-провижн при `/sub`; хостит чужой прод — не трогать его контейнеры |

Путь юзера: клиент → entry :443 → exit :443 → xray → интернет.
Путь админа: браузер → `https://<dashboard-host>:9443` (Caddy на exit) → entry :8080.

**`/opt/vpn-bot` на entry — НЕ git-checkout.** Это rsync-приёмник; код, который
не задеплоен скриптом, в проде не существует.

## 2. Протоколы и тиры

- **VLESS + XTLS-Reality** (:443) — базовый TCP-протокол. SNI сейчас `www.bing.com`
  (Microsoft-сертификат перерос 8192-байтный буфер xray — AGENTS.md §23);
  менять SNI = три слоя разом (панель, HAProxy ACL на entry, `SNI_VALUE` в .env).
  Каждый клиент инбаунда обязан нести `flow=xtls-rprx-vision`: пустой flow =
  мёртвый Reality при зелёных портах (2026-09-01..04, 4 дня; починка —
  `scripts/restore_reality_flow.py --apply` + рестарт xray со стороны панели).
- **Hysteria2** — freemium-тир, доступен всем (в т.ч. демо). Порт-хоппинг:
  формат `HY2_HOP_PORTS` различается для sing-box (`server_ports` массив `"X:Y"`)
  и URI (`mport` через запятую) — не путать.
- **Hysteria2 Turbo (hy2t)** — второй инстанс на exit :8402 с Brutal CC,
  **paid-дефолт**. Auth разведён: `/api/hy2/auth` (demo+paid) vs `/api/hy2t/auth`
  (только paid).
- **VMess + WS/httpupgrade** — CF-fronted инбаунды (через локальный 3x-ui entry).
- Роутинг клиента: RU-направления — direct (+ QUIC:443 carve-out для VK),
  весь UDP — через UDP-нативный селектор `calls` (звонки работают и у freemium).

**Тиры:** демо = freemium **10 ГБ/мес навсегда** (`subscription_expiry IS NULL` —
это норма), продлевается месячной джобой; paid = **100 ГБ/мес до даты**
`subscription_expiry` + Turbo + резервная DE-нода.

## 3. Код (bot/ в корне репо)

```
bot/
├── main.py                  # entry-point: init_services() + sync polling
├── config/                  # Settings (класс Settings!), constants (UserState, STATE_TRANSITIONS)
├── core/
│   ├── web_server.py        # aiohttp: dashboard API + webapp + /sub + hy2-auth + /health(version)
│   ├── database.py          # deprecated-фасад → repositories/
│   ├── repositories/        # user / ticket / node / message_map (+ async-адаптеры)
│   ├── state_machine.py     # переходы статусов
│   └── cluster/             # multi-node код (в проде не активен)
├── handlers/
│   ├── admin/               # миксины: base(роутинг ADMIN_COMMANDS)/users/ops/stats/broadcast
│   ├── callbacks/           # user.py (ключи/демо), admin.py (аппрувы, mail-заявки), forum.py
│   ├── commands.py, messages.py, payments.py (Stars), ai_handler.py (/ai)
├── services/
│   ├── billing.py           # grant_paid_access() — ЕДИНСТВЕННОЕ определение paid-тира
│   ├── xui_service.py       # API-first клиент обеих панелей (xui.db на entry = None)
│   ├── vpn.py, subscription.py  # генерация ключей, сборка /sub
│   ├── notifications.py     # шедулер джоб + _deliver_user_notice (TG или email для ext_*)
│   ├── dpi_monitor.py       # DPIMonitor — авто-понижение каскада по сигналам (джоба каждые 10 мин)
│   ├── email_service.py     # исходящие письма (Gmail-релей)
│   ├── mail_intake.py       # IMAP-поллер заявок (3 мин)
│   ├── user_lifecycle.py    # revoke_user_key — единый путь отзыва ключа
│   ├── fallback_node.py     # DE-резерв для paid
│   └── hermes_client.py + agent_factory.py  # /ai бэкенд
├── models/user.py           # User dataclass
├── webapp/                  # дашборд (index.html + app.js + style.css, отдаёт сам бот)
└── utils/                   # admin_token (HMAC для дашборда), validators, rate_limit…
```

## 4. Данные

**bot.db** (entry, volume `vpn-bot_vpn-bot-data`, путь `/var/lib/vpn-bot/bot.db`):

| Таблица | Суть | Грабли |
|---|---|---|
| `users` | профиль+статус+квота | `email` = синтетический панельный id (`user_…@nekovo.ru`), реальная почта — **`contact_email`**; email-only юзеры имеют `chat_id = ext_<crc32>` |
| `subscriptions` | биллинг-периоды | колонки в проде `started_at`/`expires_at` (НЕ start_date/end_date) |
| `email_requests` | входящие заявки с почты | дедуп по Message-ID |
| `ai_sessions` | сессии /ai | ключи `pm:<id>` / `topic:<id>:<thread>` |
| `dpi_metrics` | 5-мин срезы DPI-сигналов | `asn` с префиксом `AS`; country `*GLOBAL*`/`*TUNNEL*` |
| `app_settings` | key/value тюнинги | `cascade_protocol_order` / `cascade_by_asn` / `cascade_by_country` — операторские; `cascade_auto` + `dpi_monitor_state` пишет ТОЛЬКО DPIMonitor — руками не править, откат одной командой `/cascade reset` |
| `admin_actions`, `notification_log`, `tickets`, `ticket_messages`, `message_map`, `nodes` | аудит/дедуп/саппорт/ноды | |

**Учёт трафика:** источник правды — `client_traffics` панели exit (все
xray-протоколы в одну строку на email; UNIQUE(email) ⇒ один клиент/квота на
юзера, тир-гейт только в боте). Hy2-байты доливает мост на exit. Бот
зеркалирует в `users.traffic_*` каждые 10 мин, предупреждает на 80%/100%.

## 5. Пользовательские флоу

- `/start` → «Запросить демо» → аппрув админа → выбор платформы → ключ.
  Цепочка: `NEW → PENDING_DEMO → PLATFORM_SELECT → DEMO`; paid — только через
  `grant_paid_access` (Stars-оплата, `/approve_payment`, кнопка ⭐ в дашборде).
- **`/sub/<token>`** — три формата: default sing-box JSON (Hiddify/Karing),
  `?format=links` (plain-text ссылки, v2rayNG), `?format=xray` (полный конфиг).
- **Клиенты:** Android/PC → Hiddify, iOS → Karing (Happ и Hiddify выпилены из RU
  App Store волнами 2026-07).
- **Email-only юзеры**: письмо на ящик → карточка с кнопками в топике заявок →
  демо-ключ письмом; либо админ вручную `/addmail user@x [ГБ] [дней]`.
  Провижн идемпотентен по `contact_email` (UUID сохраняется).
- Смена платформы — кнопки `setplat:*` прямо на карточке ключа.

## 6. Админка

**Telegram** (форум-группа, ответы всегда в топик источника): `/users`,
`/users_all`, `/find <текст>` (ищет и по contact_email), `/pending`, `/quota`,
`/expire`, `/addmail`, `/approve_payment`, `/broadcast`, `/backup`, `/ban`,
`/unban`, `/reset`, `/protocols` (живость протоколов по таблице проб
`outbound_health` + результат аудита полей клиентов панели — тот же взгляд, что
у алерта `protocol_down`), `/cascade` (действующий порядок каскада с тегами
тиров + авто-понижения DPIMonitor с since/reason и per-ASN записями;
`/cascade AS31133` — порядок для ASN, `/cascade reset` — снять все
авто-понижения, `/cascade on|off` — монитор), `/ai <вопрос>` (Hermes-агент).
PM-fallback при выключенном форуме.

**Дашборд** `https://<dashboard-host>:9443/?admin_token=…` (HMAC-токен из
`/admin`, TTL 1ч): список юзеров (онлайн-бейджи, гео, шаринг-детект), карточка
юзера (квота/лимит/дата/⭐ paid), действия approve/reject/ban/unban/revoke/
reset/grant_paid/grant_100gb (все — через модалку подтверждения), рассылка,
health, DPI-сигналы, алерты.

**AI-агент `/ai`**: Nous Hermes на entry :4097, модель
`minimax/minimax-m3:free` через OpenRouter (+fallback), 7 наших skills
(vpn-ops, server-admin, incident-response, billing-ops, **user-ops**,
code-review, dpi-analysis). Skills деплоятся ТОЛЬКО `scripts/deploy_hermes_skills.sh`.
Правило агента с 2026-09-05: любая диагностика начинается с
`protocol_healthcheck.py`, ответ — результат первым, без нарратива, ≤ ~900
символов (hermes/AGENTS.md). **Гард бесплатной модели**
`scripts/hermes_model_guard.py` (на entry-хосте под root, systemd-таймер раз в
30 мин; деплой вместе с вотчдогом — `scripts/deploy_hermes_host.sh`): если
текущая `:free`-модель пропала из `/api/v1/models` или стала платной —
переключает `model.default` на первый **бесплатный** fallback (старая уходит в
конец цепочки), делает бэкап config.yaml, рестартит hermes-api и пишет в топик
AI. Если бесплатных не осталось — **ничего не переписывает**, только критикал
(платную модель гард не ставит никогда). Отдельно: рост `usage` ключа
OpenRouter по `/api/v1/key` > $0.001 между запусками = критикал независимо от
причины. Все вызовы OpenRouter/Telegram с хоста — через HTTPS_PROXY из
соответствующих .env; `--dry-run` ничего не пишет и не шлёт.

## 7. Почта и мониторинг

- **Исходящие**: Gmail SMTP-релей (~500 писем/день, свой MTA невозможен — нет
  PTR). Ключи, квота-алерты, продления; ext_*-юзерам все уведомления идут
  письмом через `_deliver_user_notice`.
- **Входящие**: IMAP-поллер каждые 3 мин → `email_requests` + карточка.
- **Мониторинг**: probe-proxy сайдкар (честные пробы через реальные туннели
  per-protocol), `/onlines` по clientStats.lastOnline, DPI-гейты по когортам
  (country, ASN) с трендом, AlertManager (Telegram-egress outage и др.),
  вотчдог hermes-api (не рестартит при живом агент-лупе).
- **Живость протоколов** (после 4-дневного мёртвого Reality 2026-09-01..04 при
  зелёных `ss`/`iptables`/`docker ps`): таблица `outbound_health` — 10 проб на
  протокол каждые 15 мин, **7/10 ok — норма** (vk/yandex/sberbank честно падают
  через туннель), живость = `latency_ms IS NOT NULL OR status='ok'`. Алерт
  `protocol_down:<tag>` (3 прогона без единого ответа с latency, critical) +
  `protocol_down:all` (все разом = probe-proxy / линк / exit, один инцидент) +
  `protocol_down:probe_pipeline` (пробы не пишутся — слепота). С 2026-09-05 зонд покрывает и **hy2t** (inbound :18085 у probe-proxy, только при заданном `HY2T_PORT`; таблица портов — `HealthChecker.probe_ports_for`), а `outbound_health` чистится ежедневной джобой (ретенция 30 дней, батчами по 20k строк). Критикал уходит
  в топик AI и **автоматически запускает агент-диагностику** (первым действием
  агенту предписан `protocol_healthcheck.py`, ничего не менять); ответ пишется в
  `alert_history.kimi_analysis` (вкладка Alerts дашборда) **и постится в тот же
  топик** («Диагностика по алерту»; при недоступности агента — строка-отказ с
  отсылкой к `/protocols`). В отличие от `dpi_*`, где анализ только на дашборде.
- **Скрипты-«глаза»**: `scripts/protocol_healthcheck.py` (на entry-хосте под
  root: ИТОГ по протоколам, ранжированные подозреваемые, точные следующие
  команды, `--json`, exit 0/1/2) — STEP 0 любой диагностики у агента и у
  человека; `scripts/verify_panel_client_fields.py` (в контейнере бота,
  `PYTHONPATH=/app`) — аудит per-protocol полей клиентов панели, гоняется как
  post-deploy smoke в `deploy_to_entry.sh`.
- **Фидбэк-петля (с 2026-09-06, IMPROVEMENT_PLAN A1)**: сигналы (`outbound_health`
  DARK/DEGRADED, Reality handshake-fail per-ASN в `dpi_metrics`, шторм
  `hy2_auth_log` у одного юзера → его `users.last_asn`, ≥2 отчёта «не работает»
  с одного ASN за 6 ч) → **DPIMonitor** (`services/dpi_monitor.py`, джоба
  каждые 10 мин; гистерезис 2 плохих оценки → понижение, 6 хороших → возврат,
  ≥30 мин между сменами, ≤2 смены за прогон, все пробы тёмные разом = ничего)
  → `app_settings.cascade_auto` → `get_cascade_order` ставит понижённые
  протоколы **в конец, не удаляя** → `/sub`, карточка ключа, `?format=links`.
  Операторские `cascade_protocol_order` / `cascade_by_asn` всегда старше авто
  (решают базовый порядок, авто только переставляет внутри него). Каждая смена
  — строка в `admin_actions` + пост в топик AI; `/cascade` показывает, что и
  почему понижено, `/cascade reset` снимает всё одной командой.

## 8. Тесты — 4 уровня

Каждый уровень ловит класс багов, невидимый остальным (доказано 2026-08-30,
когда три бага дашборда прошли мимо 1789 зелёных юнитов):

```bash
python3 -m pytest tests/ -q      # 1) unit + 2) integration на реальном SQLite (~2050, ~45с)
python3 -m pytest tests/e2e -q   # 3) Playwright-смоук дашборда (6, ~14с) — ОТДЕЛЬНОЙ стадией
./scripts/deploy_to_entry.sh     # 4) деплой со сверкой git-sha в /health
```

- unit+mutmut — логика функций; моки не видят межслойные эффекты.
- `tests/integration/test_web_actions_integration.py` — все действия дашборда
  против реальной БД, ассерты по итоговой строке.
- `tests/e2e` — реальный браузер против реального WebAppServer; ловит мёртвый
  JS. Не смешивать с общим прогоном (sync-Playwright травит pytest-asyncio).
- Новому сьюту доверять только после «верни баг — убедись, что покраснел».

## 9. Деплой

```bash
git commit …                     # штамп берётся из HEAD
./scripts/deploy_to_entry.sh     # bot/ + scripts/ → entry, rebuild --no-deps, сверка sha
```

Правила, оплаченные инцидентами:
- **Только `--no-deps`** для vpn-bot: без него compose пересоздаёт 3x-ui с
  unpinned `:latest` (2026-07-19 это снесло панель).
- **Никогда не rsync'ать compose/.env** на entry — там ручные правки
  (HTTPS_PROXY для РКН-обхода, WEB_BIND), которых нет в репо.
- Деплой скилов Hermes — отдельный `deploy_hermes_skills.sh` (подстановка
  плейсхолдеров из untracked `hermes_skill_vars.local`).
- Бэкапы: `scripts/backup.sh` + systemd-таймер (7 снапшотов).

## 10. Статус и планы

Активный roadmap — `docs/IMPROVEMENT_PLAN.md` (lockdown-режим, CI, охват
фидбэк-петли A1.2 — сама петля замкнута DPIMonitor'ом 2026-09-06). Multi-node cluster-код (`bot/core/cluster/`) написан, но в
проде один exit + DE-резерв; авто-провижн нод через API провайдера — «когда-нибудь»
(у AdminVPS API есть, у BitCloud нет).

Хронология всех инцидентов и решений: AGENTS.md §8–29.
