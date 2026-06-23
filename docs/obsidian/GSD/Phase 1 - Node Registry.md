# Phase 1: Node Registry ✅

## Статус
**Завершена:** 2026-04-07

## Выполненные задачи
- [x] Создать таблицу `nodes`
- [x] Создать таблицу `user_nodes`
- [x] Создать таблицу `node_failover_log`
- [x] Создать модель `Node` (dataclass)
- [x] Добавить CRUD методы в Database
- [x] Написать скрипт миграции

## Созданные файлы

### База данных
**Файл:** `bot/core/database.py`

Добавлены таблицы:
- `nodes` — информация о нодах (Entry/Exit)
- `user_nodes` — связь пользователей с нодами
- `node_failover_log` — история переключений

Добавлены методы:
- `create_node()` — создание ноды
- `get_node()` — получение по ID
- `get_nodes()` — список с фильтрацией
- `get_primary_node()` — получение primary ноды
- `update_node_status()` — обновление статуса
- `update_node_clients_count()` — обновление счетчика клиентов
- `assign_user_to_node()` — назначение пользователя
- `get_user_nodes()` — получение назначения пользователя
- `get_users_on_node()` — список пользователей на ноде
- `log_failover()` — логирование переключения

### Модели
**Файл:** `bot/models/node.py`

Созданы dataclass:
- `Node` — основная модель ноды
- `UserNodeAssignment` — назначение пользователя
- `NodeFailoverLog` — лог переключения

Enum:
- `NodeType` — ENTRY/EXIT
- `NodeStatus` — ACTIVE/MAINTENANCE/OFFLINE/DEGRADED

### Миграция
**Файл:** `scripts/migrate_nodes.py`

Создает:
- Initial Exit node из конфигурации
- Привязку существующих пользователей

## Схема таблиц

### nodes
| Поле | Тип | Описание |
|------|-----|----------|
| id | INTEGER PK | ID ноды |
| name | TEXT | "exit-frankfurt-1" |
| type | TEXT | "entry" или "exit" |
| host | TEXT | IP или домен |
| api_port | INTEGER | Порт API (2026) |
| vpn_port | INTEGER | Порт VPN (443) |
| public_key | TEXT | Для VLESS Reality |
| sni | TEXT | SNI домен |
| region | TEXT | "eu", "ru", "us" |
| status | TEXT | active/offline/etc |
| is_primary | BOOLEAN | Главная нода |
| current_clients | INTEGER | Текущее число клиентов |

## Тесты
- Все существующие тесты проходят ✅
- Новая схема совместима с текущей БД

## Следующий шаг
→ [Phase 2 - Node Manager Service](Phase%202%20-%20Node%20Manager%20Service.md)