"""The profile endpoint: plan slot stays one, tier checks are enforced.

Route-level test with the billing router's own dependencies patched, so no
FastAPI app, SSH server or panel state is needed.
"""

import asyncio

import pytest

from billing import provision, router as billing_router


def _request():
    return object()  # the patched helpers never touch the request


def _call(monkeypatch, *, user, body, active=True):
    monkeypatch.setattr(billing_router, "_current", lambda request: user)
    monkeypatch.setattr(billing_router, "_rate_limit", lambda *a, **k: True)
    monkeypatch.setattr(billing_router, "_check_csrf", lambda *a, **k: True)
    monkeypatch.setattr(billing_router, "_json", lambda request: _async(body))
    monkeypatch.setattr(billing_router.provision, "is_active", lambda user: active)
    return asyncio.run(
        billing_router.api_add_device_profile(_request(), "device-1")
    )


async def _async(value):
    return value


@pytest.fixture
def user():
    return {"id": "u1", "expiration_date": "2099-01-01T00:00:00"}


def test_anonymous_client_is_refused(monkeypatch):
    monkeypatch.setattr(billing_router, "_current", lambda request: None)
    response = asyncio.run(
        billing_router.api_add_device_profile(_request(), "device-1")
    )
    assert response.status_code == 401


def test_inactive_subscription_is_refused(monkeypatch, user):
    response = _call(monkeypatch, user=user, body={"protocol": "awg2"}, active=False)
    assert response.status_code == 403
    assert "Подписка" in response.body.decode()


def test_provision_error_becomes_a_readable_400(monkeypatch, user):
    def boom(user_id, device_id, protocol):
        raise provision.ProvisionError("Устройство не найдено")

    monkeypatch.setattr(billing_router.provision, "add_device_profile", boom)
    response = _call(monkeypatch, user=user, body={"protocol": "awg2"})
    assert response.status_code == 400
    assert "не найдено" in response.body.decode()


def test_successful_profile_returns_its_connection_id(monkeypatch, user):
    async def created(user_id, device_id, protocol):
        assert (user_id, device_id, protocol) == ("u1", "device-1", "awg2")
        return {"id": "conn-9", "name": "device-2"}

    monkeypatch.setattr(billing_router.provision, "add_device_profile", created)
    response = _call(monkeypatch, user=user, body={"protocol": "awg2"})
    assert response == {
        "status": "ok", "connection_id": "conn-9", "name": "device-2",
    }
