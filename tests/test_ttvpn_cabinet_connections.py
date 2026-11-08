"""Личный кабинет: страница подключений и выдача конфигурации устройства.

Что проверяется:

* ``GET /cabinet/connections`` (``billing.router.page_connections``) — какой
  шаблон и какие подсказки получает клиент в трёх состояниях: подписка активна
  без устройств, подписка не активна, устройство уже создано;
* ``POST /api/billing/connections/{id}/config`` — авторизация по сессии
  кабинета, изоляция чужих устройств, отказ при неактивной подписке и
  заголовок ``Cache-Control: no-store`` у успешного ответа.

Внешние зависимости (панель, SSH, Telegram, брендинг) замоканы. Пути
состояния вынесены в ``tempfile.TemporaryDirectory`` через переменные
окружения ещё до импорта ``app``: модуль на импорте вызывает
``billing.store.init_db`` и не должен трогать рабочие ``data.json`` и
``billing.db``. Тесты детерминированы и не делают сетевых вызовов.
"""

import os
import tempfile
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

# Изоляция состояния до импорта приложения. ``setdefault`` уважает окружение,
# которое уже задал запускающий (см. команду запуска в задаче), но не даёт
# тесту упасть на рабочих файлах репозитория при обычном ``pytest``.
_TMP_ENV = tempfile.TemporaryDirectory(prefix="ttvpn-cabinet-")
for _name, _filename in (
    ("DATA_FILE", "data.json"),
    ("BILLING_DB_PATH", "billing.db"),
    ("SSH_KNOWN_HOSTS_FILE", "known_hosts"),
    ("TUNNEL_STATE_FILE", "tunnels.json"),
):
    os.environ.setdefault(_name, os.path.join(_TMP_ENV.name, _filename))
os.environ.setdefault("SECRET_KEY", "isolated-test-secret")

import app as panel  # noqa: E402  (импорт после изоляции окружения)
from billing import (  # noqa: E402
    provision,
    router as billing_router,
    settings as billing_settings,
    store,
)


TEMPLATES = Path(__file__).resolve().parents[1] / "templates"
USER_ID = "u-cabinet"
OTHER_USER_ID = "u-other"


def _future(days: int = 30) -> str:
    from datetime import datetime, timedelta

    return (datetime.now() + timedelta(days=days)).isoformat(timespec="seconds")


class _PanelStub:
    """Минимальный ``app`` для рендера: только ``tpl``."""

    def __init__(self, environment):
        self._environment = environment

    def tpl(self, request, template, **kwargs):
        kwargs.setdefault("site_settings", {})
        kwargs.setdefault("current_user", None)
        kwargs.setdefault("static_v", "test")
        return HTMLResponse(
            self._environment.get_template(template).render(request=request, **kwargs)
        )


@pytest.fixture
def cabinet(tmp_path, monkeypatch):
    """Изолированное приложение кабинета поверх временной SQLite.

    Возвращает ``(client, state)``. Поля ``state`` переключают сценарий:
    ``uid`` (сессия), ``active`` (подписка), ``connections`` (устройства
    панели) и ``data`` (то, что вернёт ``app.load_data``).
    """
    db = str(tmp_path / "billing.db")
    store.init_db(db)

    environment = Environment(
        loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape()
    )
    state = {
        "uid": USER_ID,
        "user": {
            "id": USER_ID,
            "email": "test@example.test",
            "expiration_date": _future(),
        },
        "active": True,
        "connections": [],
        "data": {"servers": [{"name": "Тестовый сервер", "protocols": {}}],
                 "user_connections": []},
    }

    # Панель и хранилище — только временные, без рабочих файлов.
    monkeypatch.setattr(panel, "DATA_FILE", str(tmp_path / "data.json"))
    monkeypatch.setattr(panel, "load_data", lambda: state["data"])
    monkeypatch.setattr(
        panel, "protocol_display_name",
        lambda protocol, *args, **kwargs: f"protocol:{protocol}",
    )

    monkeypatch.setattr(billing_router, "_panel", lambda: _PanelStub(environment))
    monkeypatch.setattr(billing_router, "_db", lambda: db)
    # ``provision.user_devices`` импортирует ``db_path`` внутри вызова.
    monkeypatch.setattr(billing_settings, "db_path", lambda: db)

    # Сессия кабинета: ``_uid`` — единственная точка, где читается cookie.
    monkeypatch.setattr(billing_router, "_uid", lambda request: state["uid"])
    monkeypatch.setattr(
        provision, "find_user",
        lambda uid: state["user"]
        if uid and state["user"] and uid == state["user"]["id"] else None,
    )
    monkeypatch.setattr(billing_router, "_csrf", lambda request: "test-csrf")
    monkeypatch.setattr(
        billing_router, "_sync_panel_session", lambda request, user: None
    )
    monkeypatch.setattr(
        billing_router, "get_settings",
        lambda: SimpleNamespace(
            telegram_bot_username="", telegram_bot_token="", telegram_ready=False
        ),
    )
    monkeypatch.setattr(billing_router.branding, "current", lambda: {})
    monkeypatch.setattr(billing_router, "_trial_state", lambda user, settings: {})
    monkeypatch.setattr(billing_router, "_device_limit", lambda user: 5)

    # ``billing_router.provision`` — тот же модуль, поэтому патча достаточно.
    monkeypatch.setattr(provision, "is_active", lambda user: bool(state["active"]))
    monkeypatch.setattr(
        provision, "user_connections", lambda user_id: list(state["connections"])
    )
    monkeypatch.setattr(
        provision, "user_devices",
        lambda user_id: store.ensure_devices(
            db, user_id, list(state["connections"])
        ),
    )

    # Лимит запросов — модульный словарь; чистим, чтобы тесты не влияли друг
    # на друга при общем адресе TestClient.
    billing_router._RATE.clear()

    application = FastAPI()
    application.include_router(billing_router.router)
    with TestClient(application) as client:
        yield client, state

    billing_router._RATE.clear()


# --------------------------------------------------------------- страница

def test_active_subscription_without_devices_offers_retry_not_renewal(cabinet):
    """Активная подписка и пустой список: клиенту предлагают создать устройство.

    Слово «Продлить» здесь было бы ложью — оплаченные дни ещё идут, продлевать
    нечего. Вместо этого страница показывает кнопку повторного создания.
    """
    client, state = cabinet
    state["active"] = True
    state["connections"] = []

    response = client.get("/cabinet/connections")

    assert response.status_code == 200
    assert "Продлить" not in response.text
    assert 'onclick="retryDevices(this)"' in response.text
    assert "Создать устройство" in response.text
    assert "Устройство ещё не создано" in response.text


def test_inactive_subscription_points_to_plans(cabinet):
    """Без активной подписки страница ведёт на выбор тарифа."""
    client, state = cabinet
    state["active"] = False
    state["connections"] = []

    response = client.get("/cabinet/connections")

    assert response.status_code == 200
    assert 'href="/plans">Выбрать тариф</a>' in response.text
    assert "не активна" in response.text
    assert 'onclick="retryDevices(this)"' not in response.text


def test_device_row_shows_name_and_config_tabs(cabinet):
    """Созданное устройство показывает своё имя и все три способа подключения."""
    client, state = cabinet
    state["active"] = True
    state["connections"] = [{
        "id": "conn-1",
        "user_id": USER_ID,
        "name": "Телефон",
        "server_id": 0,
        "protocol": "awg3",
        "client_id": "peer-1",
    }]

    response = client.get("/cabinet/connections")

    assert response.status_code == 200
    assert "Телефон" in response.text
    assert 'data-device="conn-1"' in response.text
    assert ">QR-код</button>" in response.text
    assert ">Ключ vpn://</button>" in response.text
    assert ">Файл конфигурации</button>" in response.text
    assert "Тестовый сервер" in response.text
    assert "protocol:awg3" in response.text
    # Импортируемый ключ Amnezia — кнопка открытия приложения доступна.
    assert "openInAmnezia(this)" in response.text


def test_non_importable_protocol_hides_the_open_button(cabinet):
    """Для протокола без импортируемого ключа кнопку открытия приложения не рисуем.

    ``vpn://`` для Telemt/сырого конфига — только QR-fallback, ОС его не
    импортирует, поэтому кнопка была бы ложным обещанием.
    """
    client, state = cabinet
    state["active"] = True
    state["connections"] = [{
        "id": "conn-tg",
        "user_id": USER_ID,
        "name": "Телефон",
        "server_id": 0,
        "protocol": "telemt",
        "client_id": "peer-tg",
    }]

    response = client.get("/cabinet/connections")

    assert response.status_code == 200
    assert 'data-device="conn-tg"' in response.text
    assert "openInAmnezia(this)" not in response.text
    assert ">Ключ vpn://</button>" in response.text


def test_connections_page_requires_login(cabinet):
    """Без сессии страница подключений отправляет на вход."""
    client, state = cabinet
    state["uid"] = None

    response = client.get("/cabinet/connections", follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"].startswith("/cabinet/login")


# --------------------------------------------------------------- конфигурация

def test_config_requires_session(cabinet, monkeypatch):
    """Без сессии — 401, конфигурация не строится вовсе."""
    client, state = cabinet
    state["uid"] = None
    called = []

    async def forbidden(*args, **kwargs):
        called.append(args)
        raise AssertionError("connection_config must not run without a session")

    monkeypatch.setattr(provision, "connection_config", forbidden)

    response = client.post("/api/billing/connections/conn-1/config")

    assert response.status_code == 401
    assert response.json()["error"] == "Требуется вход в кабинет"
    assert called == []


def test_config_hides_foreign_device(cabinet):
    """Чужое устройство неотличимо от несуществующего: 404.

    Здесь работает настоящая ``provision.connection_config``: владелец
    проверяется по ``user_id`` из billing-сессии, до любых SSH-вызовов.
    """
    client, state = cabinet
    state["active"] = True
    state["data"]["user_connections"] = [{
        "id": "foreign-1",
        "user_id": OTHER_USER_ID,
        "name": "Чужой телефон",
        "server_id": 0,
        "protocol": "awg3",
        "client_id": "peer-x",
    }]

    response = client.post("/api/billing/connections/foreign-1/config")

    assert response.status_code == 404
    assert response.json()["error"] == "Устройство не найдено"
    assert "Чужой телефон" not in response.text


def test_config_rejected_when_subscription_inactive(cabinet, monkeypatch):
    """Неактивная подписка — 403 с машинным кодом, до проверки владельца.

    По коду (billing/router.py) проверка ``provision.is_active`` стоит раньше
    вызова ``connection_config``, поэтому даже чужой идентификатор получает
    403, а не 404. Тест фиксирует этот фактический порядок.
    """
    client, state = cabinet
    state["active"] = False
    called = []

    async def spy(*args, **kwargs):
        called.append(args)
        raise AssertionError("connection_config must not run for an inactive plan")

    monkeypatch.setattr(provision, "connection_config", spy)

    response = client.post("/api/billing/connections/foreign-1/config")

    assert response.status_code == 403
    assert response.json()["code"] == "subscription_inactive"
    assert called == []


def test_config_success_returns_payload_with_no_store(cabinet, monkeypatch):
    """Успех: 200, конфигурация в теле и запрет кеширования ключа."""
    client, state = cabinet
    state["active"] = True
    seen = []

    async def fake_config(user_id, connection_id):
        seen.append((user_id, connection_id))
        return {
            "config": "[Interface]\nPrivateKey = test\n",
            "vpn_key": "vpn://test",
            "qr": "data:image/png;base64,test",
        }

    monkeypatch.setattr(provision, "connection_config", fake_config)

    response = client.post("/api/billing/connections/conn-1/config")

    assert response.status_code == 200
    assert seen == [(USER_ID, "conn-1")]
    assert response.json()["vpn_key"] == "vpn://test"
    assert "PrivateKey" in response.json()["config"]
    assert response.headers["cache-control"] == "no-store"


def test_config_backend_failure_is_502(cabinet, monkeypatch):
    """Ошибка SSH/менеджера не роняет кабинет: 502 с понятным текстом."""
    client, state = cabinet
    state["active"] = True

    async def broken(user_id, connection_id):
        raise RuntimeError("ssh: connection refused")

    monkeypatch.setattr(provision, "connection_config", broken)

    response = client.post("/api/billing/connections/conn-1/config")

    assert response.status_code == 502
    assert "Сервер не отдал конфигурацию" in response.json()["error"]
    assert "cache-control" not in response.headers


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))


# --------------------------------------------------------- признак ключа

def test_vpn_importable_matches_the_protocols_the_client_can_open():
    """Открывать по vpn:// можно только те протоколы, что Amnezia импортирует.

    AWG-семейство и xray дают полноценный ключ. Telemt и прочие отдают
    ``vpn://`` лишь как QR-fallback, поэтому признак должен быть False.
    """
    from billing.provision import vpn_importable

    for protocol in ("awg", "awg2", "awg3", "awg_legacy", "awg3__2", "xray"):
        assert vpn_importable(protocol) is True, protocol
    for protocol in ("telemt", "socks5", "dns", "nginx", "", None):
        assert vpn_importable(protocol) is False, protocol
