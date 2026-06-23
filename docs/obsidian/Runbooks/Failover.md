# Runbook: Failover

## Ручной Failover пользователя

### Проверка текущей ноды
```bash
# Внутри vpn-bot контейнера
docker exec vpn-bot python3 -c "
from bot.core.database import Database
db = Database('/var/lib/vpn-bot/bot.db')
user = db.get_user('CHAT_ID')
print(f'User: {user.username}')
print(f'Email: {user.email}')
"
```

### Переключение на резервную ноду

**Через Telegram (для админа):**
```
/failover @username
```

**Через Python (ручной):**
```bash
docker exec vpn-bot python3 -c "
import asyncio
from bot.services.node_manager import NodeManager
from bot.services.xui_sync import XUISyncService

async def failover():
    nm = NodeManager(...)
    xui = XUISyncService(nm, ...)
    success = await xui.failover_user('CHAT_ID', OLD_NODE_ID, 'manual')
    print(f'Failover success: {success}')

asyncio.run(failover())
"
```

## Массовый Failover (если нода упала)

```bash
# Получить список пользователей на упавшей ноде
docker exec vpn-bot python3 -c "
from bot.core.database import Database
db = Database('/var/lib/vpn-bot/bot.db')
# TODO: добавить метод get_users_on_node
"

# Переключить всех
for user in users:
    # failover каждого
```

## Проверка после Failover

```bash
# Проверить, что пользователь на новой ноде
docker exec vpn-bot python3 -c "
from bot.services.node_manager import NodeManager
nm = NodeManager(...)
nodes = nm.get_user_nodes('CHAT_ID')
print(nodes)
"
```