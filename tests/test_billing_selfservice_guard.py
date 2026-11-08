"""Paid plans own the device count: self-service cannot mint extra peers.

The guard lives in ``connection_service`` so it applies to every channel
(cabinet form, Telegram bot, API), not only to the client host middleware.
"""

import asyncio

import pytest

from connection_service import ConnectionService, SelfServiceError


class _FakePanel:
    """Minimal stand-in for the panel data the service validates against."""

    def __init__(self, user):
        self.user = user

    def load_data(self):
        return {
            "users": [self.user],
            "servers": [{
                "uid": "server-uid",
                "host": "198.51.100.7",
                "self_service_enabled": True,
                "protocols": {"awg": {"installed": True, "port": "55424"}},
            }],
            "user_connections": [],
            "settings": {"self_service": {
                "enabled": True,
                "web_enabled": True,
                "telegram_enabled": True,
                "max_connections_per_user": 5,
                "allowed_protocols": ["awg"],
            }},
        }

    def save_data(self, data):
        raise AssertionError("no write should happen")


def _service(panel):
    return ConnectionService(
        load_data=panel.load_data,
        save_data=panel.save_data,
        data_lock=asyncio.Lock(),
        get_ssh=lambda server: None,
        get_protocol_manager=lambda ssh, protocol: None,
        manager_call=lambda *args, **kwargs: {},
        generate_vpn_link=lambda *args, **kwargs: "vpn://x",
    )


def _user(**extra):
    return {
        "id": "user-1",
        "description": "self-registered",
        "enabled": True,
        "expiration_date": "2099-01-01T00:00:00",
        **extra,
    }


@pytest.mark.parametrize("source", ["web", "telegram"])
def test_billing_client_cannot_add_a_peer(monkeypatch, source):
    import connection_service

    monkeypatch.setattr(connection_service, "_plan_owns_devices", lambda user: True)
    panel = _FakePanel(_user())

    with pytest.raises(SelfServiceError) as error:
        asyncio.run(
            _service(panel).create_user_connection("user-1", 0, "awg", "phone", source)
        )

    assert error.value.status_code == 403
    assert error.value.forbidden is True


def test_operator_created_account_keeps_self_service(monkeypatch):
    import connection_service

    panel = _FakePanel(_user(description="created by admin"))
    # The real helper checks the marker first, so an operator account is never
    # blocked even if billing would consider the id managed.
    assert connection_service._plan_owns_devices(panel.user) is False


def test_billing_helper_only_trusts_the_self_registered_marker():
    from connection_service import _plan_owns_devices

    assert _plan_owns_devices({"id": "x", "description": ""}) is False
    assert _plan_owns_devices({}) is False
