# GSD: Multi-Node API Architecture

## Цель
Переход от прямого доступа к SQLite (DB mode) к HTTP API с поддержкой нескольких Exit/Entry Nodes и автоматического failover.

## Статус
**Текущая фаза:** Phase 0 - Cleanup ✅ (Завершена)

## Таймлайн
| Фаза | Статус | Длительность |
|------|--------|--------------|
| Phase 0: Cleanup | ✅ Done | 2 дня |
| Phase 1: Node Registry | ✅ Done | 1 день |
| Phase 2: Node Manager | ✅ Done | 2 дня |
| Phase 3: Multi-Node X-UI Sync | ✅ Done | 2 дня |
| Phase 4: VLESS Multi-Node | ⏳ Next | 1 день |
| Phase 5: Health Monitor | ⏳ Pending | 2 дня |
| Phase 6: Admin Commands | ⏳ Pending | 1 день |
| Phase 7: Testing | ⏳ Pending | 2 дня |

## Ключевые Даты
- **Начало:** 2026-04-07
- **Планируемое завершение:** ~13 дней
- **MVP готовность:** 4-5 дней

## Архитектура
- **Current:** [Current Architecture](../Architecture/Current.md)
- **Target:** [Target Multi-Node Architecture](../Architecture/Target%20Multi-Node.md)

## Runbooks
- [Deploy](../Runbooks/Deploy.md)
- [Failover](../Runbooks/Failover.md)

## Решения (ADR)
- [001: API vs DB Access](../Decisions/001-API-vs-DB-Access.md)