from concurrent.futures import ThreadPoolExecutor

from billing.store import reserve_assistant_request


def test_quota_per_user_and_day(tmp_path):
    db = str(tmp_path / 'billing.db')
    reserve = lambda user, day='2026-11-08': reserve_assistant_request(
        db, user, day=day, user_limit=2, global_limit=3)
    assert reserve('a') and reserve('a')
    assert not reserve('a')
    assert reserve('b')
    assert not reserve('c')
    assert reserve('a', '2026-11-09')


def test_global_limit_is_atomic_under_concurrency(tmp_path):
    db = str(tmp_path / 'billing.db')
    def reserve(index):
        return reserve_assistant_request(db, str(index), day='2026-11-08',
                                         user_limit=2, global_limit=5)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(reserve, range(20)))
    assert sum(results) == 5
