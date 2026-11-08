from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


class MainContent(HTMLParser):
    def __init__(self):
        super().__init__()
        self.in_main = False
        self.text = []
        self.tags = []

    def handle_starttag(self, tag, attrs):
        if tag == "main":
            self.in_main = True
        if self.in_main:
            self.tags.append((tag, dict(attrs)))

    def handle_endtag(self, tag):
        if tag == "main":
            self.in_main = False

    def handle_data(self, data):
        if self.in_main and data.strip():
            self.text.append(data.strip())


def render(document_type):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    return env.get_template("billing_legal_unavailable.html").render(
        document_type=document_type,
        site_settings={"title": "TTVPN", "logo": ""},
        brand={"ready": False, "operator_ready": False},
        request=SimpleNamespace(url=SimpleNamespace(path="/legal")),
        landing_url="/", csrf="test", static_v="test",
    )


@pytest.mark.parametrize("document_type,title", [
    ("privacy", "Политика конфиденциальности"),
    ("terms", "Условия использования"),
    ("unknown", "Юридическая информация"),
    ('<img src=x onerror="alert(1)">', "Юридическая информация"),
])
def test_legal_unavailable_page(document_type, title):
    html = render(document_type)
    content = MainContent()
    content.feed(html)

    assert f"<title>{title} — TTVPN</title>" in html
    assert "Оплата картой или через СБП" not in html
    assert content.text == [title, "Документ подготавливается и пока не опубликован."]
    assert content.tags == [
        ("main", {"class": "bc-card", "aria-labelledby": "legal-title"}),
        ("h1", {"id": "legal-title"}),
        ("p", {}),
    ]
    if document_type.startswith("<"):
        assert document_type not in html


def test_default_footer_does_not_promise_payment_methods():
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape(["html"]))
    template = env.from_string('{% extends "billing_base.html" %}{% block content %}Тест{% endblock %}')
    html = template.render(site_settings={"title": "TTVPN"}, brand={"ready": False},
                           request=SimpleNamespace(url=SimpleNamespace(path="/")),
                           landing_url="/", csrf="test", static_v="test")
    assert "Оплата картой или через СБП" not in html
