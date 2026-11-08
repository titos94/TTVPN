"""Unit tests for pwa.build_manifest() and on-disk icon assets."""
import unittest
from pathlib import Path

from PIL import Image

from pwa import build_manifest

ROOT = Path(__file__).resolve().parents[1]
ICONS = ROOT / 'static' / 'icons'


class TestBuildManifest(unittest.TestCase):
    def test_required_keys_and_defaults(self):
        m = build_manifest({}, 'en')
        self.assertEqual(m['name'], 'Amnezia Web Panel')
        self.assertEqual(m['short_name'], 'Amnezia')
        self.assertEqual(m['start_url'], '/')
        self.assertEqual(m['scope'], '/')
        self.assertEqual(m['display'], 'standalone')
        self.assertEqual(m['background_color'], '#0a0a0f')
        self.assertEqual(m['theme_color'], '#0a0a0f')
        self.assertEqual(m['dir'], 'ltr')
        self.assertIn('icons', m)
        self.assertIn('shortcuts', m)
        urls = {s['url'] for s in m['shortcuts']}
        self.assertEqual(urls, {'/my', '/users'})

    def test_custom_title_subtitle(self):
        m = build_manifest({'title': 'MyVPN', 'subtitle': 'Admin'}, 'en')
        self.assertEqual(m['short_name'], 'MyVPN')
        self.assertEqual(m['name'], 'MyVPN Admin')

    def test_rtl_for_fa(self):
        m = build_manifest({'title': 'Amnezia'}, 'fa')
        self.assertEqual(m['dir'], 'rtl')

    def test_icon_files_exist_at_declared_sizes(self):
        m = build_manifest({}, 'en')
        self.assertGreaterEqual(len(m['icons']), 4)
        for icon in m['icons']:
            src = icon['src']
            self.assertTrue(src.startswith('/static/icons/'), src)
            path = ROOT / src.lstrip('/')
            self.assertTrue(path.is_file(), f'missing icon {path}')
            w, h = (int(x) for x in icon['sizes'].split('x'))
            with Image.open(path) as im:
                self.assertEqual(im.size, (w, h), path.name)
            self.assertIn(icon['purpose'], ('any', 'maskable'))

    def test_uploaded_brand_icon_keeps_safe_png_fallbacks(self):
        brand = {'ready': True, 'icon_url': '/billing/brand/icon?v=42', 'version': 42}
        m = build_manifest({'title': 'TTVPN'}, 'ru', brand)
        self.assertEqual(m['icons'][0], {'src': '/billing/brand/icon?v=42', 'purpose': 'any'})
        self.assertTrue(any(i['src'] == '/static/icons/icon-512.png' for i in m['icons']))
        self.assertTrue(any(i['src'] == '/static/icons/maskable-512.png' for i in m['icons']))

    def test_brand_without_upload_keeps_static_icons(self):
        for brand in (None, {}, {'ready': False, 'icon_url': ''}):
            m = build_manifest({}, 'en', brand)
            for icon in m['icons']:
                self.assertTrue(icon['src'].startswith('/static/icons/'), icon)

    def test_apple_touch_icon_exists(self):
        path = ICONS / 'apple-touch-icon-180.png'
        self.assertTrue(path.is_file())
        with Image.open(path) as im:
            self.assertEqual(im.size, (180, 180))


if __name__ == '__main__':
    unittest.main()
