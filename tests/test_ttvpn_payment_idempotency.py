"""Идемпотентность начисления оплаченного периода (billing/store.py).

Проверяется атомарное право на начисление: два одновременных уведомления
ЮKassa об одном платеже не должны начислить дни дважды, повтор уже
применённого платежа не начисляет ничего, а упавший обработчик не блокирует
заказ навсегда (аренда права истекает, явный откат разрешает повтор).
"""

import sqlite3
import threading
from datetime import datetime, timedelta, timezone

import pytest

from billing import store

MOMENT = datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

# Схема payments до появления состояния обработки: ровно то, что лежит в
# установках прошлых версий.
LEGACY_SCHEMA = """
CREATE TABLE payments (
    id                  TEXT PRIMARY KEY,
    user_id             TEXT NOT NULL,
    plan_id             TEXT NOT NULL,
    provider            TEXT NOT NULL,
    provider_payment_id TEXT UNIQUE,
    idempotency_key     TEXT UNIQUE NOT NULL,
    amount_kopecks      INTEGER NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'RUB',
    status              TEXT NOT NULL DEFAULT 'pending',
    confirmation_url    TEXT,
    applied             INTEGER NOT NULL DEFAULT 0,
    created_at          TEXT NOT NULL,
    paid_at             TEXT,
    raw                 TEXT
);
"""


def _now(offset_seconds=0):
    return (MOMENT + timedelta(seconds=offset_seconds)).isoformat()


def _db(tmp_path):
    path = str(tmp_path / "billing.db")
    store.init_db(path)
    return path


def _payment(db, *, status="succeeded", key="key-1"):
    payment = store.create_payment(
        db, user_id="u1", plan_id="p1", provider="yookassa",
        amount_kopecks=19900, currency="RUB", idempotency_key=key,
    )
    if status:
        assert store.mark_status(db, payment["id"], status) is True
    return payment


def _legacy_db(path):
    """База прошлой версии: колонок состояния обработки ещё нет."""
    conn = sqlite3.connect(path)
    try:
        conn.executescript(LEGACY_SCHEMA)
        conn.executemany(
            "INSERT INTO payments (id, user_id, plan_id, provider, idempotency_key,"
            " amount_kopecks, currency, status, applied, created_at, paid_at)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            [
                # Платёж, начисленный прошлой версией: дни уже выданы.
                ("granted", "u1", "p1", "yookassa", "key-granted", 19900, "RUB",
                 "succeeded", 1, "2025-01-01T00:00:00+00:00",
                 "2025-01-01T00:00:00+00:00"),
                # Оплачен, но ещё не начислен.
                ("waiting", "u2", "p1", "yookassa", "key-waiting", 19900, "RUB",
                 "succeeded", 0, "2025-01-02T00:00:00+00:00",
                 "2025-01-02T00:00:00+00:00"),
            ],
        )
        conn.commit()
    finally:
        conn.close()


def _columns(path):
    conn = sqlite3.connect(path)
    try:
        return {row[1] for row in conn.execute("PRAGMA table_info(payments)")}
    finally:
        conn.close()


# ------------------------------------------------------------- одна победа


def test_simultaneous_claims_have_exactly_one_winner(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)
    workers = 8
    barrier = threading.Barrier(workers)
    results, errors = [], []
    lock = threading.Lock()

    def worker():
        barrier.wait(timeout=10)
        try:
            claim = store.claim_payment_application(db, payment["id"], now=_now())
        except Exception as exc:  # noqa: BLE001 - падение должно быть видно
            with lock:
                errors.append(exc)
            return
        with lock:
            results.append(claim)

    threads = [threading.Thread(target=worker) for _ in range(workers)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(30)

    assert not errors, errors
    assert len(results) == workers
    granted = [claim for claim in results if claim.status == store.CLAIM_GRANTED]
    assert len(granted) == 1, "право на начисление должно достаться одному"
    assert {claim.status for claim in results if claim.status != store.CLAIM_GRANTED} == {
        store.CLAIM_IN_PROGRESS
    }
    assert granted[0].token

    row = store.get_payment(db, payment["id"])
    assert row["applied_state"] == store.APPLIED_IN_PROGRESS
    assert row["applied_claim_token"] == granted[0].token
    assert row["applied"] == 1  # старый флаг: «начисляется или начислено»
    assert row["applied_at"] is None


def test_second_claim_while_first_is_running_is_not_granted(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)

    first = store.claim_payment_application(db, payment["id"], now=_now())
    second = store.claim_payment_application(db, payment["id"], now=_now(1))

    assert first.status == store.CLAIM_GRANTED
    assert second.status == store.CLAIM_IN_PROGRESS
    assert second.token == ""
    assert store.get_payment(db, payment["id"])["applied_claim_token"] == first.token


# ------------------------------------------------------- повтор и финализация


def test_replay_after_grant_does_not_claim_the_days_again(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)

    claim = store.claim_payment_application(db, payment["id"], now=_now())
    assert claim.status == store.CLAIM_GRANTED
    assert store.confirm_payment_applied(db, payment["id"], claim.token, now=_now(1)) is True

    replay = store.claim_payment_application(db, payment["id"], now=_now(600))
    assert replay.status == store.CLAIM_ALREADY_APPLIED
    assert replay.token == ""

    row = store.get_payment(db, payment["id"])
    assert row["applied_state"] == store.APPLIED_APPLIED
    assert row["applied"] == 1
    assert row["applied_at"] == _now(1)
    assert row["applied_claim_token"] is None
    # Закрытое право нельзя подтвердить повторно.
    assert store.confirm_payment_applied(db, payment["id"], claim.token, now=_now(2)) is False


def test_foreign_or_empty_token_cannot_finish_the_grant(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)

    claim = store.claim_payment_application(db, payment["id"], now=_now())
    assert store.confirm_payment_applied(db, payment["id"], "") is False
    assert store.confirm_payment_applied(db, payment["id"], "not-a-token") is False

    row = store.get_payment(db, payment["id"])
    assert row["applied_state"] == store.APPLIED_IN_PROGRESS
    assert row["applied_at"] is None


def test_granted_order_is_retried_for_devices_like_before(tmp_path):
    """Старый флаг applied остаётся совместимым с повторным провижинингом."""
    db = _db(tmp_path)
    payment = _payment(db)
    claim = store.claim_payment_application(db, payment["id"], now=_now())

    pending = store.last_unprovisioned_payment(db, "u1")
    assert pending is not None and pending["id"] == payment["id"]

    assert store.confirm_payment_applied(db, payment["id"], claim.token) is True
    store.mark_provisioned(db, payment["id"])
    assert store.last_unprovisioned_payment(db, "u1") is None


# --------------------------------------------------- откат и истечение аренды


def test_failed_handler_releases_the_claim_for_an_immediate_retry(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)

    first = store.claim_payment_application(db, payment["id"], now=_now())
    assert store.release_payment_claim(db, payment["id"], first.token) is True

    released = store.get_payment(db, payment["id"])
    assert released["applied_state"] == store.APPLIED_FAILED
    assert released["applied"] == 0
    assert released["applied_claim_token"] is None
    assert store.last_unprovisioned_payment(db, "u1") is None

    second = store.claim_payment_application(db, payment["id"], now=_now(2))
    assert second.status == store.CLAIM_GRANTED
    assert second.token != first.token
    # Устаревшее право не может завершить начисление.
    assert store.confirm_payment_applied(db, payment["id"], first.token) is False
    assert store.confirm_payment_applied(db, payment["id"], second.token, now=_now(3)) is True


def test_release_with_a_stale_token_does_not_touch_an_owned_claim(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)

    first = store.claim_payment_application(db, payment["id"], now=_now())
    assert store.release_payment_claim(db, payment["id"], "not-a-token") is False
    assert store.release_payment_claim(db, payment["id"], "") is False
    assert store.get_payment(db, payment["id"])["applied_claim_token"] == first.token


def test_expired_claim_is_taken_over_instead_of_sticking_forever(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)

    first = store.claim_payment_application(db, payment["id"], ttl_seconds=60, now=_now())
    assert first.status == store.CLAIM_GRANTED

    still_owned = store.claim_payment_application(
        db, payment["id"], ttl_seconds=60, now=_now(59))
    assert still_owned.status == store.CLAIM_IN_PROGRESS

    takeover = store.claim_payment_application(
        db, payment["id"], ttl_seconds=60, now=_now(61))
    assert takeover.status == store.CLAIM_GRANTED
    assert takeover.token != first.token
    assert store.confirm_payment_applied(db, payment["id"], first.token) is False
    assert store.confirm_payment_applied(db, payment["id"], takeover.token) is True

    row = store.get_payment(db, payment["id"])
    assert row["applied_state"] == store.APPLIED_APPLIED
    assert row["applied"] == 1


# -------------------------------------------------------- отказы и совместимость


def test_unpaid_order_cannot_be_claimed(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db, status=None)

    claim = store.claim_payment_application(db, payment["id"], now=_now())
    assert claim.status == store.CLAIM_REFUSED

    row = store.get_payment(db, payment["id"])
    assert row["applied_state"] == store.APPLIED_PENDING
    assert row["applied"] == 0
    assert row["applied_claim_token"] is None


def test_unknown_payment_is_reported_as_missing(tmp_path):
    db = _db(tmp_path)
    assert store.claim_payment_application(db, "nope").status == store.CLAIM_MISSING
    assert store.confirm_payment_applied(db, "nope", "token") is False


def test_legacy_boolean_claim_is_still_single_shot(tmp_path):
    """claim_applied/unclaim_applied продолжают работать по-старому."""
    db = _db(tmp_path)
    payment = _payment(db)

    assert store.claim_applied(db, payment["id"]) is True
    assert store.claim_applied(db, payment["id"]) is False
    # Повтор уведомления считается уже применённым, а не «в процессе».
    replay = store.claim_payment_application(db, payment["id"], now=_now())
    assert replay.status == store.CLAIM_ALREADY_APPLIED

    store.unclaim_applied(db, payment["id"])
    assert store.get_payment(db, payment["id"])["applied"] == 0
    assert store.claim_applied(db, payment["id"]) is True


def test_legacy_unclaim_does_not_cancel_a_leased_grant(tmp_path):
    db = _db(tmp_path)
    payment = _payment(db)

    claim = store.claim_payment_application(db, payment["id"], now=_now())
    store.unclaim_applied(db, payment["id"])

    row = store.get_payment(db, payment["id"])
    assert row["applied_state"] == store.APPLIED_IN_PROGRESS
    assert row["applied_claim_token"] == claim.token
    assert store.confirm_payment_applied(db, payment["id"], claim.token) is True


# ----------------------------------------------------------------- миграции


def test_old_database_is_migrated_without_losing_rows(tmp_path):
    path = str(tmp_path / "billing.db")
    _legacy_db(path)

    store.init_db(path)

    assert {"applied_state", "applied_claim_token", "applied_claimed_at",
            "applied_at"} <= _columns(path)
    granted = store.get_payment(path, "granted")
    assert granted["user_id"] == "u1"          # данные не потеряны
    assert granted["applied"] == 1
    assert granted["applied_state"] == store.APPLIED_APPLIED
    assert granted["applied_at"] == "2025-01-01T00:00:00+00:00"

    # Начисленный ранее платёж больше никогда не получит право на начисление.
    assert store.claim_payment_application(path, "granted").status == store.CLAIM_ALREADY_APPLIED
    assert store.claim_payment_application(path, "waiting").status == store.CLAIM_GRANTED
    assert len(store.list_payments(path)) == 2

    # Повторная миграция ничего не сбрасывает и не начисляет заново.
    store.init_db(path)
    assert store.get_payment(path, "waiting")["applied_state"] == store.APPLIED_IN_PROGRESS
    assert store.get_payment(path, "granted")["applied_state"] == store.APPLIED_APPLIED


def test_missing_state_column_does_not_break_the_claim(tmp_path):
    """База прошлой версии, панель ещё не перезапускалась: init_db не звали."""
    path = str(tmp_path / "billing.db")
    _legacy_db(path)
    assert "applied_state" not in _columns(path)

    claim = store.claim_payment_application(path, "waiting", now=_now())
    assert claim.status == store.CLAIM_GRANTED
    assert store.confirm_payment_applied(path, "waiting", claim.token, now=_now(1)) is True

    row = store.get_payment(path, "waiting")
    assert row["applied_state"] == store.APPLIED_APPLIED
    assert row["user_id"] == "u2"
    assert row["applied"] == 1

    # Старый платёж с applied = 1 тоже распознан как начисленный.
    assert store.claim_payment_application(path, "granted").status == store.CLAIM_ALREADY_APPLIED


def test_database_without_payments_table_is_not_created_by_a_claim(tmp_path):
    path = str(tmp_path / "billing.db")
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")

    assert store.claim_payment_application(path, "any").status == store.CLAIM_MISSING
    assert store.confirm_payment_applied(path, "any", "token") is False
    with sqlite3.connect(path) as conn:
        tables = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"meta"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
