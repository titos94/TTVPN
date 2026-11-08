"""Ограниченное хранилище снимков для отдельно изолированного процесса.

Идентичность передаёт доверенный транспорт после аутентификации; этот модуль
её не устанавливает. Сам по себе модуль не обеспечивает изоляцию процесса.
Конфигурация и результаты не должны содержать секретов или пользовательских
идентификаторов. Внешний ввод-вывод и запуск процесса здесь отсутствуют.
"""

from threading import Lock
import math

from isolated_monitor import MAX_AGE, MAX_EVENTS, MAX_TIMESTAMP, aggregate, validate_event


MAX_SLOTS = 100  # Пространство идентификаторов v1: slot-00..slot-99.
MAX_PRODUCERS = 100
MAX_IDENTITY_LENGTH = 128


class MonitorRuntime:
    """Неизменяемая регистрация slot -> component и identity -> множество слотов."""

    def __init__(self, slots, producers):
        if type(slots) is not dict or not 1 <= len(slots) <= MAX_SLOTS:
            raise ValueError('Некорректная регистрация слотов')
        if type(producers) is not dict or not 1 <= len(producers) <= MAX_PRODUCERS:
            raise ValueError('Некорректная регистрация источников')
        registered = {}
        for slot, component in slots.items():
            # Используем именно проверку схемы v1, включая синтаксис ID.
            validate_event({'schema_version': 1, 'component': component, 'id': slot,
                            'timestamp': 0, 'state': 'unknown', 'metrics': {}})
            registered[slot] = component
        permissions = {}
        covered = set()
        for identity, assigned in producers.items():
            if (type(identity) is not str or not 1 <= len(identity) <= MAX_IDENTITY_LENGTH
                    or type(assigned) not in (set, frozenset)
                    or not 1 <= len(assigned) <= MAX_SLOTS
                    or any(type(slot) is not str or slot not in registered for slot in assigned)):
                raise ValueError('Некорректная регистрация источника')
            permissions[identity] = frozenset(assigned)
            covered.update(assigned)
        if covered != set(registered) or len(registered) > MAX_EVENTS:
            raise ValueError('Неполная регистрация источников')
        self._slots = registered
        self._permissions = permissions
        self._snapshots = {}
        self._lock = Lock()

    def accept(self, identity, event, *, now):
        """Принять снимок с доверенным временем приёма; не сохранять будущие данные."""
        if type(identity) is not str or identity not in self._permissions:
            raise ValueError('Источник не зарегистрирован')
        item = validate_event(event)
        if (type(now) not in (int, float) or not 0 <= now <= MAX_TIMESTAMP
                or (type(now) is float and not math.isfinite(now))):
            raise ValueError('Некорректное время приёма')
        if item['timestamp'] > now or now - item['timestamp'] > MAX_AGE:
            raise ValueError('Время события вне допустимого интервала')
        slot = item['id']
        if slot not in self._permissions[identity] or self._slots.get(slot) != item['component']:
            raise ValueError('Слот или компонент не разрешён')
        with self._lock:
            previous = self._snapshots.get(slot)
            if previous is not None and item['timestamp'] <= previous['timestamp']:
                raise ValueError('Повторное или устаревшее событие')
            self._snapshots[slot] = item

    def snapshot(self, *, now):
        """Вернуть только статусы; отсутствующие и просроченные слоты — unknown."""
        with self._lock:
            events = [self._snapshots.get(slot) or {
                'schema_version': 1, 'component': component, 'id': slot,
                'timestamp': 0, 'state': 'unknown', 'metrics': {},
            } for slot, component in self._slots.items()]
            summary = aggregate(events, now=now)
        return {'state': summary['state'],
                'missing_components': summary['missing_components'],
                'slots': [{'id': event['id'], 'component': event['component'],
                           'state': event['state']} for event in summary['events']]}
