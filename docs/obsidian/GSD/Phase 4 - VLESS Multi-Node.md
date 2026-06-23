# Phase 4: VLESS Multi-Node ⏳

## Статус
**В очереди:** Следующая фаза

## Цель
Обновить VPNService для генерации VLESS-ссылок с учетом multi-node (разные Entry/Exit ноды).

## Задачи
- [ ] Обновить `VPNService.generate_vless_link()`
- [ ] Добавить метод `generate_vless_link_for_user()`
- [ ] Интеграция с NodeManager для получения нод пользователя
- [ ] Поддержка разных Entry/Exit комбинаций
- [ ] Тесты

## Архитектура

```python
class VPNService:
    async def generate_vless_link_for_user(chat_id, user=None)
        # Получить ноды пользователя
        # Если нет назначения — найти лучшие
        # Сгенерировать VLESS с параметрами нод
```

## Зависимости
- Phase 1: Node Registry ✅
- Phase 2: Node Manager ✅
- Phase 3: X-UI Sync ✅

## Блокеры
- Нет

## Оценка
- Длительность: 1 день
- Сложность: Средняя