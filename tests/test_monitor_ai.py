import asyncio
import json
from types import SimpleNamespace

import pytest

import app
import monitor_ai


def test_snapshot_contains_only_bounded_allowed_data():
    report = {'monitor': 'isolated', 'secret': 'SECRET', 'components': [
        {'component': 'panel', 'state': 'ok', 'password': 'SECRET'},
        {'component': 'vpn_node', 'state': 'down', 'down': 1, 'ip': 'SECRET'},
    ], 'health': {'disk_free_percent': 87, 'secret': 'SECRET'}}
    snapshot = monitor_ai.safe_snapshot(report)
    assert snapshot == {'status': 'unknown', 'issues': [], 'components': {'panel': {'state': 'ok'},
                                        'vpn_node': {'state': 'down', 'down': 1}},
                        'health': {'disk_free_percent': 87}}
    assert 'SECRET' not in json.dumps(snapshot)


def test_analysis_requires_admin_and_csrf(monkeypatch):
    monkeypatch.setattr(app, '_check_admin', lambda req: None)
    assert asyncio.run(app.assistant_analyze_monitor(SimpleNamespace())).status_code == 403
    monkeypatch.setattr(app, '_check_admin', lambda req: {'role': 'admin'})
    monkeypatch.setattr(app, '_cert_csrf_ok', lambda req: False)
    assert asyncio.run(app.assistant_analyze_monitor(SimpleNamespace())).status_code == 403


def test_analysis_endpoint_success_and_cooldown(monkeypatch):
    monkeypatch.setattr(app, '_check_admin', lambda req: {'role': 'admin'})
    monkeypatch.setattr(app, '_cert_csrf_ok', lambda req: True)
    monkeypatch.setattr(app.monitor_export, 'monitor_configured', lambda: True)
    async def fetch():
        return {'monitor': 'isolated', 'components': [{'component': 'panel', 'state': 'ok'}]}
    async def analyze(report, settings):
        return 'В норме'
    monkeypatch.setattr(app.monitor_export, 'fetch_status', fetch)
    monkeypatch.setattr(app, '_assistant_resolve', lambda: {'enabled': True})
    monkeypatch.setattr(monitor_ai, 'analyze', analyze)
    monkeypatch.setattr(app, '_monitor_ai_last', 0.0)
    response = asyncio.run(app.assistant_analyze_monitor(SimpleNamespace()))
    assert response.status_code == 200
    assert json.loads(response.body)['analysis'] == 'В норме'
    assert asyncio.run(app.assistant_analyze_monitor(SimpleNamespace())).status_code == 429


def test_snapshot_rejects_missing_monitor():
    with pytest.raises(ValueError):
        monitor_ai.safe_snapshot({'monitor': 'unknown'})


def test_analyze_sends_only_allowlisted_snapshot(monkeypatch):
    import httpx
    seen = {}
    def handler(request):
        seen['url'] = str(request.url)
        seen['payload'] = json.loads(request.content)
        return httpx.Response(200, json={'choices': [{'message': {'content': 'Нужна проверка'}}]})
    real_client = httpx.AsyncClient
    def client(**kwargs):
        assert kwargs['follow_redirects'] is False
        assert kwargs['trust_env'] is False
        return real_client(transport=httpx.MockTransport(handler), **kwargs)
    monkeypatch.setattr(monitor_ai.httpx, 'AsyncClient', client)
    report = {'monitor': 'isolated', 'secret': 'PRIVATE',
              'components': [{'component': 'panel', 'state': 'ok', 'token': 'PRIVATE'}]}
    settings = {'enabled': True, 'api_key': 'test-key', 'provider': 'routerai',
                'model': 'deepseek/deepseek-v4-flash'}
    assert asyncio.run(monitor_ai.analyze(report, settings)) == 'Нужна проверка'
    assert seen['url'] == 'https://routerai.ru/api/v1/chat/completions'
    assert 'PRIVATE' not in json.dumps(seen['payload'])
    assert 'tools' not in seen['payload']
