"""Изолированные проверки обработчика уведомлений ЮKassa."""

import asyncio
from types import SimpleNamespace

import pytest

from billing import router, store, yookassa


class Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


@pytest.fixture
def scenario(tmp_path, monkeypatch):
    db = str(tmp_path / "billing.db")
    store.init_db(db)
    payment = store.create_payment(
        db, user_id="test-user", plan_id="test-plan", provider="yookassa",
        amount_kopecks=19900, currency="RUB", idempotency_key="test-key",
    )
    store.set_provider_payment(db, payment["id"], "provider-id", "", "pending")
    monkeypatch.setattr(router, "_db", lambda: db)
    monkeypatch.setattr(router, "get_settings", lambda: SimpleNamespace(
        yookassa_ready=True, yookassa_shop_id="test", yookassa_secret_key="test"))
    verified = dict(provider_payment_id="provider-id", status="succeeded",
                    amount_kopecks=19900, currency="RUB",
                    internal_payment_id=payment["id"], raw={})
    calls = []

    def verify(**kwargs):
        calls.append(kwargs["provider_payment_id"])
        return yookassa.VerifiedPayment(**verified)

    monkeypatch.setattr(yookassa, "verify_payment", verify)
    monkeypatch.setattr(store, "get_plan", lambda *a, **k: {"days": 30})
    grants = []

    async def extend(user_id, days):
        grants.append((user_id, days))

    async def provision(*args):
        return True

    monkeypatch.setattr(router.provision, "extend_subscription", extend)
    monkeypatch.setattr(router, "_provision_paid_devices", provision)
    monkeypatch.setattr(router, "_trigger_peer_sync", lambda *a: None)

    def send(payload=None):
        if payload is None:
            payload = {"event": "payment.succeeded", "object": {"id": "provider-id"}}
        return asyncio.run(router.api_yookassa_webhook(Request(payload)))

    def row():
        return store.get_payment(db, payment["id"])

    return SimpleNamespace(db=db, payment=payment, verified=verified,
                           calls=calls, grants=grants, send=send, row=row)


@pytest.mark.parametrize("field,value", [
    ("amount_kopecks", 19899), ("currency", "USD"),
    ("internal_payment_id", "another-payment"),
])
def test_mismatched_verified_terms_are_rejected(scenario, field, value):
    scenario.verified[field] = value
    response = scenario.send()
    assert response.status_code == 400
    assert scenario.row()["status"] == "pending"
    assert not scenario.row()["applied"]
    assert scenario.grants == []


def test_replay_grants_only_once_and_preserves_paid_at(scenario):
    assert scenario.send() == {"ok": True}
    first = scenario.row()
    assert first["status"] == "succeeded"
    assert first["paid_at"]
    assert first["applied"]
    assert scenario.send() == {"ok": True}
    assert scenario.row()["paid_at"] == first["paid_at"]
    assert scenario.grants == [("test-user", 30)]
    assert scenario.calls == ["provider-id", "provider-id"]


def test_verified_status_overrides_untrusted_event(scenario):
    scenario.verified["status"] = "pending"
    assert scenario.send() == {"ok": True}
    assert scenario.row()["status"] == "pending"
    assert scenario.grants == []


def test_cancel_and_late_cancel_do_not_grant_or_revoke(scenario):
    assert scenario.send() == {"ok": True}
    paid_at = scenario.row()["paid_at"]
    scenario.verified["status"] = "canceled"
    assert scenario.send() == {"ok": True}
    assert scenario.row()["status"] == "succeeded"
    assert scenario.row()["paid_at"] == paid_at
    assert scenario.grants == [("test-user", 30)]


def test_canceled_payment_is_not_applied(scenario):
    scenario.verified["status"] = "canceled"
    assert scenario.send() == {"ok": True}
    assert scenario.row()["status"] == "canceled"
    assert scenario.grants == []
    scenario.verified["status"] = "succeeded"
    assert scenario.send().status_code == 503
    assert scenario.row()["status"] == "canceled"
    assert scenario.grants == []


def test_verification_failure_has_no_effect(scenario, monkeypatch):
    def fail(**kwargs):
        raise yookassa.YooKassaError("offline")
    monkeypatch.setattr(yookassa, "verify_payment", fail)
    assert scenario.send().status_code == 400
    assert scenario.row()["status"] == "pending"
    assert scenario.grants == []


def test_failed_grant_returns_503_and_can_be_retried(scenario, monkeypatch):
    async def fail(*args):
        raise RuntimeError("test failure")
    monkeypatch.setattr(router.provision, "extend_subscription", fail)
    assert scenario.send().status_code == 503
    assert scenario.row()["status"] == "succeeded"
    assert not scenario.row()["applied"]

    async def succeed(user_id, days):
        scenario.grants.append((user_id, days))
    monkeypatch.setattr(router.provision, "extend_subscription", succeed)
    assert scenario.send() == {"ok": True}
    assert scenario.row()["applied"]
    assert scenario.grants == [("test-user", 30)]


def test_concurrent_notification_does_not_ack_unfinished_grant(scenario, monkeypatch):
    """A second delivery must not get 200 while the first grant is unfinished.

    The claim in ``billing.store`` gives the right to grant days to exactly one
    handler; the replayed notice therefore sees ``in_progress`` and answers 503,
    so the provider keeps retrying instead of being told the order is settled.
    """
    async def check():
        started, release = asyncio.Event(), asyncio.Event()

        async def fail_after_delay(*args):
            started.set()
            await release.wait()
            raise RuntimeError("test failure")

        monkeypatch.setattr(router.provision, "extend_subscription", fail_after_delay)
        request = Request({"object": {"id": "provider-id"}})
        first = asyncio.create_task(router.api_yookassa_webhook(request))
        await asyncio.wait_for(started.wait(), timeout=2)
        try:
            second = await router.api_yookassa_webhook(request)
        finally:
            release.set()
            result = await first
        assert result.status_code == 503
        assert getattr(second, "status_code", 200) == 503

    asyncio.run(check())


def test_bad_json_and_missing_id_do_not_verify(scenario):
    assert scenario.send(ValueError("bad json")).status_code == 400
    assert scenario.send({"object": {}}).status_code == 400
    assert scenario.calls == []
    assert scenario.row()["status"] == "pending"


@pytest.mark.parametrize("payload", [[], {"object": [1]}])
def test_wrong_json_shape_returns_400(scenario, payload):
    assert scenario.send(payload).status_code == 400
    assert scenario.calls == []


@pytest.mark.parametrize("provider_id", [None, 1, True, [], {}, "", "  ", " x ", "x" * 129])
def test_invalid_provider_id_returns_400(scenario, provider_id):
    assert scenario.send({"object": {"id": provider_id}}).status_code == 400
    assert scenario.calls == []


def test_provider_id_at_length_limit_is_verified(scenario):
    provider_id = "x" * 128
    assert scenario.send({"object": {"id": provider_id}}) == {"ok": True}
    assert scenario.calls == [provider_id]


def test_unknown_payment_is_acknowledged_without_grant(scenario):
    assert scenario.send({"object": {"id": "unknown"}}) == {"ok": True}
    assert scenario.calls == ["unknown"]
    assert scenario.row()["status"] == "pending"
    assert scenario.grants == []
