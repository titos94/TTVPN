from html.parser import HTMLParser
from pathlib import Path
from types import SimpleNamespace
from urllib.parse import parse_qs, urlsplit

import pytest
from jinja2 import Environment, FileSystemLoader, select_autoescape


TEMPLATES = Path(__file__).resolve().parents[1] / "templates"


def render_page(template, plans, **overrides):
    env = Environment(loader=FileSystemLoader(TEMPLATES), autoescape=select_autoescape())
    context = dict(
        plans=plans, site_settings={}, brand={}, landing_url="/", trial_public=False,
        trial_days=7, tg_ready=False, format_money=lambda value: f"{value / 100:g} ₽",
        request=SimpleNamespace(url=SimpleNamespace(path="/")), current_user=None,
        billing_user=None, csrf="", static_v="test", active=False, expiry="",
        trial={}, promo_code="", selected_plan="", plan_days={}, base_iso="2026-01-01",
    )
    context.update(overrides)
    return env.get_template(template).render(**context)


def render_landing(plans, **overrides):
    return render_page("billing_landing.html", plans, **overrides)


class PlanLinks(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links = []
        self.active = None

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "bc-plan" in attrs.get("class", "").split():
            self.active = {"tag": tag, "attrs": attrs, "text": ""}
            self.links.append(self.active)

    def handle_data(self, data):
        if self.active is not None:
            self.active["text"] += data

    def handle_endtag(self, tag):
        if self.active is not None and tag == self.active["tag"]:
            self.active = None


def test_plan_cards_are_native_links_with_encoded_selection():
    plan_id = "тариф & '1'"
    html = render_landing([dict(id=plan_id, name="Тестовый тариф", description="",
                                days=30, price_kopecks=50000, device_limit=5)])
    parser = PlanLinks()
    parser.feed(html)
    assert len(parser.links) == 1
    link = parser.links[0]
    assert link["tag"] == "a"
    assert "onclick" not in link["attrs"]
    assert "tabindex" not in link["attrs"]
    assert "Тестовый тариф" in link["text"] and "Выбрать" in link["text"]
    url = urlsplit(link["attrs"]["href"])
    assert url.path == "/plans"
    assert parse_qs(url.query) == {"plan": [plan_id]}


def test_empty_plans_and_trial_are_rendered_without_unsupported_claims():
    html = render_landing([], trial_public=True)
    assert "Тарифы уточняются" in html
    assert "Пробный период: 7 дней. Доступность и активация — в личном кабинете." in html
    for claim in ("без рекламы и логов", "Подключение за минуту", "Франкфурте",
                  "устойчив к блокировкам", "сразу после оплаты", "Цена окончательная",
                  "Оплата картой или через СБП"):
        assert claim not in html


def test_selected_plan_card_and_approximate_comparison():
    plans = [dict(id="a", name="Первый", description="", days=90,
                  price_kopecks=10001, device_limit=5),
             dict(id="b", name="Второй", description="", days=30,
                  price_kopecks=7000, device_limit=5)]
    landing = render_landing(plans, trial_public=True)
    assert "от ≈ 33.34 ₽" in landing
    assert "≈ 33.34 ₽ за 30 дней" in landing
    assert "активация в кабинете" in landing
    html = render_page("billing_plans.html", plans, selected_plan="b",
                       plan_days={"a": 90, "b": 30}, trial={"available": True, "days": 7, "devices": 1})
    parser = PlanLinks()
    parser.feed(html)
    assert [link["tag"] for link in parser.links] == ["button", "button"]
    assert [link["attrs"]["aria-pressed"] for link in parser.links] == ["false", "true"]
    assert all(link["attrs"]["type"] == "button" for link in parser.links)
    assert all(link["attrs"]["onclick"] == "selectPlan(this.dataset.plan)" for link in parser.links)
    assert "≈ 33.34 ₽" in html
    assert "Примерная стоимость за 30 дней" in html
    assert 'id="trialBtn"' in html
    # Способ оплаты и момент выдачи доступа — неподтверждённые обещания.
    for claim in ("Оплата картой", "через СБП", "включаются сразу",
                  "не проходят через наш сервер"):
        assert claim not in html


def test_keyboard_selection_and_checkout_with_mocked_network():
    playwright = pytest.importorskip("playwright.sync_api")
    plans = [dict(id="a", name="Первый", description="", days=30,
                  price_kopecks=10000, device_limit=5),
             dict(id="b", name="Второй", description="", days=90,
                  price_kopecks=20000, device_limit=5)]
    requests = []
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip("Локальный браузер Chromium не установлен")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(reduced_motion="reduce")

        def handle(route):
            request = route.request
            url = urlsplit(request.url)
            if url.netloc != "ttvpn.test":
                return route.abort()
            if url.path == "/":
                html = render_landing(plans)
            elif url.path == "/plans":
                selected = parse_qs(url.query).get("plan", [""])[0]
                html = render_page("billing_plans.html", plans, selected_plan=selected,
                                   plan_days={"a": 30, "b": 90})
            elif url.path == "/api/billing/checkout":
                requests.append(request.post_data_json)
                if len(requests) == 1:
                    return route.fulfill(status=400, json={"error": "Тестовая оплата"})
                return route.fulfill(json={"confirmation_url": "/mock-confirmation"})
            elif url.path == "/mock-confirmation":
                return route.fulfill(content_type="text/html", body="Тестовая страница подтверждения")
            elif url.path == "/api/billing/promo/check":
                return route.fulfill(json={"code": "TEST", "label": "скидка",
                                           "kind": "percent", "final_kopecks": 15000})
            elif url.path == "/static/css/ttvpn-ui.css":
                css = (TEMPLATES.parent / "static/css/ttvpn-ui.css").read_text()
                return route.fulfill(content_type="text/css", body=css)
            else:
                return route.fulfill(status=404, body="")
            route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)

        page.route("**/*", handle)
        page.goto("http://ttvpn.test/")
        landing_link = page.locator('a.bc-plan[href="/plans?plan=b"]')
        landing_link.focus()
        page.keyboard.press("Enter")
        page.wait_for_url("**/plans?plan=b")
        first = page.locator('button[data-plan="a"]')
        second = page.locator('button[data-plan="b"]')
        assert first.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(17, 26, 33)"
        assert second.evaluate("el => getComputedStyle(el).backgroundColor") == "rgb(27, 50, 70)"
        assert second.get_attribute("aria-pressed") == "true"
        assert "200 ₽" in page.locator("#totalPrice").inner_text()
        page.locator("#promoCode").fill("TEST")
        page.locator("#promoCode").press("Enter")
        page.get_by_text("Промокод TEST принят: скидка").wait_for()
        assert "150 ₽" in page.locator("#totalPrice").inner_text()
        first.focus()
        assert first.evaluate("el => getComputedStyle(el).outlineStyle") == "solid"
        page.keyboard.press("Space")
        assert first.get_attribute("aria-pressed") == "true"
        assert second.get_attribute("aria-pressed") == "false"
        assert "100 ₽" in page.locator("#totalPrice").inner_text()
        assert "Тариф изменён" in page.locator("#promoHint").inner_text()
        page.locator("#buyBtn").click()
        page.get_by_text("Тестовая оплата").wait_for()
        assert requests == [{"csrf": "", "plan_id": "a"}]
        page.locator("#buyBtn").click()
        page.wait_for_url("**/mock-confirmation")
        assert requests == [{"csrf": "", "plan_id": "a"}] * 2
        page.go_back()
        assert page.locator("#buyBtn").is_enabled()
        browser.close()


def test_delayed_promo_results_cannot_override_new_selection():
    playwright = pytest.importorskip("playwright.sync_api")
    plans = [dict(id="a", name="Первый", description="", days=30,
                  price_kopecks=10000, device_limit=5),
             dict(id="b", name="Второй", description="", days=30,
                  price_kopecks=20000, device_limit=5)]
    with playwright.sync_playwright() as p:
        if not Path(p.chromium.executable_path).is_file():
            pytest.skip("Локальный браузер Chromium не установлен")
        browser = p.chromium.launch(headless=True)
        page = browser.new_page(reduced_motion="reduce")
        html = render_page("billing_plans.html", plans, selected_plan="a",
                           plan_days={"a": 30, "b": 30})
        page.route("**/*", lambda route: route.fulfill(
            status=200, content_type="text/html; charset=utf-8", body=html
        ) if route.request.url == "http://ttvpn.test/plans" else route.abort())
        page.goto("http://ttvpn.test/plans")
        page.evaluate("""() => {
            window.pendingPromos = [];
            bcPost = (url, payload) => new Promise((resolve, reject) => {
                window.pendingPromos.push({payload, resolve, reject});
            });
        }""")
        page.locator("#promoCode").fill("TEST")
        page.locator("#promoCode").press("Enter")
        assert page.evaluate("pendingPromos[0].payload.plan_id") == "a"
        assert page.locator("#buyBtn").is_disabled()
        page.evaluate("() => buy()")
        assert page.evaluate("pendingPromos.length") == 1
        page.locator('button[data-plan="b"]').click()
        assert page.locator("#buyBtn").is_enabled()
        page.locator("#promoCode").press("Enter")
        assert page.evaluate("pendingPromos[1].payload.plan_id") == "b"
        assert page.locator("#buyBtn").is_disabled()
        page.evaluate("""async () => {
            pendingPromos[0].resolve({code:'TEST', label:'старая скидка', kind:'percent', final_kopecks:100});
            await Promise.resolve();
        }""")
        assert page.locator("#buyBtn").is_disabled()
        page.evaluate("""async () => {
            pendingPromos[1].resolve({code:'TEST', label:'новая скидка', kind:'percent', final_kopecks:18000});
            await Promise.resolve();
        }""")
        assert page.locator("#buyBtn").is_enabled()
        assert "180 ₽" in page.locator("#totalPrice").inner_text()
        assert "новая скидка" in page.locator("#promoHint").inner_text()
        page.locator('button[data-plan="a"]').click()
        page.locator("#promoCode").press("Enter")
        assert page.locator("#buyBtn").is_disabled()
        page.locator('button[data-plan="b"]').click()
        assert page.locator("#buyBtn").is_enabled()
        page.evaluate("""async () => {
            pendingPromos[2].reject(Object.assign(new Error('старый ответ'),
                {status:409, payload:{plan_id:'a'}}));
            await Promise.resolve();
        }""")
        assert page.locator('button[data-plan="b"]').get_attribute("aria-pressed") == "true"
        assert "200 ₽" in page.locator("#totalPrice").inner_text()
        assert "старый ответ" not in page.locator("#err").inner_text()
        page.locator("#promoCode").press("Enter")
        assert page.locator("#buyBtn").is_disabled()
        page.locator("#promoCode").fill("")
        assert page.locator("#buyBtn").is_enabled()
        page.locator("#promoCode").press("Enter")
        assert page.locator("#buyBtn").is_enabled()
        page.evaluate("""async () => {
            pendingPromos[3].resolve({code:'TEST', label:'устарело', kind:'percent', final_kopecks:100});
            await Promise.resolve();
        }""")
        assert "200 ₽" in page.locator("#totalPrice").inner_text()
        page.locator("#promoCode").fill("TEST")
        page.locator("#promoCode").press("Enter")
        page.evaluate("""async () => {
            pendingPromos[4].reject(Object.assign(new Error('другой тариф'),
                {status:409, payload:{plan_id:'a'}}));
            await Promise.resolve();
        }""")
        assert page.evaluate("pendingPromos[5].payload.plan_id") == "a"
        assert page.locator("#buyBtn").is_disabled()
        page.evaluate("""async () => {
            pendingPromos[5].reject(new Error('проверка не удалась'));
            await Promise.resolve();
        }""")
        assert page.locator("#buyBtn").is_enabled()
        assert "проверка не удалась" in page.locator("#err").inner_text()
        page.evaluate("() => { buy(); }")
        assert page.locator("#buyBtn").is_disabled()
        page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        assert page.locator("#buyBtn").is_disabled()
        page.evaluate("""async () => {
            pendingPromos[6].reject(new Error('оплата не удалась'));
            await Promise.resolve();
        }""")
        assert page.locator("#buyBtn").is_enabled()
        page.evaluate("""() => {
            buyPending = true; buyNavigationPending = true; updateBuyState();
            window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: false}));
        }""")
        assert page.locator("#buyBtn").is_disabled()
        page.evaluate("() => window.dispatchEvent(new PageTransitionEvent('pageshow', {persisted: true}))")
        assert page.locator("#buyBtn").is_enabled()
        browser.close()
