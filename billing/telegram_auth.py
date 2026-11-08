from __future__ import annotations

import hashlib
import hmac
import time
from typing import Optional

MAX_AUTH_AGE_SECONDS = 86400


class TelegramAuthError(ValueError):
    pass


def verify_login_widget(data: dict, bot_token: str,
                        max_age: int = MAX_AUTH_AGE_SECONDS) -> dict:
    """Verify a Telegram Login Widget payload.

    Telegram signs the payload with HMAC-SHA256 where the key is
    SHA256(bot_token). No server -> api.telegram.org call is required, so this
    works on hosts where the Telegram API is blocked.
    """
    if not bot_token:
        raise TelegramAuthError("Telegram bot token is not configured")
    received_hash = str(data.get("hash") or "")
    if not received_hash or len(received_hash) != 64:
        raise TelegramAuthError("Missing Telegram signature")

    pairs = sorted((k, v) for k, v in data.items() if k != "hash")
    data_check_string = "\n".join(f"{k}={v}" for k, v in pairs)
    secret_key = hashlib.sha256(bot_token.encode()).digest()
    expected = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(expected, received_hash):
        raise TelegramAuthError("Invalid Telegram signature")

    try:
        auth_date = int(data.get("auth_date") or 0)
    except (TypeError, ValueError):
        raise TelegramAuthError("Invalid auth_date") from None
    if auth_date <= 0 or abs(time.time() - auth_date) > max_age:
        raise TelegramAuthError("Telegram login is stale")

    telegram_id = str(data.get("id") or "").strip()
    if not telegram_id.isdigit():
        raise TelegramAuthError("Invalid Telegram id")

    return {
        "telegram_id": telegram_id,
        "username": str(data.get("username") or ""),
        "first_name": str(data.get("first_name") or ""),
        "last_name": str(data.get("last_name") or ""),
        "photo_url": str(data.get("photo_url") or ""),
    }
