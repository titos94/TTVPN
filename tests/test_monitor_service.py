"""Изолированный монитор: хранение, проверки и HTTP-контракт."""
import json
import threading
import time
import urllib.error
import urllib.request

import monitor_export
from monitor_service import MonitorServer, MonitorStore

TOKEN = 'test-monitor-token-' + 'x' * 32


def _payload(now=None, nodes=('ok', 'down')):
    now = time.time() if now is None else now
    return monitor_export.build_push_payload(
        data={'servers': []}, telegram_enabled=True, telegram_running=True,
        disk_percent_value=25, node_states=list(nodes),
        load_average_1m=0.4, now=now)


def _probe(store, now, panel='ok', proxy='ok'):
    store._probes = {'panel': {'state': panel, 'timestamp': now},
                     'proxy': {'state': proxy, 'timestamp': now}}


def test_snapshot_reports_fresh_states_and_counts():
    now = time.time()
    store = MonitorStore()
    assert store.ingest(_payload(now), now=now) == 4
    _probe(store, now)
    report = store.snapshot(now=now)
    assert report['monitor'] == 'isolated'
    assert report['status'] == 'attention'
    assert report['state'] == 'down'
    assert 'vpn_nodes_down' in report['issues']
    components = {item['component']: item for item in report['components']}
    assert components['panel']['state'] == 'ok'
    assert components['proxy']['state'] == 'ok'
    assert components['vpn_node']['ok'] == 1
    assert components['vpn_node']['down'] == 1
    assert report['health']['disk_percent'] == 25


def test_probe_failure_overrides_pushed_panel_state():
    now = time.time()
    store = MonitorStore()
    store.ingest(_payload(now, nodes=('ok',)), now=now)
    _probe(store, now, panel='down')
    report = store.snapshot(now=now)
    components = {item['component']: item for item in report['components']}
    assert components['panel']['state'] == 'down'
    assert report['status'] == 'attention'
    assert 'panel_down' in report['issues']


def test_http_probe_does_not_hide_degraded_panel_state():
    now = time.time()
    store = MonitorStore()
    payload = _payload(now, nodes=('ok',))
    payload['events'][0]['state'] = 'degraded'
    payload['events'][0]['metrics'] = {'disk_percent': 95}
    store.ingest(payload, now=now)
    _probe(store, now, panel='ok')
    report = store.snapshot(now=now)
    components = {item['component']: item for item in report['components']}
    assert components['panel']['state'] == 'degraded'
    assert 'panel_degraded' in report['issues']


def test_empty_store_is_unknown_not_ok():
    now = time.time()
    report = MonitorStore().snapshot(now=now)
    assert report['status'] == 'unknown'
    assert 'panel_data_missing' in report['issues']
    assert 'vpn_nodes_unknown' in report['issues']


def test_stale_push_is_flagged_and_old_events_become_unknown():
    now = time.time()
    store = MonitorStore()
    store.ingest(_payload(now - 400, nodes=('ok',)), now=now - 400)
    _probe(store, now)
    report = store.snapshot(now=now)
    assert report['status'] == 'unknown'
    assert 'panel_data_stale' in report['issues']


def test_ingest_rejects_reserved_and_invalid_events():
    now = time.time()
    store = MonitorStore()
    base = {'schema_version': 1, 'component': 'vpn_node', 'id': 'slot-99',
            'timestamp': int(now), 'state': 'ok', 'metrics': {}}
    for payload in (
        {'events': [base]},
        {'events': [dict(base, id='slot-02', timestamp=int(now) + 3600)]},
        {'events': []},
        {'events': 'nope'},
    ):
        try:
            store.ingest(payload, now=now)
        except ValueError:
            pass
        else:
            raise AssertionError('payload accepted: ' + repr(payload))


def test_ingest_rejects_stale_and_conflicting_slots():
    now = time.time()
    store = MonitorStore()
    store.ingest(_payload(now, nodes=('ok',)), now=now)
    events = _payload(now + 1, nodes=('ok',))['events']
    events[2]['timestamp'] = int(now)  # older than the stored slot-02
    try:
        store.ingest({'events': [events[2]]}, now=now + 1)
    except ValueError:
        pass
    else:
        raise AssertionError('stale slot accepted')
    conflicting = dict(events[2], component='telegram', timestamp=int(now) + 2)
    try:
        store.ingest({'events': [conflicting]}, now=now + 2)
    except ValueError:
        pass
    else:
        raise AssertionError('slot reuse accepted')


def test_http_requires_token_and_accepts_valid_push():
    now = time.time()
    store = MonitorStore()
    _probe(store, now)
    server = MonitorServer(('127.0.0.1', 0), TOKEN, store)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]

    def call(method, path, payload=None, token=TOKEN):
        headers = {'Content-Type': 'application/json'}
        if token is not None:
            headers['X-Monitor-Token'] = token
        data = None if payload is None else json.dumps(payload).encode()
        request = urllib.request.Request(
            f'http://127.0.0.1:{port}{path}', data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=5) as response:
                return response.status, json.loads(response.read())
        except urllib.error.HTTPError as error:
            return error.code, json.loads(error.read())

    try:
        assert call('GET', '/healthz', token=None) == (200, {'ok': True})
        assert call('GET', '/status', token=None)[0] == 401
        assert call('GET', '/status', token='wrong')[0] == 401
        assert call('POST', '/ingest', _payload(time.time()), token=None)[0] == 401
        status, body = call('POST', '/ingest', _payload(time.time()))
        assert status == 200 and body['status'] == 'accepted'
        status, body = call('GET', '/status')
        assert status == 200 and body['monitor'] == 'isolated'
        assert call('GET', '/unknown')[0] == 404
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
