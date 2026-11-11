# Runbook: TLS на перегоне exit → entry (:8448)

Фон — AGENTS.md §53. Всё, что exit шлёт боту, идёт через интернет из-за границы в РФ, и до этой
правки шло открытым HTTP на entry :8080:

| Кто на exit | Что везёт | Куда сейчас | Куда после |
|---|---|---|---|
| Caddy `<dashboard-host>:9443` (WEBAPP_URL) | дашборд: initData, admin_token, юзеры, contact_email; каждый `/sub` (uuid, ключи Reality, серверы) | `reverse_proxy 130.49.146.10:8080` | `import entry_hop` → `https://130.49.146.10:8448`, пин |
| Caddy `sub.nekoweather.xyz:2096` (CF-зеркало) | то же для профилей через Cloudflare | то же | то же |
| hysteria (`/etc/hysteria/config.yaml`) | auth-колбэк на каждое hy2-подключение: пароль = uuid юзера | `http://130.49.146.10:8080/api/hy2/auth` | `https://entry-hop.invalid:8448/api/hy2/auth` |
| hysteria-turbo (`config-turbo.yaml`) | то же для hy2t | `…/api/hy2t/auth` | `https://entry-hop.invalid:8448/api/hy2t/auth` |
| exit-dpi-reporter (таймер 5 мин) | `X-DPI-Token`; этот же токен открывает `/api/xhr/users`, `/api/ssh/users` | `BOT_URL` (по умолчанию был `http://130.49.146.10:8080`) | `https://130.49.146.10:8448` + `BOT_CA_FILE` |
| xhr-exit / ssh-exit sync (не развёрнуты) | токен, списки uuid / хэшей | `BOT_URL` = домен дашборда | так и остаётся (TLS до Caddy, дальше пин); или прямо на :8448 с `BOT_CA_FILE` |

С entry на сам entry (`127.0.0.1:8080`: таймер protocol-healthcheck, smoke деплоя, Hermes, healthcheck
докера) не ходит через перегон — их не трогаем.

**Как устроено.** Бот сам слушает HTTPS на 8448 (тот же aiohttp, те же маршруты; `request.remote`
по-прежнему адрес exit). Сертификат самоподписанный, делается один раз внутри контейнера vpn-bot
(`scripts/hop_tls.py init`), ключ не покидает том `vpn-bot_vpn-bot-data` (`/var/lib/vpn-bot/hop-tls/`,
0700/0600). В сертификате два имени: `entry-hop.invalid` и адрес entry; name constraints разрешают
ровно их, так что даже утёкший ключ не подпишет ничего другого. Exit пинит этот сертификат:

- Caddy и репортёр доверяют **только** пину (trust pool Caddy заменяет системные CA; у репортёра
  пин — единственный cafile);
- hysteria не умеет свой CA для auth-колбэка, поэтому drop-in `systemd/hysteria-entry-hop.conf`
  добавляет пин к системным CA (`SSL_CERT_DIR`) и подменяет ей `/etc/hosts` (`BindReadOnlyPaths`),
  чтобы `entry-hop.invalid` указывал на entry. Проверяется имя `.invalid` — его не выпустит ни один
  публичный CA, так что системные CA не могут подменить entry (а IP-сертификаты Let's Encrypt теперь
  выпускает);
- curl в sync-скриптах: `--cacert <пин> --capath /dev/null` (Debian-сборка curl иначе грузит ещё и
  `/etc/ssl/certs`).

Всё это проверено на стенде с версиями exit (`tests/integration/test_hop_tls_stand.py`: Caddy 2.11.3,
hysteria 2.9.2, x509 Go).

## Переменные (на машине оператора, из checkout репо; ssh-алиасы `entry`, `vpn-exit`, root)

```bash
ENTRY_IP=130.49.146.10
EXIT_IP=84.75.76.109
TS=$(date -u +%Y%m%d-%H%M%S)      # одна метка на все бэкапы этого прогона
```

`/opt/vpn-bot/.env`, `/var/lib/vpn-bot/secrets/` и приватные ключи не печатать: ниже из `.env` читаются
только строки `WEB_BIND` / `WEB_TLS_BIND`, из `/etc/exit-dpi.env` — только `BOT_URL` / `BOT_CA_FILE`.

## 0. Предпроверка (только чтение)

```bash
ssh entry 'ss -ltnp "sport = :8448"; iptables -t nat -S | grep -w 8448; echo "--- free if nothing above"'
ssh entry 'iptables -S DOCKER-USER | grep -E "entry-fw-(8080|8448)"; ls -l /etc/iptables/rules.v4'
ssh entry 'cd /opt/vpn-bot && grep -n "8080:8080" docker-compose.yml && grep -E "^WEB_(TLS_)?BIND=" .env'
ssh entry 'docker exec vpn-bot printenv WEBAPP_URL SUB_MIRROR_URLS ENTRY_NODE_IP'
ssh vpn-exit 'caddy version; grep -n "reverse_proxy" /etc/caddy/Caddyfile; ls /etc/nekovpn-hop 2>&1 | tail -1'
ssh vpn-exit 'grep -nE "^\s*(url|insecure):" /etc/hysteria/config.yaml /etc/hysteria/config-turbo.yaml'
ssh vpn-exit 'systemctl is-active hysteria hysteria-turbo caddy exit-dpi-reporter.timer; systemctl cat hysteria hysteria-turbo | grep -nE "^# /|ExecStart|Environment|BindReadOnly"'
ssh vpn-exit 'grep -E "^(BOT_URL|BOT_CA_FILE)=" /etc/exit-dpi.env || echo "BOT_URL: the script default"; python3 --version; curl --version | head -1'
```

Ожидается: 8448 на entry свободен (ни слушателя, ни DNAT); в compose ровно одна строка
`"${WEB_BIND:-127.0.0.1}:8080:8080"`, в `.env` `WEB_BIND=0.0.0.0`; в Caddyfile ровно две строки
`reverse_proxy 130.49.146.10:8080` без собственных блоков `{ … }`; в конфигах hysteria `url:` колбэка
на `http://130.49.146.10:8080/api/hy2/auth` и `…/api/hy2t/auth`, нигде `insecure: true` под `auth`;
`/etc/nekovpn-hop` ещё нет; python3 ≥ 3.7. Иначе — остановиться и разобраться, не править вслепую.
Если `hysteria-turbo` не active — ниже его пропускать (не запускать то, что выключено).

## 1. Код (ничего не меняет в поведении)

```bash
./scripts/deploy_to_entry.sh
```

Слушатель TLS поднимается, только когда в томе бота есть сертификат, — пока его нет. Smoke деплоя
печатает `hop TLS: off …` и проходит.

## 2. Entry: сертификат, фаервол, порт (8080 работает как работал)

2.1 Сертификат (один раз; повтор без `--force` откажется):

```bash
ssh entry 'docker exec -w /app vpn-bot python3 scripts/hop_tls.py init'
```

Печатает `names: DNS:entry-hop.invalid, IP Address:130.49.146.10`, срок (10 лет), `sha256: …` и PEM.
Запомнить sha256 — его сверяем на exit.

2.2 Фаервол — **до** публикации порта, чтобы 8448 ни секунды не был открыт миру (опубликованный
докером порт идёт мимо ufw; матч по `ctorigdstport`, как у entry-fw-8444/2026):

```bash
ssh entry bash -s -- "$EXIT_IP" <<'SH'
set -e
iptables -I DOCKER-USER 1 -p tcp -m conntrack --ctorigdstport 8448 --ctdir ORIGINAL -m comment --comment entry-fw-8448 -j DROP
iptables -I DOCKER-USER 1 -s "$1/32" -p tcp -m conntrack --ctorigdstport 8448 --ctdir ORIGINAL -m comment --comment entry-fw-8448 -j RETURN
iptables -S DOCKER-USER | grep -n entry-fw-8448
SH
```

Порядок в выводе: сначала RETURN (exit), потом DROP. Сохранить в `/etc/iptables/rules.v4` (там полный
iptables-save с цепочками докера — вставляем строки, не пересохраняем целиком):

```bash
ssh entry "cp -a /etc/iptables/rules.v4 /etc/iptables/rules.v4.bak-$TS"
ssh entry python3 - "$EXIT_IP" <<'PY'
import sys
p = '/etc/iptables/rules.v4'
lines = open(p).read().split('\n')
assert not any('entry-fw-8448' in l for l in lines), 'already persisted'
spec = '-p tcp -m conntrack --ctorigdstport 8448 --ctdir ORIGINAL -m comment --comment entry-fw-8448'
add = [f'-A DOCKER-USER -s {sys.argv[1]}/32 {spec} -j RETURN', f'-A DOCKER-USER {spec} -j DROP']
i = next(i for i, l in enumerate(lines) if l.startswith('-A DOCKER-USER'))
lines[i:i] = add
open(p, 'w').write('\n'.join(lines))
print('rules.v4: inserted at line', i + 1)
PY
ssh entry 'iptables-restore --test < /etc/iptables/rules.v4 && echo "rules.v4 ok"'
```

2.3 Порт в compose и `.env` (оба файла на entry ручные — правим точечно, с бэкапом):

```bash
ssh entry "cd /opt/vpn-bot && cp -a docker-compose.yml docker-compose.yml.bak-$TS && cp -a .env .env.bak-$TS"
ssh entry python3 - <<'PY'
p = '/opt/vpn-bot/docker-compose.yml'
s = open(p).read()
old = '      - "${WEB_BIND:-127.0.0.1}:8080:8080"\n'
assert s.count(old) == 1, 'the 8080 line is not as expected: edit by hand'
assert ':8448:8448' not in s, 'already there'
open(p, 'w').write(s.replace(old, old + '      - "${WEB_TLS_BIND:-127.0.0.1}:8448:8448"\n', 1))
e = '/opt/vpn-bot/.env'
t = open(e).read()
if not any(l.startswith('WEB_TLS_BIND=') for l in t.splitlines()):
    open(e, 'a').write(('' if t.endswith('\n') else '\n') + 'WEB_TLS_BIND=0.0.0.0\n')
print('compose + .env: done')
PY
ssh entry 'cd /opt/vpn-bot && grep -n ":8448:8448" docker-compose.yml && grep -E "^WEB_(TLS_)?BIND=" .env'
```

2.4 Пересоздать только vpn-bot (образ уже собран деплоем; ~10–30 с без бота: Telegram, `/sub` через
Caddy и новые hy2-подключения ждут — делать в тихий час):

```bash
ssh entry 'cd /opt/vpn-bot && docker compose up -d --no-deps --no-build --force-recreate vpn-bot'
ssh entry 'docker port vpn-bot; docker logs vpn-bot 2>&1 | grep "hop TLS" | tail -2'
ssh entry 'docker exec -w /app vpn-bot python3 scripts/hop_tls.py check'
```

Ожидается: `8448/tcp -> 0.0.0.0:8448` (и 8080 как раньше), `hop TLS: listening on https://0.0.0.0:8448`,
`{"state": "ok", …, "version": "<sha деплоя>"}`. С машины оператора (не exit) порт закрыт:

```bash
curl -m 5 -sk -o /dev/null -w '%{http_code}\n' https://$ENTRY_IP:8448/health    # 000 = отброшено
```

## 3. Exit: пин и проверка перегона

```bash
ssh entry 'docker exec -w /app vpn-bot python3 scripts/hop_tls.py show --pem' > /tmp/entry-hop.pem
openssl x509 -in /tmp/entry-hop.pem -noout -fingerprint -sha256     # = sha256 из шага 2.1
scp /tmp/entry-hop.pem vpn-exit:/tmp/entry-hop.pem
ssh vpn-exit bash -s -- "$ENTRY_IP" <<'SH'
set -e
install -d -m 0755 /etc/nekovpn-hop
install -m 0644 /tmp/entry-hop.pem /etc/nekovpn-hop/entry-hop.pem
rm /tmp/entry-hop.pem
# hysteria's /etc/hosts (the drop-in binds it): the real one plus the hop's name
{ cat /etc/hosts; printf '\n%s entry-hop.invalid\n' "$1"; } > /etc/nekovpn-hop/hosts
chmod 0644 /etc/nekovpn-hop/hosts
openssl x509 -in /etc/nekovpn-hop/entry-hop.pem -noout -fingerprint -sha256
SH
```

Сертификат публичный, ему можно идти через машину оператора; подлинность — сверкой отпечатка на обоих
концах. Перегон с exit, по имени и по адресу, только с пином:

```bash
ssh vpn-exit bash -s -- "$ENTRY_IP" <<'SH'
pin='--cacert /etc/nekovpn-hop/entry-hop.pem --capath /dev/null'
curl -sS -m 10 $pin --connect-to "entry-hop.invalid:8448:$1:8448" https://entry-hop.invalid:8448/health; echo
curl -sS -m 10 $pin "https://$1:8448/health"; echo
curl -sS -m 10 -o /dev/null "https://$1:8448/health" || echo 'system CAs refused: right'
SH
```

Оба первых — `{"status": "healthy", …, "version": "<sha>"}`; третий обязан упасть
(`self-signed certificate`). Большой ответ (что ТСПУ/MTU не режут длинный TLS-поток; `app.js` —
публичная статика, по открытому каналу утечки нет):

```bash
ssh vpn-exit bash -s -- "$ENTRY_IP" <<'SH'
curl -s -o /dev/null -w 'plain %{http_code} %{size_download}\n' "http://$1:8080/app.js"
curl -s --cacert /etc/nekovpn-hop/entry-hop.pem --capath /dev/null -o /dev/null \
     -w 'tls   %{http_code} %{size_download}\n' "https://$1:8448/app.js"
SH
```

Размеры совпадают, оба 200.

## 4. Exit: DPI-репортёр

```bash
scp scripts/exit_dpi_reporter.py vpn-exit:/usr/local/bin/exit_dpi_reporter.py.new
ssh vpn-exit bash -s -- "$TS" "$ENTRY_IP" <<'SH'
set -e
cp -a /etc/exit-dpi.env "/etc/exit-dpi.env.bak-$1"
cp -a /usr/local/bin/exit_dpi_reporter.py "/usr/local/bin/exit_dpi_reporter.py.bak-$1"
sed -i '/^BOT_URL=/d; /^BOT_CA_FILE=/d' /etc/exit-dpi.env
[ -z "$(tail -c1 /etc/exit-dpi.env)" ] || echo >> /etc/exit-dpi.env
printf 'BOT_URL=https://%s:8448\nBOT_CA_FILE=/etc/nekovpn-hop/entry-hop.pem\n' "$2" >> /etc/exit-dpi.env
install -m 0755 /usr/local/bin/exit_dpi_reporter.py.new /usr/local/bin/exit_dpi_reporter.py
rm /usr/local/bin/exit_dpi_reporter.py.new
grep -E '^(BOT_URL|BOT_CA_FILE)=' /etc/exit-dpi.env
systemctl start exit-dpi-reporter.service || true
journalctl -u exit-dpi-reporter -n 3 --no-pager
SH
```

Ожидается `reported N conns / …`. Новый скрипт отказывается слать токен открытым http не на loopback
(`report not sent: BOT_URL … is plain http …`), так что старый `BOT_URL=http://…:8080` с ним не
заработает — env и скрипт меняются вместе.

## 5. Exit: Caddy (reload без разрыва)

```bash
scp scripts/caddy_entry_hop.py vpn-exit:/tmp/caddy_entry_hop.py
ssh vpn-exit "cp -a /etc/caddy/Caddyfile /etc/caddy/Caddyfile.bak-$TS && python3 /tmp/caddy_entry_hop.py"
```

Сухой прогон печатает diff: обе строки `reverse_proxy 130.49.146.10:8080` → `import entry_hop` и
сниппет после глобального блока (если он есть):

<!-- caddy-snippet:begin -->
```
(entry_hop) {
	reverse_proxy https://130.49.146.10:8448 {
		transport http {
			tls_server_name entry-hop.invalid
			tls_trust_pool file /etc/nekovpn-hop/entry-hop.pem
		}
	}
}
```
<!-- caddy-snippet:end -->

`tls_trust_pool file` заменяет системные CA для этого апстрима: проходит только пин и только с именем
`entry-hop.invalid`. Скрипт отказывается и ничего не пишет, если строк не две, у какой-то есть свой
блок `{ … }`, сниппет уже есть или пина нет; тогда — правка руками по тому же образцу (внутрь чужого
блока `reverse_proxy` добавить `transport http { … }` и сменить апстрим), затем `caddy validate`.
Применение:

```bash
ssh vpn-exit 'python3 /tmp/caddy_entry_hop.py --apply && rm /tmp/caddy_entry_hop.py'
```

`--apply` пишет (своим бэкапом поверх нашего `.bak-$TS`), гоняет `caddy validate` (при отказе
возвращает файл) и делает `systemctl reload caddy`. Если §52 уже перенёс admin Caddy на сокет —
reload идёт через сокет, ничего не меняется. Проверка (с exit, через сам Caddy):

```bash
ssh vpn-exit bash -s <<'SH'
dash=$(grep -m1 -oE '[a-z0-9.-]+\.[a-z]+:9443' /etc/caddy/Caddyfile)
curl -sS -m 10 "https://$dash/health"; echo
curl -sS -m 10 https://sub.nekoweather.xyz:2096/health; echo
SH
ssh entry 'docker exec -w /app vpn-bot python3 scripts/hop_tls.py verify'
```

`verify` ходит из контейнера бота тремя путями — сам слушатель 8448 (с пином), `WEBAPP_URL` (Caddy
на exit) и каждое зеркало из `SUB_MIRROR_URLS` (Cloudflare → Caddy) — за `/health` и профилем
служебного аккаунта `probe_monitor` (`?format=clash` и обычный `/sub`) и требует 200, одну версию и
одинаковые байты. Печатает только коды, размеры и начало sha256; токен и профиль — никогда.
Строка зеркала с `URLError` — это может быть путь entry → Cloudflare, а не перегон: тогда сверить
`curl https://sub.nekoweather.xyz:2096/health` с exit (выше) и с машины оператора.

## 6. Exit: hysteria (рестарт: hy2-сессии и звонки рвутся, клиенты переподключаются за секунды)

Тихий час. Drop-in из репо и правка `url:` колбэка:

```bash
scp systemd/hysteria-entry-hop.conf vpn-exit:/tmp/hysteria-entry-hop.conf
ssh vpn-exit bash -s -- "$TS" "$ENTRY_IP" <<'SH'
set -e
old="http://$2:8080/api/hy2"
for f in /etc/hysteria/config.yaml /etc/hysteria/config-turbo.yaml; do
  [ "$(grep -cF "$old" "$f")" = 1 ] || { echo "$f: the url line is not as expected"; exit 1; }
done
for f in /etc/hysteria/config.yaml /etc/hysteria/config-turbo.yaml; do
  cp -a "$f" "$f.bak-$1"
  sed -i "s|$old|https://entry-hop.invalid:8448/api/hy2|" "$f"
  grep -n 'entry-hop.invalid' "$f"
done
for u in hysteria hysteria-turbo; do
  install -D -m 0644 /tmp/hysteria-entry-hop.conf "/etc/systemd/system/$u.service.d/entry-hop.conf"
done
rm /tmp/hysteria-entry-hop.conf
systemctl daemon-reload
systemctl show hysteria hysteria-turbo -p Environment -p BindReadOnlyPaths
SH
```

`show` должен показать `SSL_CERT_DIR=/etc/ssl/certs:/etc/nekovpn-hop` и `/etc/nekovpn-hop/hosts:/etc/hosts`
у обоих. Рестарт и проверка:

```bash
ssh vpn-exit 'systemctl restart hysteria hysteria-turbo && sleep 3 && systemctl is-active hysteria hysteria-turbo'
ssh entry 'docker exec vpn-bot curl -s -m 20 -x http://probe-proxy:18082 -o /dev/null -w "hy2 probe %{http_code}\n" https://www.gstatic.com/generate_204'
ssh entry 'docker logs --since 5m vpn-bot 2>&1 | grep -c "hy2_auth: allow"'
```

`hy2 probe 204` — проба прошла через hy2, значит колбэк дошёл до бота по TLS и бот пустил. Счётчик
allow > 0 (живые юзеры переподключились). hysteria не пишет причину отказа колбэка даже на debug:
если проба не 204 — откат шага 6 (ниже) и разбор; на стенде без пина клиент получает
`authentication error`, а бот не видит ни одного запроса.

## 7. Наблюдение и закрытие 8080

Через сутки после шагов 4–6 на 8080 с exit не должно ходить ничего. Счётчики правил DOCKER-USER это
показывают (обнулить — и смотреть):

```bash
ssh entry 'iptables -Z DOCKER-USER'
# … час, лучше сутки …
ssh entry 'iptables -L DOCKER-USER -v -n -x | grep -E "entry-fw-(8080|8448)"'
```

У RETURN-правила 8080 для адреса exit — 0 пакетов, у RETURN 8448 — растёт. Если 8080 не ноль: что-то
на exit ещё ходит открыто — найти (`ss -tnp "dport = :8080"` на exit) прежде чем закрывать.
Закрыть (мгновенно, без пересоздания бота):

```bash
ssh entry bash -s <<'SH'
r=$(iptables -S DOCKER-USER | grep -- '--comment entry-fw-8080' | grep -- '-j RETURN')
printf '%s\n' "$r"
[ "$(printf '%s\n' "$r" | grep -c .)" = 1 ] || { echo 'not exactly one RETURN rule for 8080: look first'; exit 1; }
iptables ${r/-A/-D} && echo closed
SH
ssh vpn-exit "curl -m 5 -s -o /dev/null -w '%{http_code}\n' http://$ENTRY_IP:8080/health"     # 000
```

Записать напечатанную строку `-A DOCKER-USER … -j RETURN` — это откат. Убрать её же из
`/etc/iptables/rules.v4`:

```bash
ssh entry "cp -a /etc/iptables/rules.v4 /etc/iptables/rules.v4.bak-8080-$TS"
ssh entry python3 - <<'PY'
p = '/etc/iptables/rules.v4'
lines = open(p).read().split('\n')
hit = [l for l in lines if 'entry-fw-8080' in l and '-j RETURN' in l]
assert len(hit) == 1, hit
lines.remove(hit[0])
open(p, 'w').write('\n'.join(lines))
print('removed:', hit[0])
PY
ssh entry 'iptables-restore --test < /etc/iptables/rules.v4 && echo "rules.v4 ok"'
```

Позже, на ближайшем
плановом деплое: `WEB_BIND=127.0.0.1` в `.env` entry (8080 остаётся только для своих на entry) +
`docker compose up -d --no-deps --no-build --force-recreate vpn-bot`; `docker port vpn-bot` →
`8080/tcp -> 127.0.0.1:8080`.

## Откат (в обратном порядке; каждый шаг отдельно)

- **7 (8080 закрыт):** `iptables -I DOCKER-USER 1 <записанная строка без -A DOCKER-USER>` и строку назад
  в rules.v4; если уже `WEB_BIND=127.0.0.1` — вернуть `0.0.0.0` и пересоздать vpn-bot.
- **6 (hysteria):**
  ```bash
  ssh vpn-exit bash -s -- "$TS" <<'SH'
  for f in config config-turbo; do cp -a "/etc/hysteria/$f.yaml.bak-$1" "/etc/hysteria/$f.yaml"; done
  rm -f /etc/systemd/system/hysteria.service.d/entry-hop.conf \
        /etc/systemd/system/hysteria-turbo.service.d/entry-hop.conf
  systemctl daemon-reload && systemctl restart hysteria hysteria-turbo
  systemctl is-active hysteria hysteria-turbo
  SH
  ```
- **5 (Caddy):**
  ```bash
  ssh vpn-exit "cp -a /etc/caddy/Caddyfile.bak-$TS /etc/caddy/Caddyfile && caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile && systemctl reload caddy"
  ```
- **4 (репортёр):**
  ```bash
  ssh vpn-exit "cp -a /etc/exit-dpi.env.bak-$TS /etc/exit-dpi.env && cp -a /usr/local/bin/exit_dpi_reporter.py.bak-$TS /usr/local/bin/exit_dpi_reporter.py && systemctl start exit-dpi-reporter.service"
  ```
- **2 (entry):** слушатель 8448 можно оставить — он никому не мешает. Убрать совсем: правила
  `entry-fw-8448` (`iptables -D` по двум строкам из шага 2.2 и из rules.v4), compose и `.env` из
  `.bak-$TS`, пересоздать vpn-bot. Сертификат в томе слушатель включает сам — `hop_tls.py` его
  только делает; удалить `/var/lib/vpn-bot/hop-tls` = выключить.

## Ротация сертификата, смена адреса entry

- Новый сертификат: `hop_tls.py init --force [--ip <новый адрес>]` → пересоздать vpn-bot (сертификат
  читается на старте) → шаг 3 (новый пин, отпечаток) → `systemctl reload caddy` (пул читается при
  загрузке конфига) → рестарт hysteria (корни кэшируются в процессе); репортёр читает файл на каждом
  запуске. Между новым сертификатом и новым пином exit отрезан (подписки 502, новые hy2 не входят,
  отчёты не доходят) — делать одним заходом.
- Срок — 10 лет (`hop_tls.py show`).
- Смена IP entry: `--ip`, строка в `/etc/nekovpn-hop/hosts`, апстрим в сниппете Caddy, `BOT_URL`
  репортёра, правила фаервола — плюс всё остальное, что смена entry и так тянет.

## Что проверяет деплой

`scripts/deploy_to_entry.sh` после `/health` на 8080 зовёт `hop_tls.py check`: нет сертификата —
`off`, пропуск; есть — HTTPS-слушатель обязан ответить той же версией через пин (имя
`entry-hop.invalid`) и нести в сертификате `ENTRY_NODE_IP`, иначе деплой падает: после переключения
exit без слушателя отваливаются все подписки, hy2 и отчёты.
