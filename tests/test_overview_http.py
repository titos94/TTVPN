"""Real overview HTTP requests, isolated from startup and external services."""
import asyncio
import base64
import json
import os
from pathlib import Path
import socket
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
SECRET = 'synthetic-overview-session-secret'


@pytest.mark.parametrize('billing_enabled', ['0', '1'])
def test_overview_http_in_isolated_process(tmp_path, billing_enabled):
    data = {
        'users': [
            {'id': 'admin', 'username': 'admin-test', 'role': 'admin', 'enabled': True},
            {'id': 'support', 'username': 'support-test', 'role': 'support', 'enabled': True},
            {'id': 'user', 'username': 'user-test', 'role': 'user', 'enabled': True},
            {'id': 'disabled', 'username': 'disabled-test', 'role': 'admin', 'enabled': False},
            {'id': 'none', 'username': 'none-test', 'role': 'none', 'enabled': True},
            {'id': 'unknown', 'username': 'unknown-test', 'role': 'unknown', 'enabled': True},
        ],
        'servers': [{'name': '<Synthetic>', 'host': 'node.invalid', 'password': 'synthetic-secret'}],
        'user_connections': [],
    }
    data_file = tmp_path / 'data.json'
    data_file.write_text(json.dumps(data), encoding='utf-8')
    env = {
        'PATH': os.environ.get('PATH', ''), 'HOME': str(tmp_path),
        'PYTHONPATH': str(ROOT), 'PYTHONDONTWRITEBYTECODE': '1',
        'DATA_FILE': str(data_file), 'SECRET_KEY': SECRET,
        'BILLING_DB_PATH': str(tmp_path / 'billing.db'),
        'TUNNEL_STATE_FILE': str(tmp_path / 'tunnels.json'),
        'SSH_KNOWN_HOSTS_FILE': str(tmp_path / 'known_hosts'),
        'BILLING_ENABLED': billing_enabled,
    }
    result = subprocess.run([sys.executable, str(Path(__file__).resolve()), billing_enabled],
                            cwd=ROOT, env=env, capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    assert json.loads(data_file.read_text(encoding='utf-8')) == data


def _run_http_checks(billing_enabled):
    # Block actual network access even if an import or handler unexpectedly tries it.
    def forbidden(*args, **kwargs):
        raise AssertionError('network access forbidden in overview HTTP tests')

    socket.socket.connect = forbidden
    socket.socket.connect_ex = forbidden
    socket.create_connection = forbidden

    import httpx
    from itsdangerous import TimestampSigner
    import app as panel

    def cookie(user_id):
        payload = base64.b64encode(json.dumps({'user_id': user_id}).encode())
        return TimestampSigner(SECRET).sign(payload).decode()

    async def check():
        transport = httpx.ASGITransport(app=panel.app)
        async with httpx.AsyncClient(transport=transport, base_url='http://synthetic.invalid',
                                     follow_redirects=False) as client:
            async def get(user_id=None):
                cookies = {'session': cookie(user_id)} if user_id else {}
                return await client.get('/overview-preview', cookies=cookies)

            for user_id in (None, 'missing', 'disabled', 'none'):
                response = await get(user_id)
                assert response.status_code == 302, (user_id, response.status_code)
                assert response.headers['location'] == '/login'
            for user_id in ('user', 'unknown'):
                assert (await get(user_id)).status_code == 403
            response = await client.get('/overview-preview', cookies={'session': cookie('admin') + 'tampered'})
            assert response.status_code == 302 and response.headers['location'] == '/login'

            routes = {getattr(route, 'path', '') for route in panel.app.routes}
            assert ('/billing/brand/logo' in routes) == (billing_enabled == '1')
            for role in ('admin', 'support'):
                response = await get(role)
                assert response.status_code == 200
                assert response.headers['cache-control'] == 'no-store'
                assert '&lt;Synthetic&gt;' in response.text and 'node.invalid' in response.text
                assert 'synthetic-secret' not in response.text
                assert '<span>TTVPN</span>' in response.text
                assert ('href="/servers/new"' in response.text) == (role == 'admin')
                shell = response.text.split('<div class="tt-overview"', 1)[1].split('</main>', 1)[0]
                assert ('href="/billing-admin"' in shell) == (role == 'admin' and billing_enabled == '1')

            # A synthetic SVG is sufficient to exercise real branding discovery.
            brand_dir = Path(panel.DATA_FILE).parent / 'branding'
            brand_dir.mkdir(exist_ok=True)
            (brand_dir / 'logo.svg').write_text('<svg xmlns="http://www.w3.org/2000/svg"></svg>')
            response = await get('admin')
            assert response.status_code == 200 and response.headers['cache-control'] == 'no-store'
            assert ('<img src="/billing/brand/logo?v=' in response.text) == (billing_enabled == '1')
            assert ('<span>TTVPN</span>' in response.text) == (billing_enabled == '0')

    asyncio.run(check())


if __name__ == '__main__':
    _run_http_checks(sys.argv[1])
