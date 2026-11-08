"""Обезличенные снимки панели для изолированного монитора."""
import asyncio
import json
import time

import monitor_export


def test_build_events_contains_only_slots_and_numbers():
    events = monitor_export.build_events(
        now=1_700_000_000, telegram_enabled=True, telegram_running=False,
        node_states=['ok', 'down', 'unknown'], disk_percent_value=41.2)
    serialized = json.dumps(events, ensure_ascii=False)
    assert 'server' not in serialized
    assert 'example' not in serialized
    assert '@' not in serialized
    assert events[0]['component'] == 'panel'
    assert events[0]['metrics'] == {'disk_percent': 41.2}
    assert events[1]['component'] == 'telegram'
    assert events[1]['state'] == 'down'
    assert [event['state'] for event in events[2:]] == ['ok', 'down', 'unknown']
    assert [event['id'] for event in events] == ['slot-00', 'slot-01', 'slot-02', 'slot-03', 'slot-04']


def test_disabled_telegram_is_not_reported_as_failure():
    events = monitor_export.build_events(
        now=100, telegram_enabled=False, telegram_running=False,
        node_states=[], disk_percent_value=None)
    assert events[1]['state'] == 'ok'
    assert events[0]['state'] == 'degraded'
    assert events[2]['state'] == 'unknown'
    assert events[2]['component'] == 'vpn_node'


def test_high_disk_degrades_panel_state():
    events = monitor_export.build_events(
        now=100, telegram_enabled=False, telegram_running=False,
        node_states=['ok'], disk_percent_value=95)
    assert events[0]['state'] == 'degraded'
    assert events[0]['metrics'] == {'disk_percent': 95}


def test_probe_node_states_checks_tcp_only():
    async def scenario():
        server = await asyncio.start_server(lambda reader, writer: writer.close(), '127.0.0.1', 0)
        port = server.sockets[0].getsockname()[1]
        try:
            return await monitor_export.probe_node_states([
                {'host': '127.0.0.1', 'ssh_port': port},
                {'host': '127.0.0.1', 'ssh_port': 1},
                {'host': '', 'ssh_port': 22},
                {'host': '127.0.0.1', 'ssh_port': 'bad'},
            ])
        finally:
            server.close()
            await server.wait_closed()

    assert asyncio.run(scenario()) == ['ok', 'down', 'unknown', 'unknown']


def test_push_payload_marks_active_slots():
    payload = monitor_export.build_push_payload(
        data={'servers': []}, telegram_enabled=False, telegram_running=False,
        disk_percent_value=20, node_states=['ok', 'ok'], now=100)
    assert payload['active_vpn_slots'] == ['slot-02', 'slot-03']
    assert payload['health']['disk_percent'] == 20
    assert payload['health']['telegram_enabled'] is False


def test_normalize_status_keeps_bounded_fields_only():
    body = {
        'status': 'attention',
        'state': 'degraded',
        'checked_at': '2026-01-01T00:00:00+00:00',
        'components': [
            {'component': 'panel', 'state': 'ok'},
            {'component': 'telegram', 'state': 'degraded'},
            {'component': 'proxy', 'state': 'ok'},
            {'component': 'vpn_node', 'state': 'down', 'ok': 1, 'degraded': 0, 'down': 1, 'unknown': 0},
        ],
        'health': {'disk_percent': 30, 'load_average_1m': 0.5, 'telegram_enabled': True},
        'issues': ['vpn_nodes_down', 'user@example.com', 'x' * 100],
        'secret': 'must not pass',
    }
    clean = monitor_export.normalize_status(body)
    assert clean['monitor'] == 'isolated'
    assert clean['status'] == 'attention'
    assert clean['health']['disk_free_percent'] == 70.0
    assert clean['issues'] == ['vpn_nodes_down']
    assert 'secret' not in clean
    assert clean['components'][3]['down'] == 1


def test_normalize_status_rejects_unknown_shapes():
    assert monitor_export.normalize_status({'status': 'ok'}) is None
    assert monitor_export.normalize_status({'status': 'weird', 'components': [{'component': 'panel', 'state': 'ok'}]}) is None
    assert monitor_export.normalize_status({'status': 'ok', 'components': [{'component': 'db', 'state': 'ok'}]}) is None


def test_disk_percent_is_bounded():
    value = monitor_export.disk_percent('.')
    assert value is None or 0 <= value <= 100
