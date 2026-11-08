from __future__ import annotations

import os
import secrets
import time
from dataclasses import dataclass
from typing import Optional

from billing import crypto


def _env(name: str, default: str = "") -> str:
    value = os.environ.get(name)
    return value if value not in (None, "") else default


def _int_env(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except (TypeError, ValueError):
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    raw = _env(name, "1" if default else "0").strip().lower()
    return raw in ("1", "true", "yes", "on")


@dataclass(frozen=True)
class BillingSettings:
    enabled: bool
    yookassa_shop_id: str
    yookassa_secret_key: str
    currency: str
    telegram_bot_token: str
    telegram_bot_username: str
    public_base: str
    server_id: Optional[int]
    protocol: str
    trial_enabled: bool
    trial_days: int
    trial_device_limit: int
    trial_ip_limit: int
    verify_email: bool
    code_ttl_minutes: int
    code_resend_seconds: int
    mock: bool
    db_path: str
    receipt_enabled: bool
    vat_code: int
    provision_devices: bool

    @property
    def yookassa_ready(self) -> bool:
        return bool(self.yookassa_shop_id and self.yookassa_secret_key)

    @property
    def telegram_ready(self) -> bool:
        return bool(self.telegram_bot_token and self.telegram_bot_username)

    def as_dict(self) -> dict:
        """Non-secret view, used by the admin UI and /api/billing/status."""
        return {
            "enabled": self.enabled,
            "currency": self.currency,
            "public_base": self.public_base,
            "server_id": self.server_id,
            "protocol": self.protocol,
            "trial_enabled": self.trial_enabled,
            "trial_days": self.trial_days,
            "trial_device_limit": self.trial_device_limit,
            "trial_ip_limit": self.trial_ip_limit,
            "verify_email": self.verify_email,
            "code_ttl_minutes": self.code_ttl_minutes,
            "code_resend_seconds": self.code_resend_seconds,
            "mock": self.mock,
            "receipt_enabled": self.receipt_enabled,
            "vat_code": self.vat_code,
            "provision_devices": self.provision_devices,
            "yookassa_ready": self.yookassa_ready,
            "telegram_ready": self.telegram_ready,
        }


def _data_dir() -> str:
    import app as panel  # late import: app.py is fully defined by the time we run

    return os.path.dirname(panel.DATA_FILE) or "."


def db_path() -> str:
    return _env("BILLING_DB_PATH", os.path.join(_data_dir(), "billing.db"))


# Settings that may be overridden at runtime from the admin UI. Anything else
# stays read-only in .env (db path, feature flag, base URL fallbacks).
_OVERRIDES = {
    "yookassa_shop_id": ("str", False),
    "yookassa_secret_key": ("str", True),
    "telegram_bot_token": ("str", True),
    "telegram_bot_username": ("str", False),
    "currency": ("str", False),
    "trial_enabled": ("bool", False),
    "trial_days": ("int", False),
    "trial_device_limit": ("int", False),
    "trial_ip_limit": ("int", False),
    "verify_email": ("bool", False),
    "code_ttl_minutes": ("int", False),
    "code_resend_seconds": ("int", False),
    "mock": ("bool", False),
    "receipt_enabled": ("bool", False),
    "vat_code": ("int", False),
    "provision_devices": ("bool", False),
    "server_id": ("opt_int", False),
    "protocol": ("str", False),
    "public_base": ("str", False),
}

_CACHE_VALUE: Optional[BillingSettings] = None
_CACHE_AT = 0.0
_CACHE_TTL = 3.0


def invalidate_cache() -> None:
    global _CACHE_VALUE, _CACHE_AT
    _CACHE_VALUE = None
    _CACHE_AT = 0.0


def _coerce(kind: str, raw: str):
    if kind == "str":
        return str(raw or "").strip()
    if kind == "int":
        try:
            return int(str(raw).strip())
        except (TypeError, ValueError):
            return None
    if kind == "opt_int":
        text = str(raw or "").strip()
        if text == "":
            return None
        try:
            return int(text)
        except (TypeError, ValueError):
            return None
    if kind == "bool":
        return str(raw or "").strip().lower() in ("1", "true", "yes", "on")
    return raw


def _db_overrides(path: str) -> dict:
    from billing import store  # local import: store imports nothing from here

    try:
        rows = store.read_settings(path)
    except Exception:  # noqa: BLE001 - a broken DB must not break the panel
        return {}
    values = {}
    for key, (raw, is_secret) in rows.items():
        if key not in _OVERRIDES:
            continue
        kind, secret = _OVERRIDES[key]
        text = crypto.decrypt(raw) if (is_secret or secret) else raw
        coerced = _coerce(kind, text)
        if coerced is None and kind in ("int", "opt_int"):
            continue
        values[key] = coerced
    return values


def _base() -> BillingSettings:
    path = db_path()
    server_id_raw = _env("BILLING_SERVER_ID", "").strip()
    try:
        server_id = int(server_id_raw) if server_id_raw != "" else None
    except ValueError:
        server_id = None
    return BillingSettings(
        enabled=_bool_env("BILLING_ENABLED", True),
        yookassa_shop_id=_env("YOOKASSA_SHOP_ID"),
        yookassa_secret_key=_env("YOOKASSA_SECRET_KEY"),
        currency=_env("BILLING_CURRENCY", "RUB").upper(),
        telegram_bot_token=_env("TELEGRAM_BOT_TOKEN"),
        telegram_bot_username=_env("TELEGRAM_BOT_USERNAME").lstrip("@"),
        public_base=_env("BILLING_PUBLIC_BASE", "https://lk.ttvpn.ru").rstrip("/"),
        server_id=server_id,
        protocol=_env("BILLING_PROTOCOL", ""),
        trial_enabled=_bool_env("BILLING_TRIAL_ENABLED", False),
        trial_days=_int_env("BILLING_TRIAL_DAYS", 0),
        trial_device_limit=_int_env("BILLING_TRIAL_DEVICE_LIMIT", 5),
        trial_ip_limit=_int_env("BILLING_TRIAL_IP_LIMIT", 2),
        verify_email=_bool_env("BILLING_VERIFY_EMAIL", True),
        code_ttl_minutes=_int_env("BILLING_CODE_TTL_MINUTES", 10),
        code_resend_seconds=_int_env("BILLING_CODE_RESEND_SECONDS", 60),
        mock=_bool_env("BILLING_MOCK", False),
        db_path=path,
        receipt_enabled=_bool_env("BILLING_RECEIPT_ENABLED", False),
        vat_code=_int_env("BILLING_VAT_CODE", 1),
        provision_devices=_bool_env("BILLING_PROVISION_DEVICES", True),
    )


def get_settings() -> BillingSettings:
    """Env defaults, overridden by values saved in the admin UI.

    Cached for a few seconds: the settings are read on every checkout and page
    render, and a SQLite read per call would be wasteful. Saves call
    invalidate_cache() so the UI change is visible immediately.
    """
    global _CACHE_VALUE, _CACHE_AT
    now = time.monotonic()
    if _CACHE_VALUE is not None and (now - _CACHE_AT) < _CACHE_TTL:
        return _CACHE_VALUE

    base = _base()
    overrides = _db_overrides(base.db_path)
    if overrides:
        base = BillingSettings(**{**base.__dict__, **overrides})
    _CACHE_VALUE = base
    _CACHE_AT = now
    return base


def save_overrides(values: dict, reset: list[str] | None = None) -> None:
    """Persist admin-provided values. Empty strings are ignored unless reset."""
    from billing import store

    path = db_path()
    rows: dict[str, tuple[str, bool]] = {}
    for key, raw in values.items():
        if key not in _OVERRIDES:
            continue
        kind, secret = _OVERRIDES[key]
        if isinstance(raw, bool):
            text = "1" if raw else "0"
        else:
            text = str(raw).strip()
        if text == "":
            continue
        rows[key] = (crypto.encrypt(text) if secret else text, secret)
    store.write_settings(path, rows)
    clear = [key for key in (reset or []) if key in _OVERRIDES]
    store.delete_settings(path, clear)
    invalidate_cache()


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)
