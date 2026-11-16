# Runbook: релей авторизации hy2 на exit (A6)

Фон — AGENTS.md §56, IMPROVEMENT_PLAN раздел F, пункт A6. Обе hysteria на exit на каждое новое
подключение спрашивают бота, пускать ли ключ. Пока бот не отвечает (деплой, рестарт — 15–30 с;
сбой — дольше), hysteria отказывает всем новым hy2- и hy2t-подключениям. Релей встаёт между ними на
loopback exit и отвечает за бота из памяти:

| Кто на exit | Сейчас (после §53) | После |
|---|---|---|
| hysteria (`/etc/hysteria/config.yaml`) | `https://entry-hop.invalid:8448/api/hy2/auth` | `http://127.0.0.1:9979/api/hy2/auth` |
| hysteria-turbo (`config-turbo.yaml`) | `https://entry-hop.invalid:8448/api/hy2t/auth` | `http://127.0.0.1:9979/api/hy2t/auth` |

**Как устроено.** `hy2-auth-relay.service` (systemd, `DynamicUser`, root-копия скрипта в
`/usr/local/lib/nekovpn-hy2-relay/`) слушает только loopback и каждый запрос hysteria передаёт
боту по перегону с пином (`BOT_URL` / `BOT_CA_FILE` / `DPI_REPORT_TOKEN` из `/etc/exit-dpi.env`, как
у репортёра). Ответ бота уходит hysteria как есть. Раз в минуту релей забирает у бота список ключей,
которые каждый маршрут пускает сейчас (`GET /api/hy2/keys`, только sha256-хэши). Когда бот не
отвечает, релей пускает ключ, если бот пустил его на этом маршруте или перечислил в списке не
раньше 15 минут назад; остальных — нет. Отказ бота убирает ключ из памяти (отказ hy2 — и с hy2t).
Туннельные юзеры релей не достают: ACL hysteria (§52) режет 127.0.0.0/8 — шаг 0 это проверяет.

Проверено на стенде (`tests/integration/test_hy2_auth_relay_stand.py`: hysteria 2.9.2, настоящий
бот по TLS с пином, ACL из §52).

## Переменные (на машине оператора, из checkout репо; ssh-алиасы `entry`, `vpn-exit`, root)

```bash
TS=$(date -u +%Y%m%d-%H%M%S)      # одна метка на все бэкапы этого прогона
```

`/etc/exit-dpi.env` целиком не печатать: ниже из него показываются только `BOT_URL` / `BOT_CA_FILE`,
токен — только счётчиком строк.

## 0. Предпроверка (только чтение)

```bash
ssh vpn-exit 'grep -nE "^[^#]*url:" /etc/hysteria/config.yaml /etc/hysteria/config-turbo.yaml'
ssh vpn-exit 'grep -c "reject(127.0.0.0/8)" /etc/hysteria/config.yaml /etc/hysteria/config-turbo.yaml'
ssh vpn-exit 'systemctl is-active hysteria hysteria-turbo exit-dpi-reporter.timer; python3 --version'
ssh vpn-exit 'ss -ltnH "sport = :9979"; echo "--- 9979 free if nothing above"'
ssh vpn-exit 'grep -E "^(BOT_URL|BOT_CA_FILE)=" /etc/exit-dpi.env; grep -c "^DPI_REPORT_TOKEN=" /etc/exit-dpi.env; ls -l /etc/nekovpn-hop/entry-hop.pem'
```

Ожидается: в каждом конфиге ровно одна строка колбэка `https://entry-hop.invalid:8448/api/hy2/auth`
(в turbo — `…/api/hy2t/auth`); `reject(127.0.0.0/8)` в обоих (`:1`); юниты active; python3 ≥ 3.11;
порт 9979 свободен; `BOT_URL=https://130.49.146.10:8448`, `BOT_CA_FILE=/etc/nekovpn-hop/entry-hop.pem`,
токен `1`; пин на месте. Нет ACL — остановиться: без него юзеры туннеля достанут релей (и всё прочее
на loopback exit, §52). Если `hysteria-turbo` не active — шаг 3 править только `config.yaml`.

## 1. Бот: список ключей (`GET /api/hy2/keys`)

Обычный деплой (`./scripts/deploy_to_entry.sh`) после мержа. Проверка с exit, по перегону с пином и
токеном репортёра; печатает только счётчики:

```bash
ssh vpn-exit python3 - <<'PY'
import json, ssl, urllib.request
env = {}
for line in open('/etc/exit-dpi.env'):
    key, sep, value = line.strip().partition('=')
    if sep and not key.startswith('#'):
        env[key.strip()] = value.strip().strip('"').strip("'")
url = env.get('BOT_URL', 'https://130.49.146.10:8448').rstrip('/')
ctx = ssl.create_default_context(cafile=env.get('BOT_CA_FILE', '/etc/nekovpn-hop/entry-hop.pem'))
req = urllib.request.Request(url + '/api/hy2/keys',
                             headers={'X-DPI-Token': env['DPI_REPORT_TOKEN']})
doc = json.load(urllib.request.urlopen(req, timeout=15, context=ctx))
print({route: len(doc[route]) for route in ('hy2', 'hy2t')})
PY
```

Ожидается `{'hy2': N, 'hy2t': M}`: N — активные ключи (демо и платные), M — платные. `HTTP Error 404`
— на entry старый бот (деплой не дошёл).

## 2. Exit: релей (юзеров не касается — hysteria пока спрашивает бота сама)

```bash
D=$(ssh vpn-exit mktemp -d)
ssh vpn-exit "mkdir $D/scripts $D/systemd"
scp scripts/hy2_auth_relay.py scripts/install_hy2_auth_relay.sh "vpn-exit:$D/scripts/"
scp systemd/hy2-auth-relay.service "vpn-exit:$D/systemd/"
ssh vpn-exit "bash $D/scripts/install_hy2_auth_relay.sh; rm -rf $D"
```

Ожидается `installed: …, hy2-auth-relay.service running` и `hysteria does not ask the relay yet`.
Состояние релея и проход запроса до бота:

```bash
ssh vpn-exit 'curl -s 127.0.0.1:9979/status; echo'
ssh vpn-exit "curl -s -H 'Content-Type: application/json' -d '{\"addr\": \"127.0.0.1:1\", \"auth\": \"a6-rollout-check\"}' http://127.0.0.1:9979/api/hy2/auth; echo"
ssh entry 'docker logs --since 5m vpn-bot 2>&1 | grep -c "hy2_auth: deny from 127.0.0.1:1"'
```

В статусе `"key_list": {"on": true, …, "size": {"hy2": N, "hy2t": M}, "last_error": null}` с теми же
N и M, что в шаге 1, и `"bot": {…, "last_error": null, …}`. Запрос — `{"ok": false}` (это не ключ),
а в логе бота строка этого отказа (`1`): запрос дошёл до бота через релей.

## 3. Exit: hysteria спрашивает релей (рестарт hysteria: hy2-сессии и звонки рвутся, клиенты переподключаются за 30–60 с)

Тихий час. Сначала проверка, по которой судят переключение: свежий клиент hysteria на exit
подключается к каждому экземпляру по loopback с ключом служебного аккаунта проб (`probe_internal`,
оплачен до 2099) и качает `generate_204`. Ключ идёт через stdin и на экран не выводится. SNI клиента —
имя из сертификата сервера: hysteria 2.9 по умолчанию (`tls.sniGuard: dns-san`) отвечает клиенту без
подходящего SNI `tls: internal error`. Положить скрипт на exit:

```bash
ssh vpn-exit 'cat > /root/hy2-fresh-check.sh' <<'CHECK'
set -u
read -r KEY
[ -n "$KEY" ] || { echo "no key on stdin"; exit 1; }
D=$(mktemp -d); chmod 700 "$D"
cpid=""
trap '[ -n "$cpid" ] && kill "$cpid" 2>/dev/null; rm -rf "$D"' EXIT
n=0
for cfg in /etc/hysteria/config.yaml /etc/hysteria/config-turbo.yaml; do
  n=$((n + 1)); socks=$((18410 + n))
  port=$(grep -E '^listen:' "$cfg" | grep -oE '[0-9]+$')
  cert=$(grep -E '^[[:space:]]+cert:' "$cfg" | head -1 | awk '{print $2}')
  sni=$(openssl x509 -in "$cert" -noout -ext subjectAltName | grep -oE 'DNS:[^, ]+' | head -1 | cut -d: -f2)
  pw=$(grep -E '^[[:space:]]+password:' "$cfg" | head -1 | awk '{print $2}' | tr -d "\"'")
  printf 'server: 127.0.0.1:%s\nauth: "%s"\ntls:\n  sni: "%s"\n  insecure: true\nobfs:\n  type: salamander\n  salamander:\n    password: "%s"\nsocks5:\n  listen: 127.0.0.1:%s\n' \
    "$port" "$KEY" "$sni" "$pw" "$socks" > "$D/client$n.yaml"
  timeout 30 /usr/local/bin/hysteria client -c "$D/client$n.yaml" >/dev/null 2>&1 &
  cpid=$!
  sleep 2
  code=$(curl -s -m 12 -x "socks5h://127.0.0.1:$socks" -o /dev/null -w '%{http_code}' https://www.gstatic.com/generate_204)
  echo "$(basename "$cfg") :$port $(grep -E '^[[:space:]]+url:' "$cfg" | awk '{print $2}') -> $code"
  kill "$cpid" 2>/dev/null; cpid=""
done
CHECK
```

Прогнать её ДО правки, на старом колбэке:

```bash
probe_key() { ssh entry 'python3 -c "import json; c = json.load(open(\"/opt/vpn-bot/probe-proxy/config.json\")); print(next(o[\"password\"] for o in c[\"outbounds\"] if o.get(\"type\") == \"hysteria2\" and o.get(\"password\")))"'; }
probe_key | ssh vpn-exit 'bash /root/hy2-fresh-check.sh'
```

Ожидается `-> 204` у обоих конфигов со старым `https://entry-hop.invalid…`: это проверяет саму проверку.
Только потом правка `url:` колбэка:

```bash
ssh vpn-exit bash -s -- "$TS" <<'SH'
set -e
old="https://entry-hop.invalid:8448/api/hy2"
new="http://127.0.0.1:9979/api/hy2"
systemctl is-active --quiet hy2-auth-relay || { echo "the relay is not running: step 2 first"; exit 1; }
for f in /etc/hysteria/config.yaml /etc/hysteria/config-turbo.yaml; do
  [ "$(grep -cF "$old" "$f")" = 1 ] || { echo "$f: the url line is not as expected"; exit 1; }
  grep -qE '^[^#]*reject\(127\.0\.0\.0/8\)' "$f" || { echo "$f: no ACL closing loopback (AGENTS.md §52)"; exit 1; }
done
for f in /etc/hysteria/config.yaml /etc/hysteria/config-turbo.yaml; do
  cp -a "$f" "$f.bak-a6-$1"
  sed -i "s|$old|$new|" "$f"
  grep -n "$new" "$f"
done
SH
```

Рестарт и сразу та же проверка:

```bash
ssh vpn-exit 'systemctl restart hysteria hysteria-turbo && sleep 2 && systemctl is-active hysteria hysteria-turbo'
probe_key | ssh vpn-exit 'bash /root/hy2-fresh-check.sh'
ssh vpn-exit 'curl -s 127.0.0.1:9979/status; echo'
```

`-> 204` у обоих конфигов, теперь с `http://127.0.0.1:9979/…`: hysteria спросила релей, релей — бота, бот
пустил; в статусе `served.bot_allow` вырос на 2. Не 204 — сразу откат шага 3 (ниже) и разбор:
`journalctl -u hy2-auth-relay -n 50`.

**Не судить по пробе через probe-proxy сразу после рестарта.** Сайдкар держит долгое QUIC-соединение к
старому процессу hysteria. Оно мертво, но клиент узнаёт об этом только по таймауту простоя: у sing-box
около 30 с, на сервере `quic.maxIdleTimeout: 60s`, у клиентов до 60 с. Проба, запущенная в эти секунды,
висит до своего таймаута и даёт `000` при рабочем релее; 2026-11-16 первую попытку так откатили зря.
Живые клиенты переподключаются те же 30–60 с, а hy2 вечером — 0–8 подключений за 10 минут: «за минуту ни
одного пропуска» ничего не доказывает. Ровный поток отказов (клиент с мёртвым ключом раз в ~7 с) — норма.
Пробы с entry (18082 — hy2, 18085 — hy2t) — через минуту, на новых соединениях; потом убрать скрипт:

```bash
for p in 18082 18085; do ssh entry "docker exec vpn-bot curl -s -m 20 -x http://probe-proxy:$p -o /dev/null -w '$p %{http_code}\n' https://www.gstatic.com/generate_204"; done
ssh vpn-exit 'rm -f /root/hy2-fresh-check.sh'
```

Время в журналах exit — +05:00 (Asia/Aqtau): абсолютное `journalctl --since` писать с `UTC`.

## 4. Проверка «бот лежит» — на ближайшем деплое бота

Нарочно бота не останавливать: проверкой служит следующий обычный деплой.

```bash
./scripts/deploy_to_entry.sh
ssh vpn-exit 'journalctl -u hy2-auth-relay --since "-10 min" --no-pager | grep -E "from memory|did not answer|key list"'
```

Во время рестарта бота — `the bot did not answer` и `allow … from memory` для тех, кто подключался в
это окно; после — снова `key list: …`. Пропусков из памяти нет в `hy2_auth_log` бота: они только в
журнале релея.

## Откат (в обратном порядке; каждый шаг отдельно)

- **3 (hysteria):** конфиги из бэкапа, рестарт. Drop-in §53 остаётся на месте, так что колбэк сразу
  снова идёт к боту по перегону:
  ```bash
  ssh vpn-exit bash -s -- "$TS" <<'SH'
  for f in config config-turbo; do cp -a "/etc/hysteria/$f.yaml.bak-a6-$1" "/etc/hysteria/$f.yaml"; done
  systemctl restart hysteria hysteria-turbo
  systemctl is-active hysteria hysteria-turbo
  SH
  ```
- **2 (релей):** только после отката шага 3 — иначе hysteria отказывает всем:
  ```bash
  ssh vpn-exit bash -s <<'SH'
  if grep -qE '^[^#]*http://(127\.[0-9.]+|\[::1\]|localhost)(:[0-9]+)?/api/hy2t?/auth' /etc/hysteria/config.yaml /etc/hysteria/config-turbo.yaml; then
    echo 'hysteria still asks the relay: roll back step 3 first'; exit 1
  fi
  systemctl disable --now hy2-auth-relay
  rm -f /etc/systemd/system/hy2-auth-relay.service
  rm -rf /usr/local/lib/nekovpn-hy2-relay
  systemctl daemon-reload
  echo removed
  SH
  ```
- **1 (бот):** маршрут `/api/hy2/keys` можно оставить: закрыт тем же токеном, что и прочие ручки exit,
  и отдаёт только хэши.

## Обновление релея, ручки

- Новый скрипт — шаг 2 заново, пока бот отвечает: рестарт релея опустошает его память (список от бота
  заполняет её за секунду), а на ~1 с рестарта новые hy2-подключения получают отказ.
- `/etc/hy2-auth-relay.env` (необязательный, читается systemd как данные): `HY2_RELAY_MEMORY_S` —
  только уменьшить окно (900 — потолок, 0 — без памяти, релей лишь передаёт запросы);
  `HY2_RELAY_LISTEN` — другой адрес loopback. После правки: `systemctl restart hy2-auth-relay`.
- Смена порта 9979 — и в `url:` обоих конфигов hysteria, и проверка `ss -ltnH` после.

## Что видно

- `curl -s 127.0.0.1:9979/status` на exit: когда бот отвечал и падал, возраст и размер списка, сколько
  ключей помнит каждый маршрут, сколько ответов дал бот и сколько память. Ни ключей, ни хэшей, ни id.
- `journalctl -u hy2-auth-relay`: каждый пропуск из памяти — с id и возрастом; отказы и сбои бота —
  не чаще строки в 10 с.
- healthcheck (§50, §56): `healthcheck:hy2_relay` (critical), когда работающая hysteria спрашивает
  релей, а он не active. После выкатки — переустановить таймер healthcheck на entry
  (`sudo /opt/vpn-bot/scripts/install_protocol_healthcheck.sh`), он запускает root-копию скрипта.
