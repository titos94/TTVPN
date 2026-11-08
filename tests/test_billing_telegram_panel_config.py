import asyncio
from types import SimpleNamespace

from billing import router
from billing import store


def test_enabled_panel_bot_is_used_for_cabinet_login(monkeypatch):
    bot = SimpleNamespace(is_running=lambda: True, running_username=lambda: 'example_bot')
    panel = SimpleNamespace(tg_bot=bot, load_data=lambda: {
        'settings': {'telegram': {'enabled': True, 'token': 'panel-token'}}
    })
    monkeypatch.setattr(router, '_panel', lambda: panel)
    monkeypatch.setattr(router, 'get_settings', lambda: SimpleNamespace(
        telegram_bot_token='old-billing-token', telegram_bot_username='old_bot'))

    assert router._tg_token() == 'panel-token'
    assert router._tg_username() == 'example_bot'
    assert router.api_tg_config() == {'enabled': True, 'bot_username': 'example_bot'}


def test_stopped_panel_bot_does_not_advertise_stale_username(monkeypatch):
    bot = SimpleNamespace(is_running=lambda: False, running_username=lambda: 'stale_bot')
    panel = SimpleNamespace(tg_bot=bot, load_data=lambda: {
        'settings': {'telegram': {'enabled': True, 'token': 'panel-token'}}
    })
    monkeypatch.setattr(router, '_panel', lambda: panel)

    assert router._tg_username() == ''
    assert router.api_tg_config()['enabled'] is False


def test_existing_billing_only_config_remains_available(monkeypatch):
    monkeypatch.setattr(router, '_panel', lambda: SimpleNamespace(load_data=lambda: {}))
    monkeypatch.setattr(router, 'get_settings', lambda: SimpleNamespace(
        telegram_bot_token='billing-token', telegram_bot_username='billing_bot'))

    assert router._tg_token() == 'billing-token'
    assert router._tg_username() == 'billing_bot'


def test_disabled_panel_bot_is_not_restarted_via_legacy_billing_settings(monkeypatch):
    monkeypatch.setattr(router, '_panel', lambda: SimpleNamespace(load_data=lambda: {
        'settings': {'telegram': {'enabled': False, 'token': 'panel-token'}}
    }))
    monkeypatch.setattr(router, 'get_settings', lambda: SimpleNamespace(
        telegram_bot_token='old-token', telegram_bot_username='old_bot'))

    assert router._tg_token() == ''
    assert router._tg_username() == ''


def test_one_tap_confirms_once_and_creates_customer(tmp_path, monkeypatch):
    db = str(tmp_path / 'billing.db')
    store.init_db(db)
    bot = SimpleNamespace(is_running=lambda: True, running_username=lambda: 'example_bot')
    panel = SimpleNamespace(tg_bot=bot, load_data=lambda: {
        'settings': {'telegram': {'enabled': True, 'token': 'panel-token'}}
    })
    monkeypatch.setattr(router, '_panel', lambda: panel)
    monkeypatch.setattr(router, '_db', lambda: db)
    monkeypatch.setattr(router, '_client_ip', lambda request: '127.0.0.1')
    monkeypatch.setattr(router, '_rate_limit', lambda *args: True)
    monkeypatch.setattr(router, '_render', lambda request, template, **kwargs: kwargs)
    monkeypatch.setattr(router.provision, 'find_user_by_telegram', lambda tg_id: None)
    created = []

    async def create_customer(*, telegram_id, trial_days):
        created.append(telegram_id)
        return {'id': 'customer-1', 'role': 'user', 'enabled': True}

    monkeypatch.setattr(router.provision, 'create_customer', create_customer)
    monkeypatch.setattr(router.provision, 'is_active', lambda user: False)
    request = SimpleNamespace(session={})
    page = asyncio.run(router.page_tg_start(request))
    token = page['token']
    assert page['link'].startswith('https://t.me/example_bot?start=lk_')
    assert store.confirm_login_token(db, token, '12345')

    result = asyncio.run(router.api_tg_poll(request, token))

    assert result['status'] == 'ok'
    assert request.session['billing_uid'] == 'customer-1'
    assert created == ['12345']
    assert asyncio.run(router.api_tg_poll(request, token)).status_code == 403
