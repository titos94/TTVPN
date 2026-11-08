"""Deployment boundaries, using an isolated ASGI application."""
import unittest
import os
import subprocess
import sys
import tempfile
from types import SimpleNamespace
from unittest.mock import patch

from fastapi import FastAPI, Request
from fastapi.testclient import TestClient
from billing import router as billing


class DeploymentSecurityTests(unittest.TestCase):
    def setUp(self):
        app = FastAPI()

        @app.get('/secret')
        def secret():
            return {'ok': True}

        @app.get('/api/billing/ip')
        def ip(request: Request):
            return {'ip': billing._client_ip(request)}

        @app.get('/api/certificates/ask/{token}')
        def ask(token: str):
            return {'token': token}

        billing.install_host_gate(app)
        self.client = TestClient(app)
        self.settings = patch.object(billing, 'get_settings', return_value=SimpleNamespace(public_base='https://lk.example'))
        self.hosts = patch.object(billing, '_hosts', return_value=({'lk.example'}, {'admin.example'}, {'example'}))
        self.settings.start()
        self.hosts.start()
        self.addCleanup(self.settings.stop)
        self.addCleanup(self.hosts.stop)

    def test_forwarded_host_cannot_bypass_client_gate(self):
        with patch.dict('os.environ', {'TTVPN_PRODUCTION': '1'}):
            response = self.client.get('/secret', headers={'host': 'lk.example', 'x-forwarded-host': 'admin.example'})
            self.assertEqual(response.status_code, 404)
            response = self.client.get('/secret', headers={'host': 'example'}, follow_redirects=False)
            self.assertEqual(response.status_code, 302)

    def test_unknown_host_is_not_admin(self):
        with patch.dict('os.environ', {'TTVPN_PRODUCTION': '1'}):
            self.assertEqual(self.client.get('/secret', headers={'host': 'evil.example'}).status_code, 404)
            self.assertEqual(self.client.get('/secret', headers={'host': 'admin.example'}).status_code, 200)
            self.assertEqual(self.client.get('/secret', headers={'host': 'evil.example', 'x-forwarded-host': 'admin.example'}).status_code, 404)

    def test_caddy_ask_path_is_not_blocked_by_public_host_gate(self):
        with patch.dict('os.environ', {'TTVPN_PRODUCTION': '1'}):
            response = self.client.get('/api/certificates/ask/test-token',
                                       headers={'host': 'amnezia_panel'})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json(), {'token': 'test-token'})

    def test_legacy_unknown_host_remains_compatible(self):
        with patch.dict('os.environ', {'TTVPN_PRODUCTION': '0'}):
            self.assertEqual(self.client.get('/secret', headers={'host': 'testserver'}).status_code, 200)

    def test_forwarded_ip_untrusted_by_default(self):
        with patch.dict('os.environ', {}, clear=True):
            response = self.client.get('/api/billing/ip', headers={'host': 'lk.example', 'x-forwarded-for': '198.51.100.1'})
        self.assertNotEqual(response.json()['ip'], '198.51.100.1')

    def test_production_bootstrap_and_secure_cookie(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DATA_FILE=os.path.join(tmp, 'data.json'),
                       BILLING_DB_PATH=os.path.join(tmp, 'billing.db'), BILLING_ENABLED='1',
                       SECRET_KEY='synthetic-secret', TTVPN_PRODUCTION='1', TTVPN_SECURE_COOKIE='1')
            script = '''
import asyncio
from unittest.mock import patch
from starlette.middleware.sessions import SessionMiddleware
import app
assert next(m.kwargs['https_only'] for m in app.app.user_middleware if m.cls is SessionMiddleware)
with patch.object(app, 'load_data', return_value={'users': []}), \\
     patch.object(app, '_start_conn_monitor') as monitor:
    try:
        asyncio.run(app.startup())
    except RuntimeError as exc:
        assert 'Empty user database' in str(exc)
    else:
        raise AssertionError('production bootstrap was not blocked')
    monitor.assert_not_called()
'''
            subprocess.run([sys.executable, '-c', script], env=env, check=True,
                           capture_output=True, text=True)

    def test_production_app_fails_closed_and_enforces_admin_host(self):
        with tempfile.TemporaryDirectory() as tmp:
            env = dict(os.environ, DATA_FILE=os.path.join(tmp, 'data.json'),
                       BILLING_DB_PATH=os.path.join(tmp, 'billing.db'), BILLING_ENABLED='1',
                       SECRET_KEY='synthetic-secret', TTVPN_PRODUCTION='1')
            def run(script, **overrides):
                return subprocess.run([sys.executable, '-c', script], env=dict(env, **overrides),
                                      capture_output=True, text=True)
            result = run('import app', SECRET_KEY='')
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('SECRET_KEY is required', result.stderr)
            result = run('import app', BILLING_ENABLED='0')
            self.assertNotEqual(result.returncode, 0)
            result = run("from unittest.mock import patch\nwith patch('billing.store.init_db', side_effect=RuntimeError('synthetic failure')):\n import app")
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('Production billing/host gate initialization failed', result.stderr)
            result = run('''
from fastapi.testclient import TestClient
from unittest.mock import patch
import app
user = {'id': 'fixture', 'username': 'fixture', 'role': 'user', 'enabled': True, 'description': 'self-registered'}
data = {'users': [user], 'servers': [], 'user_connections': [], 'settings': {'self_service': {'enabled': True}}}
client = TestClient(app.app, base_url='https://admin.ttvpn.ru')
with patch.object(app, 'get_current_user', return_value=user), \\
     patch.object(app.self_service_connections, 'load_data', return_value=data), \\
     patch.object(app.self_service_connections, '_settings', return_value={'enabled': True}), \\
     patch.object(app.self_service_connections, 'get_ssh', side_effect=AssertionError('remote call')):
    r = client.post('/api/my/connections/add', json={'server_id': 0, 'protocol': 'awg', 'name': 'test-device'})
    assert r.status_code == 403, (r.status_code, r.text)
    assert 'subscription plan' in r.text, r.text
assert client.get('/api/users', headers={'host': 'unknown.example'}).status_code == 404
''')
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_proxy_requires_matching_peer_network(self):
        from starlette.requests import Request
        def request(peer, forwarded):
            return Request({'type': 'http', 'method': 'GET', 'path': '/', 'headers': [(b'x-forwarded-for', forwarded.encode())], 'client': (peer, 1234), 'server': ('test', 80), 'scheme': 'http', 'query_string': b''})
        with patch.dict('os.environ', {'BILLING_TRUST_PROXY': '1', 'BILLING_TRUSTED_PROXY_CIDRS': '10.0.0.0/8'}):
            self.assertEqual(billing._client_ip(request('10.1.2.3', '192.0.2.1, 198.51.100.2')), '198.51.100.2')
            self.assertEqual(billing._client_ip(request('192.0.2.9', '198.51.100.2')), '192.0.2.9')
            self.assertEqual(billing._client_ip(request('10.1.2.3', 'garbage')), '10.1.2.3')


if __name__ == '__main__':
    unittest.main()
