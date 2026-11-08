from __future__ import annotations

import base64
import hashlib
import logging
import os
from typing import Optional

logger = logging.getLogger(__name__)

PREFIX = "enc:"


def _fernet():
    """Derive a Fernet key from the panel SECRET_KEY.

    SECRET_KEY already protects every session cookie, so compromising it means
    full admin access anyway; deriving from it avoids a second secret to keep
    in sync between containers.
    """
    secret = os.environ.get("SECRET_KEY", "")
    if not secret:
        return None
    try:
        from cryptography.fernet import Fernet
    except ImportError:  # pragma: no cover - cryptography ships with paramiko
        logger.warning("billing: cryptography is unavailable, secrets stored as plain text")
        return None
    digest = hashlib.sha256(secret.encode()).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt(value: str) -> str:
    if not value:
        return ""
    f = _fernet()
    if f is None:
        return value
    return PREFIX + f.encrypt(value.encode()).decode()


def decrypt(value: str) -> str:
    if not value:
        return ""
    if not value.startswith(PREFIX):
        return value
    f = _fernet()
    if f is None:
        return ""
    try:
        return f.decrypt(value[len(PREFIX):].encode()).decode()
    except Exception:  # noqa: BLE001 - wrong/rotated SECRET_KEY
        logger.warning("billing: could not decrypt a stored secret (SECRET_KEY changed?)")
        return ""


def mask(value: str) -> str:
    if not value:
        return ""
    if len(value) <= 8:
        return "•" * len(value)
    return "•" * 6 + value[-4:]


def looks_masked(value: Optional[str]) -> bool:
    return bool(value) and set(str(value)) <= {"•"}
