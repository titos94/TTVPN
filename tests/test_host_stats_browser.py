"""Unavailable host probes must not look like successful zero readings."""
import os
from pathlib import Path
import re
import unittest


class HostStatsBrowserTests(unittest.TestCase):
    def test_partial_statistics_keep_valid_fields_visible(self):
        from playwright.sync_api import sync_playwright
        source = (Path(__file__).resolve().parents[1] / 'templates/server.html').read_text(encoding='utf8')
        function = re.search(r'    async function loadServerStats\(.*?\n    }', source, re.S)[0]
        with sync_playwright() as pw:
            options = {'executable_path': os.environ['PANEL_BROWSER_EXECUTABLE']} if os.environ.get('PANEL_BROWSER_EXECUTABLE') else {}
            browser = pw.chromium.launch(headless=True, **options)
            try:
                page = browser.new_page()
                page.set_content('<div id="serverStats"></div><div id="serverIpv6"></div><script>'
                    'const SERVER_ID = 0; const formatBytes = n => String(n || 0);'
                    'let stats = {}; const apiCall = async () => stats;'
                    # Icons are SVG constants defined earlier in the page script;
                    # here they only need to be present so the pills render.
                    "const STAT_ICONS = {cpu: 'cpu', ram: 'ram', disk: 'disk', net: 'net', gateway: 'gw'};"
                    + function + '</script>')
                page.evaluate('''async () => {
                    stats = {cpu: 0, ram_percent: 25, disk_percent: 60, net_rx: 0, net_tx: 0,
                             errors: {cpu: 'unavailable', network: 'unavailable'}};
                    await loadServerStats();
                }''')
                def pill(field):
                    return page.locator(f'#serverStats [data-stat="{field}"]').inner_text().split()[-1]

                # A failed probe must read as unknown, never as a healthy zero.
                self.assertEqual(pill('cpu'), '—')
                self.assertEqual(pill('net'), '—')
                self.assertEqual(pill('ram'), '25%')
                self.assertEqual(pill('disk'), '60%')
                text = page.locator('#serverStats').inner_text()
                self.assertNotIn('0%', text.replace('60%', ''))
                page.evaluate('stats = {cpu: 0, ram_percent: 0, disk_percent: 0}; loadServerStats()')
                self.assertEqual(pill('cpu'), '0%')
            finally:
                browser.close()
