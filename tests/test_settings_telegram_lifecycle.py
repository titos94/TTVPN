import asyncio
from unittest.mock import AsyncMock

import app as panel


def test_saving_settings_stops_running_bot_on_event_loop(monkeypatch):
    monkeypatch.setattr(panel, '_check_admin', lambda request: {'role': 'admin'})
    monkeypatch.setattr(panel, 'load_data', lambda: {'settings': {}, 'servers': []})
    monkeypatch.setattr(panel, 'save_data', lambda data: None)
    monkeypatch.setattr(panel.tg_bot, 'is_running', lambda: True)
    stop = AsyncMock()
    monkeypatch.setattr(panel.tg_bot, 'stop_bot', stop)
    payload = panel.SaveSettingsRequest(
        appearance={}, sync={}, captcha={}, telegram={'enabled': False}, ssl={}
    )

    result = asyncio.run(panel.save_settings(None, payload))

    assert result['status'] == 'success'
    stop.assert_awaited_once()


def test_changing_bot_token_restarts_existing_bot(monkeypatch):
    monkeypatch.setattr(panel, '_check_admin', lambda request: {'role': 'admin'})
    monkeypatch.setattr(panel, 'load_data', lambda: {
        'settings': {'telegram': {'token': 'old-token', 'enabled': True}}, 'servers': []
    })
    monkeypatch.setattr(panel, 'save_data', lambda data: None)
    state = {'running': True, 'launched': None}
    monkeypatch.setattr(panel.tg_bot, 'is_running', lambda: state['running'])

    async def stop():
        state['running'] = False

    def launch(token, *args, **kwargs):
        state['launched'] = token
        state['running'] = True

    monkeypatch.setattr(panel.tg_bot, 'stop_bot', stop)
    monkeypatch.setattr(panel.tg_bot, 'launch_bot', launch)
    payload = panel.SaveSettingsRequest(
        appearance={}, sync={}, captcha={}, telegram={'enabled': True, 'token': 'new-token'}, ssl={}
    )

    result = asyncio.run(panel.save_settings(None, payload))

    assert result['status'] == 'success'
    assert state['launched'] == 'new-token'
