"""Выпуск сертификатов из панели: роли, DNS-проверка, статус и защита.

Сеть не используется: DNS, TLS-проба и сохранение подменяются двойниками.
"""
import asyncio
import json
from types import SimpleNamespace
from unittest import mock

import pytest

import app as panel

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


def _admin(monkeypatch, request=None):
    monkeypatch.setattr(panel, '_check_admin', lambda req: {'role': 'admin'})


def _memory_state(monkeypatch, data):
    monkeypatch.setattr(panel, 'load_data', lambda: data)

    def _save(updated):
        data['settings'] = updated.get('settings', data.get('settings', {}))

    monkeypatch.setattr(panel, 'save_data', _save)


def test_active_cert_domains_includes_admin_and_strips_unknown_fields():
    data = _data(
        {'domain': 'admin.ttvpn.ru', 'role': 'admin', 'status': 'active',
         'checked_at': 7,
         'certificate': {'issuer': 'LE', 'subject': 'CN=admin.ttvpn.ru',
                         'not_after': '2027-01-01T00:00:00+00:00',
                         'secret_note': 'drop-me'},
         'last_error': None},
        _item('lk.ttvpn.ru', 'client'),
        _item('gone.ttvpn.ru', 'admin', status='removed'),
        _item('bad.ttvpn.ru', 'other'),
    )
    result = panel._active_cert_domains(data)
    assert [row['domain'] for row in result] == ['admin.ttvpn.ru', 'lk.ttvpn.ru']
    assert result[0]['certificate']['issuer'] == 'LE'
    assert 'secret_note' not in result[0]['certificate']
    assert result[0]['checked_at'] == 7
    assert result[1]['certificate'] is None
    assert result[1]['last_error'] is None


def test_public_cert_domains_keeps_admin_out_of_billing_gate():
    data = _data(_item('admin.ttvpn.ru', 'admin'), _item('lk.ttvpn.ru', 'client'))
    assert panel._public_cert_domains(data) == [
        {'domain': 'lk.ttvpn.ru', 'role': 'client', 'created_at': None}]


def test_ask_endpoint_allows_admin_subdomain(monkeypatch):
    monkeypatch.setenv('TTVPN_CERT_ASK_TOKEN', ASK_TOKEN)
    panel._cert_ask_state.update(window=0.0, total=0, by_ip={})
    data = _data(_item('admin.ttvpn.ru', 'admin'))
    request = SimpleNamespace(client=SimpleNamespace(host='172.18.0.5'))
    with mock.patch.object(panel, 'load_data', return_value=data):
        allowed = panel.certificate_ask(request, ASK_TOKEN, 'admin.ttvpn.ru')
        denied = panel.certificate_ask(request, ASK_TOKEN, 'other.ttvpn.ru')
    assert allowed.status_code == 200
    assert denied.status_code == 404


def test_domain_issue_requires_admin_and_csrf(monkeypatch):
    monkeypatch.setattr(panel, '_check_admin', lambda req: None)
    response = asyncio.run(panel.certificate_domain_issue(_request({'domain': 'lk.ttvpn.ru'})))
    assert response.status_code == 403

    monkeypatch.setattr(panel, '_check_admin', lambda req: {'role': 'admin'})
    response = asyncio.run(panel.certificate_domain_issue(
        _request({'domain': 'lk.ttvpn.ru'}, csrf='wrong')))
    assert response.status_code == 403


def test_domain_issue_rejects_unknown_domain(monkeypatch):
    monkeypatch.setattr(panel, '_check_admin', lambda req: {'role': 'admin'})
    monkeypatch.setattr(panel, 'load_data', lambda: _data(_item('lk.ttvpn.ru', 'client')))
    monkeypatch.setattr(panel, '_probe_certificate',
                        lambda domain: pytest.fail('проба не должна вызываться'))
    response = asyncio.run(panel.certificate_domain_issue(_request({'domain': 'evil.ttvpn.ru'})))
    assert response.status_code == 404


def test_domain_issue_stores_certificate(monkeypatch):
    _admin(monkeypatch)
    state = _data(_item('lk.ttvpn.ru', 'client'))
    _memory_state(monkeypatch, state)
    certificate = {'issuer': 'CN=R3', 'subject': 'CN=lk.ttvpn.ru',
                   'not_before': '2026-01-01T00:00:00+00:00',
                   'not_after': '2027-01-01T00:00:00+00:00', 'serial': 'ab'}
    monkeypatch.setattr(panel, '_probe_certificate', lambda domain: dict(certificate))
    response = asyncio.run(panel.certificate_domain_issue(_request({'domain': 'lk.ttvpn.ru'})))
    body = json.loads(response.body)
    assert response.status_code == 200
    assert body['certificate']['not_after'] == certificate['not_after']
    saved = state['settings']['certificate_domains'][0]
    assert saved['certificate']['issuer'] == 'CN=R3'
    assert saved['last_error'] is None
    assert isinstance(saved['checked_at'], int)


def test_domain_issue_records_probe_error(monkeypatch):
    _admin(monkeypatch)
    state = _data(_item('lk.ttvpn.ru', 'client'))
    _memory_state(monkeypatch, state)

    def _boom(domain):
        raise ValueError('Домен не резолвится: нет A/AAAA-записей')

    monkeypatch.setattr(panel, '_probe_certificate', _boom)
    response = asyncio.run(panel.certificate_domain_issue(_request({'domain': 'lk.ttvpn.ru'})))
    assert response.status_code == 502
    saved = state['settings']['certificate_domains'][0]
    assert saved['certificate'] is None
    assert 'не резолвится' in saved['last_error']


def test_domain_add_accepts_admin_role_after_dns_check(monkeypatch):
    _admin(monkeypatch)
    state = {'settings': {}}
    _memory_state(monkeypatch, state)
    monkeypatch.setattr(panel, '_verify_domain_points_here', lambda name: True)
    response = asyncio.run(panel.certificate_domain_add(
        _request({'domain': 'admin.ttvpn.ru', 'role': 'admin'})))
    assert response.status_code == 200
    saved = state['settings']['certificate_domains'][0]
    assert saved['role'] == 'admin'
    assert saved['certificate'] is None
    assert saved['checked_at'] is None


def test_domain_add_rejects_foreign_dns(monkeypatch):
    _admin(monkeypatch)
    state = {'settings': {}}
    _memory_state(monkeypatch, state)

    def _reject(name):
        raise ValueError('Домен не указывает на этот сервер: 8.8.8.8')

    monkeypatch.setattr(panel, '_verify_domain_points_here', _reject)
    response = asyncio.run(panel.certificate_domain_add(
        _request({'domain': 'evil.example.com', 'role': 'client'})))
    assert response.status_code == 400
    assert '8.8.8.8' in json.loads(response.body)['error']
    assert 'certificate_domains' not in state['settings']


def test_domain_add_rejects_unknown_role(monkeypatch):
    _admin(monkeypatch)
    monkeypatch.setattr(panel, '_verify_domain_points_here',
                        lambda name: pytest.fail('роль должна отсекаться раньше'))
    response = asyncio.run(panel.certificate_domain_add(
        _request({'domain': 'evil.example.com', 'role': 'root'})))
    assert response.status_code == 400


def test_verify_domain_matches_panel_addresses(monkeypatch):
    monkeypatch.setattr(panel, 'load_data',
                        lambda: {'settings': {'ssl': {'domain': 'admin.ttvpn.ru'}}})

    def _resolve(domain):
        return ({'93.184.216.34'}, set())

    monkeypatch.setattr(panel, '_resolve_domain_addresses', _resolve)
    assert panel._verify_domain_points_here('lk.ttvpn.ru') is True


def test_verify_domain_rejects_foreign_address(monkeypatch):
    monkeypatch.setattr(panel, 'load_data',
                        lambda: {'settings': {'ssl': {'domain': 'admin.ttvpn.ru'}}})

    def _resolve(domain):
        return ({'93.184.216.34'}, set()) if domain == 'admin.ttvpn.ru' else ({'1.1.1.1'}, set())

    monkeypatch.setattr(panel, '_resolve_domain_addresses', _resolve)
    with pytest.raises(ValueError, match='не указывает на этот сервер'):
        panel._verify_domain_points_here('evil.example.com')


def test_probe_rejects_internal_address(monkeypatch):
    monkeypatch.setattr(panel, '_resolve_domain_addresses',
                        lambda domain: ({'10.0.0.5'}, set()))
    with pytest.raises(ValueError, match='внутренний адрес'):
        panel._probe_certificate('vpn.example.com')


def test_ask_rejects_short_configured_token(monkeypatch):
    """Короткий токен считается незаданным: он уходит в URL и попадает в логи."""
    monkeypatch.setenv('TTVPN_CERT_ASK_TOKEN', 'short')
    panel._cert_ask_state.update(window=0.0, total=0, by_ip={})
    request = SimpleNamespace(client=SimpleNamespace(host='172.18.0.5'))
    response = panel.certificate_ask(request, 'short', 'admin.ttvpn.ru')
    assert response.status_code == 403


def test_ask_rate_limit_counts_bad_tokens(monkeypatch):
    """Неверный токен обязан попадать в лимит, иначе это бесплатный оракул."""
    monkeypatch.setenv('TTVPN_CERT_ASK_TOKEN', ASK_TOKEN)
    panel._cert_ask_state.update(window=0.0, total=0, by_ip={})
    request = SimpleNamespace(client=SimpleNamespace(host='203.0.113.5'))
    statuses = [panel.certificate_ask(request, 'wrong-token', 'admin.ttvpn.ru').status_code
                for _ in range(125)]
    assert statuses[0] == 403
    assert 429 in statuses
    assert statuses.count(429) >= 5


def test_ask_responses_are_never_cached(monkeypatch):
    monkeypatch.setenv('TTVPN_CERT_ASK_TOKEN', ASK_TOKEN)
    panel._cert_ask_state.update(window=0.0, total=0, by_ip={})
    request = SimpleNamespace(client=SimpleNamespace(host='203.0.113.6'))
    with mock.patch.object(panel, 'load_data', return_value=_data(_item('admin.ttvpn.ru', 'admin'))):
        allowed = panel.certificate_ask(request, ASK_TOKEN, 'admin.ttvpn.ru')
        denied = panel.certificate_ask(request, ASK_TOKEN, 'nope.ttvpn.ru')
        forbidden = panel.certificate_ask(request, 'x', 'admin.ttvpn.ru')
    for response in (allowed, denied, forbidden):
        assert response.headers.get('cache-control') == 'no-store'


def test_issue_latch_blocks_parallel_probe_for_same_domain():
    panel._cert_issue_inflight.clear()
    assert panel._cert_issue_slot('lk.ttvpn.ru') is True
    try:
        assert panel._cert_issue_slot('lk.ttvpn.ru') is False
        assert panel._cert_issue_slot('admin.ttvpn.ru') is True
        assert panel._cert_issue_slot('third.ttvpn.ru') is False
    finally:
        panel._cert_issue_release('lk.ttvpn.ru')
        panel._cert_issue_release('admin.ttvpn.ru')
        panel._cert_issue_inflight.clear()


def test_malformed_content_length_is_not_a_crash(monkeypatch):
    request = _request({'domain': 'lk.ttvpn.ru'})
    request.headers['content-length'] = 'not-a-number'
    assert panel._content_length(request) == 0


def test_store_result_ignores_removed_domain(monkeypatch):
    state = _data(_item('lk.ttvpn.ru', 'client'))
    _memory_state(monkeypatch, state)
    changed = asyncio.run(panel._store_certificate_result(
        'gone.ttvpn.ru', certificate={'issuer': 'x'}))
    assert changed is False
    assert [row['domain'] for row in state['settings']['certificate_domains']] == ['lk.ttvpn.ru']
    assert state['settings']['certificate_domains'][0].get('certificate') is None


def test_ask_token_is_redacted_in_access_log():
    import logging as logging_module
    record = logging_module.LogRecord(
        'uvicorn.access', logging_module.INFO, __file__, 1,
        '%s - "%s %s HTTP/%s" %d',
        ('172.18.0.2', 'GET', '/api/certificates/ask/' + ASK_TOKEN + '?domain=a.ttvpn.ru',
         '1.1', 200), None)
    assert panel._RedactAskToken().filter(record) is True
    message = record.getMessage()
    assert ASK_TOKEN not in message
    assert '<redacted>' in message


def test_public_cert_domains_unchanged_by_admin_role_support():
    """Гейт биллинга не должен начать пускать admin-хосты."""
    data = _data(_item('admin.ttvpn.ru', 'admin'), _item('lk.ttvpn.ru', 'client'),
                 _item('ttvpn.ru', 'landing'))
    public = panel._public_cert_domains(data)
    assert {row['domain'] for row in public} == {'lk.ttvpn.ru', 'ttvpn.ru'}
