# ADR 001: API vs DB Access для 3X-UI

## Статус
**Принято:** 2026-04-07

## Контекст
Сейчас бот использует прямой доступ к SQLite БД 3X-UI через volume mount. Это создает проблемы:
- Требует прав на запись в volume
- Конфликты при одновременном доступе
- Невозможно масштабировать на несколько нод

## Решение
Использовать **HTTP API 3X-UI** как primary способ, с fallback на прямой доступ к БД.

## Последствия

### Положительные
- ✅ Нет необходимости в volume mount
- ✅ Масштабируемость на несколько нод
- ✅ Чистая архитектура (нет coupling через ФС)
- ✅ Возможность использовать 3X-UI на удаленных серверах

### Отрицательные
- ❌ Зависимость от HTTP API (если 3X-UI недоступен — не работает)
- ❌ Latency выше, чем локальный SQLite
- ❌ Нужно управлять сессиями и cookie

## Альтернативы

| Подход | За | Против |
|--------|-----|--------|
| Только DB | Быстро, надежно | Не масштабируется |
| Только API | Чистая архитектура | Зависимость от API |
| **API + DB Fallback** ✅ | Лучшее от обоих | Сложнее в реализации |

## Реализация
```python
class XUIService:
    def __init__(self, api_config: dict, db_path: str):
        self.api = XUIAPIClient(api_config)  # Primary
        self.db = XUIDatabase(db_path)        # Fallback
    
    async def get_client_traffic(self, email: str):
        # Try API first
        try:
            return await self.api.get_client_traffic(email)
        except:
            # Fallback to DB
            return self.db.get_client_traffic(email)
```

## Ссылки
- [Phase 3: Multi-Node X-UI Sync](../GSD/Phase%203%20-%20Multi-Node%20X-UI%20Sync.md)