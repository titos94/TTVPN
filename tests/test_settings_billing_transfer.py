"""Настройки ЮKassa и «Прочее» находятся в общих настройках, не в биллинге."""
from types import SimpleNamespace
from unittest import mock

import app as panel
from billing.settings import BillingSettings


def _billing_settings():
    return BillingSettings(
        enabled=True,
        yookassa_shop_id="123456",
        yookassa_secret_key="live-secret-value",
        currency="RUB",
        telegram_bot_token="",
        telegram_bot_username="",
        public_base="https://lk.example.test",
        server_id=None,
        protocol="",
        trial_enabled=False,
        trial_days=0,
        trial_device_limit=1,
        trial_ip_limit=0,
        verify_email=True,
        code_ttl_minutes=10,
        code_resend_seconds=60,
        mock=False,
        db_path="",
        receipt_enabled=False,
        vat_code=1,
        provision_devices=True,
    )


def test_settings_contains_payment_and_extra_forms():
    text = open("templates/settings.html", encoding="utf-8").read()
    for marker in ("id=\"kassaShop\"", "id=\"kassaSecret\"", "id=\"extraVat\"",
                   "id=\"extraBase\"", "function saveKassa", "function checkKassa",
                   "function saveExtra", "/api/billing/admin/yookassa/check"):
        assert marker in text


def test_billing_admin_has_no_payment_configuration_forms():
    text = open("templates/billing_admin.html", encoding="utf-8").read()
    for marker in ("id=\"kassaShop\"", "id=\"kassaSecret\"", "id=\"extraVat\"",
                   "href=\"#kassa\"", "href=\"#extra\"", "function saveKassa",
                   "function saveExtra"):
        assert marker not in text


def test_settings_page_passes_billing_context_without_secret_value():
    request = SimpleNamespace(session={})
    rendered = {}
    with mock.patch.object(panel, "_check_admin", return_value={"role": "admin"}), \
         mock.patch.object(panel, "load_data", return_value={"settings": {}, "servers": []}), \
         mock.patch("billing.settings.get_settings", return_value=_billing_settings()), \
         mock.patch("billing.router._csrf", return_value="csrf-test-value"), \
         mock.patch.object(panel, "tpl", side_effect=lambda request, template, **kwargs: rendered.update(kwargs) or kwargs):
        panel.settings_page(request)
    assert rendered["billing_csrf"] == "csrf-test-value"
    assert rendered["billing_mail_settings"]["yookassa_shop_id"] == "123456"
    assert "live-secret-value" not in str(rendered)
