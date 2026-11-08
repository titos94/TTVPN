"""Read-only overview contract, without starting the app or connecting nodes."""
import ast
from pathlib import Path
from types import SimpleNamespace

import pytest


def load_view(user, data, loader=None):
    tree = ast.parse((Path(__file__).resolve().parents[1] / 'app.py').read_text())
    view = next(node for node in tree.body
                if isinstance(node, ast.FunctionDef) and node.name == 'overview_preview')
    view.decorator_list = []
    view.args.args[0].annotation = None
    module = ast.fix_missing_locations(ast.Module(body=[view], type_ignores=[]))
    class Rendered:
        def __init__(self, template, values):
            self.template, self.values, self.headers = template, values, {}

    context = {
        'get_current_user': lambda request: user,
        'load_data': loader or (lambda: data),
        'RedirectResponse': lambda **kw: ('redirect', kw),
        'HTMLResponse': lambda content, status_code: ('error', status_code),
        'tpl': lambda request, template, **kw: Rendered(template, kw),
        'logger': SimpleNamespace(exception=lambda *args: None),
    }
    exec(compile(module, 'app.py', 'exec'), context)
    return context['overview_preview'](object())


def test_permissions():
    assert load_view(None, {})[0] == 'redirect'
    assert load_view({'role': 'user'}, {}) == ('error', 403)
    assert load_view({'role': 'none'}, {}) == ('error', 403)


def test_aggregates_exclude_secrets_and_do_not_claim_connections_are_devices():
    data = {'users': [{'role': 'admin'}, {'role': 'user'}],
            'servers': [{'name': 'FRA', 'host': 'example.test', 'password': 'secret'}],
            'user_connections': [{'id': 'configuration'}]}
    for role in ('admin', 'support'):
        response = load_view({'role': role}, data)
        assert response.template == 'ttvpn_overview.html'
        assert response.headers['Cache-Control'] == 'no-store'
        result = response.values
        overview = result['overview']
        assert overview['users'] == 1 and overview['servers'] == 1
        assert overview['devices'] is None and overview['revenue'] is None
        assert overview['server_rows'] == [{'name': 'FRA', 'host': 'example.test'}]
        assert 'secret' not in repr(result)


def test_template_renders_admin_and_support_without_fake_actions():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    root = Path(__file__).resolve().parents[1]
    env = Environment(loader=FileSystemLoader(root / 'templates'),
                      autoescape=select_autoescape(['html']))
    template = env.get_template('ttvpn_overview.html')
    data = {'users': 1, 'devices': None, 'servers': 1, 'subscriptions': None,
            'revenue': None, 'server_rows': [{'name': '<Node>', 'host': 'example.test'}]}
    for role in ('admin', 'support'):
        html = template.render(overview=data, brand={'ready': False},
                               current_user={'role': role, 'username': 'test'},
                               site_settings={}, request=SimpleNamespace(url=SimpleNamespace(path='/overview-preview')),
                               lang='ru', static_v='1', _=lambda key: key,
                               translations_json='{}')
        assert '&lt;Node&gt;' in html and 'Не проверено' in html
        assert ('href="/servers/new"' in html) == (role == 'admin')
        assert 'Нет данных' in html


def test_template_preserves_uploaded_logo_proportions_and_escapes_url():
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    root = Path(__file__).resolve().parents[1]
    env = Environment(loader=FileSystemLoader(root / 'templates'),
                      autoescape=select_autoescape(['html']))
    html = env.get_template('ttvpn_overview.html').render(
        overview={'users': 0, 'devices': None, 'servers': 0, 'subscriptions': None,
                  'revenue': None, 'server_rows': []},
        brand={'ready': True, 'logo_url': '/billing/brand/logo?v=1&x=2'},
        current_user={'role': 'admin', 'username': 'test'}, site_settings={},
        request=SimpleNamespace(url=SimpleNamespace(path='/overview-preview')),
        lang='ru', static_v='1', _=lambda key: key, translations_json='{}')
    assert '/billing/brand/logo?v=1&amp;x=2' in html
    css = (root / 'static/css/ttvpn-shell.css').read_text()
    assert 'object-fit: contain' in css


def render_overview(overview, role='admin', billing_available=True):
    from jinja2 import Environment, FileSystemLoader, select_autoescape

    root = Path(__file__).resolve().parents[1]
    env = Environment(loader=FileSystemLoader(root / 'templates'),
                      autoescape=select_autoescape(['html']))
    return env.get_template('ttvpn_overview.html').render(
        overview=overview, brand={'ready': False}, billing_available=billing_available,
        current_user={'role': role, 'username': 'test'}, site_settings={},
        request=SimpleNamespace(url=SimpleNamespace(path='/overview-preview')),
        lang='ru', static_v='1', _=lambda key: key, translations_json='{}')


def test_billing_navigation_hidden_when_module_unavailable():
    overview = {'users': 0, 'devices': None, 'servers': 0, 'subscriptions': None,
                'revenue': None, 'server_rows': []}
    html = render_overview(overview, billing_available=False)
    # The inherited base shell has its own hidden navigation; check only our shell.
    shell = html.split('<div class="tt-overview"', 1)[1].split('</main>', 1)[0]
    assert 'href="/billing-admin"' not in shell


@pytest.mark.parametrize('user, expected', [
    (None, ('redirect', {'url': '/login', 'status_code': 302})),
    ({'role': 'user'}, ('error', 403)),
    ({'role': 'unknown'}, ('error', 403)),
    ({'username': 'missing-role'}, ('error', 403)),
])
def test_denied_requests_never_load_data(user, expected):
    def forbidden_load():
        pytest.fail('unauthorized request loaded private data')

    assert load_view(user, {}, loader=forbidden_load) == expected


@pytest.mark.parametrize('role', ['admin', 'support'])
def test_empty_and_missing_server_fields(role):
    empty = load_view({'role': role}, {})
    assert empty.headers['Cache-Control'] == 'no-store'
    assert empty.values['overview'] == {
        'users': 0, 'devices': None, 'servers': 0, 'subscriptions': None,
        'revenue': None, 'server_rows': [],
    }
    html = render_overview(empty.values['overview'], role)
    assert 'class="tt-empty" colspan="3">Нет данных' in html
    assert 'href="/server/' not in html

    result = load_view({'role': role}, {'servers': [{}, {'host': 'host.test'},
                                                    {'name': '', 'host': ''}]})
    assert result.values['overview']['server_rows'] == [
        {'name': 'Сервер', 'host': '—'},
        {'name': 'host.test', 'host': 'host.test'},
        {'name': 'Сервер', 'host': '—'},
    ]


@pytest.mark.parametrize('role', ['admin', 'support'])
def test_private_fields_never_reach_overview_or_rendered_html(role):
    secrets = ['ssh-password-unique', 'private-key-unique', 'token-unique',
               'user-password-unique', 'connection-secret-unique']
    data = {
        'users': [{'role': 'user', 'password': secrets[3]}, {'role': 'support'}],
        'servers': [{'name': 'Public', 'host': 'public.test', 'password': secrets[0],
                     'private_key': secrets[1], 'token': secrets[2]}],
        'user_connections': [{'config': secrets[4]}],
    }
    response = load_view({'role': role}, data)
    assert response.values['overview']['users'] == 1
    assert response.values['overview']['server_rows'] == [
        {'name': 'Public', 'host': 'public.test'}]
    html = render_overview(response.values['overview'], role)
    for secret in secrets:
        assert secret not in repr(response.values)
        assert secret not in html


@pytest.mark.parametrize('role', ['admin', 'support'])
def test_links_controls_and_escaped_server_values(role):
    overview = {'users': 0, 'devices': None, 'servers': 1, 'subscriptions': None,
                'revenue': None, 'server_rows': [
                    {'name': '<script>alert("x")</script>',
                     'host': 'host" onmouseover="evil'}]}
    html = render_overview(overview, role)
    assert '<script>alert("x")</script>' not in html
    assert '&lt;script&gt;alert(&#34;x&#34;)&lt;/script&gt;' in html
    assert 'host&#34; onmouseover=&#34;evil' in html
    for href in ('/overview-preview', '/', '/users', '/my', '/logout'):
        assert f'href="{href}"' in html
    assert 'href="/server/' not in html
    table = html.split('<table class="tt-table">', 1)[1].split('</table>', 1)[0]
    assert table.count('<th scope="col">') == 3
    assert table.count('<td>') == 3
    for href in ('/servers/new', '/billing-admin', '/settings'):
        assert (f'href="{href}"' in html) == (role == 'admin')
    assert 'aria-pressed="false"' in html
    assert 'aria-label="Переключить тему"' in html


@pytest.mark.parametrize('server_id', [None, '', 0, 1, 42])
def test_server_id_never_creates_unsafe_index_link(server_id):
    overview = {'users': 0, 'devices': None, 'servers': 1, 'subscriptions': None,
                'revenue': None, 'server_rows': [{'id': server_id, 'name': '', 'host': None}]}
    html = render_overview(overview)
    assert 'href="/server/' not in html
    assert html.count('Нет данных') >= 5
