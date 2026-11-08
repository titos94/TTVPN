"""Автономная обработка обезличенных снимков без ввода-вывода.

Схема v1: только встроенные dict/list и примитивы; timestamp и now —
Unix-время в секундах. ID — заранее назначенный слот slot-00..slot-99,
а не имя, адрес или идентификатор клиента. Вызывающий обязан сам обеспечить
это соответствие и доверенность источника: проверка синтаксиса не доказывает
обезличенность или подлинность данных.

Проценты CPU, памяти и диска показывают насыщение ресурсов; диапазон 0..100.
Они не определяют состояние автоматически: state задаётся источником.
Функции не накапливают события и не читают часы или внешнее состояние.
"""

import math
import re


COMPONENTS = frozenset(('panel', 'telegram', 'proxy', 'vpn_node'))
STATES = frozenset(('ok', 'degraded', 'down', 'unknown'))
METRICS = frozenset(('cpu_percent', 'memory_percent', 'disk_percent'))
MAX_EVENTS = 256
MAX_TIMESTAMP = 4_102_444_800  # 2100-01-01 UTC
MAX_AGE = 300
_FIELDS = frozenset(('schema_version', 'component', 'id', 'timestamp', 'state', 'metrics'))
_ID = re.compile(r'slot-[0-9]{2}\Z', re.ASCII)


def _number(value, low, high):
    return (type(value) in (int, float) and low <= value <= high
            and (type(value) is int or math.isfinite(value)))


def validate_event(event):
    """Вернуть независимую копию снимка или вызвать ValueError без данных ввода."""
    if type(event) is not dict or len(event) != len(_FIELDS):
        raise ValueError('Некорректная схема события')
    if any(type(k) is not str for k in event) or set(event) != _FIELDS:
        raise ValueError('Некорректные поля события')
    version, component, identifier = event['schema_version'], event['component'], event['id']
    timestamp, state, metrics = event['timestamp'], event['state'], event['metrics']
    if type(version) is not int or version != 1:
        raise ValueError('Некорректная версия схемы')
    if type(component) is not str or component not in COMPONENTS:
        raise ValueError('Некорректный тип компонента')
    if type(identifier) is not str or _ID.fullmatch(identifier) is None:
        raise ValueError('Некорректный идентификатор')
    if not _number(timestamp, 0, MAX_TIMESTAMP):
        raise ValueError('Некорректное время события')
    if type(state) is not str or state not in STATES:
        raise ValueError('Некорректное состояние')
    if type(metrics) is not dict or len(metrics) > len(METRICS):
        raise ValueError('Некорректные метрики')
    for name, value in metrics.items():
        if type(name) is not str or name not in METRICS or not _number(value, 0, 100):
            raise ValueError('Некорректная метрика')
    return {'schema_version': 1, 'component': component, 'id': identifier,
            'timestamp': timestamp, 'state': state, 'metrics': dict(metrics)}


def aggregate(events, *, now):
    """Сводка одного ограниченного пакета; дубликаты (компонент, слот) запрещены.

    Все четыре типа обязательны. Отсутствие конкретной ожидаемой реплики при
    наличии другой реплики того же типа здесь не обнаруживается. Устаревшие
    более чем на MAX_AGE секунд и любые будущие снимки становятся unknown.
    Приоритет сводки: down, degraded, unknown, ok.
    """
    if type(events) is not list or len(events) > MAX_EVENTS:
        raise ValueError('Некорректный пакет событий')
    if not _number(now, 0, MAX_TIMESTAMP):
        raise ValueError('Некорректное текущее время')
    seen = set()
    result = []
    for raw in events:
        item = validate_event(raw)
        key = (item['component'], item['id'])
        if key in seen:
            raise ValueError('Повторный слот компонента')
        seen.add(key)
        if item['timestamp'] > now or now - item['timestamp'] > MAX_AGE:
            item['state'] = 'unknown'
            item['metrics'] = {}
        result.append(item)
    result.sort(key=lambda item: (item['component'], item['id']))
    missing = sorted(COMPONENTS - {item['component'] for item in result})
    states = {item['state'] for item in result}
    if missing:
        states.add('unknown')
    overall = next(state for state in ('down', 'degraded', 'unknown', 'ok')
                   if state in states)
    return {'state': overall, 'missing_components': missing, 'events': result}
