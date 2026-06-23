# Phase 2: Node Manager Service ✅

## Статус
**Завершена:** 2026-04-07

## Выполненные задачи
- [x] Создать `NodeManager` класс
- [x] Реализовать health checks через API
- [x] Реализовать автоматический failover detection
- [x] Создать кэширование API клиентов
- [x] Добавить методы балансировки нагрузки
- [x] Написать тесты (16 шт)

## Созданные файлы

### Основной сервис
**Файл:** `bot/services/node_manager.py`

Ключевые методы:
- `get_all_nodes()` — список нод с фильтрацией
- `get_node()` — получение по ID
- `get_primary_exit()` — primary Exit нода
- `get_available_entry()` — лучшая Entry нода (с учетом нагрузки)
- `health_check()` — проверка здоровья ноды
- `health_check_all()` — проверка всех нод параллельно
- `find_best_exit_for_user()` — выбор лучшей Exit ноды для пользователя
- `assign_user_to_node()` — назначение пользователя на ноды
- `get_node_stats()` — статистика ноды

### Тесты
**Файл:** `tests/unit/test_node_manager.py` (16 тестов)

Покрытие:
- Получение нод (все, по ID, фильтрация)
- Выбор primary ноды
- Балансировка нагрузки (предпочтение менее загруженной)
- Geo-выбор (регион пользователя)
- Health checks (успех/неудача)
- Статистика нод

## Архитектура

```python
NodeManager
├── get_api_client(node_id) → XUIAPIClient (cached)
├── health_check(node_id) → {healthy, latency_ms, ...}
├── health_check_all() → параллельная проверка всех нод
├── find_best_exit_for_user(chat_id) → алгоритм выбора
│   ├── Учет текущего региона
│   ├── Загрузка нод (current/max)
│   ├── Приоритет primary ноды
│   └── Fallback на любую доступную
└── assign_user_to_node(chat_id, exit_id, entry_id)
    └── Auto-select entry если не указан
```

## Алгоритм выбора ноды

### Для Exit Node:
1. Текущий регион пользователя (если есть)
2. Загрузка ноды: `current_clients / max_clients`
3. Приоритет `is_primary`
4. Вес (`weight`)

### Для Entry Node:
1. Регион пользователя (geo-близость)
2. Загрузка ноды
3. Primary preference

## Health Check

**Успех:**
- Получение списка inbounds через API
- Измерение latency
- Статус → ACTIVE

**Неудача:**
- Ловим Exception
- Статус → DEGRADED
- Логируем ошибку

## Кэширование

API клиенты кэшируются в `self._api_clients[node_id]` для переиспользования сессий.

## Тесты

| Метод | Покрытие |
|-------|----------|
| get_all_nodes | ✅ Фильтрация по типу/статусу |
| get_node | ✅ Найден/не найден |
| get_primary_exit | ✅ Primary выбор, fallback |
| get_available_entry | ✅ Geo, load balancing |
| health_check | ✅ Успех, неудача |
| health_check_all | ✅ Параллельный запуск |
| find_best_exit_for_user | ✅ Алгоритм выбора |
| assign_user_to_node | ✅ Назначение |
| get_node_stats | ✅ Статистика |

**Итого:** 16/16 тестов ✅

## Интеграция с БД

Использует методы из Phase 1:
- `db.get_nodes()`
- `db.get_node()`
- `db.update_node_status()`
- `db.assign_user_to_node()`
- `db.get_user_nodes()`
- `db.get_users_on_node()`

## Следующий шаг
→ [Phase 3 - Multi-Node X-UI Sync](Phase%203%20-%20Multi-Node%20X-UI%20Sync.md)