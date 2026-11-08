"""Хранение ключа API помощника: шифрование и безопасный публичный вид.

Открытый ключ никогда не покидает сервер: в `data.json` лежит только
шифротекст (Fernet/AES-128-CBC + HMAC), в браузер уходят лишь `key_last4` и
признак наличия ключа. Ключ шифрования выводится из `SECRET_KEY` панели,
поэтому смена `SECRET_KEY` делает старый шифротекст нечитаемым — это
ожидаемое поведение, а не ошибка.
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import re
from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.kdf.hkdf import HKDF


DEFAULT_MODEL = 'gpt-4.1-mini'
PROVIDER = 'openai'
MIN_KEY_LENGTH = 20
MAX_KEY_LENGTH = 512

_MODEL_PATTERN = re.compile(r'^[A-Za-z0-9._:-]+(?:/[A-Za-z0-9._:-]+)?$')
PROVIDER_URLS = {'openai': 'https://api.openai.com/v1', 'routerai': 'https://routerai.ru/api/v1',
                 'groq': 'https://api.groq.com/openai/v1'}
_PRINTABLE_ASCII = re.compile(r'^[\x21-\x7e]+$')

# Неподвижная соль/назначение: ключ должен выводиться одинаково при каждом
# старте процесса, иначе сохранённый шифротекст перестанет читаться.
_KDF_SALT = b'ttvpn-assistant-key-v1'
_KDF_INFO = b'api-key-encryption'
#: Отдельная соль для второй версии: старые записи остаются читаемыми.
_KDF_SALT_V2 = b'ttvpn-assistant-key-v2-pbkdf2'
_PBKDF2_ITERATIONS = 600_000
#: Префикс формата шифротекста новой версии.
_FORMAT_V2 = 'v2:'


class AssistantKeyError(ValueError):
    """Ключ нельзя сохранить или прочитать."""


def _derive_v1(secret: str) -> bytes:
    """Первая версия: HKDF без растяжения (только чтение старых записей)."""
    return HKDF(algorithm=hashes.SHA256(), length=32, salt=_KDF_SALT,
                info=_KDF_INFO).derive(secret.encode('utf-8'))


def _derive_v2(secret: str) -> bytes:
    """PBKDF2 с растяжением: словарная атака по копии data.json дороже.

    Итерации выбраны так, чтобы расшифровка занимала десятые доли секунды:
    ключ читается при показе настроек и при запросе помощника, а результат
    кэшируется на процесс.
    """
    return hashlib.pbkdf2_hmac('sha256', secret.encode('utf-8'), _KDF_SALT_V2,
                               _PBKDF2_ITERATIONS, dklen=32)


@lru_cache(maxsize=8)
def _fernet(secret: str, version: int = 2) -> Fernet:
    if not isinstance(secret, str) or not secret.strip():
        raise AssistantKeyError('Не настроен SECRET_KEY панели')
    derived = _derive_v1(secret) if version == 1 else _derive_v2(secret)
    return Fernet(base64.urlsafe_b64encode(derived))


def validate_api_key(value: object) -> str:
    """Проверить форму ключа, не обращаясь к провайдеру."""
    if not isinstance(value, str):
        raise AssistantKeyError('Ключ должен быть строкой')
    key = value.strip()
    if not MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH:
        raise AssistantKeyError(
            f'Ключ должен содержать от {MIN_KEY_LENGTH} до {MAX_KEY_LENGTH} символов')
    if not _PRINTABLE_ASCII.match(key):
        raise AssistantKeyError('Ключ содержит недопустимые символы')
    return key


def validate_model(value: object) -> str:
    if value is None or (isinstance(value, str) and not value.strip()):
        return DEFAULT_MODEL
    if (not isinstance(value, str) or len(value.strip()) > 128
            or not _MODEL_PATTERN.fullmatch(value.strip())
            or any(len(part) > 64 for part in value.strip().split('/'))):
        raise AssistantKeyError('Недопустимое имя модели')
    return value.strip()


def encrypt_api_key(api_key: str, secret: str) -> str:
    """Вернуть base64-шифротекст ключа с префиксом формата."""
    key = validate_api_key(api_key)
    token = _fernet(secret, 2).encrypt(key.encode('utf-8')).decode('ascii')
    return _FORMAT_V2 + token


def decrypt_api_key(token: object, secret: str) -> str:
    """Расшифровать сохранённый ключ; при порче вернуть пустую строку."""
    if not isinstance(token, str) or not token:
        return ''
    text = token
    version = 1
    if text.startswith(_FORMAT_V2):
        text = text[len(_FORMAT_V2):]
        version = 2
    try:
        raw = _fernet(secret, version).decrypt(text.encode('ascii'))
    except (InvalidToken, ValueError, binascii.Error):
        return ''
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return ''


def public_view(settings: object) -> dict:
    """Безопасный вид настроек: без шифротекста и без открытого ключа."""
    if not isinstance(settings, dict):
        settings = {}
    last4 = settings.get('key_last4')
    return {
        'enabled': bool(settings.get('enabled')),
        'provider': settings.get('provider') if isinstance(settings.get('provider'), str) and settings.get('provider') in PROVIDER_URLS else PROVIDER,
        'model': settings.get('model') or DEFAULT_MODEL,
        'key_configured': bool(settings.get('api_key_encrypted')),
        'key_last4': last4 if isinstance(last4, str) and len(last4) <= 4 else '',
        'updated_at': settings.get('updated_at') if isinstance(settings.get('updated_at'), int) else None,
        'default_model': DEFAULT_MODEL,
    }
