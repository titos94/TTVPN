"""Registered devices: one plan slot per device, several profiles per device."""

import json
import sys
import types

import pytest

from billing import provision, store


def _db(tmp_path):
    path = str(tmp_path / "billing.db")
    store.init_db(path)
    return path


def test_new_device_is_adopted_for_each_connection(tmp_path):
    db = _db(tmp_path)
    connections = [
        {"id": "c1", "name": "device-1", "protocol": "awg3"},
        {"id": "c2", "name": "device-2", "protocol": "awg3"},
    ]
    devices = store.ensure_devices(db, "u1", connections)

    assert len(devices) == 2
    assert {d["connection_ids"][0] for d in devices} == {"c1", "c2"}

    # Idempotent: a second pass must not create duplicate devices.
    again = store.ensure_devices(db, "u1", connections)
    assert len(again) == 2


def test_two_profiles_of_one_device_use_one_slot(tmp_path):
    db = _db(tmp_path)
    first = store.ensure_devices(db, "u1", [{"id": "c1", "name": "Телефон"}])
    device_id = first[0]["id"]

    # A second protocol profile is attached to the same device.
    store.attach_connection(db, "u1", device_id, "c2")
    devices = store.ensure_devices(db, "u1", [
        {"id": "c1", "name": "Телефон"},
        {"id": "c2", "name": "Телефон"},
    ])

    assert len(devices) == 1, "one device must consume exactly one plan slot"
    assert sorted(devices[0]["connection_ids"]) == ["c1", "c2"]


def test_attach_refuses_a_foreign_device(tmp_path):
    db = _db(tmp_path)
    store.ensure_devices(db, "owner", [{"id": "c1", "name": "one"}])
    foreign = store.list_devices(db, "owner")[0]["id"]

    with pytest.raises(ValueError):
        store.attach_connection(db, "intruder", foreign, "c9")

    assert store.list_devices(db, "intruder") == []


def test_rename_is_owner_scoped(tmp_path):
    db = _db(tmp_path)
    device = store.ensure_devices(db, "u1", [{"id": "c1", "name": "device-1"}])[0]

    assert store.rename_device(db, "u1", device["id"], "Ноутбук") is True
    assert store.list_devices(db, "u1")[0]["name"] == "Ноутбук"
    assert store.rename_device(db, "u2", device["id"], "Чужой") is False
    assert store.list_devices(db, "u1")[0]["name"] == "Ноутбук"


def test_device_without_live_peers_releases_its_slot(tmp_path):
    db = _db(tmp_path)
    store.ensure_devices(db, "u1", [
        {"id": "c1", "name": "one"},
        {"id": "c2", "name": "two"},
    ])
    # The panel no longer lists c1 (peer removed outside billing).
    devices = store.ensure_devices(db, "u1", [{"id": "c2", "name": "two"}])
    assert len(devices) == 1
    assert devices[0]["connection_ids"] == ["c2"]


def test_peer_name_reuse_after_deletion():
    assert provision._free_peer_name({"device-1", "device-3"}) == "device-2"
    assert provision._free_peer_name(set()) == "device-1"


def test_peer_name_avoids_names_from_the_create_journal():
    """A deleted peer cannot be recreated under the same name.

    ``peer_creates`` replays a journaled operation keyed on the name and
    refuses to recreate an identity whose link was removed. Picking such a name
    would leave the paid slot permanently empty.
    """
    entries = {
        "op-1": {"identity": ["u1", "srv-uid", "awg3", "device-1"]},
        "op-2": {"identity": ["u1", "srv-uid", "awg3", "device-2"]},
        "op-3": {"identity": ["u2", "srv-uid", "awg3", "device-3"]},
    }

    module = types.ModuleType("app")
    module.peer_creates = types.SimpleNamespace(
        journal=types.SimpleNamespace(read=lambda: entries)
    )
    original = sys.modules.get("app")
    sys.modules["app"] = module
    try:
        used = provision._used_peer_names("u1", "srv-uid", "awg3", [])
        assert used == {"device-1", "device-2"}
        assert provision._free_peer_name(used) == "device-3"
    finally:
        if original is not None:
            sys.modules["app"] = original
        else:
            sys.modules.pop("app", None)


def test_ensure_connections_creates_one_peer_per_missing_slot(tmp_path, monkeypatch):
    db = _db(tmp_path)
    connections: list[dict] = []

    async def fake_create(user_id, server_id, protocol, name, operation_id=None):
        connections.append({"id": f"conn-{len(connections) + 1}", "name": name,
                            "server_id": server_id, "protocol": protocol})
        return {"status": "success"}

    module = types.ModuleType("app")
    module.load_data = lambda: {
        "servers": [{"uid": "srv-uid"}],
        "user_connections": list(connections),
    }
    module.peer_creates = types.SimpleNamespace(
        create=fake_create,
        journal=types.SimpleNamespace(read=lambda: {}),
    )
    monkeypatch.setitem(sys.modules, "app", module)

    def fake_user_connections(user_id):
        return list(connections)

    monkeypatch.setattr(provision, "user_connections", fake_user_connections)
    monkeypatch.setattr(provision, "_panel_settings_db", lambda: db)

    import asyncio
    asyncio.run(provision.ensure_connections(
        "u1", device_limit=2, server_id=0, protocol="awg3",
    ))

    assert [conn["name"] for conn in connections] == ["device-1", "device-2"]
    assert len(store.list_devices(db, "u1")) == 2

    # The third run must add nothing: two devices already fill the plan.
    asyncio.run(provision.ensure_connections(
        "u1", device_limit=2, server_id=0, protocol="awg3",
    ))
    assert len(connections) == 2


def test_moving_a_profile_keeps_one_slot(tmp_path):
    db = _db(tmp_path)
    first = store.ensure_devices(db, "u1", [{"id": "c1", "name": "Телефон"}])[0]
    second = store.ensure_devices(db, "u1", [
        {"id": "c1", "name": "Телефон"},
        {"id": "c2", "name": "Ноутбук"},
    ])
    assert len(second) == 2

    # The client regroups c2 under the first device: it must not count twice.
    store.attach_connection(db, "u1", first["id"], "c2")
    devices = store.list_devices(db, "u1")
    assert len(devices) == 2
    assert sorted(devices[0]["connection_ids"]) == ["c1", "c2"]

    # An emptied device releases its slot on the next sync.
    assert len(store.ensure_devices(db, "u1", [
        {"id": "c1", "name": "Телефон"},
        {"id": "c2", "name": "Ноутбук"},
    ])) == 1


def test_device_for_connection_is_idempotent(tmp_path):
    db = _db(tmp_path)
    first = store.device_for_connection(db, "u1", "c1", "Телефон")
    again = store.device_for_connection(db, "u1", "c1", "Другое имя")

    assert first["id"] == again["id"]
    assert len(store.list_devices(db, "u1")) == 1
    # Adoption never renames an existing device behind the client's back.
    assert again["name"] == "Телефон"


def test_profile_options_hide_attached_and_foreign_protocols():
    device = {"connection_ids": ["c1"]}
    connections = {"c1": {"id": "c1", "protocol": "awg3", "server_id": 0}}
    servers = [{"protocols": {
        "awg3": {"installed": True},
        "awg2": {"installed": True},
        "wireguard": {"installed": True},
        "awg_legacy": {"installed": False},
    }}]

    options = provision.profile_options(device, connections, servers)
    assert [option["id"] for option in options] == ["awg2"]

    device["connection_ids"] = ["c1", "c2", "c3"]
    connections["c2"] = {"id": "c2", "protocol": "awg2", "server_id": 0}
    connections["c3"] = {"id": "c3", "protocol": "awg_old", "server_id": 0}
    assert provision.profile_options(device, connections, servers) == []


def test_add_device_profile_uses_the_device_server(tmp_path, monkeypatch):
    db = _db(tmp_path)
    connections = [
        {"id": "c1", "name": "device-1", "server_id": 2, "protocol": "awg3"},
    ]
    device = store.ensure_devices(db, "u1", connections)[0]
    created: list[tuple] = []

    async def fake_create(user_id, server_id, protocol, name, operation_id=None):
        created.append((server_id, protocol, name))
        connections.append({
            "id": f"c{len(connections) + 1}", "name": name,
            "server_id": server_id, "protocol": protocol,
        })

    module = types.ModuleType("app")
    module.load_data = lambda: {
        "servers": [{}, {}, {"protocols": {"awg3": {}, "awg2": {}}}],
        "user_connections": list(connections),
    }
    module.peer_creates = types.SimpleNamespace(create=fake_create)
    monkeypatch.setitem(sys.modules, "app", module)
    monkeypatch.setattr(provision, "user_connections", lambda user_id: list(connections))
    monkeypatch.setattr(provision, "_panel_settings_db", lambda: db)

    import asyncio
    new = asyncio.run(provision.add_device_profile("u1", device["id"], "awg2"))

    assert created == [(2, "awg2", "device-2")]
    assert new["protocol"] == "awg2"
    devices = store.list_devices(db, "u1")
    assert len(devices) == 1, "a second profile must not take a new slot"
    assert sorted(devices[0]["connection_ids"]) == ["c1", "c2"]


def test_add_device_profile_caps_profiles_per_device(tmp_path, monkeypatch):
    db = _db(tmp_path)
    connections = [
        {"id": "c1", "name": "device-1", "server_id": 0, "protocol": "awg3"},
        {"id": "c2", "name": "device-2", "server_id": 0, "protocol": "awg2"},
    ]
    device = store.ensure_devices(db, "u1", connections)[0]
    store.attach_connection(db, "u1", device["id"], "c2")
    store.attach_connection(db, "u1", device["id"], "c3")
    connections.append({"id": "c3", "name": "device-3", "server_id": 0, "protocol": "awg_legacy"})

    monkeypatch.setattr(provision, "user_connections", lambda user_id: list(connections))
    monkeypatch.setattr(provision, "_panel_settings_db", lambda: db)

    import asyncio
    with pytest.raises(provision.ProvisionError) as error:
        asyncio.run(provision.add_device_profile("u1", device["id"], "awg3"))

    assert str(provision.MAX_PROFILES_PER_DEVICE) in str(error.value)


def test_add_device_profile_refuses_unknown_device_and_protocol(tmp_path, monkeypatch):
    db = _db(tmp_path)
    connections = [{"id": "c1", "name": "device-1", "server_id": 0, "protocol": "awg3"}]
    device = store.ensure_devices(db, "u1", connections)[0]

    module = types.ModuleType("app")
    module.load_data = lambda: {
        "servers": [{"protocols": {"awg3": {}}}],
        "user_connections": list(connections),
    }
    module.peer_creates = types.SimpleNamespace(
        create=lambda *args, **kwargs: pytest.fail("must not create a peer")
    )
    monkeypatch.setitem(sys.modules, "app", module)
    monkeypatch.setattr(provision, "user_connections", lambda user_id: list(connections))
    monkeypatch.setattr(provision, "_panel_settings_db", lambda: db)

    import asyncio

    with pytest.raises(provision.ProvisionError):
        asyncio.run(provision.add_device_profile("u1", "no-such-device", "awg3"))
    with pytest.raises(provision.ProvisionError):
        asyncio.run(provision.add_device_profile("u1", device["id"], "xray"))


def _profile_env(tmp_path, monkeypatch, records):
    """Device with one ``awg3`` profile and a server exposing ``records``."""
    db = _db(tmp_path)
    connections = [{"id": "c1", "name": "device-1", "server_id": 0, "protocol": "awg3"}]
    device = store.ensure_devices(db, "u1", connections)[0]
    created: list[tuple] = []

    async def fake_create(user_id, server_id, protocol, name, operation_id=None):
        created.append((server_id, protocol, name))
        connections.append({
            "id": f"c{len(connections) + 1}", "name": name,
            "server_id": server_id, "protocol": protocol,
        })

    module = types.ModuleType("app")
    module.load_data = lambda: {
        "servers": [{"protocols": records}],
        "user_connections": list(connections),
    }
    module.peer_creates = types.SimpleNamespace(create=fake_create)
    monkeypatch.setitem(sys.modules, "app", module)
    monkeypatch.setattr(provision, "user_connections", lambda user_id: list(connections))
    monkeypatch.setattr(provision, "_panel_settings_db", lambda: db)
    return device, created


def test_add_device_profile_refuses_a_duplicate_protocol(tmp_path, monkeypatch):
    """The cabinet hides attached protocols; the API must not create a twin."""
    device, created = _profile_env(
        tmp_path, monkeypatch, {"awg3": {}, "awg2": {}}
    )

    import asyncio
    with pytest.raises(provision.ProvisionError) as error:
        asyncio.run(provision.add_device_profile("u1", device["id"], "awg3"))

    assert "уже добавлен" in str(error.value)
    assert created == [], "a refused request must not touch the server"


def test_add_device_profile_refuses_an_uninstalled_protocol(tmp_path, monkeypatch):
    device, created = _profile_env(
        tmp_path, monkeypatch, {"awg3": {}, "awg_legacy": {"installed": False}}
    )

    import asyncio
    with pytest.raises(provision.ProvisionError) as error:
        asyncio.run(provision.add_device_profile("u1", device["id"], "awg_legacy"))

    assert "не установлен" in str(error.value)
    assert created == []


def test_add_device_profile_without_protocol_takes_the_first_free_one(tmp_path, monkeypatch):
    device, created = _profile_env(
        tmp_path,
        monkeypatch,
        {"awg3": {}, "awg_old": {"installed": False}, "awg2": {}},
    )

    import asyncio
    new = asyncio.run(provision.add_device_profile("u1", device["id"], None))

    assert created == [(0, "awg2", "device-2")]
    assert new["protocol"] == "awg2"


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
