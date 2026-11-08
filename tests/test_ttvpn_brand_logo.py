"""Тесты отображения логотипа сервиса в каркасе панели."""
import os
import tempfile
import unittest
from unittest import mock
from types import SimpleNamespace

import app as panel
from billing import branding


class BrandLogoTests(unittest.TestCase):
    def test_branding_current_ready_after_png_upload(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_file = os.path.join(tmp, "data.json")
            with open(data_file, "w", encoding="utf-8") as handle:
                handle.write("{}")
            with mock.patch.object(panel, "DATA_FILE", data_file):
                payload = b"\x89PNG\r\n\x1a\n" + b"\x00" * 8
                branding.save("logo.png", payload)
                state = branding.current()
                self.assertTrue(state["ready"])
                self.assertIn("/billing/brand/logo?v=", state["logo_url"])

    def test_base_template_has_brand_ready_branch_and_class(self):
        text = open("templates/base.html", encoding="utf-8").read()
        self.assertIn("{% if brand is defined and brand.ready %}", text)
        self.assertIn('class="tt-brand-logo"', text)

    def test_login_template_has_login_brand_logo(self):
        text = open("templates/login.html", encoding="utf-8").read()
        self.assertIn('class="login-brand-logo"', text)

    def test_settings_has_brand_upload_control(self):
        text = open("templates/settings.html", encoding="utf-8").read()
        self.assertIn('id="brandFile"', text)
        self.assertIn('/api/billing/admin/brand', text)
        self.assertIn('id="opFile"', text)

    def test_settings_provides_brand_csrf_for_admin(self):
        request = SimpleNamespace(session={})
        with mock.patch.object(panel, '_check_admin', return_value={'role': 'admin'}), \
             mock.patch.object(panel, 'load_data', return_value={'settings': {}, 'servers': []}), \
             mock.patch('billing.settings.get_settings', return_value=SimpleNamespace(
                 enabled=True, yookassa_shop_id='', as_dict=lambda: {'enabled': True})), \
             mock.patch.object(panel, 'tpl', side_effect=lambda request, template, **kwargs: kwargs):
            context = panel.settings_page(request)
        self.assertTrue(context['billing_csrf'])
        self.assertEqual(request.session['billing_csrf'], context['billing_csrf'])
        self.assertTrue(context['backup_csrf'])

    def test_css_brand_logo_has_no_frame(self):
        text = open("static/css/ttvpn-ui.css", encoding="utf-8").read()
        rule_start = text.index(".tt-brand-logo {")
        rule_end = text.index("}", rule_start)
        rule = text[rule_start:rule_end + 1]
        self.assertIn("background: transparent;", rule)
        self.assertIn("border: 0;", rule)
        self.assertIn("border-radius: 0;", rule)
        self.assertIn("box-shadow: none;", rule)

    def test_tpl_exposes_brand_without_logo_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            data_file = os.path.join(tmp, "data.json")
            with open(data_file, "w", encoding="utf-8") as handle:
                handle.write('{"settings": {"appearance": {"title": "TTVPN"}}}')
            with mock.patch.object(panel, "DATA_FILE", data_file):
                with mock.patch.object(panel, "load_data", return_value={"settings": {"appearance": {"title": "TTVPN"}}}):
                    ctx = {}
                    original = panel.templates.TemplateResponse
                    def capture(name, context):
                        ctx.update(context)
                        return original(name, context)
                    with mock.patch.object(panel.templates, "TemplateResponse", side_effect=capture):
                        panel.tpl(mock.Mock(cookies={}), "base.html")
                    self.assertIn("brand", ctx)
                    self.assertFalse(ctx["brand"].get("ready", True))


if __name__ == "__main__":
    unittest.main()
