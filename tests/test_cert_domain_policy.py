import pytest

from cert_domain_policy import normalize_hostname, validate_dns_addresses, validate_role, verify_txt_token


def test_idna_and_roles():
    assert normalize_hostname(' ПРИМЕР.РФ. ') == 'xn--e1afmkfd.xn--p1ai'
    for role in ('landing', 'client', 'admin'):
        assert validate_role(role) == role
    for role in ('Admin', '', None, [], 1):
        with pytest.raises(ValueError):
            validate_role(role)


@pytest.mark.parametrize('name', [
    'foo.localhost', 'foo.local', 'foo.internal', 'foo.invalid', 'foo.test',
    'foo.example', 'foo.onion', 'foo.home.arpa', 'foo.lan',
    'https://domain.com', 'domain.com:443', '*.domain.com', '127.0.0.1', None,
])
def test_bad_hostname(name):
    with pytest.raises(ValueError):
        normalize_hostname(name)


def test_dns_valid_and_rebinding():
    allowed = ['8.8.8.8', '2606:4700:4700::1111']
    assert validate_dns_addresses(['8.8.8.8'], ['2606:4700:4700::1111'], allowed)
    for a, aaaa in [([], []), (['8.8.8.8', '1.1.1.1'], []),
                     (['8.8.8.8', '127.0.0.1'], []),
                     (['8.8.8.8'], ['::1']),
                     (['8.8.8.8'], ['::ffff:8.8.8.8']),
                     (['2606:4700:4700::1111'], []),
                     ([], ['8.8.8.8'])]:
        with pytest.raises(ValueError):
            validate_dns_addresses(a, aaaa, allowed)


def test_dns_bad_types_and_limits():
    for a, aaaa, allowed in [(None, [], ['8.8.8.8']), ([], [], []),
                              (['8.8.8.8'] * 65, [], ['8.8.8.8']),
                              (['8.8.8.8'], [], ['127.0.0.1']),
                              (['8.8.8.8'], [], ['::ffff:8.8.8.8']),
                              (['8.8.8.8'], [], '8.8.8.8'),
                              ([123], [], ['8.8.8.8'])]:
        with pytest.raises(ValueError):
            validate_dns_addresses(a, aaaa, allowed)


def test_txt_verification_and_limits():
    secret = 'secret-token-1234567890'
    assert verify_txt_token(['other-token-1234567890', secret], secret)
    assert not verify_txt_token(['other-token-1234567890'], secret)
    for records, token in [([], secret), ([secret] * 65, secret),
                           ([secret], None), ([None], secret),
                           (['short'], secret), ([secret], 'x' * 257),
                           ([secret], 'é' * 20), (secret, secret)]:
        with pytest.raises(ValueError) as error:
            verify_txt_token(records, token)
        assert secret not in str(error.value)
