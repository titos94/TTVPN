"""Read-only diagnostic building blocks for the TTVPN assistant.

Never send credentials or configuration files to an external model. This
module deliberately has no remote execution or automatic repair capability.
"""

from __future__ import annotations

import os
import re
import time
from datetime import datetime, timezone

import httpx

from assistant_settings import DEFAULT_MODEL, PROVIDER_URLS, validate_model


_SECRET = re.compile(
    r'(?i)(?:-----BEGIN [^-]*(?:PRIVATE KEY|OPENSSH)|(?:vpn|amnezia|vless|trojan|ss)://|'
    r'\b(?:privatekey|presharedkey|authorization|api[_ -]?key|token|password|пароль|токен|ключ)\s*[:=]\s*\S+|'
    r'\[(?:interface|peer)\]|'
    r'\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b|'
    r'(?<!\w)\+?\d[\d ()-]{8,}\d(?!\w)|'
    r'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/]))'
)


def validate_message(message: object) -> str:
    """Bound the input and reject common credentials rather than transmit them."""
    if not isinstance(message, str):
        raise ValueError('Введите текст сообщения')
    text = message.strip()
    if not text or len(text) > 1200:
        raise ValueError('Сообщение должно содержать от 1 до 1200 символов')
    if _SECRET.search(text):
        raise ValueError('Не отправляйте пароли, токены или VPN-ключи')
    return text


_resolver = None


def configure_resolver(resolver):
    """Панель передаёт функцию, читающую настройки ИИ из data.json.

    Модуль намеренно не импортирует app.py: иначе получился бы циклический
    импорт, а помощник должен оставаться тестируемым отдельно.
    """
    global _resolver
    _resolver = resolver


def _env_key():
    return os.environ.get('TTVPN_ASSISTANT_API_KEY', '').strip()


def _env_enabled():
    return (os.environ.get('TTVPN_ASSISTANT_ENABLED', '').strip().lower()
            in ('1', 'true', 'yes', 'on'))


def _settings():
    """Активные настройки: env имеет приоритет над сохранёнными в панели."""
    env_key = _env_key()
    if env_key:
        return {
            'enabled': _env_enabled(),
            'api_key': env_key,
            'model': (os.environ.get('TTVPN_ASSISTANT_MODEL', '').strip()
                      or DEFAULT_MODEL),
            'source': 'env',
            'provider': os.environ.get('TTVPN_ASSISTANT_PROVIDER', 'openai'),
        }
    if _resolver is None:
        return {'enabled': False, 'api_key': '', 'model': DEFAULT_MODEL,
                'source': 'none'}
    try:
        resolved = _resolver() or {}
    except Exception:  # noqa: BLE001 - недоступные настройки не должны ломать UI
        return {'enabled': False, 'api_key': '', 'model': DEFAULT_MODEL,
                'source': 'none'}
    return {
        'enabled': bool(resolved.get('enabled')) and bool(resolved.get('api_key')),
        'api_key': resolved.get('api_key') or '',
        'model': resolved.get('model') or DEFAULT_MODEL,
        'source': resolved.get('source') or 'settings',
        'provider': resolved.get('provider', 'openai'),
    }


def assistant_enabled() -> bool:
    """Require deliberate opt-in as well as a configured provider key."""
    return bool(_settings()['enabled'])


async def answer_customer(message: str) -> str:
    """Request a text-only answer; no account or infrastructure context is sent."""
    message = validate_message(message)
    settings = _settings()
    key = settings['api_key']
    if not settings['enabled'] or not key:
        raise RuntimeError('Помощник временно недоступен')
    model = settings['model']
    try:
        validate_model(model)
        base_url = PROVIDER_URLS[settings.get('provider', 'openai')]
    except (ValueError, KeyError, TypeError):
        raise RuntimeError('Помощник временно недоступен')
    payload = {
        'model': model,
        'messages': [
            {'role': 'system', 'content': (
                'Ты помощник поддержки TTVPN. Отвечай по-русски кратко и честно. '
                'Не утверждай, что видишь аккаунт, платежи или состояние сервера. '
                'Не проси пароли, токены и VPN-ключи. Если не знаешь ответ, '
                'предложи обратиться к оператору. Не обещай цены или гарантии.'
            )},
            {'role': 'user', 'content': message},
        ],
        'max_tokens': 350,
    }
    try:
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            response = await client.post(
                base_url + '/chat/completions',
                headers={'Authorization': f'Bearer {key}'}, json=payload,
            )
            response.raise_for_status()
            text = response.json()['choices'][0]['message']['content']
            if not isinstance(text, str) or not text.strip():
                raise ValueError('empty answer')
            return text.strip()[:4000]
    except (httpx.HTTPError, ValueError, KeyError, IndexError, TypeError):
        raise RuntimeError('Помощник временно недоступен') from None


def health_report(*, telegram_enabled: bool, telegram_running: bool) -> dict:
    """Read resource metrics only; do not open application data or secret files."""
    try:
        fs = os.statvfs('/')
        disk_free_percent = round(100 * fs.f_bavail / fs.f_blocks, 1) if fs.f_blocks else None
    except OSError:
        disk_free_percent = None
    try:
        load_average = round(os.getloadavg()[0], 2)
    except (AttributeError, OSError):
        load_average = None
    return {
        'disk_free_percent': disk_free_percent,
        'load_average_1m': load_average,
        'telegram': ('running' if telegram_running else
                     'stopped' if telegram_enabled else 'disabled'),
    }


class ServiceMonitor:
    """Bounded, single-process monitor; its sole repair starts the configured bot."""

    def __init__(self, *, cooldown_seconds: int = 300):
        self.cooldown_seconds = cooldown_seconds
        self.last_restart_attempt = None
        self.last_report = {'status': 'starting', 'issues': [], 'repair': 'none'}

    def check(self, *, telegram_enabled: bool,
              telegram_running: bool, restart_bot, now: float | None = None,
              health: dict | None = None) -> dict:
        now = time.monotonic() if now is None else now
        if health is None:
            health = health_report(telegram_enabled=telegram_enabled,
                                   telegram_running=telegram_running)
        issues = []
        free = health.get('disk_free_percent')
        if free is None:
            issues.append('disk_status_unknown')
        elif free < 10:
            issues.append('disk_space_low')
        if health['telegram'] == 'stopped':
            issues.append('telegram_stopped')
        repair = 'none'
        if health['telegram'] == 'stopped':
            if self.last_restart_attempt is not None and now - self.last_restart_attempt < self.cooldown_seconds:
                repair = 'cooldown'
            else:
                self.last_restart_attempt = now
                try:
                    launched = restart_bot()
                    repair = 'telegram_restart_requested' if launched else 'telegram_restart_skipped'
                except Exception:
                    repair = 'telegram_restart_failed'
        self.last_report = {
            'status': 'attention' if issues else 'ok',
            'issues': issues,
            'repair': repair,
            'health': health,
            'checked_at': datetime.now(timezone.utc).isoformat(),
        }
        return self.last_report.copy()
