"""PWA web app manifest builder.

Pure functions only — kept free of app.py imports so unit tests can exercise
manifest shape without loading the full FastAPI application.
"""


def build_manifest(site_settings, lang, brand=None):
    """Return a Web App Manifest dict for the given appearance settings and language.

    ``site_settings`` is typically ``data['settings']['appearance']`` (title,
    subtitle, logo). Defaults match the appearance seed in ``load_data()``.

    ``brand`` is ``billing.branding.current()``. When the operator uploaded a
    logo, the installed app must use it: the static icons below are the fork's
    default shield, so a white-labelled panel would show somebody else's mark
    on the home screen. The fallback stays ``/static/icons/`` so a stock
    install still resolves to real files.
    """
    settings = site_settings or {}
    title = (settings.get('title') or 'Amnezia').strip() or 'Amnezia'
    subtitle = (settings.get('subtitle') or 'Web Panel').strip() or 'Web Panel'
    name = f'{title} {subtitle}'.strip()

    brand = brand or {}
    brand_icon = brand.get('icon_url') if brand.get('ready') else None
    icons = [
            {
                'src': '/static/icons/icon-192.png',
                'sizes': '192x192',
                'type': 'image/png',
                'purpose': 'any',
            },
            {
                'src': '/static/icons/icon-512.png',
                'sizes': '512x512',
                'type': 'image/png',
                'purpose': 'any',
            },
            {
                'src': '/static/icons/maskable-192.png',
                'sizes': '192x192',
                'type': 'image/png',
                'purpose': 'maskable',
            },
            {
                'src': '/static/icons/maskable-512.png',
                'sizes': '512x512',
                'type': 'image/png',
                'purpose': 'maskable',
            },
        ]
    if brand_icon:
        # The brand endpoint can return SVG or GIF instead of PNG. Do not
        # invent a MIME type, size, or maskable safe zone for uploaded art.
        # Keep the known-size PNGs as fallbacks for installability.
        icons.insert(0, {'src': brand_icon, 'purpose': 'any'})

    return {
        'name': name,
        'short_name': title,
        'description': name,
        'start_url': '/',
        'scope': '/',
        'display': 'standalone',
        'background_color': '#0a0a0f',
        'theme_color': '#0a0a0f',
        'dir': 'rtl' if lang == 'fa' else 'ltr',
        'lang': lang or 'en',
        'icons': icons,
        'shortcuts': [
            {
                'name': 'Connections',
                'short_name': 'Connections',
                'url': '/my',
                'icons': [{'src': '/static/icons/icon-192.png', 'sizes': '192x192'}],
            },
            {
                'name': 'Users',
                'short_name': 'Users',
                'url': '/users',
                'icons': [{'src': '/static/icons/icon-192.png', 'sizes': '192x192'}],
            },
        ],
    }
