"""Проверка ограничения профилей при параллельных запросах без VPN-ноды."""

import asyncio
from types import SimpleNamespace

import pytest

from billing import provision, store
import peer_lifecycle_lock as lifecycle


@pytest.fixture
def setup_device(tmp_path, monkeypatch):
    db = str(tmp_path / "billing.db")
    store.init_db(db)
    connections = [
        {"id": "c1", "name": "device-1", "server_id": 0, "protocol": "awg3"},
        {"id": "c2", "name": "device-2", "server_id": 0, "protocol": "awg2"},
    ]
    device = store.ensure_devices(db, "u1", connections)[0]
    store.attach_connection(db, "u1", device["id"], "c2")
    assert len(store.list_devices(db, "u1")[0]["connection_ids"]) == 2
    assert provision.MAX_PROFILES_PER_DEVICE == 3

    panel = SimpleNamespace(load_data=lambda: {"servers": [{
        "uid": "server-1", "protocols": {
            "awg3": {}, "awg2": {}, "awg": {}, "awg_legacy": {},
        },
    }]})
    monkeypatch.setattr(provision, "_panel", lambda: panel)
    monkeypatch.setattr(provision, "_panel_settings_db", lambda: db)
    monkeypatch.setattr(provision, "user_connections", lambda user_id: list(connections))
    monkeypatch.setattr(provision, "_used_peer_names", lambda *args: set())
    return SimpleNamespace(db=db, device=device, connections=connections, panel=panel)


def test_sequential_addition_respects_cap(setup_device):
    env = setup_device
    calls = []

    async def create(user_id, server_id, protocol, name, operation_id):
        calls.append(protocol)
        env.connections.append({"id": "new", "name": name, "server_id": server_id,
                                "protocol": protocol})

    env.panel.peer_creates = SimpleNamespace(create=create)

    async def run():
        await provision.add_device_profile("u1", env.device["id"], "awg")
        with pytest.raises(provision.ProvisionError):
            await provision.add_device_profile("u1", env.device["id"], "awg_legacy")

    asyncio.run(run())
    assert calls == ["awg"]
    assert len(store.list_devices(env.db, "u1")[0]["connection_ids"]) == 3


def test_parallel_additions_cannot_exceed_profile_cap(setup_device):
    env = setup_device
    reached_create = []

    async def run():
        first_entered = asyncio.Event()
        second_waiting = asyncio.Event()
        release = asyncio.Event()

        class TrackingLock(asyncio.Lock):
            async def acquire(self):
                if self.locked():
                    second_waiting.set()
                return await super().acquire()

        lifecycle._mutation_locks[asyncio.get_running_loop()] = TrackingLock()

        @lifecycle.serialize_peer_lifecycle
        async def create(user_id, server_id, protocol, name, operation_id):
            reached_create.append(protocol)
            first_entered.set()
            await release.wait()
            env.connections.append({"id": "new-" + protocol, "name": name,
                                    "server_id": server_id, "protocol": protocol})

        env.panel.peer_creates = SimpleNamespace(create=create)
        first = asyncio.create_task(provision.add_device_profile("u1", env.device["id"], "awg"))
        await asyncio.wait_for(first_entered.wait(), 3)
        second = asyncio.create_task(provision.add_device_profile("u1", env.device["id"], "awg_legacy"))
        try:
            # Вторая задача действительно дошла до занятой блокировки, прежде
            # чем первая сможет завершить создание и привязать профиль.
            await asyncio.wait_for(second_waiting.wait(), 3)
        finally:
            release.set()
        return await asyncio.wait_for(
            asyncio.gather(first, second, return_exceptions=True), 3)

    results = asyncio.run(run())
    for result in results:
        if isinstance(result, Exception) and not isinstance(result, provision.ProvisionError):
            raise result
    profiles = store.list_devices(env.db, "u1")[0]["connection_ids"]
    assert len(profiles) == provision.MAX_PROFILES_PER_DEVICE
    assert reached_create == ["awg"]
    assert sum(isinstance(result, dict) for result in results) == 1
    assert sum(isinstance(result, provision.ProvisionError) for result in results) == 1
