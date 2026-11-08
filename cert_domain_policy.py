"""Чистая политика доменов сертификатов; сетевые запросы здесь не выполняются."""

import hmac
import ipaddress

from domain_validation import normalize_certificate_domain


_ROLES = frozenset(('landing', 'client', 'admin'))
_SPECIAL_ZONES = frozenset((
    'localhost', 'local', 'internal', 'invalid', 'test', 'example', 'onion',
    'home', 'home.arpa', 'arpa', 'alt', 'localdomain', 'lan', 'corp',
))
_MAX_RECORDS = 64
_MIN_TOKEN_LENGTH = 16
_MAX_TOKEN_LENGTH = 256


def normalize_hostname(value):
    """Нормализовать публичное доменное имя без специальных зон."""
    name = normalize_certificate_domain(value)
    if any(name == zone or name.endswith('.' + zone) for zone in _SPECIAL_ZONES):
        raise ValueError('Недопустимая доменная зона')
    return name


def validate_role(role):
    """Проверить назначение домена без неявных преобразований."""
    if not isinstance(role, str) or role not in _ROLES:
        raise ValueError('Недопустимая роль домена')
    return role


def _records(values):
    if not isinstance(values, (list, tuple)) or not 1 <= len(values) <= _MAX_RECORDS:
        raise ValueError('Недопустимый набор записей')
    return values


def _address(value):
    if not isinstance(value, str) or len(value) > 45 or '%' in value:
        raise ValueError('Недопустимый IP-адрес')
    try:
        address = ipaddress.ip_address(value)
    except ValueError:
        raise ValueError('Недопустимый IP-адрес') from None
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        raise ValueError('Недопустимый IP-адрес')
    if not address.is_global:
        raise ValueError('Недопустимый IP-адрес')
    return address


def validate_public_addresses(values):
    """Все адреса должны быть публичными; вернуть нормализованные строки."""
    return [str(_address(value)) for value in _records(values)]


def validate_dns_addresses(a_records, aaaa_records, allowed_addresses):
    """Все A и AAAA должны быть глобальными и входить в разрешённый набор."""
    if not isinstance(a_records, (list, tuple)) or not isinstance(aaaa_records, (list, tuple)):
        raise ValueError('Недопустимый набор записей')
    if len(a_records) + len(aaaa_records) == 0 or len(a_records) + len(aaaa_records) > _MAX_RECORDS:
        raise ValueError('Недопустимый набор записей')
    allowed = {_address(value) for value in _records(allowed_addresses)}
    for records, version in ((a_records, 4), (aaaa_records, 6)):
        for value in records:
            address = _address(value)
            if address.version != version or address not in allowed:
                raise ValueError('DNS-записи не соответствуют разрешённым адресам')
    return True


def verify_txt_token(records, expected_token):
    """Сравнить заранее подготовленный токен с ограниченным набором TXT."""
    def token_bytes(value):
        if not isinstance(value, str) or not _MIN_TOKEN_LENGTH <= len(value) <= _MAX_TOKEN_LENGTH:
            raise ValueError('Недопустимый TXT-токен')
        try:
            return value.encode('ascii')
        except UnicodeEncodeError:
            raise ValueError('Недопустимый TXT-токен') from None

    expected = token_bytes(expected_token)
    answers = [token_bytes(value) for value in _records(records)]
    matched = False
    for answer in answers:
        matched |= hmac.compare_digest(answer, expected)
    return matched
