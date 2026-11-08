from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Optional

import httpx

API_URL = "https://api.yookassa.ru/v3/payments"


class YooKassaError(RuntimeError):
    pass


@dataclass(frozen=True)
class CreatedPayment:
    provider_payment_id: str
    confirmation_url: str
    status: str
    raw: dict


@dataclass(frozen=True)
class VerifiedPayment:
    provider_payment_id: str
    status: str
    amount_kopecks: int
    currency: str
    internal_payment_id: str
    raw: dict


def _auth(shop_id: str, secret_key: str) -> tuple[str, str]:
    return shop_id, secret_key


def create_payment(*, shop_id: str, secret_key: str, amount_kopecks: int, currency: str,
                   description: str, idempotency_key: str, return_url: str,
                   internal_payment_id: str, receipt: Optional[dict] = None) -> CreatedPayment:
    if not shop_id or not secret_key:
        raise YooKassaError("YooKassa credentials are not configured")
    payload: dict = {
        "amount": {
            "value": f"{Decimal(amount_kopecks) / Decimal(100):.2f}",
            "currency": currency,
        },
        "capture": True,
        "confirmation": {"type": "redirect", "return_url": return_url},
        "description": description[:128],
        "metadata": {"internal_payment_id": internal_payment_id},
    }
    if receipt:
        payload["receipt"] = receipt
    try:
        response = httpx.post(
            API_URL,
            json=payload,
            auth=_auth(shop_id, secret_key),
            headers={"Idempotence-Key": idempotency_key},
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        return CreatedPayment(
            provider_payment_id=data["id"],
            confirmation_url=data["confirmation"]["confirmation_url"],
            status=data["status"],
            raw=data,
        )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise YooKassaError(f"YooKassa did not create the payment: {exc}") from exc


def verify_payment(*, shop_id: str, secret_key: str,
                   provider_payment_id: str) -> VerifiedPayment:
    if not shop_id or not secret_key:
        raise YooKassaError("YooKassa credentials are not configured")
    try:
        response = httpx.get(
            f"{API_URL}/{provider_payment_id}",
            auth=_auth(shop_id, secret_key),
            timeout=20,
        )
        response.raise_for_status()
        data = response.json()
        value = Decimal(data["amount"]["value"])
        return VerifiedPayment(
            provider_payment_id=data["id"],
            status=data["status"],
            amount_kopecks=int(value * 100),
            currency=data["amount"]["currency"],
            internal_payment_id=str(data.get("metadata", {}).get("internal_payment_id", "")),
            raw=data,
        )
    except (httpx.HTTPError, KeyError, ValueError) as exc:
        raise YooKassaError(f"YooKassa payment could not be verified: {exc}") from exc


def check_credentials(*, shop_id: str, secret_key: str) -> dict:
    """Ask YooKassa about this shop so the admin UI can validate keys."""
    if not shop_id or not secret_key:
        raise YooKassaError("Не заданы shopId и секретный ключ")
    try:
        response = httpx.get(
            "https://api.yookassa.ru/v3/me",
            auth=_auth(shop_id, secret_key),
            timeout=15,
        )
        if response.status_code in (401, 403):
            raise YooKassaError("ЮKassa отклонила ключи (401/403)")
        response.raise_for_status()
        data = response.json()
        return {
            "shop_id": str(data.get("shop_id") or data.get("account_id") or shop_id),
            "status": str(data.get("status") or ""),
            "test": bool(data.get("test", False)),
            "fiscalization_enabled": bool(data.get("fiscalization_enabled", False)),
        }
    except httpx.HTTPError as exc:
        raise YooKassaError(f"ЮKassa недоступна: {exc}") from exc
