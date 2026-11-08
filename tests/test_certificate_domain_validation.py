import pytest

from domain_validation import normalize_certificate_domain


@pytest.mark.parametrize(('value', 'expected'), [
    (' Example.COM. ', 'example.com'),
    ('пример.рф', 'xn--e1afmkfd.xn--p1ai'),
    ('vpn.my-domain.example', 'vpn.my-domain.example'),
])
def test_valid_hostnames(value, expected):
    assert normalize_certificate_domain(value) == expected


@pytest.mark.parametrize('value', [
    '', 'localhost', 'https://example.com', 'example.com:443', '*.example.com',
    'example.com/path', '127.0.0.1', '::1', 'bad..example', '-bad.example',
    'bad-.example', 'example.com\nother.example', 'a' * 64 + '.example', None,
])
def test_rejects_unsafe_or_invalid_names(value):
    with pytest.raises(ValueError):
        normalize_certificate_domain(value)
