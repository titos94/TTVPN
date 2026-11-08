"""Настройки ИИ-помощника: шифрование ключа, роли, CSRF, приоритет env.

Сеть не используется: запрос к провайдеру подменяется двойником.
"""
import asyncio
import logging
import time
from types import SimpleNamespace
from unittest import mock

import pytest

import app as panel
import assistant_settings
import ttvpn_assistant

SECRET = 'unit-test-secret-key-for-hkdf'
API_KEY = 'sk-test-' + 'k' * 32
OLD_KEY = 'sk-old-' + 'o' * 32


@pytest.fixture(autouse=True)
def _reset_probe_slot():
    """TTL-метка проверки общая для процесса, поэтому сбрасывается на тест."""
    panel._assistant_probe_started = 0.0
    yield
    panel._assistant_probe_started = 0.0


def _request(body=None, csrf='token'):
    payload = {} if body is None else body

    async def _json():
        return payload

    headers = {'content-length': '128'}
    if csrf is not None:
        headers['x-backup-csrf'] = csrf
    return SimpleNamespace(
        session={'backup_csrf': 'token'},
        headers=headers,
        json=_json,
        client=SimpleNamespace(host='172.18.0.5'),
    )


def _as_admin(monkeypatch, role='admin'):
    monkeypatch.setattr(panel, '_check_admin', lambda request: {'role': role, 'username': 'root'})


def _state(monkeypatch, data=None):
    data = data if data is not None else {'settings': {}}
    monkeypatch.setattr(panel, 'load_data', lambda: data)

    def _save(updated):
        data['settings'] = updated.get('settings', {})

    monkeypatch.setattr(panel, 'save_data', _save)
    return data


def _stored(data):
    return (data.get('settings') or {}).get('assistant') or {}


# --- шифрование ------------------------------------------------------------

def test_api_key_roundtrip_does_not_keep_plaintext():
    token = assistant_settings.encrypt_api_key(API_KEY, SECRET)
    assert API_KEY not in token
    assert assistant_settings.decrypt_api_key(token, SECRET) == API_KEY


def test_legacy_v1_token_still_decrypts():
    """Записи без префикса — старый формат HKDF; они должны читаться."""
    legacy = assistant_settings._fernet(SECRET, 1).encrypt(API_KEY.encode()).decode('ascii')
    assert not legacy.startswith(assistant_settings._FORMAT_V2)
    assert assistant_settings.decrypt_api_key(legacy, SECRET) == API_KEY


def test_decrypt_with_other_secret_returns_empty():
    token = assistant_settings.encrypt_api_key(API_KEY, SECRET)
    assert assistant_settings.decrypt_api_key(token, 'another-secret') == ''


def test_decrypt_garbage_returns_empty():
    assert assistant_settings.decrypt_api_key('not-a-token', SECRET) == ''
    assert assistant_settings.decrypt_api_key(None, SECRET) == ''
    assert assistant_settings.decrypt_api_key('', SECRET) == ''


def test_encrypt_requires_secret():
    with pytest.raises(assistant_settings.AssistantKeyError):
        assistant_settings.encrypt_api_key(API_KEY, '')


@pytest.mark.parametrize('value', ['short', 'k' * 19, 'k' * 513, 'key with space' * 3,
                                   'ключ' * 10])
def test_validate_api_key_rejects_bad_form(value):
    with pytest.raises(assistant_settings.AssistantKeyError):
        assistant_settings.validate_api_key(value)


def test_validate_api_key_strips_spaces():
    assert assistant_settings.validate_api_key('  ' + API_KEY + '  ') == API_KEY


@pytest.mark.parametrize('value', ['', None])
def test_validate_model_defaults(value):
    assert assistant_settings.validate_model(value) == assistant_settings.DEFAULT_MODEL


@pytest.mark.parametrize('value', ['../../etc', 'model name', 'a' * 65, 'model;rm -rf'])
def test_validate_model_rejects_bad_form(value):
    with pytest.raises(assistant_settings.AssistantKeyError):
        assistant_settings.validate_model(value)


def test_routerai_model_and_provider_are_supported():
    assert assistant_settings.validate_model('deepseek/deepseek-v4-flash') == 'deepseek/deepseek-v4-flash'
    assert assistant_settings.public_view({'provider': 'routerai'})['provider'] == 'routerai'
    assert assistant_settings.PROVIDER_URLS['routerai'] == 'https://routerai.ru/api/v1'


def test_public_view_never_exposes_key():
    view = assistant_settings.public_view({
        'enabled': True,
        'model': 'gpt-test',
        'api_key_encrypted': 'ciphertext',
        'key_last4': '1234',
        'updated_at': 1,
    })
    assert 'api_key_encrypted' not in view
    assert 'api_key' not in view
    assert view['key_configured'] is True
    assert view['key_last4'] == '1234'


# --- GET -------------------------------------------------------------------

def test_get_settings_hides_key(monkeypatch):
    _as_admin(monkeypatch)
    token = assistant_settings.encrypt_api_key(API_KEY, SECRET)
    _state(monkeypatch, {'settings': {'assistant': {
        'enabled': True, 'model': 'gpt-test',
        'api_key_encrypted': token, 'key_last4': API_KEY[-4:]}}})
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.delenv('TTVPN_ASSISTANT_API_KEY', raising=False)
    response = panel.api_assistant_settings(_request())
    body = response.body.decode()
    assert response.status_code == 200
    assert API_KEY not in body
    assert token not in body
    assert '"key_configured":true' in body


def test_get_settings_denied_for_support(monkeypatch):
    _as_admin(monkeypatch, role='support')
    _state(monkeypatch)
    assert panel.api_assistant_settings(_request()).status_code == 403


def test_get_settings_cache_control(monkeypatch):
    _as_admin(monkeypatch)
    _state(monkeypatch)
    monkeypatch.delenv('TTVPN_ASSISTANT_API_KEY', raising=False)
    response = panel.api_assistant_settings(_request())
    assert response.headers.get('cache-control') == 'no-store'


# --- POST сохранение -------------------------------------------------------

def test_save_stores_encrypted_key_only(monkeypatch):
    _as_admin(monkeypatch)
    data = _state(monkeypatch)
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.delenv('TTVPN_ASSISTANT_API_KEY', raising=False)
    response = asyncio.run(panel.api_assistant_settings_save(
        _request({'enabled': True, 'model': 'gpt-test', 'api_key': API_KEY})))
    assert response.status_code == 200
    stored = _stored(data)
    assert stored['api_key_encrypted'] and stored['api_key_encrypted'] != API_KEY
    assert API_KEY not in str(stored)
    assert stored['key_last4'] == API_KEY[-4:]
    assert stored['enabled'] is True and stored['model'] == 'gpt-test'
    assert API_KEY not in response.body.decode()


def test_save_empty_key_keeps_existing(monkeypatch):
    _as_admin(monkeypatch)
    token = assistant_settings.encrypt_api_key(OLD_KEY, SECRET)
    data = _state(monkeypatch, {'settings': {'assistant': {
        'enabled': True, 'model': 'gpt-old',
        'api_key_encrypted': token, 'key_last4': OLD_KEY[-4:]}}})
    monkeypatch.setenv('SECRET_KEY', SECRET)
    response = asyncio.run(panel.api_assistant_settings_save(
        _request({'enabled': False, 'model': '', 'api_key': ''})))
    assert response.status_code == 200
    stored = _stored(data)
    assert stored['api_key_encrypted'] == token
    assert stored['key_last4'] == OLD_KEY[-4:]
    assert stored['enabled'] is False
    assert stored['model'] == assistant_settings.DEFAULT_MODEL


def test_save_keeps_unrelated_settings(monkeypatch):
    _as_admin(monkeypatch)
    data = _state(monkeypatch, {'settings': {'ssl': {'panel_port': 443},
                                             'telegram': {'enabled': True}}})
    monkeypatch.setenv('SECRET_KEY', SECRET)
    asyncio.run(panel.api_assistant_settings_save(
        _request({'enabled': True, 'model': 'gpt-test', 'api_key': API_KEY})))
    assert data['settings']['ssl']['panel_port'] == 443
    assert data['settings']['telegram']['enabled'] is True


def test_save_rejects_bad_model(monkeypatch):
    _as_admin(monkeypatch)
    _state(monkeypatch)
    monkeypatch.setenv('SECRET_KEY', SECRET)
    response = asyncio.run(panel.api_assistant_settings_save(
        _request({'enabled': True, 'model': 'bad model!'})))
    assert response.status_code == 400
    assert 'модели' in response.body.decode()


def test_save_rejects_short_key(monkeypatch):
    _as_admin(monkeypatch)
    _state(monkeypatch)
    monkeypatch.setenv('SECRET_KEY', SECRET)
    response = asyncio.run(panel.api_assistant_settings_save(
        _request({'enabled': True, 'model': 'gpt-test', 'api_key': 'abc'})))
    assert response.status_code == 400


def test_save_denied_for_support(monkeypatch):
    _as_admin(monkeypatch, role='support')
    _state(monkeypatch)
    response = asyncio.run(panel.api_assistant_settings_save(
        _request({'enabled': True, 'model': 'gpt-test', 'api_key': API_KEY})))
    assert response.status_code == 403


def test_save_requires_csrf(monkeypatch):
    _as_admin(monkeypatch)
    _state(monkeypatch)
    monkeypatch.setenv('SECRET_KEY', SECRET)
    response = asyncio.run(panel.api_assistant_settings_save(
        _request({'enabled': True, 'model': 'gpt-test', 'api_key': API_KEY}, csrf=None)))
    assert response.status_code == 403


def test_save_without_secret_key_returns_503(monkeypatch):
    _as_admin(monkeypatch)
    _state(monkeypatch)
    monkeypatch.delenv('SECRET_KEY', raising=False)
    response = asyncio.run(panel.api_assistant_settings_save(
        _request({'enabled': True, 'model': 'gpt-test', 'api_key': API_KEY})))
    assert response.status_code == 503
    assert 'SECRET_KEY' in response.body.decode()


def test_save_rejects_oversized_body(monkeypatch):
    _as_admin(monkeypatch)
    _state(monkeypatch)
    monkeypatch.setenv('SECRET_KEY', SECRET)
    request = _request({'enabled': True})
    request.headers['content-length'] = '999999'
    response = asyncio.run(panel.api_assistant_settings_save(request))
    assert response.status_code == 413


# --- clear-key -------------------------------------------------------------

def test_clear_key_removes_secret_but_keeps_settings(monkeypatch):
    _as_admin(monkeypatch)
    token = assistant_settings.encrypt_api_key(API_KEY, SECRET)
    data = _state(monkeypatch, {'settings': {'assistant': {
        'enabled': True, 'model': 'gpt-test',
        'api_key_encrypted': token, 'key_last4': API_KEY[-4:]}}})
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.delenv('TTVPN_ASSISTANT_API_KEY', raising=False)
    response = asyncio.run(panel.api_assistant_settings_clear_key(_request({})))
    assert response.status_code == 200
    stored = _stored(data)
    assert 'api_key_encrypted' not in stored
    assert 'key_last4' not in stored
    assert stored['enabled'] is True and stored['model'] == 'gpt-test'
    assert '"key_configured":false' in response.body.decode()


def test_clear_key_denied_for_support(monkeypatch):
    _as_admin(monkeypatch, role='support')
    _state(monkeypatch)
    assert asyncio.run(panel.api_assistant_settings_clear_key(_request({}))).status_code == 403


# --- приоритет env ---------------------------------------------------------

def test_env_key_takes_priority_over_saved(monkeypatch):
    token = assistant_settings.encrypt_api_key(OLD_KEY, SECRET)
    _state(monkeypatch, {'settings': {'assistant': {
        'enabled': True, 'model': 'gpt-saved', 'api_key_encrypted': token}}})
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.setenv('TTVPN_ASSISTANT_API_KEY', API_KEY)
    monkeypatch.setenv('TTVPN_ASSISTANT_MODEL', 'gpt-env')
    resolved = panel._assistant_resolve()
    assert resolved['source'] == 'env'
    assert resolved['api_key'] == API_KEY
    assert resolved['model'] == 'gpt-env'
    assert panel._assistant_public()['env_override'] is True


def test_env_key_alone_does_not_enable_assistant(monkeypatch):
    _state(monkeypatch)
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.setenv('TTVPN_ASSISTANT_API_KEY', API_KEY)
    monkeypatch.delenv('TTVPN_ASSISTANT_ENABLED', raising=False)
    assert not ttvpn_assistant.assistant_enabled()


def test_saved_settings_disable_assistant_without_key(monkeypatch):
    _state(monkeypatch, {'settings': {'assistant': {'enabled': True}}})
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.delenv('TTVPN_ASSISTANT_API_KEY', raising=False)
    ttvpn_assistant.configure_resolver(panel._assistant_resolve)
    assert not ttvpn_assistant.assistant_enabled()


# --- проверка подключения --------------------------------------------------

def test_test_endpoint_without_key(monkeypatch):
    _as_admin(monkeypatch)
    _state(monkeypatch)
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.delenv('TTVPN_ASSISTANT_API_KEY', raising=False)
    response = asyncio.run(panel.api_assistant_settings_test(_request({})))
    assert response.status_code == 200
    assert '"ok":false' in response.body.decode()


def test_test_endpoint_uses_probe_and_hides_key(monkeypatch, caplog):
    _as_admin(monkeypatch)
    token = assistant_settings.encrypt_api_key(API_KEY, SECRET)
    _state(monkeypatch, {'settings': {'assistant': {
        'enabled': True, 'model': 'gpt-test', 'api_key_encrypted': token}}})
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.delenv('TTVPN_ASSISTANT_API_KEY', raising=False)
    seen = {}

    def _probe(key, model, provider='openai'):
        seen['key'] = key
        seen['model'] = model
        return {'ok': True, 'message': 'Ключ работает'}

    monkeypatch.setattr(panel, '_assistant_probe_key', _probe)
    with caplog.at_level(logging.DEBUG):
        response = asyncio.run(panel.api_assistant_settings_test(_request({})))
    body = response.body.decode()
    assert response.status_code == 200
    assert seen == {'key': API_KEY, 'model': 'gpt-test'}
    assert API_KEY not in body
    assert API_KEY not in caplog.text


def test_test_endpoint_denied_for_support(monkeypatch):
    _as_admin(monkeypatch, role='support')
    _state(monkeypatch)
    assert asyncio.run(panel.api_assistant_settings_test(_request({}))).status_code == 403


def test_probe_handles_provider_failure(monkeypatch):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *args, **kwargs):
            raise OSError('network down')

    monkeypatch.setattr('httpx.Client', _Client)
    result = panel._assistant_probe_key(API_KEY, 'gpt-test')
    assert result == {'ok': False, 'message': 'Провайдер недоступен'}


def test_probe_rejects_unauthorized_key(monkeypatch):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *args, **kwargs):
            return SimpleNamespace(status_code=401, json=lambda: {})

    monkeypatch.setattr('httpx.Client', _Client)
    result = panel._assistant_probe_key(API_KEY, 'gpt-test')
    assert result['ok'] is False
    assert API_KEY not in result['message']


# --- страница настроек -----------------------------------------------------

def test_general_settings_dump_hides_assistant_ciphertext(monkeypatch):
    _as_admin(monkeypatch, role='support')
    token = assistant_settings.encrypt_api_key(API_KEY, SECRET)
    _state(monkeypatch, {'settings': {
        'assistant': {'enabled': True, 'api_key_encrypted': token,
                      'key_last4': API_KEY[-4:]},
        'telegram': {'token': 'tg-secret-token', 'enabled': True},
        'ssl': {'key_text': 'PRIVATE', 'panel_port': 443},
    }})
    response = panel.api_get_settings(_request())
    body = response.body.decode()
    assert response.status_code == 200
    assert token not in body
    assert 'tg-secret-token' not in body
    assert 'PRIVATE' not in body
    # Безопасные настройки остаются доступными.
    assert 'panel_port' in body


def test_save_rejects_non_boolean_enabled(monkeypatch):
    _as_admin(monkeypatch)
    data = _state(monkeypatch)
    monkeypatch.setenv('SECRET_KEY', SECRET)
    response = asyncio.run(panel.api_assistant_settings_save(
        _request({'enabled': 'false', 'model': 'gpt-test', 'api_key': API_KEY})))
    assert response.status_code == 400
    assert 'api_key_encrypted' not in _stored(data)


def test_probe_slot_recovers_after_ttl(monkeypatch):
    _as_admin(monkeypatch)
    token = assistant_settings.encrypt_api_key(API_KEY, SECRET)
    _state(monkeypatch, {'settings': {'assistant': {
        'enabled': True, 'model': 'gpt-test', 'api_key_encrypted': token}}})
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.setattr(panel, '_assistant_probe_key',
                        lambda key, model, provider='openai': {'ok': True, 'message': 'ok'})
    panel._assistant_probe_started = time.monotonic()
    first = asyncio.run(panel.api_assistant_settings_test(_request({})))
    assert first.status_code == 429
    # Истёк TTL — зависший ранее поток не должен запирать эндпоинт навсегда.
    panel._assistant_probe_started = time.monotonic() - panel._ASSISTANT_PROBE_TTL - 1
    second = asyncio.run(panel.api_assistant_settings_test(_request({})))
    assert second.status_code == 200


def test_unreadable_key_is_reported(monkeypatch):
    _as_admin(monkeypatch)
    _state(monkeypatch, {'settings': {'assistant': {
        'enabled': True, 'api_key_encrypted': 'garbage-token'}}})
    monkeypatch.setenv('SECRET_KEY', SECRET)
    monkeypatch.delenv('TTVPN_ASSISTANT_API_KEY', raising=False)
    view = panel._assistant_public()
    assert view['key_unreadable'] is True
    assert view['effective'] == 'none'


def test_probe_treats_redirect_as_failure(monkeypatch):
    class _Client:
        def __init__(self, *args, **kwargs):
            pass

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def get(self, *args, **kwargs):
            return SimpleNamespace(status_code=302, json=lambda: {})

    monkeypatch.setattr('httpx.Client', _Client)
    result = panel._assistant_probe_key(API_KEY, 'gpt-test')
    assert result['ok'] is False


def test_settings_page_has_assistant_section():
    source = open('templates/settings.html', encoding='utf-8').read()
    assert 'id="settings-assistant"' in source
    assert 'href="#settings-assistant"' in source
    assert 'id="assistant_api_key"' in source
    assert 'function saveAssistantSettings' in source
    assert 'function testAssistant' in source
    assert 'function clearAssistantKey' in source
    # ключ сохраняется только через POST, в ответе его быть не может
    assert '/api/settings/assistant' in source
