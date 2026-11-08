import asyncio
from types import SimpleNamespace

import pytest

import app as panel
from ttvpn_assistant import ServiceMonitor, answer_customer, assistant_enabled, health_report, validate_message
from billing import router


def test_report_contains_only_safe_statuses(monkeypatch):
    monkeypatch.setattr('ttvpn_assistant.os.statvfs', lambda path: SimpleNamespace(f_bavail=20, f_blocks=100))
    monkeypatch.setattr('ttvpn_assistant.os.getloadavg', lambda: (0.5, 0, 0))
    assert health_report(telegram_enabled=True, telegram_running=False) == {
        'disk_free_percent': 20.0, 'load_average_1m': 0.5, 'telegram': 'stopped'
    }


def test_health_is_not_available_to_support_or_anonymous(monkeypatch):
    for user in (None, {'role': 'support'}):
        monkeypatch.setattr(panel, '_check_admin', lambda request, u=user: u)
        response = asyncio.run(panel.assistant_health(SimpleNamespace()))
        assert response.status_code == 403


@pytest.mark.parametrize('message', [
    'vpn://sensitive-key', 'PrivateKey = sensitive',
    '-----BEGIN PRIVATE KEY-----', 'token: abcdef', 'пароль: секрет',
    'amnezia://sensitive', '[Interface]\nAddress = 10.0.0.2',
    'Почта user@example.org', '+7 (999) 123-45-67',
    'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopq=',
    '', 'x' * 1201,
])
def test_rejects_obvious_secrets_and_invalid_input(message):
    with pytest.raises(ValueError):
        validate_message(message)


@pytest.mark.parametrize('provider,model,url', [
    ('openai', 'gpt-4.1-mini', 'https://api.openai.com/v1/chat/completions'),
    ('routerai', 'deepseek/deepseek-v4-flash', 'https://routerai.ru/api/v1/chat/completions'),
])
def test_external_call_contains_only_question_and_static_instructions(monkeypatch, provider, model, url):
    captured = {}
    monkeypatch.setenv('TTVPN_ASSISTANT_API_KEY', 'test-key')
    monkeypatch.setenv('TTVPN_ASSISTANT_ENABLED', '1')
    monkeypatch.setenv('TTVPN_ASSISTANT_PROVIDER', provider)
    monkeypatch.setenv('TTVPN_ASSISTANT_MODEL', model)

    class FakeClient:
        def __init__(self, **kwargs):
            assert kwargs['follow_redirects'] is False
            assert kwargs['trust_env'] is False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, *, headers, json):
            captured.update(url=url, headers=headers, payload=json)
            return SimpleNamespace(raise_for_status=lambda: None,
                                   json=lambda: {'choices': [{'message': {'content': 'Ответ'}}]})

    monkeypatch.setattr('ttvpn_assistant.httpx.AsyncClient', FakeClient)
    assert asyncio.run(answer_customer('Как подключиться?')) == 'Ответ'
    assert captured['url'] == url
    assert captured['payload']['model'] == model
    assert captured['payload']['messages'][-1] == {'role': 'user', 'content': 'Как подключиться?'}
    assert 'tools' not in captured['payload']


def test_assistant_remains_disabled_without_explicit_opt_in(monkeypatch):
    monkeypatch.setenv('TTVPN_ASSISTANT_API_KEY', 'test-key')
    monkeypatch.delenv('TTVPN_ASSISTANT_ENABLED', raising=False)
    assert not assistant_enabled()
    with pytest.raises(RuntimeError):
        asyncio.run(answer_customer('Как подключиться?'))


def test_assistant_endpoint_requires_login_and_consent(monkeypatch):
    request = SimpleNamespace(session={}, json=lambda: None)
    monkeypatch.setattr(router, '_current', lambda request: None)
    assert asyncio.run(router.api_assistant_message(request)).status_code == 401

    monkeypatch.setattr(router, '_current', lambda request: {'id': 'u1', 'enabled': True})
    monkeypatch.setattr(router, '_rate_limit', lambda *args: True)
    async def stream():
        yield b'{"csrf":"valid","message":"question"}'
    request.stream = stream
    monkeypatch.setattr(router, '_check_csrf', lambda *args: True)
    assert asyncio.run(router.api_assistant_message(request)).status_code == 400


def test_assistant_endpoint_rejects_streaming_oversized_body(monkeypatch):
    async def stream():
        yield b'x' * 5000
        yield b'y' * 5000
    request = SimpleNamespace(session={}, stream=stream)
    monkeypatch.setattr(router, '_current', lambda request: {'id': 'u1', 'enabled': True})
    monkeypatch.setattr(router, '_rate_limit', lambda *args: True)
    assert asyncio.run(router.api_assistant_message(request)).status_code == 413


def test_monitor_restarts_only_stopped_enabled_bot_with_cooldown():
    calls = []
    monitor = ServiceMonitor(cooldown_seconds=300)

    def restart():
        calls.append('restart')
        return True

    def check(now, enabled=True, running=False):
        telegram = 'running' if running else ('stopped' if enabled else 'disabled')
        return monitor.check(telegram_enabled=enabled,
                             telegram_running=running, restart_bot=restart, now=now,
                             health={'disk_free_percent': 50, 'load_average_1m': 0.1,
                                     'telegram': telegram})

    assert check(0, enabled=False)['repair'] == 'none'
    assert check(1)['repair'] == 'telegram_restart_requested'
    assert check(61)['repair'] == 'cooldown'
    assert check(301)['repair'] == 'telegram_restart_requested'
    assert check(302, running=True)['issues'] == []
    assert calls == ['restart', 'restart']


def test_monitor_reports_low_disk_and_skipped_start():
    monitor = ServiceMonitor()
    report = monitor.check(telegram_enabled=True, telegram_running=False,
                           restart_bot=lambda: False, now=100,
                           health={'disk_free_percent': 5, 'load_average_1m': 0.2, 'telegram': 'stopped'})
    assert 'disk_space_low' in report['issues']
    assert report['repair'] == 'telegram_restart_skipped'


def test_monitor_endpoint_requires_admin(monkeypatch):
    for user in (None, {'role': 'support'}):
        monkeypatch.setattr(panel, '_check_admin', lambda request, u=user: u)
        response = asyncio.run(panel.assistant_monitor_status(SimpleNamespace()))
        assert response.status_code == 403
