"""Политика и API доменов для сертификатов Caddy."""
from types import SimpleNamespace
from unittest import mock

import app as panel
from billing import router as billing_router


def _data(*items):
    return {'settings': {'certificate_domains': list(items)}}


def test_public_cert_domains_only_active_safe_roles():
    data = _data(
        {'domain': 'vpn.example.com', 'role': 'client', 'status': 'active'},
        {'domain': 'landing.example.com', 'role': 'landing', 'status': 'active'},
        {'domain': 'old.example.com', 'role': 'client', 'status': 'removed'},
        {'domain': 'admin.example.com', 'role': 'admin', 'status': 'active'},
        {'domain': 'bad.example.com', 'role': 'client'},
    )
    assert panel._public_cert_domains(data) == [
        {'domain': 'vpn.example.com', 'role': 'client', 'created_at': None},
        {'domain': 'landing.example.com', 'role': 'landing', 'created_at': None},
    ]


def test_ask_endpoint_allows_only_authorized_domain(monkeypatch):
    monkeypatch.setenv('TTVPN_CERT_ASK_TOKEN', 'ask-token-value-' + 'x' * 20)
    panel._cert_ask_state.update(window=0.0, total=0, by_ip={})
    data = _data({'domain': 'vpn.example.com', 'role': 'client', 'status': 'active'})
    request = SimpleNamespace(client=SimpleNamespace(host='172.18.0.5'))
    with mock.patch.object(panel, 'load_data', return_value=data):
        allowed = panel.certificate_ask(request, 'ask-token-value-' + 'x' * 20, 'vpn.example.com')
        denied = panel.certificate_ask(request, 'ask-token-value-' + 'x' * 20, 'other.example.com')
        bad_token = panel.certificate_ask(request, 'wrong', 'vpn.example.com')
        invalid = panel.certificate_ask(request, 'ask-token-value-' + 'x' * 20, 'localhost')
    assert allowed.status_code == 200
    assert denied.status_code == 404
    assert bad_token.status_code == 403
    assert invalid.status_code == 404


def test_ask_endpoint_rejects_missing_server_token(monkeypatch):
    monkeypatch.delenv('TTVPN_CERT_ASK_TOKEN', raising=False)
    panel._cert_ask_state.update(window=0.0, total=0, by_ip={})
    request = SimpleNamespace(client=SimpleNamespace(host='172.18.0.5'))
    response = panel.certificate_ask(request, '', 'vpn.example.com')
    assert response.status_code == 403


def test_csrf_comparison_requires_session_and_header():
    good = SimpleNamespace(session={'backup_csrf': 'token'},
                           headers={'x-backup-csrf': 'token'})
    bad = SimpleNamespace(session={'backup_csrf': 'token'},
                          headers={'x-backup-csrf': 'other'})
    missing = SimpleNamespace(session={}, headers={})
    assert panel._cert_csrf_ok(good)
    assert not panel._cert_csrf_ok(bad)
    assert not panel._cert_csrf_ok(missing)


def test_host_gate_accepts_only_registered_custom_roles():
    panel_stub = SimpleNamespace(
        certificate_domain_hosts=lambda: ({'vpn.example.com'}, {'landing.example.com'}))
    settings_stub = SimpleNamespace(public_base='https://lk.ttvpn.ru')
    with mock.patch.object(billing_router, '_panel', return_value=panel_stub), \
         mock.patch.object(billing_router, 'get_settings', return_value=settings_stub):
        client, admin, apex = billing_router._hosts()
    assert 'vpn.example.com' in client
    assert 'landing.example.com' in apex
    assert 'admin.example.com' not in admin


def test_settings_page_has_certificate_section_and_menu():
    text = open('templates/settings.html', encoding='utf-8').read()
    assert 'id="settings-cert-domains"' in text
    assert 'href="#settings-cert-domains"' in text
    assert 'id="certDomainInput"' in text
    assert '/api/certificates/domains' in text
    assert 'removeCertDomain' in text
