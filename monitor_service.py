"""Изолированный монитор состояния TTVPN.

Отдельный непривилегированный процесс. У него нет томов с данными панели,
billing.db, VPN-конфигурации и секретов оплаты. Он принимает только
обезличенные состояния слотов от панели, сам проверяет HTTP-доступность
панели и Caddy и отдаёт администратору ограниченную сводку.

Запуск: python3 monitor_service.py
Переменные: TTVPN_MONITOR_TOKEN (обязательно, >= 32 символов),
TTVPN_MONITOR_BIND (по умолчанию 0.0.0.0:5001),
TTVPN_MONITOR_PANEL_URL, TTVPN_MONITOR_PROXY_URL.
"""

from __future__ import annotations

import json
import os
import re
import secrets
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from isolated_monitor import MAX_AGE, STATES, aggregate, validate_event

MAX_BODY = 64 * 1024
MAX_EVENTS = 64
MIN_TOKEN_LENGTH = 32
PROBE_INTERVAL = 15
PROBE_MAX_AGE = 60
SLOT = re.compile(r'slot-[0-9]{2}\Z', re.ASCII)
PROXY_SLOT = 'slot-99'
PROBE_COMPONENTS = frozenset(('panel', 'proxy'))
ISSUE_ORDER = ('panel_down', 'panel_degraded', 'proxy_down', 'proxy_degraded',
               'telegram_down', 'vpn_nodes_down', 'vpn_nodes_degraded',
               'vpn_nodes_unknown', 'panel_data_stale', 'panel_data_missing')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401 - stdlib hook
        return None


def _iso(timestamp: float) -> str:
    return datetime.fromtimestamp(timestamp, tz=timezone.utc).isoformat()


def _number(value, low, high):
    return type(value) in (int, float) and low <= value <= high


class MonitorStore:
    def __init__(self, *, panel_url: str = '', proxy_url: str = ''):
        self.panel_url = panel_url
        self.proxy_url = proxy_url
        self._lock = threading.Lock()
        self._events: dict[str, dict] = {}
        self._health: dict = {}
        self._last_push = 0.0
        self._probes: dict[str, dict] = {}
        self._started = time.time()

    # --- приём обезличенных снимков -------------------------------------

    def ingest(self, body: dict, *, now: float) -> int:
        if not isinstance(body, dict):
            raise ValueError('bad body')
        events = body.get('events')
        if not isinstance(events, list) or not 1 <= len(events) <= MAX_EVENTS:
            raise ValueError('bad events')
        accepted = []
        for raw in events:
            item = validate_event(raw)
            if item['id'] == PROXY_SLOT:
                raise ValueError('reserved slot')
            if item['timestamp'] > now + 30 or now - item['timestamp'] > MAX_AGE:
                raise ValueError('stale event')
            accepted.append(item)

        active = body.get('active_vpn_slots')
        if active is not None:
            if (not isinstance(active, list) or len(active) > MAX_EVENTS
                    or any(type(slot) is not str or SLOT.fullmatch(slot) is None
                           for slot in active)):
                raise ValueError('bad active slots')
            active_set = set(active)
        else:
            active_set = None

        health = body.get('health')
        clean_health = {}
        if isinstance(health, dict):
            disk = health.get('disk_percent')
            if _number(disk, 0, 100):
                clean_health['disk_percent'] = round(float(disk), 1)
            load = health.get('load_average_1m')
            if _number(load, 0, 10000):
                clean_health['load_average_1m'] = round(float(load), 2)
            enabled = health.get('telegram_enabled')
            if type(enabled) is bool:
                clean_health['telegram_enabled'] = enabled

        with self._lock:
            seen = set()
            for item in accepted:
                slot = item['id']
                if slot in seen:
                    raise ValueError('duplicate slot')
                seen.add(slot)
                previous = self._events.get(slot)
                if previous is not None:
                    if previous['component'] != item['component']:
                        raise ValueError('slot reuse')
                    if item['timestamp'] <= previous['timestamp']:
                        raise ValueError('stale slot')
            for item in accepted:
                self._events[item['id']] = item
            if active_set is not None:
                for slot, item in list(self._events.items()):
                    if item['component'] == 'vpn_node' and slot not in active_set:
                        del self._events[slot]
            if clean_health:
                self._health.update(clean_health)
            self._last_push = now
        return len(accepted)

    # --- независимые HTTP-проверки --------------------------------------

    def _probe_url(self, url: str) -> str:
        if not url:
            return 'unknown'
        opener = urllib.request.build_opener(NoRedirect())
        request = urllib.request.Request(url, method='GET',
                                         headers={'User-Agent': 'ttvpn-monitor/1'})
        try:
            with opener.open(request, timeout=5) as response:
                return 'ok' if response.status < 500 else 'degraded'
        except urllib.error.HTTPError as error:
            return 'ok' if error.code < 500 else 'degraded'
        except (urllib.error.URLError, OSError, ValueError):
            return 'down'

    def probe_forever(self) -> None:
        while True:
            now = time.time()
            results = {
                'panel': self._probe_url(self.panel_url),
                'proxy': self._probe_url(self.proxy_url),
            }
            with self._lock:
                for component, state in results.items():
                    self._probes[component] = {'state': state, 'timestamp': now}
            time.sleep(PROBE_INTERVAL)

    # --- сводка ---------------------------------------------------------

    def snapshot(self, *, now: float) -> dict:
        with self._lock:
            events = [dict(item) for item in self._events.values()]
            health = dict(self._health)
            last_push = self._last_push
            probes = {component: dict(value)
                      for component, value in self._probes.items()}

        by_component = {item['component']: item for item in events}
        for component in PROBE_COMPONENTS:
            probe = probes.get(component)
            if component == 'proxy':
                event = {'schema_version': 1, 'component': 'proxy', 'id': PROXY_SLOT,
                         'timestamp': 0, 'state': 'unknown', 'metrics': {}}
                if probe:
                    event['timestamp'] = int(probe['timestamp'])
                    event['state'] = probe['state']
                events.append(event)
                continue
            if not probe:
                continue
            state = probe['state']
            timestamp = int(probe['timestamp'])
            if now - timestamp > PROBE_MAX_AGE:
                state, timestamp = 'unknown', 0
            current = by_component.get('panel')
            if current is not None:
                # An HTTP 200 must not hide a degraded disk state reported by
                # the panel; keep the worst of the two observations.
                pushed = current['state']
                if state == 'down' or pushed == 'down':
                    state = 'down'
                elif state == 'degraded' or pushed == 'degraded':
                    state = 'degraded'
                elif state == 'unknown' or pushed == 'unknown':
                    state = 'unknown'
                current['state'] = state
                current['timestamp'] = timestamp
            else:
                events.append({'schema_version': 1, 'component': 'panel', 'id': 'slot-00',
                               'timestamp': timestamp, 'state': state, 'metrics': {}})

        if not events:
            events = [
                {'schema_version': 1, 'component': component, 'id': f'slot-{index:02d}',
                 'timestamp': 0, 'state': 'unknown', 'metrics': {}}
                for index, component in enumerate(('panel', 'telegram', 'proxy', 'vpn_node'))
            ]

        summary = aggregate(events, now=now)
        groups: dict[str, list[dict]] = {}
        for item in summary['events']:
            groups.setdefault(item['component'], []).append(item)

        components = []
        issues = []
        for component in ('panel', 'telegram', 'proxy', 'vpn_node'):
            items = groups.get(component, [])
            states = {item['state'] for item in items}
            state = next((candidate for candidate in ('down', 'degraded', 'unknown', 'ok')
                          if candidate in states), 'unknown')
            entry = {'component': component, 'state': state}
            if component == 'vpn_node':
                for name in ('ok', 'degraded', 'down', 'unknown'):
                    entry[name] = sum(1 for item in items if item['state'] == name)
                if entry['down']:
                    issues.append('vpn_nodes_down')
                if entry['degraded']:
                    issues.append('vpn_nodes_degraded')
                if entry['unknown'] or not items:
                    issues.append('vpn_nodes_unknown')
            elif state == 'down':
                issues.append(component + '_down')
            elif state == 'degraded':
                issues.append(component + '_degraded')
            components.append(entry)

        if last_push and now - last_push > 90:
            issues.append('panel_data_stale')
        if not last_push:
            issues.append('panel_data_missing')

        order = {code: index for index, code in enumerate(ISSUE_ORDER)}
        issues = sorted(set(issues), key=lambda code: order.get(code, 99))
        overall = summary['state']
        status = 'attention' if overall in ('down', 'degraded') else overall
        if overall == 'unknown':
            status = 'attention' if 'down' in issues else 'unknown'
        return {
            'monitor': 'isolated',
            'status': status,
            'state': overall,
            'checked_at': _iso(now),
            'components': components,
            'health': health,
            'issues': issues,
            'last_push_at': _iso(last_push) if last_push else None,
        }


class Handler(BaseHTTPRequestHandler):
    server_version = 'ttvpn-monitor'
    sys_version = ''
    protocol_version = 'HTTP/1.1'

    def log_message(self, format, *args):  # noqa: A002 - stdlib signature
        return

    def _send(self, status: int, body: dict) -> None:
        data = json.dumps(body, ensure_ascii=False, separators=(',', ':')).encode()
        self.send_response(status)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.send_header('Cache-Control', 'no-store')
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(data)

    def _authorized(self) -> bool:
        expected = self.server.token  # type: ignore[attr-defined]
        supplied = self.headers.get('X-Monitor-Token', '')
        return bool(supplied and secrets.compare_digest(supplied, expected))

    def do_GET(self):  # noqa: N802 - stdlib signature
        if self.path == '/healthz':
            return self._send(200, {'ok': True})
        if self.path != '/status':
            return self._send(404, {'error': 'not found'})
        if not self._authorized():
            return self._send(401, {'error': 'unauthorized'})
        now = time.time()
        return self._send(200, self.server.store.snapshot(now=now))  # type: ignore[attr-defined]

    def do_POST(self):  # noqa: N802 - stdlib signature
        if self.path != '/ingest':
            return self._send(404, {'error': 'not found'})
        if not self._authorized():
            # The body was not read; close so it cannot be parsed as a new
            # request on a reused keep-alive connection.
            self.close_connection = True
            return self._send(401, {'error': 'unauthorized'})
        try:
            length = int(self.headers.get('Content-Length', '0'))
        except ValueError:
            self.close_connection = True
            return self._send(400, {'error': 'bad request'})
        if not 0 < length <= MAX_BODY:
            self.close_connection = True
            return self._send(413, {'error': 'too large'})
        raw = self.rfile.read(length)
        try:
            body = json.loads(raw)
            stored = self.server.store.ingest(body, now=time.time())  # type: ignore[attr-defined]
        except (ValueError, TypeError, UnicodeDecodeError):
            return self._send(400, {'error': 'bad request'})
        return self._send(200, {'status': 'accepted', 'stored': stored})


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, token: str, store: MonitorStore):
        super().__init__(address, Handler)
        self.token = token
        self.store = store


def bind_address() -> tuple[str, int]:
    raw = os.environ.get('TTVPN_MONITOR_BIND', '0.0.0.0:5001').strip()
    host, separator, port = raw.rpartition(':')
    if not separator or not host:
        raise SystemExit('TTVPN_MONITOR_BIND must be host:port')
    try:
        number = int(port)
    except ValueError:
        raise SystemExit('TTVPN_MONITOR_BIND must be host:port') from None
    if not 1 <= number <= 65535:
        raise SystemExit('TTVPN_MONITOR_BIND must be host:port')
    return host, number


def main() -> None:
    token = os.environ.get('TTVPN_MONITOR_TOKEN', '').strip()
    if len(token) < MIN_TOKEN_LENGTH:
        raise SystemExit('TTVPN_MONITOR_TOKEN is required and must be at least 32 characters')
    store = MonitorStore(
        panel_url=os.environ.get('TTVPN_MONITOR_PANEL_URL', '').strip(),
        proxy_url=os.environ.get('TTVPN_MONITOR_PROXY_URL', '').strip(),
    )
    threading.Thread(target=store.probe_forever, daemon=True, name='probes').start()
    server = MonitorServer(bind_address(), token, store)
    server.serve_forever()


if __name__ == '__main__':
    main()
