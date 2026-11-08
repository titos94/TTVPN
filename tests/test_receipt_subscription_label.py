from types import SimpleNamespace

from billing.router import _build_receipt


def test_receipt_item_uses_generic_subscription_name():
    settings = SimpleNamespace(receipt_enabled=True, vat_code=1)
    receipt = _build_receipt(settings, {'name': 'VPN 1 месяц'},
                             {'email': 'customer@example.org'}, 19900, 'RUB')
    item = receipt['items'][0]
    assert item['description'] == 'Подписка'
    assert item['amount'] == {'value': '199.00', 'currency': 'RUB'}
    assert item['vat_code'] == 1
