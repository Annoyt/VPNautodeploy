# Runbook: Deploy

## Быстрый деплой (текущая версия)

```bash
cd /opt/vpn-bot
docker compose up -d
```

## Полный деплой (после обновления кода)

### 1. Бэкап
```bash
cd /opt/vpn-bot
# Бэкап БД
cp /var/lib/docker/volumes/vpn-bot_vpn-bot-data/_data/bot.db \
   ./backups/bot-$(date +%Y%m%d-%H%M%S).db
```

### 2. Обновление кода
```bash
git pull origin main
# Или скопировать новые файлы
```

### 3. Пересборка
```bash
docker compose down
docker compose up -d --build
```

### 4. Проверка
```bash
docker ps
# Должно быть 3 контейнера: 3x-ui, vpn-bot, vpn-traffic-collector
docker logs vpn-bot --tail 20
```

## Роллбэк

```bash
cd /opt/vpn-bot
docker compose down
# Восстановить из бэкапа
cp ./backups/bot-YYYYMMDD-HHMMSS.db \
   /var/lib/docker/volumes/vpn-bot_vpn-bot-data/_data/bot.db
docker compose up -d
```

## Проверка работоспособности

```bash
# Проверка бота
curl http://localhost:8080/health

# Проверка 3X-UI
curl http://localhost:2026/this_is_fine/

# Проверка логов
docker logs vpn-bot --tail 50 | grep -i error
```