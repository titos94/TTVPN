"""Обезличенные снимки для изолированного монитора.

Панель владеет данными и секретами, но наружу уходят только состояния
слотов и числовые показатели. Имена серверов, адреса, идентификаторы
пользователей, ключи и конфигурация в события не попадают.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import time
from datetime import datetime, timezone

import httpx

from isolated_monitor import STATES

MAX_NODES = 32
ISSUE_CODES = frozenset((
    'panel_down', 'panel_degraded', 'proxy_down', 'proxy_degraded',
    'telegram_down', 'vpn_nodes_down', 'vpn_nodes_degraded',
    'vpn_nodes_unknown', 'panel_data_stale', 'panel_data_missing',
))
PANEL_SLOT = 'slot-00'
TELEGRAM_SLOT = 'slot-01'
NODE_SLOTS = tuple(f'slot-{index:02d}' for index in range(2, 2 + MAX_NODES))
NODE_PROBE_TIMEOUT = 3.0


def monitor_config() -> tuple[str, str]:
    url = os.environ.get('TTVPN_MONITOR_URL', '').strip().rstrip('/')
    token = os.environ.get('TTVPN_MONITOR_TOKEN', '').strip()
    return url, token


def monitor_configured() -> bool:
    url, token = monitor_config()
    return bool(url and token)


def disk_percent(path: str | None = None) -> float | None:
    """Занятость диска в процентах; путь нужен, чтобы мерить том данных."""
    try:
        usage = shutil.disk_usage(path or '/')
    except OSError:
        return None
    if not usage.total:
        return None
    return round(100 * usage.used / usage.total, 1)


def load_average() -> float | None:
    try:
        return round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        return None


def _state(value: str, fallback: str = 'unknown') -> str:
    return value if value in STATES else fallback


def build_events(*, now: float, telegram_enabled: bool, telegram_running: bool,
                 node_states: list[str], disk_percent_value: float | None) -> list[dict]:
    """Собрать ограниченный пакет событий без пользовательских данных."""
    timestamp = int(now)
    if disk_percent_value is None:
        panel_state = 'degraded'
        metrics = {}
    elif 0 <= disk_percent_value <= 90:
        panel_state = 'ok'
        metrics = {'disk_percent': disk_percent_value}
    else:
        panel_state = 'degraded'
        metrics = {'disk_percent': disk_percent_value}

    if not telegram_enabled:
        telegram_state = 'ok'
    else:
        telegram_state = 'ok' if telegram_running else 'down'

    events = [
        {'schema_version': 1, 'component': 'panel', 'id': PANEL_SLOT,
         'timestamp': timestamp, 'state': panel_state, 'metrics': metrics},
        {'schema_version': 1, 'component': 'telegram', 'id': TELEGRAM_SLOT,
         'timestamp': timestamp, 'state': telegram_state, 'metrics': {}},
    ]

    states = [_state(value) for value in node_states[:MAX_NODES]]
    if not states:
        # Ноль узлов — это не «всё хорошо», а отсутствие достоверных данных.
        states = ['unknown']
    for slot, state in zip(NODE_SLOTS, states):
        events.append({'schema_version': 1, 'component': 'vpn_node', 'id': slot,
                       'timestamp': timestamp, 'state': state, 'metrics': {}})
    return events


def active_node_slots(node_states: list[str]) -> list[str]:
    count = min(len(node_states), MAX_NODES) or 1
    return list(NODE_SLOTS[:count])


async def probe_node_states(servers: list[dict]) -> list[str]:
    """Проверить только TCP-доступность SSH-порта, без авторизации и команд."""
    async def probe(server: dict) -> str:
        host = server.get('host')
        if not isinstance(host, str) or not host.strip():
            return 'unknown'
        try:
            port = int(server.get('ssh_port', 22))
        except (TypeError, ValueError):
            return 'unknown'
        if not 1 <= port <= 65535:
            return 'unknown'
        try:
            _, writer = await asyncio.wait_for(
                asyncio.open_connection(host.strip(), port), NODE_PROBE_TIMEOUT)
            writer.close()
            try:
                await writer.wait_closed()
            except (OSError, ConnectionError):
                pass
            return 'ok'
        except (OSError, asyncio.TimeoutError, ValueError):
            return 'down'

    if not isinstance(servers, list):
        return []
    targets = servers[:MAX_NODES]
    return list(await asyncio.gather(*(probe(server) for server in targets
                                       if isinstance(server, dict))))


def build_push_payload(*, data: dict, telegram_enabled: bool, telegram_running: bool,
                       disk_percent_value: float | None, node_states: list[str],
                       load_average_1m: float | None = None,
                       now: float | None = None) -> dict:
    now = time.time() if now is None else now
    return {
        'events': build_events(now=now, telegram_enabled=telegram_enabled,
                               telegram_running=telegram_running,
                               node_states=node_states,
                               disk_percent_value=disk_percent_value),
        'active_vpn_slots': active_node_slots(node_states),
        'health': {
            'disk_percent': disk_percent_value,
            'load_average_1m': load_average_1m,
            'telegram_enabled': bool(telegram_enabled),
        },
    }


async def push_once(client: httpx.AsyncClient, *, url: str, token: str,
                    payload: dict) -> dict:
    response = await client.post(url + '/ingest', json=payload,
                                 headers={'X-Monitor-Token': token})
    response.raise_for_status()
    body = response.json()
    if not isinstance(body, dict) or body.get('status') != 'accepted':
        raise ValueError('monitor rejected snapshot')
    return body


def _bounded_number(value, low: float, high: float):
    if type(value) in (int, float) and low <= value <= high:
        return float(value)
    return None


def normalize_status(body: dict, *, now: float | None = None) -> dict | None:
    """Проверить ответ монитора и привести его к безопасной форме для страницы."""
    if not isinstance(body, dict):
        return None
    status = body.get('status')
    if status not in ('ok', 'attention', 'unknown'):
        return None
    components = body.get('components')
    if not isinstance(components, list) or not components:
        return None
    clean_components = []
    for item in components:
        if not isinstance(item, dict):
            return None
        component = item.get('component')
        state = item.get('state')
        if component not in ('panel', 'telegram', 'proxy', 'vpn_node') or state not in STATES:
            return None
        entry = {'component': component, 'state': state}
        if component == 'vpn_node':
            counts = {}
            for name in ('ok', 'degraded', 'down', 'unknown'):
                value = item.get(name, 0)
                if type(value) is not int or not 0 <= value <= 1000:
                    return None
                counts[name] = value
            entry.update(counts)
        clean_components.append(entry)

    checked_at = body.get('checked_at')
    if checked_at is not None and (not isinstance(checked_at, str) or len(checked_at) > 64):
        return None
    health = body.get('health')
    clean_health = {}
    if isinstance(health, dict):
        disk = _bounded_number(health.get('disk_percent'), 0, 100)
        if disk is not None:
            clean_health['disk_percent'] = disk
            clean_health['disk_free_percent'] = round(100 - disk, 1)
        load = _bounded_number(health.get('load_average_1m'), 0, 10000)
        if load is not None:
            clean_health['load_average_1m'] = load
        enabled = health.get('telegram_enabled')
        if type(enabled) is bool:
            clean_health['telegram_enabled'] = enabled
    issues = body.get('issues')
    clean_issues = [issue for issue in issues if isinstance(issue, str)
                    and issue in ISSUE_CODES] \
        if isinstance(issues, list) else []
    return {
        'monitor': 'isolated',
        'status': status,
        'state': body.get('state') if body.get('state') in STATES else 'unknown',
        'issues': clean_issues[:16],
        'components': clean_components,
        'health': clean_health,
        'checked_at': checked_at,
        'received_at': datetime.now(timezone.utc).isoformat(),
        'now': now,
    }


async def fetch_status() -> dict | None:
    url, token = monitor_config()
    if not url or not token:
        return None
    try:
        async with httpx.AsyncClient(timeout=4, follow_redirects=False, trust_env=False) as client:
            response = await client.get(url + '/status',
                                        headers={'X-Monitor-Token': token})
            response.raise_for_status()
            return normalize_status(response.json())
    except (httpx.HTTPError, ValueError, TypeError):
        return None
