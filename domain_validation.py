"""Validation for operator-supplied DNS names; performs no network or TLS work."""

import ipaddress
import re


_LABEL = re.compile(r'^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$')


def normalize_certificate_domain(value: str) -> str:
    """Return a canonical hostname, never a URL, wildcard, or IP address."""
    if not isinstance(value, str):
        raise ValueError('Укажите доменное имя')
    name = value.strip().rstrip('.').lower()
    if not name or any(c in name for c in '/:@*\\?#'):
        raise ValueError('Укажите доменное имя без протокола, порта и маски')
    try:
        name = name.encode('idna').decode('ascii')
    except UnicodeError:
        raise ValueError('Некорректное доменное имя') from None
    if len(name) > 253:
        raise ValueError('Слишком длинное доменное имя')
    try:
        ipaddress.ip_address(name)
    except ValueError:
        pass
    else:
        raise ValueError('Для сертификата укажите доменное имя, а не IP-адрес')
    labels = name.split('.')
    if len(labels) < 2 or any(not _LABEL.fullmatch(label) for label in labels):
        raise ValueError('Некорректное доменное имя')
    if labels[-1].isdigit():
        raise ValueError('Некорректная доменная зона')
    return name
