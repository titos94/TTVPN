"""Проверка реального запуска app.py без запуска HTTP-сервера."""
import json
from pathlib import Path
import subprocess
import sys


def test_script_and_late_import_share_state(tmp_path):
    root = Path(__file__).resolve().parents[1]
    (tmp_path / 'data.json').write_text(json.dumps({'servers': [], 'users': [], 'settings': {}}))
    (tmp_path / 'sitecustomize.py').write_text('''
import os, socket, subprocess
def forbidden(*args, **kwargs):
    raise AssertionError("network/process forbidden")
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
socket.socket.sendto = forbidden
socket.socket.sendmsg = forbidden
socket.create_connection = forbidden
_send = socket.socket.send
def guarded_send(sock, *args, **kwargs):
    if sock.family in (socket.AF_INET, socket.AF_INET6): forbidden()
    return _send(sock, *args, **kwargs)
socket.socket.send = guarded_send
for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex", "gethostbyaddr", "getnameinfo"):
    setattr(socket, name, forbidden)
subprocess.Popen = forbidden
os.system = forbidden
for name in ("posix_spawn", "posix_spawnp", "fork", "forkpty"):
    if hasattr(os, name): setattr(os, name, forbidden)
import uvicorn
def run(**kwargs):
    import __main__, app
    from billing import provision
    assert app is __main__ is provision._panel()
    assert kwargs['app'] is app.app
    assert provision._panel().DATA_LOCK is app.DATA_LOCK
    print("SCRIPT_IDENTITY_OK")
uvicorn.run = run
''')
    env = {
        'DATA_FILE': str(tmp_path / 'data.json'),
        'BILLING_DB_PATH': str(tmp_path / 'billing.db'),
        'SSH_KNOWN_HOSTS_FILE': str(tmp_path / 'known_hosts'),
        'TUNNEL_STATE_FILE': str(tmp_path / 'tunnel.json'),
        'SECRET_KEY': 'synthetic-test-key-not-for-production',
        'BILLING_ENABLED': '0', 'TTVPN_PRODUCTION': '0',
        'HOME': str(tmp_path), 'TMPDIR': str(tmp_path), 'PATH': '/usr/bin:/bin',
        'PYTHONPATH': str(tmp_path), 'PYTHONNOUSERSITE': '1',
        'PYTHONDONTWRITEBYTECODE': '1',
    }
    result = subprocess.run([sys.executable, str(root / 'app.py')], cwd=tmp_path,
                            env=env, capture_output=True, text=True, timeout=30)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'SCRIPT_IDENTITY_OK' in result.stdout
