import sqlite3
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape

from billing import store


TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def render_cabinet(payments, **overrides):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    context = dict(payments=payments, user={"email": "test@example.test"}, active=False,
        expiry="", trial_record=None, devices=[], device_limit=5, trial={},
        site_settings={}, brand={}, landing_url="/", current_user=None,
        billing_user=None, request=SimpleNamespace(url=SimpleNamespace(path="/cabinet")),
        csrf="", static_v="test", format_money=lambda amount: f"{amount / 100:.2f} ₽",
    )
    context.update(overrides)
    return env.get_template("billing_cabinet.html").render(**context)


def test_history_limit_with_real_isolated_payment_query(tmp_path):
    db = tmp_path / "payments.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("""CREATE TABLE payments (
            id TEXT PRIMARY KEY, user_id TEXT, created_at TEXT,
            plan_name TEXT, promo_code TEXT, status TEXT, amount_kopecks INTEGER
        )""")
        conn.executemany(
            "INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(str(i), "test-user", f"2026-01-{i:02d}T12:00:00", f"Запись {i}",
              "", "succeeded", 10000) for i in range(1, 26)],
        )
        conn.execute("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?)",
                     ("other", "other-user", "2026-02-01", "Чужая запись", "", "succeeded", 999999))
    rows = store.list_user_payments(str(db), "test-user")
    assert len(rows) == 20
    assert [row["id"] for row in rows] == [str(i) for i in range(25, 5, -1)]
    html = render_cabinet(rows)
    assert 'class="bc-metric__label">Записи о платежах' in html
    assert 'class="bc-metric__value">20</span>' in html
    assert "Показаны последние записи о платежах, не более 20." in html
    assert "Запись 25" in html and "Запись 5</b>" not in html
    assert "Чужая запись" not in html
    assert "за всё время" not in html
    assert "2000.00 ₽" not in html and "2500.00 ₽" not in html


def test_empty_history_does_not_claim_paid_total():
    html = render_cabinet([])
    assert 'class="bc-metric__value">0</span>' in html
    assert "Платежей пока нет" in html
    assert "за всё время" not in html
    assert "0.00 ₽" not in html


def test_payment_statuses_are_distinguished_without_summing():
    statuses = ["succeeded", "pending", "canceled", "refunded", "unexpected"]
    rows = [dict(plan_name=f"Тариф {status}", created_at="2026-01-01T12:00:00",
                 promo_code="", amount_kopecks=(i + 1) * 10000, status=status)
            for i, status in enumerate(statuses)]
    html = render_cabinet(rows)
    assert 'class="bc-metric__value">5</span>' in html
    labels = ("оплачен", "ожидает", "отменён", "возвращён", "статус неизвестен")
    for status, label in zip(statuses, labels):
        row = html.split(f"<b>Тариф {status}</b>", 1)[1].split('</div>', 1)[0]
        assert f">{label}</span>" in row
    assert "1500.00 ₽" not in html
    assert "за всё время" not in html


@pytest.mark.parametrize("count,shown", [(19, 19), (20, 20), (21, 20)])
def test_history_boundaries(tmp_path, count, shown):
    db = tmp_path / "payments.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE payments (id TEXT, user_id TEXT, created_at TEXT, "
                     "plan_name TEXT, promo_code TEXT, status TEXT, amount_kopecks INTEGER)")
        conn.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [(str(i), "u", f"2026-01-{i:02d}T00:00:00", f"Запись {i}",
                           "", "pending", 10000) for i in range(1, count + 1)])
    rows = store.list_user_payments(str(db), "u")
    html = render_cabinet(rows)
    assert len(rows) == shown
    assert f'class="bc-metric__value">{shown}</span>' in html
    assert ("<b>Запись 1</b>" in html) == (count <= 20)
    assert "не более 20" in html


def test_user_and_payment_data_are_escaped():
    payload = '<script>alert("x")</script>'
    html = render_cabinet(
        [dict(plan_name=payload, promo_code=payload, created_at="2026-01-01T12:00:00",
              amount_kopecks=123, status="succeeded")], user={"email": payload})
    assert payload not in html
    assert html.count('&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;') == 3


@pytest.mark.parametrize("active", [False, True])
def test_cabinet_does_not_promise_timing_or_payment_method(active):
    html = render_cabinet([], active=active, user={"email": "test@example.test",
                                                   "expiration_date": "2026-12-01"})
    for claim in ("в течение минуты", "включится сразу", "СБП", "появятся автоматически"):
        assert claim not in html


@pytest.mark.parametrize("trial", [{"available": False}, {"available": True, "days": 7}])
def test_inactive_subscription_instructions_allow_other_options(trial):
    html = render_cabinet([], trial=trial)
    section = html.split("Как включить доступ", 1)[1].split('<div class="bc-card" id="promo">', 1)[0]
    assert "Выбрать способ" in section
    assert "Проверьте доступные варианты в кабинете." in section
    assert "Оплатить" not in section
    assert "пробный период" not in section.lower()
    assert "промокод" not in section.lower()


def test_cabinet_route_uses_isolated_database_and_real_template(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from fastapi.responses import HTMLResponse
    from fastapi.testclient import TestClient
    from billing import router as billing_router

    db = tmp_path / "payments.sqlite"
    with sqlite3.connect(db) as conn:
        conn.execute("CREATE TABLE payments (id TEXT, user_id TEXT, created_at TEXT, "
                     "plan_id TEXT, promo_code TEXT, status TEXT, amount_kopecks INTEGER)")
        conn.executemany("INSERT INTO payments VALUES (?, ?, ?, ?, ?, ?, ?)",
                         [(str(i), "u", f"2026-01-{i:02d}T00:00:00", "plan", "",
                           "succeeded", 10000) for i in range(1, 22)])
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    def tpl(request, template, **kwargs):
        return HTMLResponse(env.get_template(template).render(
            request=request, site_settings={}, current_user=None, static_v="test", **kwargs))

    user = {"id": "u", "email": "test@example.test", "expiration_date": ""}
    monkeypatch.setattr(billing_router, "_panel", lambda: SimpleNamespace(tpl=tpl))
    monkeypatch.setattr(billing_router, "_db", lambda: str(db))
    monkeypatch.setattr(billing_router, "_current", lambda request: user)
    monkeypatch.setattr(billing_router, "_sync_panel_session", lambda request, user: None)
    monkeypatch.setattr(billing_router, "_csrf", lambda request: "test")
    monkeypatch.setattr(billing_router, "get_settings", lambda: SimpleNamespace(
        telegram_bot_username="", telegram_bot_token="", telegram_ready=False))
    monkeypatch.setattr(billing_router.branding, "current", lambda: {})
    monkeypatch.setattr(billing_router.provision, "is_active", lambda user: False)
    monkeypatch.setattr(billing_router, "_renewal_base", lambda user: (datetime(2026, 1, 1), ""))
    monkeypatch.setattr(billing_router, "_flat_devices", lambda uid: [])
    monkeypatch.setattr(billing_router, "_device_limit", lambda user: 5)
    monkeypatch.setattr(billing_router, "_trial_state", lambda user, settings: {})
    monkeypatch.setattr(store, "get_trial", lambda db, uid: None)
    monkeypatch.setattr(store, "list_plans", lambda db, only_active=True: [{"id": "plan", "name": "Тариф", "days": 30}])
    app = FastAPI()
    app.include_router(billing_router.router)
    with TestClient(app) as client:
        response = client.get("/cabinet")
    assert response.status_code == 200
    assert 'class="bc-metric__value">20</span>' in response.text
    assert response.text.count('>оплачен</span>') == 20
    assert "не более 20" in response.text
    assert "за всё время" not in response.text
