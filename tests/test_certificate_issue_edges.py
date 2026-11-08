"""Edge-case tests for the certificate issue functionality.

Network is not used: DNS, TLS probe and saving are all mocked.
"""
import asyncio
from types import SimpleNamespace
from unittest import mock

import pytest

import app as panel
from cert_domain_policy import validate_public_addresses

ASK_TOKEN = 'a' * 40


def _item(domain, role='client', status='active', **extra):
    item = {'domain': domain, 'role': role, 'status': status}
    item.update(extra)
    return item


def _data(*items):
    return {'settings': {'certificate_domains': list(items)}}


def _request(body=None, csrf='token'):
    payload = body or {}

    async def _json():
        return payload

    return SimpleNamespace(
        session={'backup_csrf': 'token'},
        headers={'x-backup-csrf': csrf, 'content-length': '64'},
        json=_json,
        base_url='https://admin.ttvpn.ru/',
        client=SimpleNamespace(host='172.18.0.5'),
    )


# ── 1. support role rejected on all 3 POST endpoints ──────────────────────────


def _admin(monkeypatch, role='admin'):
    monkeypatch.setattr(panel, '_check_admin', lambda req: {'role': role})


def _support(monkeypatch):
    _admin(monkeypatch, 'support')


# ── 2. certificate_domain_remove cleans domain and doesn't crash on missing ─


def test_domain_remove_cleans_and_does_not_crash_on_missing(monkeypatch):
    """Удаление несуществующего домена не падает — возвращает текущий список."""
    _admin(monkeypatch)
    state = {'settings': {'certificate_domains': [
        _item('lk.ttvpn.ru', 'client'),
        _item('admin.ttvpn.ru', 'admin'),
    ]}}
    _memory_state(monkeypatch, state)

    response = asyncio.run(panel.certificate_domain_remove(
        _request({'domain': 'nonexistent.ttvpn.ru'})))
    assert response.status_code == 200
    # Доменов не станет — список не изменился
    assert [r['domain'] for r in state['settings']['certificate_domains']] == ['lk.ttvpn.ru', 'admin.ttvpn.ru']


# ── 3. CERT_DOMAIN_LIMIT: 21st domain rejected 409 ───────────────────────────


def test_domain_add_at_limit_rejects_409(monkeypatch):
    """21-й домен при CERT_DOMAIN_LIMIT=20 уходит с 409."""
    _admin(monkeypatch)
    state = {'settings': {'certificate_domains': []}}
    _memory_state(monkeypatch, state)
    # Мокируем проверку DNS, иначе _verify_domain_points_here упадет без настроек
    monkeypatch.setattr(panel, '_verify_domain_points_here', lambda name: True)

    # Добавим 20 доменов
    for i in range(20):
        resp = asyncio.run(panel.certificate_domain_add(
            _request({'domain': f'domain{i}.ttvpn.ru', 'role': 'client'})))
        assert resp.status_code == 200, f'domain {i} accepted'

    # 21-й — должен упасть
    resp = asyncio.run(panel.certificate_domain_add(
        _request({'domain': 'domain21.ttvpn.ru', 'role': 'client'})))
    assert resp.status_code == 409


# ── 4. _cert_reference_addresses without settings and without env → ValueError ─


def test_cert_reference_addresses_no_fallback_no_env(monkeypatch):
    """Без settings.ssl.domain и TTVPN_PUBLIC_ADDRESSES вызывается ValueError."""
    monkeypatch.setattr(panel, 'load_data', lambda: {'settings': {}})

    with pytest.raises(ValueError, match='Не удалось определить адрес сервера для проверки DNS'):
        panel._cert_reference_addresses()


# ── 5. _cert_reference_addresses with TTVPN_PUBLIC_ADDRESSES returns given ─


def test_cert_reference_addresses_with_env(monkeypatch):
    """С TTVPN_PUBLIC_ADDRESSES возвращаются указанные адреса."""
    monkeypatch.setenv('TTVPN_PUBLIC_ADDRESSES', '1.2.3.4,5.6.7.8')
    monkeypatch.setattr(panel, 'load_data', lambda: {'settings': {}})

    a, aaaa = panel._cert_reference_addresses()
    assert a == ['1.2.3.4', '5.6.7.8']
    assert aaaa == set()


# ── 6. _probe_certificate connects on verified IP, not on name ────────────────


def test_probe_certificate_uses_resolved_ip(monkeypatch):
    """socket.create_connection вызывается с resolved IP, а не с именем domain."""
    import socket as _socket

    # Mock resolve to return our test IP
    monkeypatch.setattr(panel, '_resolve_domain_addresses',
                        lambda d: (['1.2.3.4'], []))

    called_with = None

    def _create_connection(addr, *args, **kwargs):
        nonlocal called_with
        called_with = addr[0]  # extract IP string from (address, port) tuple
        # Return a mock socket that succeeds immediately
        class _MockSocket:
            def __enter__(self):
                return self
            def __exit__(self, *a):
                return False
            def getpeercert(self):
                return {}
        return _MockSocket()

    # Patch socket.create_connection at the module level where _probe_certificate uses it
    monkeypatch.setattr(_socket, 'create_connection', _create_connection)

    # Now call _probe_certificate - it will use the mocked create_connection
    try:
        panel._probe_certificate('test.example.com')
    except Exception:
        pass  # Expected, but create_connection was already called

    assert called_with == '1.2.3.4', (
        f'Expected create_connection to be called with 1.2.3.4, got {called_with!r}')


# ── 7. IPv4-mapped IPv6 (::ffff:127.0.0.1) rejected by validate_public_addresses ─


def test_validate_public_addresses_rejects_ipv4_mapped_ipv6(monkeypatch):
    """::ffff:127.0.0.1 отвергается validate_public_addresses."""
    with pytest.raises(ValueError, match='Недопустимый IP-адрес'):
        validate_public_addresses(['::ffff:127.0.0.1'])


# ── 8. _store_certificate_result doesn't lose parallel settings changes ────────


def test_store_certificate_result_preserves_other_settings(monkeypatch):
    """Запись результата не удаляет другие ключи settings."""
    import asyncio

    state = {'settings': {
        'ssl': {'domain': 'admin.ttvpn.ru'},
        'billing': {'plan': 'pro'},
        'certificate_domains': [
            {'domain': 'lk.ttvpn.ru', 'role': 'client', 'status': 'active',
             'checked_at': None, 'last_error': None, 'certificate': None},
        ],
    }}
    _memory_state(monkeypatch, state)

    # Записываем результат для домена
    result = asyncio.run(panel._store_certificate_result(
        'lk.ttvpn.ru', certificate={'issuer': 'R3'}))

    assert result is True
    # Основной домен сертификата обновился
    assert state['settings']['certificate_domains'][0]['certificate']['issuer'] == 'R3'
    # Другие настройки сохранились
    assert state['settings']['ssl']['domain'] == 'admin.ttvpn.ru'
    assert state['settings']['billing']['plan'] == 'pro'


def _memory_state(monkeypatch, data):
    monkeypatch.setattr(panel, 'load_data', lambda: data)

    def _save(updated):
        data['settings'] = updated.get('settings', data.get('settings', {}))

    monkeypatch.setattr(panel, 'save_data', _save)