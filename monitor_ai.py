"""Административный анализ только ограниченных обезличенных показателей."""
import json

import httpx

from assistant_settings import PROVIDER_URLS, validate_model

_STATES = frozenset(('ok', 'degraded', 'down', 'unknown'))
_COMPONENTS = frozenset(('panel', 'proxy', 'telegram', 'vpn_node'))


def safe_snapshot(report):
    if not isinstance(report, dict) or report.get('monitor') != 'isolated':
        raise ValueError('Монитор недоступен')
    result = {'components': {}, 'health': {}}
    for item in report.get('components', []):
        if not isinstance(item, dict):
            continue
        component, state = item.get('component'), item.get('state')
        if not isinstance(component, str) or not isinstance(state, str) or component not in _COMPONENTS or state not in _STATES:
            continue
        entry = {'state': state}
        if component == 'vpn_node':
            for key in ('ok', 'degraded', 'down', 'unknown'):
                value = item.get(key)
                if type(value) is int and 0 <= value <= 10000:
                    entry[key] = value
        result['components'][component] = entry
    health = report.get('health')
    if isinstance(health, dict):
        for key, maximum in (('disk_percent', 100), ('load_average_1m', 10000)):
            value = health.get(key)
            if type(value) in (int, float) and 0 <= value <= maximum:
                result['health'][key] = value
    if not result['components']:
        raise ValueError('Нет достоверных данных монитора')
    return result


async def analyze(report, settings):
    snapshot = safe_snapshot(report)
    if not settings.get('enabled') or not settings.get('api_key'):
        raise RuntimeError('Настройте и включите ИИ-помощника')
    try:
        url = PROVIDER_URLS[settings.get('provider', 'openai')] + '/chat/completions'
        model = validate_model(settings.get('model'))
        payload = {'model': model, 'max_tokens': 400, 'messages': [
            {'role': 'system', 'content': 'Ты помощник администратора TTVPN. Анализируй только предоставленные обезличенные показатели. Отвечай по-русски: состояние, вероятные причины, безопасные шаги проверки. Не утверждай, что выполнил действия. Состояние SSH-порта не доказывает работу VPN. Не запрашивай пароли и ключи.'},
            {'role': 'user', 'content': json.dumps(snapshot, ensure_ascii=False)},
        ]}
        async with httpx.AsyncClient(timeout=20, follow_redirects=False, trust_env=False) as client:
            async with client.stream('POST', url, headers={'Authorization': 'Bearer ' + settings['api_key']}, json=payload) as response:
                response.raise_for_status()
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(data) + len(chunk) > 131072:
                        raise ValueError('response too large')
                    data.extend(chunk)
        text = json.loads(data)['choices'][0]['message']['content']
        if not isinstance(text, str) or not text.strip():
            raise ValueError('empty response')
        return text.strip()[:4000]
    except (KeyError, TypeError, ValueError, IndexError, httpx.HTTPError):
        raise RuntimeError('Не удалось получить анализ ИИ; проверьте настройки провайдера') from None
