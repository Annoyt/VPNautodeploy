# MemPalace Guide for NekoVPN Bot Team

## Что такое MemPalace?

**MemPalace** — система долгосрочной памяти для AI агентов. Она позволяет:
- **Сохранять контекст** разработки между сессиями
- **Делиться знаниями** между разными агентами/разработчиками
- **Быстро находить** информацию о проекте

---

## Быстрый старт

### 1. Инициализация (выполнено)
```bash
cd /home/not_me/antigravity-workspace/agentstuff/VPNautodeploy/vpn-bot-refactor
mempalace init .mempalace
```

### 2. Индексация документации
```bash
# Копируем документацию в palace
cp PROJECT_CONTEXT.md .mempalace/

# Индексируем
mempalace mine .mempalace
```

### 3. Поиск информации
```bash
# Поиск по проекту
mempalace search "VLESS key generation"
mempalace search "how to approve user"
mempalace search "database schema"

# Поиск с фильтрами
mempalace search "X-UI sync" --room general
```

### 4. Wake-up контекст
```bash
# Получить краткий контекст для AI
mempalace wake-up

# Контекст для конкретного wing
mempalace wake-up --wing vpn-bot-refactor
```

---

## Структура памяти

```
Wing: vpn-bot-refactor (проект)
├── Room: general (общая информация)
│   ├── PROJECT_CONTEXT.md
│   └── (другая документация)
├── Room: architecture
│   └── архитектурные решения
├── Room: bugs
│   └── известные проблемы и решения
└── Room: decisions
    └── принятые решения
```

---

## Интеграция с Claude Code

### Вариант 1: MCP Server (рекомендуется)

```bash
# Добавить MCP сервер
claude mcp add mempalace -- python -m mempalace.mcp_server

# Теперь Claude может использовать:
# - mempalace_search
# - mempalace_status
# - mempalace_list_wings
# - mempalace_list_rooms
```

### Вариант 2: Wake-up команда

```bash
# Получить контекст перед сессией
mempalace wake-up > /tmp/context.txt

# Использовать в Claude:
# "Прочитай /tmp/context.txt и помоги с задачей"
```

### Вариант 3: Прямой поиск

```bash
# Найти информацию и передать Claude
mempalace search "как работает approve callback" | claude
```

---

## Примеры использования

### Сценарий 1: Новый разработчик
```bash
# 1. Получить общий контекст
mempalace wake-up

# 2. Найти информацию о тестах
mempalace search "test coverage"

# 3. Понять архитектуру
mempalace search "state machine transitions"
```

### Сценарий 2: Решение проблемы
```bash
# Найти похожие проблемы
mempalace search "database timeout"

# Найти решения
mempalace search "X-UI sync error handling"
```

### Сценарий 3: Добавление фичи
```bash
# Проверить существующие подходы
mempalace search "notification service"

# Найти примеры callback handlers
mempalace search "approve callback handler"
```

---

## Обновление памяти

### После сессии разработки
```bash
# 1. Сохранить изменения в git
# (делается автоматически)

# 2. Обновить документацию при необходимости
vim PROJECT_CONTEXT.md

# 3. Переиндексировать
mempalace mine .mempalace
```

### Добавление новой информации
```bash
# Создать файл с информацией
cat > .mempalace/decision_auth.md << 'EOF'
# Decision: Auth Implementation

Date: 2026-04-07
Decision: Use server-side admin validation
Context: Client-side checks can be bypassed
Consequences: All admin checks in web_server.py
EOF

# Индексировать
mempalace mine .mempalace
```

---

## Полезные команды

```bash
# Статус
mempalace status

# Поиск
mempalace search "query"
mempalace search "query" --wing vpn-bot-refactor
mempalace search "query" --room general

# Wake-up контекст
mempalace wake-up
mempalace wake-up --wing vpn-bot-refactor

# Компрессия (AAAK)
mempalace compress

# Инициализация нового wing
mempalace init ./docs
```

---

## Текущий статус

- **Palace**: `/home/not_me/antigravity-workspace/agentstuff/VPNautodeploy/vpn-bot-refactor/.mempalace`
- **Wing**: `.mempalace`
- **Room**: `general`
- **Files**: PROJECT_CONTEXT.md
- **Status**: Индексация в процессе

---

## Для других агентов

Если вы другой AI агент, читающий это:

1. **Контекст проекта**: `PROJECT_CONTEXT.md` содержит полную информацию
2. **Архитектура**: Phases 0-3 complete, modular design
3. **Тесты**: 247 passed, запускать `python -m pytest`
4. **Docker**: `docker-compose up -d` для запуска
5. **Ключевые файлы**:
   - `bot/core/database.py` - Database operations
   - `bot/handlers/callbacks.py` - Callback routing
   - `bot/services/vpn.py` - VPN key generation
   - `bot/services/xui_sync.py` - X-UI integration

**Важные правила**:
- Callback format: `approve:{chat_id}` (без username!)
- Never restart Docker для X-UI → use `pkill -HUP xray-linux-amd64`
- HTTP API приоритет над DB для X-UI

---

*Generated: 2026-04-07*
*MemPalace v3.0.0*
