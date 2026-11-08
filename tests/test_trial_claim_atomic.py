"""Concurrent trial claims must not grant multiple subscriptions or bypass IP caps."""
from concurrent.futures import ThreadPoolExecutor

from billing import store


def test_one_claim_per_account_under_concurrency(tmp_path):
    db = str(tmp_path / "billing.db")
    store.init_db(db)
    def claim(_):
        return store.claim_trial(db, user_id="user", days=10, expires_at="2099-01-01",
                                 ip="192.0.2.1", since_iso="2000-01-01", ip_limit=5)
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(claim, range(10)))
    assert results.count("claimed") == 1
    assert results.count("used") == 9


def test_ip_cap_is_atomic_across_accounts(tmp_path):
    db = str(tmp_path / "billing.db")
    store.init_db(db)
    def claim(index):
        return store.claim_trial(db, user_id=f"user-{index}", days=10,
                                 expires_at="2099-01-01", ip="192.0.2.2",
                                 since_iso="2000-01-01", ip_limit=2)
    with ThreadPoolExecutor(max_workers=10) as executor:
        results = list(executor.map(claim, range(10)))
    assert results.count("claimed") == 2
    assert results.count("ip_limit") == 8
