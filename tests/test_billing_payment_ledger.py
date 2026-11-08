"""Payment ledger: forward-only statuses, immutable ``paid_at``, honest revenue.

Webhook replays and the cabinet poll used to be able to move an already paid
order back to ``pending``/``canceled`` and to move ``paid_at`` to the retry
time, which made monthly revenue unreliable. These tests pin the rules down on
the real SQLite store and check the checkout provider classification at the
route level (no FastAPI app, SSH server or panel state needed).
"""

import asyncio
import types

import pytest

from billing import router as billing_router
from billing import store


def _db(tmp_path):
    path = str(tmp_path / "billing.db")
    store.init_db(path)
    return path


def _payment(db, **kwargs):
    params = dict(
        user_id="u1", plan_id="p1", provider="yookassa",
        amount_kopecks=19900, currency="RUB",
    )
    params.update(kwargs)
    params.setdefault("idempotency_key", f"key-{params['amount_kopecks']}-{params['provider']}")
    return store.create_payment(db, **params)


# ------------------------------------------------------------ transitions


def test_paid_at_is_written_once_and_replays_keep_it(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)

    assert store.mark_status(db, payment["id"], "succeeded") is True
    paid_at = store.get_payment(db, payment["id"])["paid_at"]
    assert paid_at

    # A replayed webhook must not move the timestamp into a later month.
    with store.connect(db) as conn:
        conn.execute(
            "UPDATE payments SET paid_at = '2024-01-31T23:59:00+00:00' WHERE id = ?",
            (payment["id"],),
        )
    assert store.mark_status(db, payment["id"], "succeeded") is True
    again = store.get_payment(db, payment["id"])
    assert again["status"] == "succeeded"
    assert again["paid_at"] == "2024-01-31T23:59:00+00:00"


def test_settled_payment_is_not_downgraded_by_a_late_notice(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)
    store.mark_status(db, payment["id"], "succeeded")
    paid_at = store.get_payment(db, payment["id"])["paid_at"]

    assert store.mark_status(db, payment["id"], "canceled") is False
    assert store.mark_status(db, payment["id"], "pending") is False

    row = store.get_payment(db, payment["id"])
    assert row["status"] == "succeeded"
    assert row["paid_at"] == paid_at


def test_canceled_payment_never_reopens(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)

    assert store.mark_status(db, payment["id"], "canceled") is True
    assert store.mark_status(db, payment["id"], "canceled") is True  # idempotent
    assert store.mark_status(db, payment["id"], "succeeded") is False

    row = store.get_payment(db, payment["id"])
    assert row["status"] == "canceled"
    assert row["paid_at"] is None


def test_refund_keeps_the_payment_date_and_leaves_revenue(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)
    store.mark_status(db, payment["id"], "succeeded")
    paid_at = store.get_payment(db, payment["id"])["paid_at"]

    assert store.mark_refunded(db, payment["id"]) is True
    row = store.get_payment(db, payment["id"])
    assert row["status"] == "refunded"
    assert row["paid_at"] == paid_at

    summary = store.payments_summary(db)
    assert summary["paid_count"] == 0
    assert summary["paid_kopecks"] == 0
    assert summary["refunded_count"] == 1
    assert summary["refunded_kopecks"] == 19900


def test_provider_status_update_cannot_reset_a_settled_row(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)
    store.mark_status(db, payment["id"], "succeeded")

    assert store.set_provider_payment(
        db, payment["id"], "yk-1", "https://pay", "pending"
    ) is True

    row = store.get_payment(db, payment["id"])
    assert row["provider_payment_id"] == "yk-1"
    assert row["status"] == "succeeded", "provider metadata must not reopen the order"


def test_unknown_status_is_rejected(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)
    with pytest.raises(ValueError):
        store.mark_status(db, payment["id"], "paid-ish")


def test_missing_payment_is_not_an_error(tmp_path):
    db = _db(tmp_path)
    assert store.mark_status(db, "nope", "succeeded") is False
    assert store.set_provider_payment(db, "nope", "yk", "/pay", "pending") is False


def test_legacy_provider_state_can_still_settle(tmp_path):
    """A row stored as ``waiting_for_capture`` must not become un-settleable."""
    db = _db(tmp_path)
    payment = _payment(db)
    with store.connect(db) as conn:
        conn.execute(
            "UPDATE payments SET status = 'waiting_for_capture' WHERE id = ?",
            (payment["id"],),
        )

    assert store.mark_status(db, payment["id"], "succeeded") is True
    assert store.get_payment(db, payment["id"])["status"] == "succeeded"


# --------------------------------------------------------------- revenue


def test_summary_separates_mock_free_and_real_money(tmp_path):
    db = _db(tmp_path)
    real = _payment(db, amount_kopecks=19900, idempotency_key="real")
    mock = _payment(db, amount_kopecks=29900, provider="mock", idempotency_key="mock")
    free = _payment(db, amount_kopecks=0, provider="promo", idempotency_key="free")
    _payment(db, amount_kopecks=49900, idempotency_key="pending")
    for row in (real, mock, free):
        store.mark_status(db, row["id"], "succeeded")

    summary = store.payments_summary(db)
    assert summary["paid_count"] == 1
    assert summary["paid_kopecks"] == 19900
    assert summary["free_count"] == 1
    assert summary["mock_count"] == 1
    assert summary["pending_count"] == 1


def test_migration_relabels_legacy_mock_and_free_payments(tmp_path):
    db = _db(tmp_path)
    with store.connect(db) as conn:
        rows = [
            # (id, provider, provider_payment_id, amount, status)
            ("legacy-mock", "yookassa", "mock_deadbeef", 19900, "succeeded"),
            ("legacy-free", "yookassa", None, 0, "succeeded"),
            ("legacy-live", "yookassa", "2f7d-live", 19900, "succeeded"),
        ]
        for payment_id, provider, provider_id, amount, status in rows:
            conn.execute(
                "INSERT INTO payments (id, user_id, plan_id, provider,"
                " provider_payment_id, idempotency_key, amount_kopecks, currency,"
                " status, created_at) VALUES (?,?,?,?,?,?,?, 'RUB', ?,"
                " '2024-01-01T00:00:00+00:00')",
                (payment_id, "u1", "p1", provider, provider_id,
                 f"key-{payment_id}", amount, status),
            )
        conn.execute("DELETE FROM meta WHERE key = 'schema_version'")

    store.init_db(db)

    providers = {
        row["id"]: row["provider"]
        for row in store.list_payments(db, 10)
    }
    assert providers == {
        "legacy-mock": "mock",
        "legacy-free": "promo",
        "legacy-live": "yookassa",
    }
    # Relabelled mock money is not reported as revenue.
    summary = store.payments_summary(db)
    assert summary["paid_kopecks"] == 19900
    assert summary["free_count"] == 1
    assert summary["mock_count"] == 1


# -------------------------------------------------------------- checkout


def _settings(**overrides):
    base = dict(
        mock=False, yookassa_ready=False, currency="RUB", db_path=":memory:",
        provision_devices=False, public_base="https://lk.example",
        yookassa_shop_id="", yookassa_secret_key="",
    )
    base.update(overrides)
    return types.SimpleNamespace(**base)


def _checkout(monkeypatch, *, settings, plan_price=19900, plan=None):
    created = {}

    def fake_create_payment(db_path, **kwargs):
        created.update(kwargs)
        return {"id": "pay-1", "idempotency_key": kwargs["idempotency_key"],
                "user_id": kwargs["user_id"], "plan_id": kwargs["plan_id"],
                "amount_kopecks": kwargs["amount_kopecks"],
                "provider": kwargs["provider"]}

    monkeypatch.setattr(billing_router, "get_settings", lambda: settings)
    monkeypatch.setattr(billing_router, "_current", lambda request: {"id": "u1"})
    monkeypatch.setattr(billing_router, "_rate_limit", lambda *a, **k: True)
    monkeypatch.setattr(billing_router, "_check_csrf", lambda *a, **k: True)
    monkeypatch.setattr(billing_router, "_json", lambda request: _async({"plan_id": "p1"}))
    monkeypatch.setattr(
        billing_router.store, "get_plan",
        lambda db_path, plan_id, only_active=True: plan or {
            "id": "p1", "name": "Месяц", "days": 30, "device_limit": 5,
            "price_kopecks": plan_price, "server_uid": "uid-a", "protocol": "awg3",
        },
    )
    monkeypatch.setattr(billing_router.store, "create_payment", fake_create_payment)
    monkeypatch.setattr(billing_router.store, "set_provider_payment", lambda *a, **k: True)
    monkeypatch.setattr(billing_router.store, "mark_status", lambda *a, **k: True)

    async def applied(payment_id):
        return True

    monkeypatch.setattr(billing_router, "_apply_payment", applied)
    response = asyncio.run(billing_router.api_checkout(object()))
    return response, created


async def _async(value):
    return value


def test_mock_checkout_is_not_recorded_as_yookassa(monkeypatch):
    response, created = _checkout(monkeypatch, settings=_settings(mock=True))
    assert created["provider"] == "mock"
    assert response["confirmation_url"] == "/billing/mock/pay-1"


def test_free_checkout_is_recorded_as_promo(monkeypatch):
    response, created = _checkout(monkeypatch, settings=_settings(), plan_price=0)
    assert created["provider"] == "promo"
    assert response["free"] is True


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
