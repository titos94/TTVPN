from __future__ import annotations

import json
import re
import sqlite3
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from typing import Iterator, NamedTuple, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS plans (
    id            TEXT PRIMARY KEY,
    slug          TEXT UNIQUE NOT NULL,
    name          TEXT NOT NULL,
    description   TEXT DEFAULT '',
    price_kopecks INTEGER NOT NULL,
    days          INTEGER NOT NULL,
    device_limit  INTEGER NOT NULL DEFAULT 5,
    server_id     INTEGER,
    server_uid    TEXT,
    protocol      TEXT,
    active        INTEGER NOT NULL DEFAULT 1,
    sort          INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS payments (
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
CREATE INDEX IF NOT EXISTS idx_payments_user ON payments(user_id);
CREATE INDEX IF NOT EXISTS idx_payments_provider ON payments(provider_payment_id);
CREATE TABLE IF NOT EXISTS settings (
    key        TEXT PRIMARY KEY,
    value      TEXT,
    secret     INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promos (
    id          TEXT PRIMARY KEY,
    code        TEXT UNIQUE NOT NULL,
    kind        TEXT NOT NULL,
    value       INTEGER NOT NULL,
    plan_id     TEXT,
    max_uses    INTEGER NOT NULL DEFAULT 0,
    used_count  INTEGER NOT NULL DEFAULT 0,
    per_user    INTEGER NOT NULL DEFAULT 1,
    valid_until TEXT,
    active      INTEGER NOT NULL DEFAULT 1,
    note        TEXT DEFAULT '',
    created_at  TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS promo_redemptions (
    id               TEXT PRIMARY KEY,
    promo_id         TEXT NOT NULL,
    user_id          TEXT NOT NULL,
    payment_id       TEXT,
    discount_kopecks INTEGER NOT NULL DEFAULT 0,
    bonus_days       INTEGER NOT NULL DEFAULT 0,
    created_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_redemptions_promo ON promo_redemptions(promo_id);
CREATE INDEX IF NOT EXISTS idx_redemptions_user ON promo_redemptions(user_id);
CREATE TABLE IF NOT EXISTS trials (
    user_id    TEXT PRIMARY KEY,
    started_at TEXT NOT NULL,
    expires_at TEXT NOT NULL,
    days       INTEGER NOT NULL,
    ip         TEXT DEFAULT '',
    source     TEXT DEFAULT 'web'
);
CREATE INDEX IF NOT EXISTS idx_trials_ip ON trials(ip);
CREATE TABLE IF NOT EXISTS email_codes (
    id            TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    purpose       TEXT NOT NULL DEFAULT 'register',
    code_hash     TEXT NOT NULL,
    password_hash TEXT,
    telegram_id   TEXT,
    expires_at    TEXT NOT NULL,
    attempts      INTEGER NOT NULL DEFAULT 0,
    created_at    TEXT NOT NULL,
    ip            TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_email_codes_email ON email_codes(email, purpose);
CREATE TABLE IF NOT EXISTS login_tokens (
    token        TEXT PRIMARY KEY,
    created_at   TEXT NOT NULL,
    expires_at   TEXT NOT NULL,
    telegram_id  TEXT,
    confirmed_at TEXT,
    used_at      TEXT,
    ip           TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_login_tokens_expires ON login_tokens(expires_at);
CREATE TABLE IF NOT EXISTS device_labels (
    user_id TEXT NOT NULL,
    device_id TEXT NOT NULL,
    label TEXT NOT NULL,
    PRIMARY KEY (user_id, device_id)
);
CREATE TABLE IF NOT EXISTS devices (
    id         TEXT PRIMARY KEY,
    user_id    TEXT NOT NULL,
    name       TEXT NOT NULL DEFAULT '',
    sort       INTEGER NOT NULL DEFAULT 0,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_devices_user ON devices(user_id);
CREATE TABLE IF NOT EXISTS device_connections (
    device_id     TEXT NOT NULL,
    connection_id TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    PRIMARY KEY (device_id, connection_id)
);
CREATE INDEX IF NOT EXISTS idx_device_connections_conn ON device_connections(connection_id);
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
"""

PROMO_KINDS = ("percent", "amount", "bonus_days", "free_days")

DEFAULT_PLANS = (
    {"slug": "month", "name": "1 месяц", "description": "5 устройств",
     "price_kopecks": 19900, "days": 30, "device_limit": 5, "sort": 10},
    {"slug": "quarter", "name": "3 месяца", "description": "5 устройств",
     "price_kopecks": 49900, "days": 90, "device_limit": 5, "sort": 20},
    {"slug": "year", "name": "12 месяцев", "description": "5 устройств",
     "price_kopecks": 149000, "days": 365, "device_limit": 5, "sort": 30},
)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _row_to_dict(row: Optional[sqlite3.Row]) -> Optional[dict]:
    return dict(row) if row is not None else None


def device_labels(db_path: str, user_id: str) -> dict:
    with connect(db_path) as conn:
        return {row['device_id']: row['label'] for row in conn.execute(
            'SELECT device_id, label FROM device_labels WHERE user_id = ?', (user_id,))}


def set_device_label(db_path: str, user_id: str, device_id: str, label: str) -> None:
    with connect(db_path) as conn:
        conn.execute('INSERT INTO device_labels (user_id, device_id, label) VALUES (?, ?, ?) '
                     'ON CONFLICT(user_id, device_id) DO UPDATE SET label = excluded.label',
                     (user_id, device_id, label))


# --------------------------------------------------------------- devices
# A "device" is the client-visible thing that consumes one plan slot. It can
# carry several VPN connections (protocol profiles); one connection belongs to
# exactly one device. The pairing lives here instead of data.json so the panel
# state and peer ownership stay untouched.

def list_devices(db_path: str, user_id: str) -> list[dict]:
    with connect(db_path) as conn:
        devices = [dict(row) for row in conn.execute(
            "SELECT * FROM devices WHERE user_id = ? ORDER BY sort, created_at", (user_id,))]
        links = [dict(row) for row in conn.execute(
            "SELECT dc.device_id, dc.connection_id, dc.created_at FROM device_connections dc"
            " JOIN devices d ON d.id = dc.device_id WHERE d.user_id = ?"
            " ORDER BY dc.created_at", (user_id,))]
    by_device: dict[str, list[str]] = {}
    for link in links:
        by_device.setdefault(link["device_id"], []).append(link["connection_id"])
    for device in devices:
        device["connection_ids"] = by_device.get(device["id"], [])
    return devices


def connection_devices(db_path: str, user_id: str) -> dict[str, str]:
    """connection_id -> device_id for every owned connection."""
    with connect(db_path) as conn:
        return {row["connection_id"]: row["device_id"] for row in conn.execute(
            "SELECT dc.connection_id, dc.device_id FROM device_connections dc"
            " JOIN devices d ON d.id = dc.device_id WHERE d.user_id = ?", (user_id,))}


def create_device(db_path: str, user_id: str, name: str = "",
                  device_id: Optional[str] = None) -> dict:
    device_id = device_id or str(uuid.uuid4())
    with connect(db_path) as conn:
        sort = conn.execute(
            "SELECT COALESCE(MAX(sort), 0) + 10 AS next FROM devices WHERE user_id = ?",
            (user_id,),
        ).fetchone()["next"]
        conn.execute(
            "INSERT INTO devices (id, user_id, name, sort, created_at) VALUES (?,?,?,?,?)",
            (device_id, user_id, (name or "").strip(), sort, _now()),
        )
        return dict(conn.execute("SELECT * FROM devices WHERE id = ?", (device_id,)).fetchone())


def rename_device(db_path: str, user_id: str, device_id: str, name: str) -> bool:
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE devices SET name = ? WHERE id = ? AND user_id = ?",
            ((name or "").strip(), device_id, user_id),
        )
        return cur.rowcount == 1


def attach_connection(db_path: str, user_id: str, device_id: str,
                      connection_id: str) -> None:
    """Attach a profile to a device; a profile belongs to exactly one device.

    Moving a profile is how a client regroups an extra connection that was
    adopted as its own device, so the old link is removed first. Otherwise the
    same peer would occupy two plan slots at once.
    """
    with connect(db_path) as conn:
        owned = conn.execute(
            "SELECT 1 FROM devices WHERE id = ? AND user_id = ?", (device_id, user_id)
        ).fetchone()
        if not owned:
            raise ValueError("device_not_found")
        conn.execute(
            "DELETE FROM device_connections WHERE connection_id = ?", (connection_id,)
        )
        conn.execute(
            "INSERT INTO device_connections (device_id, connection_id, created_at)"
            " VALUES (?,?,?) ON CONFLICT(device_id, connection_id) DO NOTHING",
            (device_id, connection_id, _now()),
        )


def device_for_connection(db_path: str, user_id: str, connection_id: str,
                          name: str = "") -> dict:
    """Return the device owning this profile, creating one in a single step.

    Device and link are written in one transaction: a parallel reader must
    never see an empty device that it could garbage-collect, and the cabinet
    must never show a device without its profile.
    """
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT d.* FROM devices d JOIN device_connections dc"
            " ON dc.device_id = d.id WHERE dc.connection_id = ? AND d.user_id = ?",
            (connection_id, user_id),
        ).fetchone()
        if row:
            return dict(row)
        device_id = str(uuid.uuid4())
        sort = conn.execute(
            "SELECT COALESCE(MAX(sort), 0) + 10 AS next FROM devices WHERE user_id = ?",
            (user_id,),
        ).fetchone()["next"]
        conn.execute(
            "INSERT INTO devices (id, user_id, name, sort, created_at) VALUES (?,?,?,?,?)",
            (device_id, user_id, (name or "").strip(), sort, _now()),
        )
        conn.execute(
            "INSERT INTO device_connections (device_id, connection_id, created_at)"
            " VALUES (?,?,?)",
            (device_id, connection_id, _now()),
        )
        return dict(conn.execute(
            "SELECT * FROM devices WHERE id = ?", (device_id,)
        ).fetchone())


def detach_connection(db_path: str, connection_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM device_connections WHERE connection_id = ?", (connection_id,))


def ensure_devices(db_path: str, user_id: str, connections: list[dict]) -> list[dict]:
    """Adopt connections that have no device yet; drop devices with no peers.

    Existing pairings are never regrouped: an earlier cabinet device keeps its
    own peers, so no client key changes identity because of this bookkeeping.
    """
    labels = device_labels(db_path, user_id)
    live = {str(conn.get("id")) for conn in connections}

    with connect(db_path) as conn:
        # Links to peers that disappeared from data.json (removed by an
        # operator, or a failed recovery) must not keep a plan slot.
        for row in conn.execute(
            "SELECT dc.connection_id FROM device_connections dc"
            " JOIN devices d ON d.id = dc.device_id WHERE d.user_id = ?",
            (user_id,),
        ).fetchall():
            if row["connection_id"] not in live:
                conn.execute(
                    "DELETE FROM device_connections WHERE connection_id = ?",
                    (row["connection_id"],),
                )
        # A device with no profiles left no longer consumes a slot.
        conn.execute(
            "DELETE FROM devices WHERE user_id = ? AND NOT EXISTS ("
            " SELECT 1 FROM device_connections dc WHERE dc.device_id = devices.id)",
            (user_id,),
        )

    for connection in connections:
        connection_id = str(connection.get("id"))
        name = labels.get(connection_id) or str(connection.get("name") or "")
        device_for_connection(db_path, user_id, connection_id, name)

    return list_devices(db_path, user_id)



@contextmanager
def connect(db_path: str) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(db_path, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=15000")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def init_db(db_path: str) -> None:
    with connect(db_path) as conn:
        conn.executescript(SCHEMA)
        _migrate(conn)


def reserve_assistant_request(db_path: str, user_id: str, *, day: str,
                              user_limit: int = 20, global_limit: int = 200) -> bool:
    """Reserve one paid request atomically, including unsuccessful attempts.

    This is a request ceiling, not a monetary budget; the provider must also
    enforce a spending limit. No message content is stored here.
    """
    with connect(db_path) as conn:
        conn.execute('BEGIN IMMEDIATE')
        conn.execute('CREATE TABLE IF NOT EXISTS assistant_daily_usage '
                     '(day TEXT NOT NULL, user_id TEXT NOT NULL, requests INTEGER NOT NULL, '
                     'PRIMARY KEY (day, user_id))')
        total = conn.execute('SELECT COALESCE(SUM(requests), 0) FROM assistant_daily_usage WHERE day = ?', (day,)).fetchone()[0]
        row = conn.execute('SELECT requests FROM assistant_daily_usage WHERE day = ? AND user_id = ?', (day, user_id)).fetchone()
        if total >= global_limit or (row and row[0] >= user_limit):
            return False
        conn.execute('INSERT INTO assistant_daily_usage (day, user_id, requests) VALUES (?, ?, 1) '
                     'ON CONFLICT(day, user_id) DO UPDATE SET requests = requests + 1', (day, user_id))
        return True


# -------------------------------------------------- payment applied state
#
# A paid order must be granted exactly once even when two webhook deliveries
# for the same payment arrive at the same moment. The single `applied` flag
# could not tell "another handler is granting the period right now" from "the
# period is already granted": the second delivery answered 200 while the first
# one was still working, and the provider stopped retrying the order.
#
# `applied_state` is the source of truth now:
#   pending     - никто не начислял, право выдаётся первому обработчику;
#   in_progress - право выдано одному обработчику (lease: токен + время);
#   applied     - дни начислены, состояние финальное;
#   failed      - попытка явно провалилась, повтор разрешён сразу.
# The legacy `applied` column stays in sync ("granted or being granted") so
# `last_unprovisioned_payment` and older callers keep working unchanged.

APPLIED_PENDING = "pending"
APPLIED_IN_PROGRESS = "in_progress"
APPLIED_APPLIED = "applied"
APPLIED_FAILED = "failed"
APPLIED_STATES = (APPLIED_PENDING, APPLIED_IN_PROGRESS, APPLIED_APPLIED, APPLIED_FAILED)

# Long enough for the whole grant (data.json update, device provisioning) and
# short enough that a crashed handler does not block the order for long.
APPLIED_CLAIM_TTL_SECONDS = 900

_APPLIED_COLUMNS = (
    ("applied_state", "TEXT NOT NULL DEFAULT 'pending'"),
    ("applied_claim_token", "TEXT"),
    ("applied_claimed_at", "TEXT"),
    ("applied_at", "TEXT"),
)


# Columns added after the first release. CREATE TABLE IF NOT EXISTS does not
# touch an existing table, so old installations need an explicit ALTER.
_ADDED_COLUMNS = {
    "plans": (
        ("server_uid", "TEXT"),
    ),
    "payments": (
        ("promo_code", "TEXT DEFAULT ''"),
        ("discount_kopecks", "INTEGER NOT NULL DEFAULT 0"),
        ("bonus_days", "INTEGER NOT NULL DEFAULT 0"),
        ("days_granted", "INTEGER NOT NULL DEFAULT 0"),
        # Terms are snapshotted at checkout: editing a plan afterwards must not
        # change what an already paid order grants.
        ("plan_days", "INTEGER NOT NULL DEFAULT 0"),
        ("plan_device_limit", "INTEGER NOT NULL DEFAULT 0"),
        ("plan_server_uid", "TEXT DEFAULT ''"),
        ("plan_protocol", "TEXT DEFAULT ''"),
        ("provisioned", "INTEGER NOT NULL DEFAULT 0"),
        *_APPLIED_COLUMNS,
    ),
}


SCHEMA_VERSION = 3


def _schema_version(conn: sqlite3.Connection) -> int:
    row = conn.execute("SELECT value FROM meta WHERE key = 'schema_version'").fetchone()
    try:
        return int(row["value"]) if row else 0
    except (TypeError, ValueError):
        return 0


def _set_schema_version(conn: sqlite3.Connection, version: int) -> None:
    conn.execute(
        "INSERT INTO meta (key, value) VALUES ('schema_version', ?)"
        " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (str(version),),
    )


LEGACY_PLAN_DESCRIPTIONS = {
    "month": ("1 устройство", "5 устройств"),
    "quarter": ("2 устройства", "5 устройств"),
    "year": ("3 устройства", "5 устройств"),
}


def _migrate(conn: sqlite3.Connection) -> None:
    for table, columns in _ADDED_COLUMNS.items():
        existing = {row["name"] for row in conn.execute(f"PRAGMA table_info({table})")}
        for name, definition in columns:
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {definition}")

    version = _schema_version(conn)
    if version < 1:
        # The agreed default is five devices for every plan. Plans are still
        # individually configurable; existing test plans are aligned once.
        conn.execute("UPDATE plans SET device_limit = 5 WHERE device_limit < 5")
        for slug, (old, new) in LEGACY_PLAN_DESCRIPTIONS.items():
            conn.execute(
                "UPDATE plans SET description = ? WHERE slug = ? AND description = ?",
                (new, slug, old),
            )
        _set_schema_version(conn, 1)

    if version < 2:
        # Provider was recorded as "yookassa" for every checkout, including the
        # mock flow, and free (promo / 100% discount) orders carried no provider
        # at all. Both made the revenue figure look different from reality.
        conn.execute(
            "UPDATE payments SET provider = 'mock'"
            " WHERE provider <> 'mock' AND substr(provider_payment_id, 1, 5) = 'mock_'"
        )
        conn.execute(
            "UPDATE payments SET provider = 'promo'"
            " WHERE provider = 'yookassa' AND amount_kopecks = 0"
            " AND status = 'succeeded'"
            " AND (provider_payment_id IS NULL OR provider_payment_id = '')"
        )
        _set_schema_version(conn, 2)

    if version < 3:
        # Orders granted before the applied state existed are terminal: a
        # replayed notification must not grant the same days again.
        _backfill_applied_state(conn)
        _set_schema_version(conn, 3)


def _ensure_applied_schema(conn: sqlite3.Connection) -> bool:
    """Make sure the applied-state columns exist; False if the table is absent.

    Called from ``_migrate`` and, defensively, from every claim: an existing
    installation may receive a webhook before the panel was restarted with this
    release. The ALTER and the backfill run inside the caller's transaction and
    only once — on any following call the columns are present, so no row is
    rewritten again.
    """
    existing = {row["name"] for row in conn.execute("PRAGMA table_info(payments)")}
    if not existing:
        return False
    missing = [column for column in _APPLIED_COLUMNS if column[0] not in existing]
    for name, definition in missing:
        conn.execute(f"ALTER TABLE payments ADD COLUMN {name} {definition}")
    if missing:
        _backfill_applied_state(conn)
    return True


def _backfill_applied_state(conn: sqlite3.Connection) -> None:
    """Map ``applied = 1`` rows written by an older release to a terminal state.

    ``applied = 1`` was the old "period granted" mark, so those rows must stop
    being claimable; otherwise the very first replay would grant the days a
    second time. Idempotent: rows owned by the new state machine are untouched.
    """
    conn.execute(
        "UPDATE payments SET applied_state = ?,"
        " applied_at = COALESCE(applied_at, paid_at)"
        " WHERE applied = 1 AND applied_state = ?",
        (APPLIED_APPLIED, APPLIED_PENDING),
    )


# ---------------------------------------------------------------- plans

def seed_plans(db_path: str, server_id: Optional[int], protocol: str) -> None:
    with connect(db_path) as conn:
        count = conn.execute("SELECT COUNT(*) AS n FROM plans").fetchone()["n"]
        if count:
            return
        for item in DEFAULT_PLANS:
            conn.execute(
                "INSERT INTO plans (id, slug, name, description, price_kopecks, days,"
                " device_limit, server_id, protocol, active, sort)"
                " VALUES (?,?,?,?,?,?,?,?,?,1,?)",
                (str(uuid.uuid4()), item["slug"], item["name"], item["description"],
                 item["price_kopecks"], item["days"], item["device_limit"],
                 server_id, protocol or None, item["sort"]),
            )


def backfill_plan_uids(db_path: str, uid_by_index: dict[int, str]) -> None:
    """Resolve legacy index-based plan targets to stable server uids once.

    The mapping is the server order *at this moment*; plans already carrying a
    uid are never touched, so re-running this after a reorder is harmless.
    """
    if not uid_by_index:
        return
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT id, server_id FROM plans WHERE (server_uid IS NULL OR server_uid = '')"
            " AND server_id IS NOT NULL"
        ).fetchall()
        for row in rows:
            uid = uid_by_index.get(int(row["server_id"]))
            if uid:
                conn.execute(
                    "UPDATE plans SET server_uid = ? WHERE id = ? AND"
                    " (server_uid IS NULL OR server_uid = '')",
                    (uid, row["id"]),
                )


def list_plans(db_path: str, only_active: bool = True) -> list[dict]:
    sql = "SELECT * FROM plans"
    if only_active:
        sql += " WHERE active = 1"
    sql += " ORDER BY sort, price_kopecks"
    with connect(db_path) as conn:
        return [dict(r) for r in conn.execute(sql).fetchall()]


def get_plan(db_path: str, plan_id: str, only_active: bool = True) -> Optional[dict]:
    sql = "SELECT * FROM plans WHERE id = ?"
    if only_active:
        sql += " AND active = 1"
    with connect(db_path) as conn:
        return _row_to_dict(conn.execute(sql, (plan_id,)).fetchone())


# ---------------------------------------------------------------- payments

# A payment moves forward only. Webhook replays, the return page and the
# cabinet poll all race for the same order, so a late "canceled" or "pending"
# must never overwrite a settled row: the granted term and the revenue history
# are derived from it.
PAYMENT_STATUSES = ("pending", "succeeded", "canceled", "refunded")


def next_payment_status(current: str, requested: str) -> str:
    """Status a row may take; a refused transition keeps the recorded value.

    ``pending`` settles as ``succeeded`` or ``canceled``; ``succeeded`` is final
    except for a refund; ``canceled`` never reopens (a retry is a new order with
    its own row). Provider states we do not model (``waiting_for_capture``)
    stay ``pending``.
    """
    current = str(current or "pending")
    requested = str(requested or "")
    if current not in PAYMENT_STATUSES:
        # A provider state we do not model (a legacy ``waiting_for_capture``
        # row) is in-flight, so it may still settle.
        current = "pending"
    if requested == current:
        return current
    if current == "pending" and requested in ("succeeded", "canceled"):
        return requested
    if current == "succeeded" and requested == "refunded":
        return "refunded"
    return current


def create_payment(db_path: str, *, user_id: str, plan_id: str, provider: str,
                   amount_kopecks: int, currency: str, idempotency_key: str,
                   promo_code: str = "", discount_kopecks: int = 0,
                   bonus_days: int = 0, days_granted: int = 0,
                   plan_days: int = 0, plan_device_limit: int = 0,
                   plan_server_uid: str = "", plan_protocol: str = "",
                   raw: Optional[dict] = None) -> dict:
    payment_id = str(uuid.uuid4())
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO payments (id, user_id, plan_id, provider, idempotency_key,"
            " amount_kopecks, currency, status, created_at, raw, promo_code,"
            " discount_kopecks, bonus_days, days_granted, plan_days,"
            " plan_device_limit, plan_server_uid, plan_protocol)"
            " VALUES (?,?,?,?,?,?,?, 'pending', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (payment_id, user_id, plan_id, provider, idempotency_key,
             amount_kopecks, currency, _now(),
             json.dumps(raw or {}, ensure_ascii=False),
             promo_code, discount_kopecks, bonus_days, days_granted,
             plan_days, plan_device_limit, plan_server_uid or "",
             plan_protocol or ""),
        )
        return _row_to_dict(conn.execute(
            "SELECT * FROM payments WHERE id = ?", (payment_id,)
        ).fetchone())


def get_payment(db_path: str, payment_id: str) -> Optional[dict]:
    with connect(db_path) as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM payments WHERE id = ?", (payment_id,)
        ).fetchone())


def get_payment_by_provider(db_path: str, provider_payment_id: str) -> Optional[dict]:
    with connect(db_path) as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM payments WHERE provider_payment_id = ?", (provider_payment_id,)
        ).fetchone())


def set_provider_payment(db_path: str, payment_id: str, provider_payment_id: str,
                         confirmation_url: str, status: str, raw: Optional[dict] = None) -> bool:
    """Attach provider data; its status goes through the same forward-only rule."""
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()
        if row is None:
            return False
        resolved = next_payment_status(row["status"], status)
        conn.execute(
            "UPDATE payments SET provider_payment_id = ?, confirmation_url = ?, status = ?,"
            " raw = ? WHERE id = ?",
            (provider_payment_id, confirmation_url, resolved,
             json.dumps(raw or {}, ensure_ascii=False), payment_id),
        )
        return True


def mark_status(db_path: str, payment_id: str, status: str) -> bool:
    """Move a payment to ``status`` without rewriting history.

    ``paid_at`` is written once, when the row first becomes ``succeeded``. A
    replayed webhook used to move it to the retry time, which silently shifted
    received money into a later month and made monthly revenue unreliable.

    Returns True when the row is in ``status`` afterwards (applied now or
    already there), False when the transition is refused or there is no such
    payment.
    """
    if status not in PAYMENT_STATUSES:
        raise ValueError(f"unknown payment status: {status}")
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT status, paid_at FROM payments WHERE id = ?", (payment_id,)
        ).fetchone()
        if row is None:
            return False
        resolved = next_payment_status(row["status"], status)
        if resolved != row["status"]:
            paid_at = row["paid_at"]
            if resolved == "succeeded" and not paid_at:
                paid_at = _now()
            conn.execute(
                "UPDATE payments SET status = ?, paid_at = ? WHERE id = ?",
                (resolved, paid_at, payment_id),
            )
        return resolved == status


def mark_refunded(db_path: str, payment_id: str) -> bool:
    """Record a refund made at the provider.

    The paid period is deliberately not revoked here: returning money and
    cutting access are separate decisions, and the ledger must stay truthful
    even when only the first one has been made. There is no UI action for this
    yet — see BILLING.md, «Возвраты».
    """
    return mark_status(db_path, payment_id, "refunded")


def _iso_before(moment: str, seconds: int) -> str:
    """ISO timestamp ``seconds`` earlier than ``moment`` (same format)."""
    try:
        base = datetime.fromisoformat(str(moment))
    except (TypeError, ValueError):
        base = datetime.now(timezone.utc)
    return (base - timedelta(seconds=max(0, int(seconds)))).isoformat()


CLAIM_GRANTED = "claimed"
CLAIM_IN_PROGRESS = "in_progress"
CLAIM_ALREADY_APPLIED = "applied"
CLAIM_MISSING = "missing"
CLAIM_REFUSED = "refused"


class AppliedClaim(NamedTuple):
    """Who got the right to grant the paid period.

    ``status`` is ``claimed`` for exactly one caller; its ``token`` is the
    proof of that right and must be passed to :func:`confirm_payment_applied`
    (days granted) or :func:`release_payment_claim` (grant failed).
    """
    status: str
    state: str = ""
    token: str = ""


def claim_payment_application(db_path: str, payment_id: str, *,
                              ttl_seconds: int = APPLIED_CLAIM_TTL_SECONDS,
                              now: Optional[str] = None) -> AppliedClaim:
    """Atomically hand the right to grant the paid period to a single caller.

    One ``UPDATE ... WHERE`` decides the winner, so two simultaneous webhook
    deliveries can never both grant days:

    * ``claimed``        — the caller owns the lease and must finish with
      ``confirm_payment_applied`` or ``release_payment_claim``;
    * ``in_progress``    — another handler is granting right now: answer the
      provider with a retryable error instead of ``200``;
    * ``applied``        — the days were granted already: acknowledge the
      delivery without granting anything;
    * ``missing``        — no such payment;
    * ``refused``        — the order is not paid, nothing may be granted.

    A lease older than ``ttl_seconds`` may be taken over, so a handler that
    crashed between the claim and the grant never blocks the order forever.
    """
    moment = now or _now()
    stale_before = _iso_before(moment, ttl_seconds)
    token = str(uuid.uuid4())
    with connect(db_path) as conn:
        if not _ensure_applied_schema(conn):
            return AppliedClaim(CLAIM_MISSING)
        cur = conn.execute(
            # A fresh lease does not match, so only one caller can win; the
            # legacy `applied` flag is kept as "granted or being granted".
            "UPDATE payments SET applied_state = ?, applied_claim_token = ?,"
            " applied_claimed_at = ?, applied = 1"
            " WHERE id = ? AND status = 'succeeded' AND applied_state <> ?"
            " AND ((applied_state IN (?, ?) AND COALESCE(applied, 0) = 0)"
            " OR (applied_state = ? AND applied_claimed_at IS NOT NULL"
            " AND applied_claimed_at <= ?))",
            (APPLIED_IN_PROGRESS, token, moment,
             payment_id, APPLIED_APPLIED,
             APPLIED_PENDING, APPLIED_FAILED,
             APPLIED_IN_PROGRESS, stale_before),
        )
        if cur.rowcount == 1:
            return AppliedClaim(CLAIM_GRANTED, APPLIED_IN_PROGRESS, token)
        row = conn.execute(
            "SELECT status, applied, applied_state, applied_claim_token"
            " FROM payments WHERE id = ?", (payment_id,),
        ).fetchone()
    if row is None:
        return AppliedClaim(CLAIM_MISSING)
    state = str(row["applied_state"] or APPLIED_PENDING)
    if state == APPLIED_APPLIED:
        return AppliedClaim(CLAIM_ALREADY_APPLIED, state)
    if state == APPLIED_IN_PROGRESS:
        if row["applied_claim_token"]:
            return AppliedClaim(CLAIM_IN_PROGRESS, state)
        # A token-less claim belongs to the deprecated boolean API: it cannot
        # be confirmed separately, so a replay is acknowledged as granted.
        return AppliedClaim(CLAIM_ALREADY_APPLIED, state)
    if row["applied"]:
        # Flag written by an older release while the state column was already
        # present (rolling upgrade): treated as granted, never granted twice.
        return AppliedClaim(CLAIM_ALREADY_APPLIED, state)
    return AppliedClaim(CLAIM_REFUSED, state)


def confirm_payment_applied(db_path: str, payment_id: str, token: str, *,
                            now: Optional[str] = None) -> bool:
    """Mark the period as granted; only the lease owner may do that.

    True means this call moved the row to the terminal ``applied`` state.
    False means the lease is gone (expired, taken over or released): the days
    must not be granted again, and the caller must not write them twice.
    """
    if not token:
        return False
    with connect(db_path) as conn:
        if not _ensure_applied_schema(conn):
            return False
        cur = conn.execute(
            "UPDATE payments SET applied_state = ?, applied_at = ?,"
            " applied_claim_token = NULL, applied_claimed_at = NULL, applied = 1"
            " WHERE id = ? AND applied_state = ? AND applied_claim_token = ?",
            (APPLIED_APPLIED, now or _now(), payment_id, APPLIED_IN_PROGRESS, token),
        )
        return cur.rowcount == 1


def release_payment_claim(db_path: str, payment_id: str, token: str, *,
                          state: str = APPLIED_FAILED) -> bool:
    """Give the right back after a failed grant so a retry is possible now."""
    if not token:
        return False
    if state not in APPLIED_STATES or state == APPLIED_APPLIED:
        raise ValueError(f"unsupported applied state: {state}")
    with connect(db_path) as conn:
        if not _ensure_applied_schema(conn):
            return False
        cur = conn.execute(
            "UPDATE payments SET applied_state = ?, applied = 0,"
            " applied_claim_token = NULL, applied_claimed_at = NULL"
            " WHERE id = ? AND applied_state = ? AND applied_claim_token = ?",
            (state, payment_id, APPLIED_IN_PROGRESS, token),
        )
        return cur.rowcount == 1


def claim_applied(db_path: str, payment_id: str) -> bool:
    """Deprecated: the old boolean "applied 0 -> 1" claim, without a lease.

    Kept for callers that cannot confirm the grant in a separate step. The
    claim is stored as an in-progress state without a token: a replay of the
    same delivery is acknowledged instead of granting the days twice, but a
    crash between this call and the grant is not recoverable. Use
    :func:`claim_payment_application` instead.
    """
    with connect(db_path) as conn:
        if not _ensure_applied_schema(conn):
            return False
        cur = conn.execute(
            "UPDATE payments SET applied = 1, applied_state = ?,"
            " applied_claim_token = NULL, applied_claimed_at = NULL"
            " WHERE id = ? AND applied = 0 AND applied_state <> ?"
            " AND status = 'succeeded'",
            (APPLIED_IN_PROGRESS, payment_id, APPLIED_APPLIED),
        )
        return cur.rowcount == 1


def unclaim_applied(db_path: str, payment_id: str) -> None:
    """Deprecated rollback for :func:`claim_applied` after a failed grant.

    Only a token-less claim is rolled back, so a handler that owns a real
    lease is never overridden and cannot grant the period twice.
    """
    with connect(db_path) as conn:
        if not _ensure_applied_schema(conn):
            return
        conn.execute(
            "UPDATE payments SET applied = 0, applied_state = ?,"
            " applied_claim_token = NULL, applied_claimed_at = NULL"
            " WHERE id = ? AND applied_state = ? AND applied_claim_token IS NULL",
            (APPLIED_FAILED, payment_id, APPLIED_IN_PROGRESS),
        )


def mark_provisioned(db_path: str, payment_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute("UPDATE payments SET provisioned = 1 WHERE id = ?", (payment_id,))


def last_unprovisioned_payment(db_path: str, user_id: str) -> Optional[dict]:
    """Newest paid order whose devices are still missing; None when settled."""
    with connect(db_path) as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM payments WHERE user_id = ? AND status = 'succeeded'"
            " AND applied = 1 AND provisioned = 0"
            " ORDER BY created_at DESC LIMIT 1",
            (user_id,),
        ).fetchone())


def list_user_payments(db_path: str, user_id: str, limit: int = 20) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM payments WHERE user_id = ? ORDER BY created_at DESC LIMIT ?",
            (user_id, limit),
        ).fetchall()
        return [dict(r) for r in rows]


# ------------------------------------------------------------- settings kv

def read_settings(db_path: str) -> dict[str, tuple[str, bool]]:
    """Return {key: (value, is_secret)}; empty dict when the DB is unavailable."""
    try:
        with connect(db_path) as conn:
            rows = conn.execute("SELECT key, value, secret FROM settings").fetchall()
    except sqlite3.Error:
        return {}
    return {row["key"]: (row["value"] or "", bool(row["secret"])) for row in rows}


def write_settings(db_path: str, values: dict[str, tuple[str, bool]]) -> None:
    if not values:
        return
    now = _now()
    with connect(db_path) as conn:
        for key, (value, secret) in values.items():
            conn.execute(
                "INSERT INTO settings (key, value, secret, updated_at) VALUES (?,?,?,?)"
                " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
                " secret=excluded.secret, updated_at=excluded.updated_at",
                (key, value, 1 if secret else 0, now),
            )


def delete_settings(db_path: str, keys: list[str]) -> None:
    if not keys:
        return
    with connect(db_path) as conn:
        conn.executemany("DELETE FROM settings WHERE key = ?", [(k,) for k in keys])


# ---------------------------------------------------------------- plans crud

def create_plan(db_path: str, *, slug: str, name: str, description: str,
                price_kopecks: int, days: int, device_limit: int,
                server_id: Optional[int], protocol: Optional[str],
                server_uid: Optional[str] = None,
                active: bool = True, sort: int = 100) -> dict:
    plan_id = str(uuid.uuid4())
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO plans (id, slug, name, description, price_kopecks, days,"
            " device_limit, server_id, server_uid, protocol, active, sort)"
            " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
            (plan_id, slug, name, description, price_kopecks, days, device_limit,
             server_id, server_uid or None, protocol or None, 1 if active else 0, sort),
        )
        row = conn.execute("SELECT * FROM plans WHERE id = ?", (plan_id,)).fetchone()
        return dict(row)


def update_plan(db_path: str, plan_id: str, fields: dict) -> None:
    allowed = ("slug", "name", "description", "price_kopecks", "days",
               "device_limit", "server_id", "server_uid", "protocol", "active", "sort")
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with connect(db_path) as conn:
        conn.execute(
            f"UPDATE plans SET {assignments} WHERE id = ?",
            (*updates.values(), plan_id),
        )


def delete_plan(db_path: str, plan_id: str) -> bool:
    """Hard-delete only when no payment references the plan (history stays)."""
    with connect(db_path) as conn:
        used = conn.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE plan_id = ?", (plan_id,)
        ).fetchone()["n"]
        if used:
            return False
        conn.execute("DELETE FROM plans WHERE id = ?", (plan_id,))
        return True


def plan_slug_exists(db_path: str, slug: str, exclude_id: Optional[str] = None) -> bool:
    with connect(db_path) as conn:
        if exclude_id:
            row = conn.execute(
                "SELECT 1 FROM plans WHERE slug = ? AND id <> ?", (slug, exclude_id)
            ).fetchone()
        else:
            row = conn.execute("SELECT 1 FROM plans WHERE slug = ?", (slug,)).fetchone()
        return row is not None


# ------------------------------------------------------------ payments admin

def list_payments(db_path: str, limit: int = 100) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM payments ORDER BY created_at DESC LIMIT ?", (limit,)
        ).fetchall()
        return [dict(r) for r in rows]


def payments_summary(db_path: str) -> dict:
    """Money counters for the admin header.

    Only real incoming money is counted. Mock checkouts (``provider = 'mock'``)
    are test data, free orders (promo codes, 100% discounts) never moved money,
    and a refunded payment stops being revenue, so each group is reported
    separately instead of inflating one total.
    """
    with connect(db_path) as conn:
        total = conn.execute(
            "SELECT COALESCE(SUM(CASE WHEN amount_kopecks > 0 THEN 1 ELSE 0 END), 0) AS n,"
            " COALESCE(SUM(CASE WHEN amount_kopecks > 0 THEN amount_kopecks ELSE 0 END), 0) AS s,"
            " COALESCE(SUM(CASE WHEN amount_kopecks > 0 THEN 0 ELSE 1 END), 0) AS free"
            " FROM payments WHERE status = 'succeeded' AND provider <> 'mock'"
        ).fetchone()
        mock = conn.execute(
            "SELECT COUNT(*) AS n FROM payments"
            " WHERE status = 'succeeded' AND provider = 'mock'"
        ).fetchone()
        refunded = conn.execute(
            "SELECT COUNT(*) AS n, COALESCE(SUM(amount_kopecks), 0) AS s"
            " FROM payments WHERE status = 'refunded'"
        ).fetchone()
        pending = conn.execute(
            "SELECT COUNT(*) AS n FROM payments WHERE status = 'pending'"
        ).fetchone()
        return {
            "paid_count": total["n"],
            "paid_kopecks": total["s"],
            "free_count": total["free"],
            "pending_count": pending["n"],
            "mock_count": mock["n"],
            "refunded_count": refunded["n"],
            "refunded_kopecks": refunded["s"],
        }


# ------------------------------------------------------------- promo codes

def normalize_code(code: str) -> str:
    return re.sub(r"[^A-Z0-9_-]+", "", str(code or "").upper())


def list_promos(db_path: str) -> list[dict]:
    with connect(db_path) as conn:
        rows = conn.execute("SELECT * FROM promos ORDER BY created_at DESC").fetchall()
        promos = [dict(row) for row in rows]
        # One aggregate query for the whole list instead of two per row.
        stats = {
            row["promo_id"]: row
            for row in conn.execute(
                "SELECT promo_id, COUNT(*) AS n,"
                " COALESCE(SUM(discount_kopecks), 0) AS d"
                " FROM promo_redemptions GROUP BY promo_id"
            ).fetchall()
        }
        for promo in promos:
            row = stats.get(promo["id"])
            promo["redemptions"] = row["n"] if row else 0
            promo["discount_total"] = row["d"] if row else 0
        return promos


def get_promo(db_path: str, promo_id: str) -> Optional[dict]:
    with connect(db_path) as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM promos WHERE id = ?", (promo_id,)
        ).fetchone())


def get_promo_by_code(db_path: str, code: str) -> Optional[dict]:
    normalized = normalize_code(code)
    if not normalized:
        return None
    with connect(db_path) as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM promos WHERE code = ?", (normalized,)
        ).fetchone())


def create_promo(db_path: str, *, code: str, kind: str, value: int,
                 plan_id: Optional[str] = None, max_uses: int = 0, per_user: int = 1,
                 valid_until: Optional[str] = None, active: bool = True,
                 note: str = "") -> dict:
    promo_id = str(uuid.uuid4())
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO promos (id, code, kind, value, plan_id, max_uses, used_count,"
            " per_user, valid_until, active, note, created_at)"
            " VALUES (?,?,?,?,?,?,0,?,?,?,?,?)",
            (promo_id, normalize_code(code), kind, value, plan_id or None, max_uses,
             per_user, valid_until or None, 1 if active else 0, note, _now()),
        )
        return dict(conn.execute("SELECT * FROM promos WHERE id = ?", (promo_id,)).fetchone())


def update_promo(db_path: str, promo_id: str, fields: dict) -> None:
    allowed = ("code", "kind", "value", "plan_id", "max_uses", "per_user",
               "valid_until", "active", "note")
    updates = {k: v for k, v in fields.items() if k in allowed}
    if not updates:
        return
    assignments = ", ".join(f"{key} = ?" for key in updates)
    with connect(db_path) as conn:
        conn.execute(f"UPDATE promos SET {assignments} WHERE id = ?",
                     (*updates.values(), promo_id))


def delete_promo(db_path: str, promo_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM promos WHERE id = ?", (promo_id,))


def user_redemptions(db_path: str, promo_id: str, user_id: str) -> int:
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM promo_redemptions WHERE promo_id = ? AND user_id = ?",
            (promo_id, user_id),
        ).fetchone()["n"]


def record_redemption(db_path: str, *, promo_id: str, user_id: str,
                      payment_id: Optional[str], discount_kopecks: int,
                      bonus_days: int) -> bool:
    """Count a redemption once per payment and bump the use counter.

    Called only after the paid period was actually granted, so an abandoned
    checkout never burns a limited promo code.
    """
    with connect(db_path) as conn:
        if payment_id:
            existing = conn.execute(
                "SELECT 1 FROM promo_redemptions WHERE payment_id = ?", (payment_id,)
            ).fetchone()
            if existing:
                return False
        conn.execute(
            "INSERT INTO promo_redemptions (id, promo_id, user_id, payment_id,"
            " discount_kopecks, bonus_days, created_at) VALUES (?,?,?,?,?,?,?)",
            (str(uuid.uuid4()), promo_id, user_id, payment_id,
             int(discount_kopecks), int(bonus_days), _now()),
        )
        conn.execute(
            "UPDATE promos SET used_count = used_count + 1 WHERE id = ?", (promo_id,)
        )
        return True


# ----------------------------------------------------------------- trials

def get_trial(db_path: str, user_id: str) -> Optional[dict]:
    with connect(db_path) as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM trials WHERE user_id = ?", (user_id,)
        ).fetchone())


def count_trials_from_ip(db_path: str, ip: str, since_iso: str) -> int:
    if not ip:
        return 0
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM trials WHERE ip = ? AND started_at >= ?",
            (ip, since_iso),
        ).fetchone()["n"]


def claim_trial(db_path: str, *, user_id: str, days: int, expires_at: str,
                ip: str, since_iso: str, ip_limit: int) -> str:
    """Reserve trial once; serialize account, payment and IP checks in SQLite.

    A successful claim is deliberately retained if the subsequent JSON update
    fails or its outcome is unknown. Such cases require operator reconciliation,
    not a retry that might grant the period twice.
    """
    with connect(db_path) as conn:
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM trials WHERE user_id = ?", (user_id,)).fetchone():
            return "used"
        if conn.execute("SELECT 1 FROM payments WHERE user_id = ? AND status = 'succeeded' LIMIT 1", (user_id,)).fetchone():
            return "paid"
        if ip_limit > 0 and ip:
            count = conn.execute(
                "SELECT COUNT(*) FROM trials WHERE ip = ? AND started_at >= ?", (ip, since_iso)
            ).fetchone()[0]
            if count >= ip_limit:
                return "ip_limit"
        conn.execute(
            "INSERT INTO trials (user_id, started_at, expires_at, days, ip, source) VALUES (?,?,?,?,?,?)",
            (user_id, _now(), expires_at, days, ip or "", "web_claim"),
        )
        return "claimed"


def create_trial(db_path: str, *, user_id: str, days: int, expires_at: str,
                 ip: str = "", source: str = "web") -> None:
    with connect(db_path) as conn:
        conn.execute(
            "INSERT INTO trials (user_id, started_at, expires_at, days, ip, source)"
            " VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(user_id) DO UPDATE SET started_at=excluded.started_at,"
            " expires_at=excluded.expires_at, days=excluded.days, ip=excluded.ip,"
            " source=excluded.source",
            (user_id, _now(), expires_at, days, ip or "", source),
        )


def trials_summary(db_path: str) -> dict:
    with connect(db_path) as conn:
        total = conn.execute("SELECT COUNT(*) AS n FROM trials").fetchone()["n"]
        recent = conn.execute(
            "SELECT COUNT(*) AS n FROM trials WHERE started_at >= ?",
            ((datetime.now(timezone.utc).replace(microsecond=0)
              - timedelta(days=30)).isoformat(),),
        ).fetchone()["n"]
        return {"total": total, "last30": recent}


def has_paid_payment(db_path: str, user_id: str) -> bool:
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT 1 FROM payments WHERE user_id = ? AND status = 'succeeded' LIMIT 1",
            (user_id,),
        ).fetchone()
        return row is not None


# ------------------------------------------------------- email verification

def create_email_code(db_path: str, *, email: str, purpose: str, code_hash: str,
                      expires_at: str, password_hash: Optional[str] = None,
                      telegram_id: Optional[str] = None, ip: str = "") -> None:
    """One live code per (email, purpose): a resend replaces the previous one."""
    with connect(db_path) as conn:
        conn.execute("DELETE FROM email_codes WHERE email = ? AND purpose = ?",
                     (email, purpose))
        conn.execute(
            "INSERT INTO email_codes (id, email, purpose, code_hash, password_hash,"
            " telegram_id, expires_at, attempts, created_at, ip)"
            " VALUES (?,?,?,?,?,?,?,0,?,?)",
            (str(uuid.uuid4()), email, purpose, code_hash, password_hash,
             telegram_id, expires_at, _now(), ip or ""),
        )


def get_email_code(db_path: str, email: str, purpose: str = "register") -> Optional[dict]:
    with connect(db_path) as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM email_codes WHERE email = ? AND purpose = ? ORDER BY created_at DESC LIMIT 1",
            (email, purpose),
        ).fetchone())


def bump_email_code_attempts(db_path: str, code_id: str) -> int:
    with connect(db_path) as conn:
        conn.execute("UPDATE email_codes SET attempts = attempts + 1 WHERE id = ?", (code_id,))
        row = conn.execute("SELECT attempts FROM email_codes WHERE id = ?", (code_id,)).fetchone()
        return int(row["attempts"]) if row else 0


def delete_email_code(db_path: str, code_id: str) -> None:
    with connect(db_path) as conn:
        conn.execute("DELETE FROM email_codes WHERE id = ?", (code_id,))


def count_email_codes_from_ip(db_path: str, ip: str, since_iso: str) -> int:
    if not ip:
        return 0
    with connect(db_path) as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM email_codes WHERE ip = ? AND created_at >= ?",
            (ip, since_iso),
        ).fetchone()["n"]


# ------------------------------------------------------- telegram login links

def create_login_token(db_path: str, *, token: str, expires_at: str, ip: str = "") -> None:
    with connect(db_path) as conn:
        # Keep the table small: an expired link can never be confirmed anyway.
        conn.execute("DELETE FROM login_tokens WHERE expires_at < ?", (_now(),))
        conn.execute(
            "INSERT INTO login_tokens (token, created_at, expires_at, ip)"
            " VALUES (?,?,?,?)",
            (token, _now(), expires_at, ip or ""),
        )


def get_login_token(db_path: str, token: str) -> Optional[dict]:
    if not token:
        return None
    with connect(db_path) as conn:
        return _row_to_dict(conn.execute(
            "SELECT * FROM login_tokens WHERE token = ?", (token,)
        ).fetchone())


def confirm_login_token(db_path: str, token: str, telegram_id: str) -> bool:
    """Bind a Telegram account to a pending browser login link.

    Called from the bot when the user presses Start on ``/start lk_<token>``.
    Only the first confirmation wins and only while the link is still valid.
    """
    telegram_id = str(telegram_id or "").strip()
    if not token or not telegram_id.isdigit():
        return False
    with connect(db_path) as conn:
        cur = conn.execute(
            "UPDATE login_tokens SET telegram_id = ?, confirmed_at = ?"
            " WHERE token = ? AND confirmed_at IS NULL AND used_at IS NULL"
            " AND expires_at > ?",
            (telegram_id, _now(), token, _now()),
        )
        return cur.rowcount == 1


def consume_login_token(db_path: str, token: str) -> Optional[dict]:
    """Claim a confirmed link exactly once; returns the row that was claimed."""
    if not token:
        return None
    with connect(db_path) as conn:
        row = conn.execute(
            "SELECT * FROM login_tokens WHERE token = ?", (token,)
        ).fetchone()
        if row is None or not row["confirmed_at"] or row["used_at"]:
            return None
        if str(row["expires_at"]) <= _now():
            return None
        cur = conn.execute(
            "UPDATE login_tokens SET used_at = ? WHERE token = ? AND used_at IS NULL",
            (_now(), token),
        )
        if cur.rowcount != 1:
            return None
        return dict(row)
