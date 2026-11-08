"""Billing plan/device fixes: defaults, stable targets, payment snapshots.

These tests use the real SQLite store and a stub ``app`` module, so they run
without starting the panel or touching any SSH server.
"""

import asyncio
import json
import sys
import types

import pytest

from billing import provision, store


def _install_fake_panel(monkeypatch, data):
    """Provide the bits of ``app`` that billing imports lazily."""
    module = types.ModuleType("app")
    module.DATA_LOCK = asyncio.Lock()
    module.load_data = lambda: json.loads(json.dumps(data))
    module.save_data = lambda payload: (data.clear(), data.update(payload))
    monkeypatch.setitem(sys.modules, "app", module)
    return module


# ------------------------------------------------------------------ plans


def test_seeded_plans_default_to_five_devices(tmp_path):
    db = str(tmp_path / "billing.db")
    store.init_db(db)
    store.seed_plans(db, 0, "awg3")
    plans = store.list_plans(db, only_active=False)
    assert plans, "seed_plans must create the default plans"
    assert {plan["device_limit"] for plan in plans} == {5}
    assert all("5 устройств" in plan["description"] for plan in plans)


def test_legacy_plans_are_raised_to_five_once(tmp_path):
    db = str(tmp_path / "billing.db")
    store.init_db(db)
    store.seed_plans(db, 0, "awg3")
    plan = store.list_plans(db, only_active=False)[0]

    # Simulate a database created before the migration.
    with store.connect(db) as conn:
        conn.execute("UPDATE plans SET device_limit = 1, description = '1 устройство'")
        conn.execute("DELETE FROM meta WHERE key = 'schema_version'")

    store.init_db(db)
    migrated = store.get_plan(db, plan["id"], only_active=False)
    assert migrated["device_limit"] == 5
    assert migrated["description"] == "5 устройств"

    # A deliberate lower limit set after the migration must survive a restart.
    store.update_plan(db, plan["id"], {"device_limit": 2, "description": "2 устройства"})
    store.init_db(db)
    assert store.get_plan(db, plan["id"], only_active=False)["device_limit"] == 2


def test_plan_target_uid_backfill_is_stable(tmp_path):
    db = str(tmp_path / "billing.db")
    store.init_db(db)
    plan = store.create_plan(
        db, slug="test", name="Тест", description="", price_kopecks=100,
        days=30, device_limit=5, server_id=1, protocol="awg3",
    )
    assert not plan.get("server_uid")

    store.backfill_plan_uids(db, {1: "uid-b"})
    assert store.get_plan(db, plan["id"], only_active=False)["server_uid"] == "uid-b"

    # Re-running after a server reorder must not rewrite a resolved link.
    store.backfill_plan_uids(db, {1: "uid-other"})
    assert store.get_plan(db, plan["id"], only_active=False)["server_uid"] == "uid-b"


# --------------------------------------------------------------- provision


def test_pick_target_follows_uid_after_reorder(monkeypatch):
    data = {"servers": [
        {"uid": "uid-a", "name": "A", "protocols": {"awg3": {}},
         "self_service_enabled": True},
        {"uid": "uid-b", "name": "B", "protocols": {"awg3": {}},
         "self_service_enabled": True},
    ]}
    _install_fake_panel(monkeypatch, data)

    index, protocol = provision.pick_target(0, "awg3", server_uid="uid-b")
    assert data["servers"][index]["uid"] == "uid-b"
    assert protocol == "awg3"

    # A plan whose server disappeared must degrade to the stored index, not
    # crash the post-payment flow.
    data["servers"].reverse()
    index, _ = provision.pick_target(1, "awg3", server_uid="uid-gone")
    assert index == 1


def test_billing_mutations_hold_the_panel_data_lock(monkeypatch):
    data = {"users": []}
    panel = _install_fake_panel(monkeypatch, data)
    seen = {}

    def save(payload):
        seen["locked"] = panel.DATA_LOCK.locked()
        data.clear()
        data.update(payload)

    panel.save_data = save

    asyncio.run(provision._mutate(lambda state: state["users"].append({"id": "u1"})))

    assert seen["locked"] is True, "data.json must be updated under DATA_LOCK"
    assert data["users"] == [{"id": "u1"}]


# ---------------------------------------------------------------- payments


def test_payment_snapshots_terms_and_tracks_provisioning(tmp_path):
    db = str(tmp_path / "billing.db")
    store.init_db(db)
    payment = store.create_payment(
        db, user_id="u1", plan_id="p1", provider="yookassa",
        amount_kopecks=19900, currency="RUB", idempotency_key="k1",
        plan_days=30, plan_device_limit=5,
        plan_server_uid="uid-b", plan_protocol="awg3",
    )
    assert payment["plan_days"] == 30
    assert payment["plan_device_limit"] == 5
    assert payment["plan_server_uid"] == "uid-b"
    assert payment["provisioned"] == 0

    # Unpaid or unapplied orders are not retried.
    assert store.last_unprovisioned_payment(db, "u1") is None

    store.mark_status(db, payment["id"], "succeeded")
    assert store.claim_applied(db, payment["id"]) is True
    pending = store.last_unprovisioned_payment(db, "u1")
    assert pending["id"] == payment["id"]

    store.mark_provisioned(db, payment["id"])
    assert store.last_unprovisioned_payment(db, "u1") is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
