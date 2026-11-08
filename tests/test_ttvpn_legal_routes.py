"""Публичные ссылки не обходят ограничения доменов."""
from types import SimpleNamespace
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse
from fastapi.testclient import TestClient
from jinja2 import Environment, FileSystemLoader, select_autoescape

from billing import router as billing


def test_legal_routes_on_public_and_client_hosts(monkeypatch):
    monkeypatch.setenv("TTVPN_PRODUCTION", "1")
    monkeypatch.setattr(billing, "_hosts", lambda: ({"lk.example"}, {"admin.example"}, {"example"}))
    monkeypatch.setattr(billing, "get_settings", lambda: SimpleNamespace(public_base="https://lk.example"))
    monkeypatch.setattr(billing, "_render", lambda request, template, **kw: HTMLResponse(kw["document_type"]))
    app = FastAPI()
    @app.get("/api/admin-check")
    def admin_check():
        return {"ok": True}

    app.include_router(billing.router)
    billing.install_host_gate(app)
    client = TestClient(app)
    for host in ("example", "lk.example"):
        for kind in ("privacy", "terms"):
            response = client.get(f"/billing/legal/{kind}", headers={"host": host})
            assert response.status_code == 200
            assert response.text == kind
    assert client.get("/billing/legal/other", headers={"host": "lk.example"}).status_code == 404
    assert client.get("/billing/legal/privacy", headers={"host": "unknown.example"}).status_code == 404
    assert client.get("/api/admin-check", headers={"host": "example"}, follow_redirects=False).status_code == 302
    assert client.get("/api/admin-check", headers={"host": "lk.example"}).status_code == 404
    assert client.get("/api/admin-check", headers={"host": "admin.example"}).status_code == 200


def test_legal_route_renders_template_with_billing_context(monkeypatch):
    monkeypatch.setattr(billing, "_hosts", lambda: ({"lk.example"}, {"admin.example"}, {"example"}))
    settings = SimpleNamespace(public_base="https://lk.example", telegram_bot_username="", telegram_bot_token="",
                               telegram_ready=False)
    monkeypatch.setattr(billing, "get_settings", lambda: settings)
    monkeypatch.setattr(billing, "_csrf", lambda request: "synthetic")
    monkeypatch.setattr(billing, "_current", lambda request: None)
    monkeypatch.setattr(billing.branding, "current", lambda: {"ready": False, "operator_ready": False})
    env = Environment(loader=FileSystemLoader(Path(__file__).resolve().parents[1] / "templates"),
                      autoescape=select_autoescape(["html"]))

    def tpl(request, template, **kwargs):
        return HTMLResponse(env.get_template(template).render(
            request=request, site_settings={"title": "TTVPN"}, static_v="test", **kwargs))

    monkeypatch.setattr(billing, "_panel", lambda: SimpleNamespace(tpl=tpl))
    app = FastAPI()
    app.include_router(billing.router)
    billing.install_host_gate(app)
    response = TestClient(app).get("/billing/legal/privacy", headers={"host": "example"})
    assert response.status_code == 200
    assert "Политика конфиденциальности" in response.text
    assert "Документ подготавливается и пока не опубликован." in response.text
