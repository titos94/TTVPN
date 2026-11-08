"""JSON writers must not roll back a commit made while they wait on the network.

Every handler covered here loads data.json, performs a slow SSH or certificate
step and then persists again. Saving that pre-network snapshot as a whole loses
anything committed in between (a payment, a traffic counter, a device row), so
the commit has to be a short re-read + targeted change under DATA_LOCK.

The slow step is parked on a real thread barrier, a parallel writer commits
through DATA_LOCK, and the test asserts the parallel change survived. Nothing
touches a network or a VPN node: SSH and managers are doubles.
"""

import asyncio
import json
import threading
from types import SimpleNamespace

import pytest

import app as panel
import peer_lifecycle_lock


class BlockingSsh:
    """SSH double: connect() parks until the test releases it."""

    def __init__(self):
        self.started = threading.Event()
        self.release = threading.Event()
        self.disconnected = False

    def connect(self):
        self.started.set()
        if not self.release.wait(5):
            raise TimeoutError('synthetic SSH barrier')

    def disconnect(self):
        self.disconnected = True


class FakeManager:
    """Protocol manager double: records the call, no remote side effects."""

    def __init__(self):
        self.calls = []

    def check_docker_installed(self):
        return True

    def update_credentials(self, port=None, username=None, password=None):
        self.calls.append(('update_credentials', port))
        return {'status': 'success', 'port': port or 1080}

    def add_client(self, *args, **kwargs):
        self.calls.append(('add_client', args[:2]))
        return {'client_id': 'client-new'}

    def remove_container(self, *args, **kwargs):
        self.calls.append(('remove_container', args))
        return {'status': 'success'}

    def get_server_status(self, protocol):
        return {'container_exists': True, 'port': '55555'}


class FakeAwgManager:
    """AWGManager double used by /check and /awg/settings/save."""

    def __init__(self, ssh, protocol=None):
        self.ssh = ssh
        self.protocol = protocol

    def prefetch_awg_state(self, protocols):
        return {}

    def exit_link_status(self, protocol):
        return {'up': True}

    def update_awg_settings(self, protocol, mtu=None, dns=None, special_junk=None, dns6=None):
        return {'mtu': mtu if mtu is not None else 1280, 'dns': dns, 'dns6': dns6}


def seed_data():
    return {
        'servers': [{
            'uid': 'server-test',
            'host': 'node.example.test',
            'ssh_port': 22,
            'username': 'fixture',
            'protocols': {'awg': {'installed': True, 'port': '55555'}},
        }],
        'users': [{'id': 'user-test', 'traffic_used': 1}],
        'user_connections': [],
    }


def read_data():
    with open(panel.DATA_FILE, encoding='utf-8') as source:
        return json.load(source)


async def commit_in_parallel(mutate):
    """A concurrent writer: the same short read-change-save cycle under DATA_LOCK."""
    async with panel.DATA_LOCK:
        data = panel.load_data()
        mutate(data)
        panel.save_data(data)


@pytest.fixture
def env(tmp_path, monkeypatch):
    data_file = tmp_path / 'data.json'
    monkeypatch.setattr(panel, 'DATA_FILE', str(data_file))
    monkeypatch.setattr(panel, 'DATA_LOCK', asyncio.Lock())
    monkeypatch.setattr(panel, '_check_admin', lambda request: True)
    monkeypatch.setattr(peer_lifecycle_lock, '_fence_path', tmp_path / 'fence.json')
    monkeypatch.setattr(peer_lifecycle_lock, '_fenced', False)
    panel.save_data(seed_data())

    ssh = BlockingSsh()
    manager = FakeManager()
    monkeypatch.setattr(panel, 'get_ssh', lambda server: ssh)
    monkeypatch.setattr(panel, 'get_protocol_manager', lambda ssh_, protocol: manager)
    monkeypatch.setattr(panel, 'AWGManager', FakeAwgManager)
    return SimpleNamespace(ssh=ssh, manager=manager, request=SimpleNamespace(headers={}))


def run(coroutine):
    return asyncio.run(coroutine)


def test_check_does_not_roll_back_parallel_user_update(env):
    """/check derives protocol metadata remotely and must commit only that."""
    request = env.request

    async def scenario():
        task = asyncio.create_task(panel.api_check_server(request, 0))
        try:
            assert await asyncio.to_thread(env.ssh.started.wait, 5)
            await commit_in_parallel(
                lambda data: data['users'][0].__setitem__('traffic_used', 999))
        finally:
            env.ssh.release.set()
        return await task

    status = run(scenario())
    assert status['connection'] == 'ok'
    data = read_data()
    assert data['users'][0]['traffic_used'] == 999
    # The metadata the check actually derived is still persisted.
    assert data['servers'][0]['protocols']['xray']['container_name'] == 'amnezia-xray'
    assert data['servers'][0]['protocols']['xray']['port'] == '55555'


def test_socks5_credentials_do_not_roll_back_parallel_payment(env):
    req = SimpleNamespace(protocol='socks5', port=1081, username='u', password='p')

    async def scenario():
        task = asyncio.create_task(
            panel.api_socks5_update_credentials(env.request, 0, req))
        try:
            assert await asyncio.to_thread(env.ssh.started.wait, 5)
            await commit_in_parallel(
                lambda data: data['users'][0].__setitem__('traffic_used', 555))
        finally:
            env.ssh.release.set()
        return await task

    result = run(scenario())
    assert result['status'] == 'success'
    data = read_data()
    assert data['users'][0]['traffic_used'] == 555
    assert data['servers'][0]['protocols']['socks5']['port'] == '1081'


def test_awg_settings_save_keeps_parallel_changes(env):
    req = SimpleNamespace(protocol='awg', mtu=1300, dns1='1.1.1.1', dns2=None,
                          dns6=None, i1=None, i2=None, i3=None, i4=None, i5=None)

    async def scenario():
        task = asyncio.create_task(panel.api_awg_settings_save(env.request, 0, req))
        try:
            assert await asyncio.to_thread(env.ssh.started.wait, 5)
            await commit_in_parallel(
                lambda data: data['users'][0].__setitem__('traffic_used', 777))
        finally:
            env.ssh.release.set()
        return await task

    result = run(scenario())
    assert result['status'] == 'success'
    data = read_data()
    assert data['users'][0]['traffic_used'] == 777
    assert data['servers'][0]['protocols']['awg']['mtu'] == 1300
    assert data['servers'][0]['protocols']['awg']['dns'] == '1.1.1.1'


def test_uninstall_keeps_parallel_connection_row(env):
    req = SimpleNamespace(protocol='awg')
    parallel_row = {'id': 'conn-parallel', 'user_id': 'user-test', 'server_id': 5,
                    'protocol': 'xray', 'client_id': 'elsewhere', 'name': 'other'}

    async def scenario():
        task = asyncio.create_task(panel.api_uninstall_protocol(env.request, 0, req))
        try:
            assert await asyncio.to_thread(env.ssh.started.wait, 5)
            await commit_in_parallel(
                lambda data: data['user_connections'].append(dict(parallel_row)))
        finally:
            env.ssh.release.set()
        return await task

    result = run(scenario())
    assert result == {'status': 'success'}
    data = read_data()
    assert 'awg' not in data['servers'][0]['protocols']
    # A row appended while the container was being removed must survive, and
    # only the rows of this server/protocol are purged.
    assert data['user_connections'] == [parallel_row]


def test_add_connection_keeps_parallel_connection_row(env):
    req = SimpleNamespace(protocol='socks5', name='laptop', user_id='user-test',
                          telemt_quota=None, telemt_max_ips=None, telemt_expiry=None,
                          telemt_secret=None, telemt_ad_tag=None, telemt_max_conns=None)
    parallel_row = {'id': 'conn-parallel', 'user_id': 'user-test', 'server_id': 5,
                    'protocol': 'xray', 'client_id': 'elsewhere', 'name': 'other'}

    async def scenario():
        task = asyncio.create_task(panel.api_add_connection(env.request, 0, req))
        try:
            assert await asyncio.to_thread(env.ssh.started.wait, 5)
            await commit_in_parallel(
                lambda data: data['user_connections'].append(dict(parallel_row)))
        finally:
            env.ssh.release.set()
        return await task

    result = run(scenario())
    assert result['client_id'] == 'client-new'
    rows = read_data()['user_connections']
    assert parallel_row in rows
    assert any(row.get('client_id') == 'client-new' for row in rows)
    assert len(rows) == 2


def test_two_handlers_committing_at_once_keep_both_changes(env):
    """Two handler commits racing after their SSH calls must not cancel out."""
    socks = SimpleNamespace(protocol='socks5', port=2088, username='u', password='p')
    awg = SimpleNamespace(protocol='awg', mtu=1420, dns1='9.9.9.9', dns2=None,
                          dns6=None, i1=None, i2=None, i3=None, i4=None, i5=None)

    async def scenario():
        first = asyncio.create_task(panel.api_socks5_update_credentials(env.request, 0, socks))
        try:
            assert await asyncio.to_thread(env.ssh.started.wait, 5)
            second = asyncio.create_task(panel.api_awg_settings_save(env.request, 0, awg))
            await asyncio.sleep(0)
        finally:
            env.ssh.release.set()
        return await asyncio.gather(first, second)

    run(scenario())
    protocols = read_data()['servers'][0]['protocols']
    assert protocols['socks5']['port'] == '2088'
    assert protocols['awg']['mtu'] == 1420


def test_wgeasy_import_keeps_parallel_user_update(env, monkeypatch):
    from managers import wgeasy_import as wgeasy

    class FakeImporter:
        def __init__(self, ssh, web_port=None):
            self.web_port = web_port

        def fetch_backup(self, password, username):
            return {'clients': []}

        def detect_source(self):
            return None, None, None, False

    monkeypatch.setattr(wgeasy, 'WgEasyImporter', FakeImporter)
    monkeypatch.setattr(
        wgeasy, 'run_import',
        lambda ssh, backup, client_ids=None, target=None, log=None:
            {'status': 'success', 'port': '51820'})
    req = SimpleNamespace(web_port=51821, username=None, password='pw',
                          target='wireguard', client_ids=None)

    async def scenario():
        task = asyncio.create_task(panel.api_wgeasy_import(env.request, 0, req))
        try:
            assert await asyncio.to_thread(env.ssh.started.wait, 5)
            await commit_in_parallel(
                lambda data: data['users'][0].__setitem__('traffic_used', 321))
        finally:
            env.ssh.release.set()
        return await task

    result = run(scenario())
    assert result['port'] == '51820'
    data = read_data()
    assert data['users'][0]['traffic_used'] == 321
    assert data['servers'][0]['protocols']['wireguard']['port'] == '51820'


def test_wgeasy_import_refuses_to_overwrite_a_slot_taken_meanwhile(env, monkeypatch):
    from managers import wgeasy_import as wgeasy

    class FakeImporter:
        def __init__(self, ssh, web_port=None):
            pass

        def fetch_backup(self, password, username):
            return {'clients': []}

        def detect_source(self):
            return None, None, None, False

    monkeypatch.setattr(wgeasy, 'WgEasyImporter', FakeImporter)
    monkeypatch.setattr(
        wgeasy, 'run_import',
        lambda ssh, backup, client_ids=None, target=None, log=None:
            {'status': 'success', 'port': '51820'})
    req = SimpleNamespace(web_port=51821, username=None, password='pw',
                          target='wireguard', client_ids=None)
    rival = {'installed': True, 'port': '11111', 'base_protocol': 'wireguard'}

    async def scenario():
        task = asyncio.create_task(panel.api_wgeasy_import(env.request, 0, req))
        try:
            assert await asyncio.to_thread(env.ssh.started.wait, 5)
            await commit_in_parallel(
                lambda data: data['servers'][0]['protocols'].__setitem__('wireguard', dict(rival)))
        finally:
            env.ssh.release.set()
        return await task

    response = run(scenario())
    assert response.status_code == 409
    assert json.loads(response.body)['code'] == 'protocol_conflict'
    assert read_data()['servers'][0]['protocols']['wireguard'] == rival
