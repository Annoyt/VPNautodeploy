# Runbook: бэкап сервиса и восстановление из него

Фон — AGENTS.md §58. Раз в сутки таймер на entry собирает в один архив всё, что нужно, чтобы
поднять сервис заново: bot.db и x-ui.db (согласованные копии), ключи, секреты, конфиги entry и exit,
настройки Hermes. Архив шифруется открытым ключом age, и бот отправляет его в топик «Бэкапы»
админ-группы. Расшифровать его может только закрытый ключ владельца.

| Что | Где |
|---|---|
| Скрипт | `scripts/service_backup.py` → root-копия `/usr/local/lib/nekovpn-backup/service_backup.py` на entry |
| Таймер | `nekovpn-backup.timer` (каждый день 01:40 UTC, ± 10 мин) → `nekovpn-backup.service` |
| Отправка | бот: `scripts/backup_send.py` в контейнере vpn-bot, через пул `TG_PROXY_URLS` |
| Куда | топик «Бэкапы» в `FORUM_GROUP_ID` (бот создаёт его сам при первой отправке) |
| Открытый ключ | `/etc/nekovpn-backup/recipients.txt` на entry |
| Закрытый ключ | только у владельца: `~/.config/nekovpn-signing/nekovpn-backup-age.key` |
| Локальные копии | `/var/lib/nekovpn-backup/archives` на entry: последние 7, зашифрованные |
| Алерт | `backup:stale` (critical, в топик AI): полного бэкапа нет 26 ч |

**Что внутри** (полный список с назначением каждого пути — `README.md` в самом архиве):

- `entry/entry.tar` — файлы entry по их путям:
  - том бота (`bot.db` — копия, снятая в контейнере; `secrets/`; `hop-tls/`);
  - `/opt/vpn-bot/.env` и `docker-compose.yml`, сайдкары проб;
  - haproxy, shadow-tls, iptables, ufw;
  - RU-зона (`/etc/ru-exit`), ssh-relay;
  - юниты и root-копии скриптов;
  - Hermes, только настройки и состояние: `config.yaml`, `.env`, `auth.json`, `SOUL.md`, `memories/`,
    `cron/`, наши скиллы, копии `state.db`, `response_store.db`, `kanban.db`, `AGENTS.md`. Его
    рантайм (`tools/` — ~1,5 ГБ у v0.23.1, кэши, логи, `sandboxes/`, стоковые скиллы) не входит.
    Если `~/.hermes` — ссылка (v0.23 → `/root/.hermes-v023`), файлы лежат под настоящим каталогом,
    ссылка — рядом;
  - ssh root и ключи хоста, crontab, sysctl;
  - `nekovpn-facts/` — версии, живые правила iptables, контейнеры, `systemctl cat`.
- `exit/exit.tar` — то же для exit:
  - `x-ui.db` (копия);
  - compose, `.env`, сертификаты панели;
  - hysteria и drop-in перегона, Caddy с его сертификатами;
  - пин перегона `/etc/nekovpn-hop`, xhr-exit, ssh-exit;
  - tinyproxy, правила, acme.sh с токеном Cloudflare;
  - ключи хоста.
- `README.md`, `MANIFEST.json` — каждый файл с размером и sha256.

Не входят: логи, кэши, venv, образы, бинарники из `/usr/local/bin` (только их список), файлы
больше 20 МБ (только в списке; это GeoIP City, 127 МБ, — бот скачает его сам), резервная DE-нода
(бот сам переустанавливает её клиентов).

Не входит и таблица `netstat_events` из bot.db — диагностика сети из приложения NekoVPN
(AGENTS.md §67). Эти записи живут 14 дней только в живой базе. Копия bot.db снимается в контейнере,
и таблица удаляется из неё до того, как копия выходит из контейнера: страницы обнуляются
(`secure_delete`), файл пересобирается (`VACUUM`). Живая база при этом только читается.
Список таких таблиц — `BOT_DB_LEFT_OUT` в `scripts/service_backup.py`.

## Ключ

Пара сгенерирована 17.11.2026 на машине владельца (`age-keygen` 1.2.1):

| | |
|---|---|
| закрытый | `~/.config/nekovpn-signing/nekovpn-backup-age.key` (0600) |
| открытый | `~/.config/nekovpn-signing/nekovpn-backup-age.pub` |
| значение открытого | `age18kpa37v26zpe3t0hzaznfwur8g9e95hcl6awzta9yue7ard3e5cqjtk48j` |

- **Закрытый ключ никогда не кладётся на сервер.** Инсталлер отказывается принять его вместо
  открытого и не печатает его.
- **Его потеря = все бэкапы бесполезны.** Сохранить офлайн вместе с ключом подписи APK (§40).
- **Новая пара:**
  1. на своей машине `age-keygen -o ~/.config/nekovpn-signing/nekovpn-backup-age-2.key`;
  2. на entry `install_service_backup.sh --recipient <новый открытый> --force`.

  Старые архивы открываются только старым закрытым ключом.
- **На своей машине** `age` ставится так: `sudo apt install age zstd`.

## Установка (один раз, по слову владельца)

Сделано 17.11.2026: бот `a963b2c`, age 1.1.1 из apt, zstd уже стоял. Шаги ниже — для
переустановки или нового entry.

С машины владельца, из checkout репо; ssh-алиасы `entry` и `vpn-exit` (root).

### 0. Предпроверка (только чтение)

```bash
ssh entry 'command -v age zstd; python3 --version; systemctl is-active docker'
ssh entry 'ssh -o BatchMode=yes -o ConnectTimeout=8 exit-node "python3 --version"'
ssh entry 'docker exec vpn-bot python3 -c "import sqlite3; print(sqlite3.sqlite_version)"'
ssh entry 'df -h /var/lib | tail -1'
```

Нужно: python3 на обоих узлах (alias `exit-node` у root на entry уже есть — его использует
healthcheck, §50) и около 1 ГБ свободного места на entry. Сборка занимает до ~0,5 ГБ, семь
хранимых архивов — ~410 МБ (сейчас архив ~58 МБ, растёт вместе с `state.db` Hermes).

### 1. Пакеты на entry

```bash
ssh entry 'apt-get install -y age && age --version && { command -v zstd || apt-get install -y zstd; }'
```

Уже стоящий zstd не переустанавливать: `apt-get install` обновил бы его, а с ним libzstd, общую
для всего узла.

### 2. Бот с отправителем

`scripts/backup_send.py` должен быть в образе бота. Это обычный деплой:

```bash
./scripts/deploy_to_entry.sh
```

### 3. Таймер

```bash
ssh entry "sudo /opt/vpn-bot/scripts/install_service_backup.sh --recipient $(cat ~/.config/nekovpn-signing/nekovpn-backup-age.pub)"
```

Инсталлер:
- кладёт root-копию скрипта и юниты;
- включает таймер;
- говорит боту, что бэкап установлен: с этой минуты алерт ждёт архивов.

### 4. Что войдёт (пробный прогон: ничего не шифрует, не хранит и не отправляет)

```bash
ssh entry '/usr/bin/python3 /usr/local/lib/nekovpn-backup/service_backup.py --dry-run'
```

Печатает пути и размеры, не содержимое. Там должны быть:
- `ok` у тома бота, `/opt/vpn-bot/.env`, `/etc/haproxy`, Hermes;
- `ok` у `/etc/hysteria`, `/etc/caddy` и копии `x-ui.db` на exit.

`absent` у необязательного пути — нормально (его просто нет на этом узле).

### 5. Первый бэкап

```bash
ssh entry 'systemctl start nekovpn-backup.service; journalctl -u nekovpn-backup -n 30 --no-pager'
ssh entry 'docker exec vpn-bot python3 /app/scripts/backup_send.py status'
```

В топике «Бэкапы» появится архив (топик бот создаёт сам при первой отправке; он есть
с 17.11.2026). Код выхода сервиса:
- `0` — архив полный;
- `1` — ушёл без части (причина — в подписи и в журнале);
- `2` — не ушёл ничего.

### 6. Проверить архив на своей машине

Скачать файл из топика и прогнать проверку ниже. Делать это не только после установки, но и время
от времени: бэкап, который ни разу не открывали, бэкапом не считается.

## Проверка архива (на машине владельца)

Переменные:

```bash
KEY=~/.config/nekovpn-signing/nekovpn-backup-age.key
ARCHIVE=~/Downloads/nekovpn-backup-20261117T014012Z.tar.zst.age    # файл из топика «Бэкапы»
```

Если архив пришёл частями (`.001`, `.002`…, когда он больше 45 МБ; при нынешних ~58 МБ частей
всегда две), сначала склеить:

```bash
cat "$ARCHIVE".[0-9][0-9][0-9] > "$ARCHIVE"
```

Расшифровать, распаковать, сверить sha256 каждого файла с манифестом и прогнать
`PRAGMA integrity_check` по обеим базам. Запускать ровно этот блок: его гоняет
`tests/unit/test_service_backup.py`.

```bash
RESTORE=${RESTORE:-$(mktemp -d)}
age -d -i "$KEY" "$ARCHIVE" | zstd -d | tar -x -C "$RESTORE"
STEM=$(basename "$(ls -d "$RESTORE"/nekovpn-backup-*)")
mkdir -p "$RESTORE/entry" "$RESTORE/exit"
for part in entry exit; do
    if [ -f "$RESTORE/$STEM/$part/$part.tar" ]; then
        tar -xpf "$RESTORE/$STEM/$part/$part.tar" -C "$RESTORE/$part" --numeric-owner
    fi
done
python3 - "$RESTORE" "$STEM" <<'PY'
import hashlib, json, sqlite3, sys
from pathlib import Path
root, stem = Path(sys.argv[1]), sys.argv[2]
manifest = json.loads((root / stem / 'MANIFEST.json').read_text())
bad = 0
for part in ('entry', 'exit'):
    pm = (manifest['parts'].get(part) or {}).get('manifest')
    if not pm:
        print(f'{part}: нет в архиве — {manifest["parts"].get(part, {}).get("error")}')
        bad += 1
        continue
    for f in pm['files']:
        data = (root / part / f['path'].lstrip('/')).read_bytes()
        if hashlib.sha256(data).hexdigest() != f['sha256']:
            print(f'{part}: sha256 не сошёлся: {f["path"]}')
            bad += 1
    print(f'{part}: файлов {len(pm["files"])}, sha256 сверены')
    for db in pm['databases']:
        if db.get('status') != 'ok':
            continue
        conn = sqlite3.connect(root / part / db['path'].lstrip('/'))
        check = conn.execute('PRAGMA integrity_check').fetchone()[0]
        tables = [t for t in ('users', 'clients') if t in (db.get('tables') or {})]
        counts = ', '.join(f'{t}: {conn.execute(f"SELECT count(*) FROM {t}").fetchone()[0]}'
                           for t in tables)
        conn.close()
        print(f'{part}: {db["path"]}: integrity_check {check}' + (f', {counts}' if counts else ''))
        bad += check != 'ok'
problems = [p for part in manifest['parts'].values()
            for p in ((part.get('manifest') or {}).get('problems') or [])]
if bad:
    print(f'АРХИВ ПОВРЕЖДЁН ИЛИ НЕПОЛОН: проблем {bad}')
elif not manifest['complete']:
    print('архив цел, но неполон: ' + ('; '.join(problems) or 'см. MANIFEST.json'))
else:
    print('архив цел')
sys.exit(1 if bad else 0)
PY
echo "распаковано в $RESTORE"
```

Распакованное содержит все секреты сервиса. Посмотрел — удалить: `rm -rf "$RESTORE"`.

## Восстановление

Порядок и команды ниже. Перед любым шагом, который меняет живой узел, — свой бэкап того, что
заменяется (`cp -a … .before-restore-$TS`).

```bash
TS=$(date -u +%Y%m%d-%H%M%S)
```

### Один файл

Самый частый случай: сломался один конфиг. Достать его из архива и положить на место, сравнив
с живым:

```bash
mkdir -p /tmp/one && tar -xpf "$RESTORE/$STEM/exit/exit.tar" -C /tmp/one --numeric-owner etc/caddy/Caddyfile
diff -u <(ssh vpn-exit cat /etc/caddy/Caddyfile) /tmp/one/etc/caddy/Caddyfile
```

### bot.db (entry)

1. Бот остановлен.
2. Старые `-wal`/`-shm` удалены. Иначе sqlite наложит журнал старой базы на восстановленный
   файл, и база испортится.
3. Том развёрнут из архива с владельцем 1000:1000. На новом сервере том сначала создаётся
   (`docker volume create`), иначе docker о нём не знает.

```bash
scp "$RESTORE/$STEM/entry/entry.tar" entry:/root/restore-entry.tar
ssh entry "cd /opt/vpn-bot && docker compose stop vpn-bot && docker volume create vpn-bot_vpn-bot-data && V=/var/lib/docker/volumes/vpn-bot_vpn-bot-data/_data && { [ ! -e \$V/bot.db ] || cp -a \$V/bot.db \$V/bot.db.before-restore-$TS; } && rm -f \$V/bot.db-wal \$V/bot.db-shm && tar -xpf /root/restore-entry.tar -C / --numeric-owner var/lib/docker/volumes/vpn-bot_vpn-bot-data/_data && ./scripts/deploy_entry_bot.sh"
ssh entry 'rm -f /root/restore-entry.tar; curl -s http://127.0.0.1:8080/health'
```

`deploy_entry_bot.sh` поднимает только vpn-bot (`--no-deps`, 3x-ui не трогает).

В восстановленной bot.db нет таблицы `netstat_events`: её нет в архиве. Бот создаёт её сам при первом
отчёте приложения, так что делать ничего не надо.

### x-ui.db (exit)

Панель остановлена, старые `-wal`/`-shm` удалены — по той же причине, что у bot.db.

```bash
scp "$RESTORE/$STEM/exit/exit.tar" vpn-exit:/root/restore-exit.tar
ssh vpn-exit "docker stop 3x-ui; docker volume create vpn-bot_3xui-data && X=/var/lib/docker/volumes/vpn-bot_3xui-data/_data && { [ ! -e \$X/x-ui.db ] || cp -a \$X/x-ui.db \$X/x-ui.db.before-restore-$TS; } && rm -f \$X/x-ui.db-wal \$X/x-ui.db-shm && tar -xpf /root/restore-exit.tar -C / --numeric-owner var/lib/docker/volumes/vpn-bot_3xui-data/_data/x-ui.db && docker start 3x-ui"
ssh vpn-exit 'rm -f /root/restore-exit.tar'
```

Клиенты, которых бот выдал после бэкапа, в восстановленной панели отсутствуют. Найти их можно
сверкой панели с ботом: `scripts/paid_keys_audit.py` (§44), «Мой ключ» у пользователя.

### Узел целиком

Новый сервер, те же роли. Если пропали оба узла, первым поднимается exit: боту нужна его панель.
Код — из репо, состояние — из архива. Подробный список путей и назначение каждого — в
`README.md` архива.

**exit:**

1. Ключи хоста `etc/ssh/ssh_host_*` и `root/.ssh` — до первого входа других машин, тогда отпечаток
   прежний.
2. docker, затем `opt/vpn-bot/docker-compose.yml` и `.env`. x-ui.db — шагом выше. После этого
   `docker compose up -d 3x-ui`, только этот сервис. Образ взять по digest из compose: xray не
   новее 26.7.10, иначе отвалится Reality в NekoVPN (§51).
3. hysteria той же версии (`nekovpn-facts/hysteria-version.txt`):
   - `etc/hysteria/*`;
   - юниты `hysteria*` с drop-in `entry-hop`;
   - `etc/nekovpn-hop/*`;
   - релей авторизации: `install_hy2_auth_relay.sh` (§56). `etc/hy2-auth-relay.env` — только если
     он есть в архиве: файл необязателен, и на exit его нет.
4. Caddy: `etc/caddy`, `var/lib/caddy/.local/share/caddy`.
5. acme.sh: `root/.acme.sh`, `etc/ssl/sub`, `etc/ssl/cdn` и crontab из архива.
6. tinyproxy (`etc/tinyproxy`), правила (`etc/ufw`, `usr/local/bin/exit_firewall.sh`), sysctl,
   `etc/gai.conf`. `/etc/iptables` на exit нет: живые правила для сверки —
   `nekovpn-facts/iptables-save.txt`.
7. xhr-exit и ssh-exit: `etc/xhr-exit*`, `etc/ssh-exit*`, затем `setup_xhr_exit.sh` и
   `setup_ssh_exit.sh`. Ключи берутся из этих каталогов, так что клиентам менять ничего не надо.
8. DPI-репортёр, коллектор hy2, watchdog: файлы из `usr/local/bin`, юниты, `etc/exit-dpi.env`.

**entry:**

1. Ключи хоста и `root/.ssh`: алиас `exit-node` и ключ, которым entry ходит на exit.
2. docker, haproxy (`etc/haproxy`), shadow-tls: юнит из архива, команда и пароль — в
   `nekovpn-facts/systemctl-cat.txt` (`/etc/shadow-tls*` на entry нет). Затем
   `etc/iptables/rules.v4`.
3. `/opt/vpn-bot`: код из репо (`scripts/deploy_to_entry.sh`), `.env` и `docker-compose.yml` из
   архива. Том бота — по шагу «bot.db» выше, затем сам бот.
4. RU-зона (`etc/ru-exit`, затем `setup_ru_exit.sh`) и ssh-relay (`etc/ssh-relay`, затем
   `setup_ssh_entry_relay.sh`).
5. Сайдкары проб: `opt/vpn-bot/probe-proxy*`.
6. Таймеры: `install_protocol_healthcheck.sh` и `install_service_backup.sh` (`etc/nekovpn-backup`
   хранит открытый ключ).
7. Hermes той же версии (`nekovpn-facts/hermes-pip-freeze.txt`): `config.yaml`, `.env`,
   `auth.json`, `SOUL.md`, `memories/`, `cron/`, наши скиллы и базы `state.db`, `response_store.db`,
   `kanban.db` из `root/.hermes` (или из `root/.hermes-v023` и ссылку `root/.hermes` — как было),
   `root/hermes-work/AGENTS.md`, затем `deploy_hermes_host.sh`. Базы — при остановленном
   `hermes-api`, без старых `-wal`/`-shm`.

**Новый адрес** у entry или exit:

- `ENTRY_NODE_IP` / `EXIT_NODE_IP` в `.env`;
- DNS доменов (дашборд, `cdn.`, `sub.`);
- правила DOCKER-USER (`entry-fw-*`);
- новый адрес entry: новый сертификат перегона (`hop_tls.py init --force --ip`) и его пин на exit
  (§53, `docs/runbooks/entry_hop_tls.md`).

## Сбои

Алерт `backup:stale` (critical, топик AI) называет причину последнего запуска. Где смотреть:

```bash
ssh entry 'systemctl status nekovpn-backup.timer nekovpn-backup.service --no-pager; journalctl -u nekovpn-backup -n 60 --no-pager'
ssh entry 'docker exec vpn-bot python3 /app/scripts/backup_send.py status'
```

| Причина в алерте / журнале | Что делать |
|---|---|
| `exit: ssh rc=255…` | entry не достучался до exit: проверить `ssh entry 'ssh exit-node uptime'`. Остальной архив ушёл |
| `bot.db: …` | копия в контейнере не снялась: бот жив? `docker ps`; архив ушёл без bot.db |
| `отправка: Telegram: …` | архив собран, лежит в `/var/lib/nekovpn-backup/archives`, бот не смог отдать его в Telegram: прокси (§24), права бота в группе |
| `не смог создать топик «Бэкапы»` | нет связи с Telegram (прокси, §24) или у бота нет права Manage Topics в группе. Что ответил Telegram, покажет `ssh entry 'docker exec vpn-bot python3 /app/scripts/backup_send.py topic --create'` |
| `топик «Бэкапы» закрыт` | открыть топик в группе |
| `на entry нет age` / `zstd` | поставить недостающий: `apt-get install age` или `apt-get install zstd`. Уже стоящий не переустанавливать (см. «Пакеты на entry») |
| `в … лежит СЕКРЕТНЫЙ ключ age` | удалить файл получателей, поставить заново с открытым ключом, сделать новую пару ключей |
| `другой запуск бэкапа ещё идёт` | подождать: запуски не пересекаются |
| `прерван сигналом SIGTERM` | юнит не уложился в 45 мин или его остановили: журнал покажет, на каком шаге |

Запустить заново вручную: `ssh entry 'systemctl start nekovpn-backup.service'`. Отправить уже
собранный архив, если не ушла только отправка: запустить сервис ещё раз (он соберёт свежий).

## Удаление

```bash
ssh entry 'sudo /opt/vpn-bot/scripts/install_service_backup.sh --remove'
```

Снимает таймер и root-копию и говорит боту забыть состояние, после чего алерт молчит. Открытый
ключ и локальные архивы остаются.
