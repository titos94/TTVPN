"""Запись Telegram не должна терять изменения или удерживать DATA_LOCK во время SSH."""

import json
from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


class ConcurrentWriteLost(AssertionError):
    pass


def test_admin_create_client_preserves_write_without_locking_during_ssh(tmp_path):
    """The bot must not lose a concurrent write, nor hold DATA_LOCK over SSH.

    The local row is committed against a freshly loaded snapshot under the
    panel's ``DATA_LOCK``, so a payment or traffic write made while the SSH
    round-trip is running survives, and the lock is never held across SSH.
    """
    root = Path(__file__).resolve().parents[1]
    env = {
        "DATA_FILE": str(tmp_path / "data.json"),
        "BILLING_DB_PATH": str(tmp_path / "billing.db"),
        "SSH_KNOWN_HOSTS_FILE": str(tmp_path / "known_hosts"),
        "TUNNEL_STATE_FILE": str(tmp_path / "tunnel.json"),
        "SECRET_KEY": "synthetic-test-key-not-for-production",
        "BILLING_ENABLED": "0",
        "TTVPN_PRODUCTION": "0",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONNOUSERSITE": "1",
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(root),
    }
    script = textwrap.dedent('''
        import asyncio
        import json
        import os
        import socket
        import sqlite3
        import subprocess
        import threading
        from pathlib import Path

        def forbidden(*args, **kwargs):
            raise AssertionError("network or subprocess forbidden")

        socket.socket.connect = forbidden
        socket.socket.connect_ex = forbidden
        socket.socket.sendto = forbidden
        socket.socket.sendmsg = forbidden
        original_send = socket.socket.send
        def guarded_send(sock, *args, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                forbidden()
            return original_send(sock, *args, **kwargs)
        socket.socket.send = guarded_send
        socket.create_connection = forbidden
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex",
                     "gethostbyaddr", "getnameinfo"):
            setattr(socket, name, forbidden)
        subprocess.Popen = forbidden
        os.system = forbidden
        for name in ("posix_spawn", "posix_spawnp", "fork", "forkpty"):
            if hasattr(os, name):
                setattr(os, name, forbidden)
        sqlite3.connect = forbidden
        from billing import store
        store.init_db = lambda *args, **kwargs: None
        import app
        import telegram_bot
        from billing import provision

        app.save_data({
            "servers": [{"uid": "server-test", "host": "server.invalid",
                         "protocols": {"wireguard": {"installed": True}}}],
            "users": [{"id": "user-test", "username": "synthetic",
                       "expiration_date": "2030-01-01T00:00:00"}],
            "user_connections": [],
        })
        entered = threading.Event()
        release = threading.Event()

        class FakeSSH:
            def connect(self):
                pass
            def disconnect(self):
                pass

        class FakeManager:
            def add_client(self, name, host):
                assert (name, host) == ("synthetic-peer", "server.invalid")
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("SSH synchronization timed out")
                return {"client_id": "synthetic-client", "config": "synthetic-config"}

        class FakeTelegram:
            def __init__(self):
                self.edits = []
            async def edit_message(self, *args, **kwargs):
                self.edits.append(args)
            async def send_message(self, *args, **kwargs):
                pass
            async def send_document(self, *args, **kwargs):
                pass

        telegram_bot._get_ssh_and_manager = lambda server, proto: (FakeSSH(), FakeManager())

        async def scenario():
            api = FakeTelegram()
            task = asyncio.create_task(telegram_bot._admin_create_client(
                api, 1, 2, 0, "wireguard", "synthetic-peer", "user-test",
                app.load_data, app.save_data, lambda *args: "", "ru"))
            try:
                assert await asyncio.to_thread(entered.wait, 5), "fake SSH was not reached"
                def concurrent_write(data):
                    data["users"][0]["expiration_date"] = "2030-02-01T00:00:00"
                    data["users"][0]["test_payment_ids"] = ["payment-test"]
                # По архитектурному контракту DATA_LOCK не удерживается на время SSH.
                try:
                    await asyncio.wait_for(provision._mutate(concurrent_write), 5)
                except asyncio.TimeoutError as exc:
                    raise AssertionError(
                        "Параллельная запись не завершилась во время SSH: "
                        "по архитектурному контракту DATA_LOCK нельзя удерживать через SSH"
                    ) from exc
                intermediate = app.load_data()
                assert intermediate["users"][0]["test_payment_ids"] == ["payment-test"]
                assert intermediate["users"][0]["expiration_date"] == "2030-02-01T00:00:00"
            finally:
                release.set()
            await asyncio.wait_for(task, 10)
            after = app.load_data()
            assert len(api.edits) == 2, repr(api.edits)
            assert len(after["user_connections"]) == 1, repr(api.edits)
            connection = after["user_connections"][0]
            assert connection["client_id"] == "synthetic-client"
            assert connection["user_id"] == "user-test"
            assert not Path(os.environ["BILLING_DB_PATH"]).exists()
            print("RESULT=" + json.dumps(after["users"][0]))

        asyncio.run(scenario())
    ''')
    run = subprocess.run(
        [sys.executable, "-c", script], cwd=root, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr + "\n" + run.stdout
    results = [line[7:] for line in run.stdout.splitlines() if line.startswith("RESULT=")]
    assert len(results) == 1, run.stdout
    user = json.loads(results[0])
    if (user.get("expiration_date") != "2030-02-01T00:00:00"
            or user.get("test_payment_ids") != ["payment-test"]):
        raise ConcurrentWriteLost(f"Параллельная запись потеряна: {user!r}")
