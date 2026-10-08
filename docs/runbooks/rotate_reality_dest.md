# Runbook: ротация Reality dest/SNI одним прогоном

Скрипт: `scripts/rotate_reality_dest.py` (E10). Фон — AGENTS.md §23: 2026-07-20
сертификат `www.microsoft.com` вырос до 8273 Б (буфер xtls/reality — 8192), и
Reality лёг у всех. Смена dest/SNI работает, только если **четыре слоя** двигаются
вместе:

| # | Слой | Где | Что меняется |
|---|------|-----|--------------|
| 1 | панель exit | inbound `INBOUND_ID` (обычно 1, `inbound-443`) | `realitySettings.dest` (или `target`) + `serverNames`, затем рестарт xray через панель |
| 2 | HAProxy entry | `/etc/haproxy/haproxy.cfg`, `acl is_reality_sni req_ssl_sni -i …` | старое имя заменяется новым на месте; `haproxy -c` на копии → `reload` |
| 3 | бот entry | `/opt/vpn-bot/.env` → `SNI_VALUE` | + `docker compose up -d --no-deps --force-recreate --no-build vpn-bot` (restart оставил бы старый env) |
| 4 | probe-proxy | `/opt/vpn-bot/probe-proxy/config.json` | генерится из `SNI_VALUE` (`gen_probe_config.py`) → `sing-box check` → restart. Без этого проба reality гаснет → `protocol_down:reality` и DPIMonitor понижает Reality |

## Когда применять

- `protocol_healthcheck.py` / алерт называет подозреваемым «Reality dest cert outgrew
  8192 buffer» (Certificate > 8000 Б), либо dest перестал отвечать с exit.
- Плановая смена маскировки (dest заблокирован/замедлен у операторов, B2).
- Слои разъехались (кто-то правил один из них руками) — тот же прогон сводит их.

Не применять «на всякий случай»: при жёсткой смене SNI **все клиенты со старым SNI
перестают проходить Reality сразу после шага 1** и возвращаются, только обновив
подписку (sing-box/Hiddify/FlClash — сами по интервалу обновления, vless://-ссылки —
только переизданием). Во время инцидента (старый dest и так мёртв) это не хуже текущего.

## Команды (с машины оператора, из checkout репо; ssh-алиасы `entry`, `vpn-exit`, root)

```bash
# 1. Проверка и план — НИЧЕГО не меняет, можно гонять сколько угодно
python3 scripts/rotate_reality_dest.py --sni www.google.com
#    exit 0 = кандидат годен, план показан; 1 = нельзя (причина в строке ИТОГ); 2 = не смог посмотреть

# 2. Применение — покажет тот же план и спросит `yes`
python3 scripts/rotate_reality_dest.py --sni www.google.com --apply

# 3. Повторная проверка в любой момент
python3 scripts/rotate_reality_dest.py --verify            # цель из снимка
python3 scripts/rotate_reality_dest.py --verify --sni www.google.com
```

Кандидата меряем **с exit** (это он ходит на dest), 3 замера, берём максимум:
TLS 1.3, ALPN h2, сертификат валиден для SNI, Certificate ≤ 8000 Б. Известные размеры
(§23, 2026-07-20): google 2520, cloudflare 2521, bing 3920, dl.google.com 4874,
microsoft 8273 (ПЛОХО). Если dest — не то же имя (IP, другой хост с тем же
сертификатом): `--dest host:port`. Имя должно быть ещё и не заблокировано/не замедлено
у российских операторов: SNI летит в открытую, ТСПУ его видит — это скрипт не меряет.

Полезные флаги: `--keep-old-sni` (оставить старое имя в serverNames и acl на
переходный период; check честно скажет, принимает ли новый dest старый SNI — если нет,
старых клиентов это не спасёт), `--no-xray-restart`, `--inbound-id N`,
`--entry`/`--exit` (другие ssh-алиасы), `--tls-probe-addr host:port`.

## Что делает --apply и что будет при сбое

1. Снимок `scripts/.rotation_snapshot.json` пишется **до** первого изменения
   (только значения: dest, имена, SNI, число клиентов, пути бэкапов — без ключей и
   содержимого файлов; в git и на прод не попадает).
2. Панель → HAProxy → `.env` + пересоздание бота → probe-proxy. Каждый шаг на хосте
   сначала сверяет, что слой всё ещё такой, каким его прочитала проверка; иначе — отказ
   без записи.
3. Сбой шагов 1–3 → автоматический откат уже сделанного в обратном порядке
   (`--no-auto-revert` — не откатывать). Откат не трогает слой, который за это время
   правил кто-то другой.
4. Сбой probe-proxy ротацию **не** откатывает (юзеры уже в порядке): повтори ту же
   команду с `--apply` — продолжит по снимку и доделает только этот шаг.
5. В конце — verify (ниже). Бэкапы файлов лежат рядом с ними на entry:
   `*.rotate-bak-<UTC>` (`.env` — с правами 0600). После успешной проверки их можно удалить.

## Что проверить после

`--verify` (запускается и сам после apply) проверяет:
- панель отдаёт новые dest/serverNames, число клиентов не упало, flow на месте;
- `config.json` на exit совпадает с панелью (после рестарта xray);
- haproxy `active`, в acl новое имя;
- `.env` и **запущенный** контейнер бота (`printenv SNI_VALUE`) на новом SNI, `/health` healthy;
- probe-proxy на новом SNI;
- TLS 1.3-рукопожатие на entry (`ENTRY_NODE_IP:ENTRY_NODE_PORT` из контейнера бота) с
  новым SNI завершается и сертификат валиден для этого имени (HAProxy → exit → Reality
  отдаёт не-Reality клиента в dest). Аутентификацию Reality это **не** проверяет.

Через ≤15 мин: `/protocols` в боте или
`ssh entry 'python3 /opt/vpn-bot/scripts/protocol_healthcheck.py'` — reality живой
(пробы ходят настоящим ключом). Свой клиент — обновить подписку и подключиться.

## Откат

```bash
python3 scripts/rotate_reality_dest.py --rollback      # покажет план отката и спросит yes
```

Возвращает значения из снимка в обратном порядке (`.env`+бот+probe → HAProxy →
панель) и проверяет. Откажется, ничего не меняя, если какой-то слой сейчас ни на
старом, ни на новом значении (его правили после ротации) — тогда обычной ротацией:
`--sni <старый> --apply --force-snapshot`. Если упавший шаг оставил бота лежать,
панель недоступна (вызовы идут через контейнер) — сначала
`ssh entry 'cd /opt/vpn-bot && docker compose up -d --no-deps vpn-bot'`, потом `--rollback`.
Ручной откат, если скрипт недоступен: бэкапы `*.rotate-bak-<UTC>` на entry +
dest/serverNames в панели (значения — в снимке, блок `old`).

## Репетиция без прода

```bash
ROTATE_FAKE=1 python3 scripts/rotate_reality_dest.py --sni www.google.com
export ROTATE_FAKE=1 ROTATE_FAKE_STATE=/tmp/rotate_fake_world.json
python3 scripts/rotate_reality_dest.py --sni www.google.com --apply
python3 scripts/rotate_reality_dest.py --rollback
ROTATE_FAKE_FAIL=haproxy_set python3 scripts/rotate_reality_dest.py --sni www.google.com --apply   # авто-откат
```

Фикстурный мир — форма прода на 2026-10 (dest bing, acl `www.bing.com www.google.com`,
размеры сертификатов из §23); снимок в fake-режиме — во временном каталоге, реальную
ротацию он не заблокирует.

## Не трогает

Ключи Reality и shortId; резервный DE-узел (свой SNI `www.google.com`, своя панель);
`.env` на exit.
