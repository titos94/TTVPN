"""Проверка сохранения снимка установки протокола во время начисления."""

from pathlib import Path
import subprocess
import sys
import textwrap

import pytest


@pytest.mark.parametrize('change', ['payment', 'server', 'protocol', 'reorder', 'endpoint',
                                    'timeout', 'exception', 'error_result', 'save_error',
                                    'save_after_error', 'docker_error', 'bad_type',
                                    'missing_status', 'unknown_status', 'invalid_protocol', 'busy_port',
                                    'relink_error'])
def test_install_does_not_overwrite_concurrent_payment(tmp_path, change):
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
        "HOME": str(tmp_path),
        "TMPDIR": str(tmp_path),
        "PATH": "/usr/bin:/bin",
        "PYTHONPATH": str(root),
        "PYTHONNOUSERSITE": "1",
        "TEST_CHANGE": change,
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

        def no_network(*args, **kwargs):
            raise AssertionError("network forbidden")
        socket.socket.connect = no_network
        socket.socket.connect_ex = no_network
        socket.create_connection = no_network
        socket.socket.sendto = no_network
        socket.socket.sendmsg = no_network
        original_send = socket.socket.send
        def guarded_send(sock, *args, **kwargs):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                no_network()
            return original_send(sock, *args, **kwargs)
        socket.socket.send = guarded_send
        for name in ("getaddrinfo", "gethostbyname", "gethostbyname_ex",
                     "gethostbyaddr", "getnameinfo"):
            setattr(socket, name, no_network)
        subprocess.Popen = no_network
        os.system = no_network
        for name in ("posix_spawn", "posix_spawnp", "fork", "forkpty"):
            if hasattr(os, name):
                setattr(os, name, no_network)

        # Импорт панели не должен создавать даже временную базу биллинга.
        def no_database(*args, **kwargs):
            raise AssertionError("database forbidden")
        sqlite3.connect = no_database
        from billing import store
        store.init_db = lambda *args, **kwargs: None
        import app
        from billing import provision

        protocols = ({"awg": {"installed": True, "exit_link": {"exit_uid": "exit-test"}}}
                     if os.environ["TEST_CHANGE"] == "relink_error" else {})
        app.save_data({
            "servers": [{"uid": "server-test", "protocols": protocols}],
            "users": [{"id": "user-test", "expiration_date": "2030-01-01T00:00:00"}],
        })
        app._check_admin = lambda request: True
        entered = threading.Event()
        release = threading.Event()

        class FakeSSH:
            def connect(self):
                pass
            def disconnect(self):
                pass

        class FakeManager:
            def install_protocol(self, *args, **kwargs):
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test synchronization timed out")
                if os.environ["TEST_CHANGE"] == "timeout":
                    raise TimeoutError("synthetic lost SSH response")
                if os.environ["TEST_CHANGE"] == "exception":
                    raise RuntimeError("synthetic-sensitive-value")
                if os.environ["TEST_CHANGE"] == "error_result":
                    return {"status": "error", "message": "synthetic-sensitive-value"}
                if os.environ["TEST_CHANGE"] == "bad_type":
                    return "synthetic-sensitive-value"
                if os.environ["TEST_CHANGE"] == "missing_status":
                    return {"message": "synthetic-sensitive-value"}
                if os.environ["TEST_CHANGE"] == "unknown_status":
                    return {"status": "unknown"}
                return {"status": "success"}

        app.get_ssh = lambda server: FakeSSH()
        def fake_docker(ssh):
            if os.environ["TEST_CHANGE"] in ("invalid_protocol", "busy_port"):
                raise AssertionError("remote mutation started before validation")
            if os.environ["TEST_CHANGE"] == "docker_error":
                entered.set()
                if not release.wait(10):
                    raise RuntimeError("test synchronization timed out")
                raise RuntimeError("synthetic-sensitive-value")
        app.ensure_docker_installed = fake_docker
        app.get_protocol_manager = lambda ssh, protocol: FakeManager()
        app.get_used_ports = lambda ssh: {"udp": {"55424": "synthetic"}}
        async def fail_relink(*args):
            raise RuntimeError("synthetic-sensitive-value")
        app.exit_link_svc.relink_entry = fail_relink

        async def scenario():
            if os.environ["TEST_CHANGE"] in ("invalid_protocol", "busy_port"):
                import peer_lifecycle_lock as lifecycle
                protocol = "not-a-real-protocol" if os.environ["TEST_CHANGE"] == "invalid_protocol" else "wireguard"
                req = app.InstallProtocolRequest(protocol=protocol, port="55424")
                result = await app.api_install_protocol(None, 0, req)
                assert result.status_code == 400
                assert not lifecycle.diagnostics()["blocked"]
                assert not Path(str(app.DATA_FILE) + ".lifecycle-uncertain.json").exists()
                assert not entered.is_set()
                print("RESULT=" + json.dumps(app.load_data()["users"][0]))
                return
            protocol = "awg" if os.environ["TEST_CHANGE"] == "relink_error" else "wireguard"
            req = app.InstallProtocolRequest(protocol=protocol, port="")
            task = asyncio.create_task(app.api_install_protocol(None, 0, req))
            payment_task = None
            try:
                assert await asyncio.to_thread(entered.wait, 5), "installer did not start"
                # Модель будущей атомарной операции: срок и ID в одном JSON.
                def payment(data):
                    user = data["users"][0]
                    user["expiration_date"] = "2030-02-01T00:00:00"
                    user["test_payment_ids"] = ["payment-test"]
                    if os.environ["TEST_CHANGE"] == "server":
                        data["servers"][0]["uid"] = "replacement"
                    if os.environ["TEST_CHANGE"] == "protocol":
                        data["servers"][0]["protocols"]["wireguard"] = {"installed": False}
                    if os.environ["TEST_CHANGE"] == "endpoint":
                        data["servers"][0]["host"] = "changed.invalid"
                    if os.environ["TEST_CHANGE"] == "reorder":
                        data["servers"].insert(0, {"uid": "other", "protocols": {}})
                payment_task = asyncio.create_task(provision._mutate(payment))
                # Запись обязана завершиться, пока SSH намеренно остановлен.
                await asyncio.wait_for(payment_task, 5)
                if os.environ["TEST_CHANGE"] in ("save_error", "save_after_error"):
                    original_save = app.save_data
                    def fail_save(data):
                        if os.environ["TEST_CHANGE"] == "save_after_error":
                            original_save(data)
                        raise OSError("synthetic-sensitive-value")
                    app.save_data = fail_save
            finally:
                release.set()
            if os.environ["TEST_CHANGE"] not in ("payment", "reorder"):
                import peer_lifecycle_lock as lifecycle
                try:
                    await asyncio.wait_for(task, 10)
                    raise AssertionError("unknown SSH outcome reported as success")
                except lifecycle.PeerLifecycleUncertain:
                    pass
                assert lifecycle.diagnostics()["blocked"]
                assert Path(str(app.DATA_FILE) + ".lifecycle-uncertain.json").exists()
                after = app.load_data()
                if os.environ["TEST_CHANGE"] == "server":
                    assert after["servers"][0]["uid"] == "replacement"
                elif os.environ["TEST_CHANGE"] == "protocol":
                    assert after["servers"][0]["protocols"]["wireguard"] == {"installed": False}
                elif os.environ["TEST_CHANGE"] == "save_after_error":
                    assert after["servers"][0]["protocols"]["wireguard"]["installed"] is True
                elif os.environ["TEST_CHANGE"] == "relink_error":
                    assert after["servers"][0]["protocols"]["awg"]["exit_link"]["exit_uid"] == "exit-test"
                else:
                    assert "wireguard" not in after["servers"][0]["protocols"]
                # Имитация повторной инициализации процесса: файл fence остаётся.
                lifecycle.configure(app.DATA_FILE)
                try:
                    await app.api_install_protocol(None, 0, req)
                    raise AssertionError("durable fence did not block retry")
                except lifecycle.PeerLifecycleUncertain:
                    pass
                print("RESULT=" + json.dumps(after["users"][0]))
                return
            result = await asyncio.wait_for(task, 10)
            assert payment_task is not None
            await asyncio.wait_for(payment_task, 10)
            after = app.load_data()
            if os.environ["TEST_CHANGE"] in ("payment", "reorder"):
                assert isinstance(result, dict) and result.get("status") == "success", repr(result)
                target = next(s for s in after["servers"] if s["uid"] == "server-test")
                assert target["protocols"]["wireguard"]["installed"] is True
            assert not Path(app.DATA_FILE).with_name("billing.db").exists()
            print("RESULT=" + json.dumps(after["users"][0]))

        asyncio.run(scenario())
    ''')
    run = subprocess.run(
        [sys.executable, "-c", script], cwd=root, env=env,
        capture_output=True, text=True, timeout=30,
    )
    assert run.returncode == 0, run.stderr + "\n" + run.stdout
    assert "synthetic-sensitive-value" not in run.stderr + run.stdout
    if change not in ('payment', 'reorder', 'invalid_protocol', 'busy_port'):
        restart = subprocess.run(
            [sys.executable, '-c', textwrap.dedent('''
                import os, socket
                def forbidden(*args, **kwargs): raise AssertionError("network forbidden")
                socket.socket.connect = socket.socket.connect_ex = forbidden
                socket.socket.sendto = socket.socket.sendmsg = forbidden
                socket.create_connection = forbidden
                import peer_lifecycle_lock as lifecycle
                lifecycle.configure(os.environ["DATA_FILE"])
                try:
                    lifecycle.check_remote_fence()
                except lifecycle.PeerLifecycleUncertain:
                    pass
                else:
                    raise AssertionError("fence missing after process restart")
            ''')], cwd=tmp_path, env=env, capture_output=True, text=True, timeout=15,
        )
        assert restart.returncode == 0, restart.stderr + restart.stdout
    import json
    lines = [line[7:] for line in run.stdout.splitlines() if line.startswith("RESULT=")]
    assert len(lines) == 1, run.stdout
    user = json.loads(lines[0])
    if change in ('invalid_protocol', 'busy_port'):
        assert user.get("expiration_date") == "2030-01-01T00:00:00"
    else:
        assert user.get("expiration_date") == "2030-02-01T00:00:00" and user.get("test_payment_ids") == ["payment-test"]
