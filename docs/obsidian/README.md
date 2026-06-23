# VPN Bot Obsidian Vault

Этот Vault содержит документацию проекта VPN Bot.

## Структура

```
docs/obsidian/
├── GSD/                    # Get Shit Done workflow
│   ├── GSD Overview.md     # Общий обзор проекта
│   ├── Phase 0 - Cleanup.md
│   ├── Phase 1 - Node Registry.md
│   └── ...
├── Architecture/           # Архитектурная документация
│   ├── Current.md          # Текущая архитектура
│   └── Target Multi-Node.md # Целевая архитектура
├── Runbooks/              # Операционные процедуры
│   ├── Deploy.md
│   └── Failover.md
├── Decisions/             # Architecture Decision Records (ADR)
│   └── 001-API-vs-DB-Access.md
└── assets/                # Изображения, диаграммы
```

## Использование

1. Открой эту папку как Vault в Obsidian
2. Начни с [[GSD Overview]]
3. Следи за прогрессом в GSD фазах

## Горячие клавиши

- `Ctrl+O` - Быстрое открытие файла
- `Ctrl+Shift+F` - Поиск по всем заметкам
- `[[` - Ссылка на другую заметку
- `Cmd/Ctrl + Click` - Переход по ссылке

## Синхронизация

Vault находится в git. После изменений:
```bash
git add docs/obsidian/
git commit -m "docs: обновление заметок"
git push
```