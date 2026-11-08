"""The cabinet device list must stay usable by the profile logic.

The second-profile feature is only reachable if the object handed to
``provision.profile_options`` still carries ``connection_ids``; without it the
list of free protocols is always empty and the "add profile" form never renders.

Everything here runs against a temporary SQLite file: both the billing store and
``provision._panel_settings_db`` are redirected, so a test can never write into
the developer's working ``billing.db``.
"""

import pytest

from billing import provision, router as billing_router, settings as billing_settings, store


@pytest.fixture
def cabinet(tmp_path, monkeypatch):
    db = str(tmp_path / "billing.db")
    store.init_db(db)
    monkeypatch.setattr(billing_router, "_db", lambda: db)
    # ``provision.user_devices`` does ``from billing.settings import db_path``
    # inside the call, so the module attribute is the only reliable seam.
    monkeypatch.setattr(billing_settings, "db_path", lambda: db)
    state = {"connections": []}

    def user_connections(user_id):
        return list(state["connections"])

    monkeypatch.setattr(provision, "user_connections", user_connections)
    monkeypatch.setattr(billing_router.provision, "user_connections", user_connections)
    return db, state


def _device(db, state, protocols):
    state["connections"] = [
        {"id": f"c{index}", "name": f"device-{index}", "server_id": 0,
         "protocol": protocol}
        for index, protocol in enumerate(protocols, start=1)
    ]
    device = store.ensure_devices(db, "u1", state["connections"])[0]
    for connection in state["connections"][1:]:
        store.attach_connection(db, "u1", device["id"], connection["id"])
    return device


def test_named_devices_keep_connection_ids(cabinet):
    db, state = cabinet
    _device(db, state, ["awg3", "awg2"])

    devices = billing_router._named_devices("u1")
    assert len(devices) == 1
    assert sorted(devices[0]["connection_ids"]) == ["c1", "c2"]

    servers = [{"protocols": {"awg3": {}, "awg2": {}, "awg": {}}}]
    owned = {str(item["id"]): item for item in devices[0]["connections"]}
    options = provision.profile_options(devices[0], owned, servers)
    assert [option["id"] for option in options] == ["awg"], (
        "the cabinet must offer the protocol that is not attached yet"
    )


def test_full_device_offers_no_more_protocols(cabinet):
    db, state = cabinet
    _device(db, state, ["awg3", "awg2", "awg"])

    devices = billing_router._named_devices("u1")
    servers = [{"protocols": {"awg3": {}, "awg2": {}, "awg": {}}}]
    owned = {str(item["id"]): item for item in devices[0]["connections"]}
    assert provision.profile_options(devices[0], owned, servers) == []
    assert len(devices[0]["connections"]) == provision.MAX_PROFILES_PER_DEVICE


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
