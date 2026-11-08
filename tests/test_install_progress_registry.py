from concurrent.futures import ThreadPoolExecutor
from uuid import uuid4

import pytest

from install_progress import InstallProgress


def test_concurrent_starts_accept_only_one():
    registry = InstallProgress()
    ids = [str(uuid4()) for _ in range(20)]
    with ThreadPoolExecutor(max_workers=20) as pool:
        results = list(pool.map(lambda op: registry.begin(op, 0, 'awg2'), ids))
    assert results.count('accepted') == 1
    assert results.count('busy') == 19


def test_replay_conflict_and_success_require_persistence():
    registry = InstallProgress()
    op = str(uuid4())
    assert registry.begin(op, 0, 'awg2') == 'accepted'
    assert registry.begin(op, 0, 'awg2') == 'replay'
    assert registry.begin(op, 1, 'awg2') == 'conflict'
    with pytest.raises(ValueError):
        registry.finish(op, 'succeeded')
    assert registry.phase(op, 'committing')
    assert registry.finish(op, 'succeeded', protocol_saved=True)
    assert registry.get(op, 0)['state'] == 'succeeded'
    assert registry.begin(op, 0, 'awg2') == 'replay'
    assert registry.get(op, 1) == {'state': 'unknown'}


def test_unknown_and_whitelisted_fields():
    registry = InstallProgress()
    op = str(uuid4())
    assert registry.get(op, 0) == {'state': 'unknown'}
    registry.begin(op, 0, 'awg3')
    with pytest.raises(ValueError):
        registry.phase(op, 'synthetic-secret-value')
    assert set(registry.get(op, 0)) == {
        'operation_id', 'server_id', 'protocol', 'state', 'phase',
        'protocol_saved', 'elapsed_seconds'}


def test_terminal_records_are_bounded_and_late_updates_are_ignored():
    registry = InstallProgress(maximum=1)
    first, second = str(uuid4()), str(uuid4())
    registry.begin(first, 0, 'awg')
    registry.finish(first, 'uncertain')
    assert registry.begin(second, 0, 'awg') == 'accepted'
    assert registry.get(first, 0) == {'state': 'unknown'}
    assert not registry.finish(first, 'succeeded', protocol_saved=True)
