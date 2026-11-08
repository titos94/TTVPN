from __future__ import annotations

import asyncio
import logging
import secrets
import uuid
from datetime import datetime, timedelta
from typing import Callable, Optional

from peer_lifecycle_lock import serialize_peer_lifecycle

logger = logging.getLogger(__name__)

AWG_BASES = ("awg3", "awg2", "awg", "awg_legacy")

# Roles that the public surface must never assign.
PUBLIC_ROLE = "user"


class ProvisionError(RuntimeError):
    pass


# ----------------------------------------------------------------- helpers

def _panel():
    import app as panel  # late import; app.py is fully defined when this runs
    return panel


def _mutate_blocking(mutator: Callable[[dict], object], attempts: int = 3):
    """Load data.json, apply mutator, save. Retries on stale-generation races.

    Runs in a worker thread; the caller must hold the panel ``DATA_LOCK`` so a
    concurrent panel write cannot load the same snapshot and silently overwrite
    this one (a lost payment period or a lost traffic counter).
    """
    panel = _panel()
    last_error: Optional[Exception] = None
    for _ in range(attempts):
        data = panel.load_data()
        result = mutator(data)
        try:
            panel.save_data(data)
            return result
        except Exception as exc:  # noqa: BLE001 - generation races surface as generic errors
            last_error = exc
            logger.warning("billing: save_data retry after error: %s", exc)
    raise ProvisionError(f"Could not persist panel data: {last_error}")


async def _mutate(mutator: Callable[[dict], object], attempts: int = 3):
    """Serialise a read-modify-write of data.json with every panel writer.

    The panel guards its own writers with ``DATA_LOCK`` across load and save,
    so taking the same lock here is what makes billing updates lossless.
    """
    panel = _panel()
    async with panel.DATA_LOCK:
        return await asyncio.to_thread(_mutate_blocking, mutator, attempts)


def find_user(user_id: str) -> Optional[dict]:
    for user in _panel().load_data().get("users", []):
        if user.get("id") == user_id:
            return user
    return None


def find_user_by_email(email: str) -> Optional[dict]:
    email = (email or "").strip().lower()
    if not email:
        return None
    for user in _panel().load_data().get("users", []):
        if str(user.get("email") or "").strip().lower() == email:
            return user
        if str(user.get("username") or "").strip().lower() == email:
            return user
    return None


def find_user_by_telegram(telegram_id: str) -> Optional[dict]:
    telegram_id = str(telegram_id or "").strip()
    if not telegram_id:
        return None
    for user in _panel().load_data().get("users", []):
        if str(user.get("telegramId") or "").strip() == telegram_id:
            return user
    return None


def get_user_by_any(*, user_id: Optional[str] = None, email: Optional[str] = None,
                    telegram_id: Optional[str] = None) -> Optional[dict]:
    if user_id:
        return find_user(user_id)
    if email:
        return find_user_by_email(email)
    if telegram_id:
        return find_user_by_telegram(telegram_id)
    return None


# ----------------------------------------------------------------- target

def pick_target(server_id: Optional[int] = None, protocol: Optional[str] = None,
                server_uid: Optional[str] = None) -> tuple[int, str]:
    data = _panel().load_data()
    servers = data.get("servers") or []
    if not servers:
        raise ProvisionError("На панели не настроен ни один VPN-сервер")

    # A plan stores the stable server uid: the array index shifts when servers
    # are reordered or deleted, which used to silently retarget paid orders.
    if server_uid:
        resolved = next(
            (i for i, s in enumerate(servers) if s.get("uid") == server_uid), None
        )
        if resolved is None:
            logger.error(
                "billing: plan target server uid %s no longer exists, "
                "falling back to the configured default", server_uid,
            )
        else:
            server_id = resolved

    if server_id is None or not (0 <= server_id < len(servers)):
        server_id = next(
            (i for i, s in enumerate(servers) if s.get("self_service_enabled")), 0
        )
    server = servers[server_id]
    protocols = server.get("protocols") or {}

    chosen = protocol if protocol in protocols else None
    if not chosen:
        chosen = next((c for c in AWG_BASES if c in protocols), None)
    if not chosen:
        chosen = next(
            (k for k in protocols if str(k).split("__", 1)[0] in AWG_BASES), None
        )
    if not chosen:
        raise ProvisionError(
            f"На сервере {server.get('name') or server.get('host')} нет AmneziaWG-протокола"
        )
    return server_id, chosen


# ----------------------------------------------------------------- users

async def create_customer(*, email: str = "", telegram_id: Optional[str] = None,
                    password: Optional[str] = None,
                    password_hash: Optional[str] = None,
                    trial_days: int = 0,
                    email_verified: bool = False) -> dict:
    panel = _panel()
    email = (email or "").strip().lower()
    telegram_id = str(telegram_id).strip() if telegram_id else None

    if not email and not telegram_id:
        raise ProvisionError("Нужен email или Telegram")

    existing = find_user_by_email(email) if email else find_user_by_telegram(telegram_id)
    if existing:
        raise ProvisionError("Пользователь уже зарегистрирован")

    username = email or f"tg_{telegram_id}"
    if any(str(u.get("username", "")).lower() == username.lower()
           for u in panel.load_data().get("users", [])):
        username = f"{username}_{secrets.token_hex(2)}"

    now = datetime.now()
    expires = (now + timedelta(days=trial_days)) if trial_days > 0 else now
    new_user = {
        "id": str(uuid.uuid4()),
        "username": username,
        "password_hash": password_hash or (panel.hash_password(password) if password else None),
        "role": PUBLIC_ROLE,
        "telegramId": telegram_id,
        "email": email or None,
        "email_verified": bool(email_verified),
        "description": "self-registered",
        "traffic_limit": 0,
        "traffic_reset_strategy": "never",
        "traffic_used": 0,
        "traffic_total": 0,
        "last_reset_at": now.isoformat(),
        "expiration_date": expires.isoformat(),
        "enabled": True,
        "disable_reasons": [],
        "created_at": now.isoformat(),
        "remnawave_uuid": None,
        "share_enabled": False,
        "share_token": secrets.token_urlsafe(16),
        "share_password_hash": None,
    }

    def apply(data: dict):
        if any(u.get("id") == new_user["id"] for u in data.get("users", [])):
            return
        if email and any(str(u.get("email") or "").strip().lower() == email
                         for u in data.get("users", [])):
            raise ProvisionError("Пользователь уже зарегистрирован")
        if any(str(u.get("username", "")).lower() == username.lower()
               for u in data.get("users", [])):
            raise ProvisionError("Логин уже занят")
        if telegram_id and any(str(u.get("telegramId") or "") == telegram_id
                               for u in data.get("users", [])):
            raise ProvisionError("Telegram уже привязан к другому аккаунту")
        # Derive enabled/disable_reasons from the paid period. A fresh account
        # with no trial is therefore inactive until its first payment, which
        # keeps /my and self-service closed to non-paying visitors.
        panel.reconcile_user_state(new_user, now)
        data.setdefault("users", []).append(new_user)

    await _mutate(apply)
    return new_user


def check_password(user: dict, password: str) -> bool:
    panel = _panel()
    stored = user.get("password_hash")
    if not stored:
        return False
    return panel.verify_password(password, stored)


async def set_password(user_id: str, password: str) -> None:
    panel = _panel()
    password_hash = panel.hash_password(password)

    def apply(data: dict):
        for user in data.get("users", []):
            if user.get("id") == user_id:
                user["password_hash"] = password_hash
                return
        raise ProvisionError("Пользователь не найден")

    await _mutate(apply)


async def extend_subscription(user_id: str, days: int) -> dict:
    """Add paid days on top of the current remaining period; re-enable access."""
    if days <= 0:
        raise ProvisionError("Некорректный срок тарифа")
    panel = _panel()
    now = datetime.now()
    result: dict = {}

    def apply(data: dict):
        user = next((u for u in data.get("users", []) if u.get("id") == user_id), None)
        if not user:
            raise ProvisionError("Пользователь не найден")
        base = now
        if user.get("expiration_date"):
            try:
                current = datetime.fromisoformat(str(user["expiration_date"]))
                if current.tzinfo is not None:
                    current = current.astimezone().replace(tzinfo=None)
                if current > now:
                    base = current
            except (ValueError, TypeError):
                pass
        user["expiration_date"] = (base + timedelta(days=days)).isoformat()
        # Recompute enabled/disable_reasons from the new expiration date while
        # preserving a manual administrator ban.
        panel.reconcile_user_state(user, now)
        result.update(user)

    await _mutate(apply)
    return result


async def set_enabled(user_id: str, enabled: bool) -> None:
    panel = _panel()
    now = datetime.now()

    def apply(data: dict):
        user = next((u for u in data.get("users", []) if u.get("id") == user_id), None)
        if not user:
            raise ProvisionError("Пользователь не найден")
        # reconcile_user_state(manual=True) means "explicitly enabled", so the
        # ban flag must be passed inverted: manual=False adds the 'manual'
        # disable reason.
        panel.reconcile_user_state(user, now, manual=bool(enabled))

    await _mutate(apply)


def is_active(user: dict) -> bool:
    if not user or not user.get("enabled", True) or user.get("disable_reasons"):
        return False
    export = user.get("expiration_date")
    if not export:
        return False
    try:
        expiry = datetime.fromisoformat(str(export))
        if expiry.tzinfo is not None:
            expiry = expiry.astimezone().replace(tzinfo=None)
        return expiry > datetime.now()
    except (ValueError, TypeError):
        return False


def is_billing_managed(user_id: Optional[str]) -> bool:
    """True for accounts created by the public registration/cabinet flow."""
    if not user_id:
        return False
    try:
        user = find_user(user_id)
    except Exception:  # noqa: BLE001
        return False
    return bool(user and str(user.get("description") or "") == "self-registered")


# ----------------------------------------------------------- connections

def user_connections(user_id: str) -> list[dict]:
    return [c for c in _panel().load_data().get("user_connections", [])
            if c.get("user_id") == user_id]


async def connection_config(user_id: str, connection_id: str) -> dict:
    """Build the config/QR payload for one device, authorized by billing uid.

    The upstream ``/api/my/connections/{id}/config`` authorizes through the
    panel session only. On the client host that session can lag behind the
    cabinet (an expired account, a login as somebody else), and the client then
    gets "not found" for their own device. Here the owner is the billing
    account, so the two identities cannot drift apart.
    """
    panel = _panel()

    def build() -> dict:
        data = panel.load_data()
        conn = next(
            (c for c in data.get("user_connections", [])
             if c.get("id") == connection_id and c.get("user_id") == user_id),
            None,
        )
        if not conn:
            raise LookupError("connection")
        servers = data.get("servers") or []
        server_id = int(conn.get("server_id") or 0)
        if not 0 <= server_id < len(servers):
            raise LookupError("server")
        server = servers[server_id]
        protocol = str(conn.get("protocol") or "")
        info = (server.get("protocols") or {}).get(protocol) or {}
        port = info.get("port", "55424")
        pub_host, pub_port = panel.protocol_public_endpoint(server, protocol)
        ssh = panel.get_ssh(server)
        ssh.connect()
        try:
            manager = panel.get_protocol_manager(ssh, protocol)
            config = panel._manager_call(
                manager, "get_client_config", protocol, conn["client_id"],
                pub_host, port, public_port=pub_port,
            )
        finally:
            ssh.disconnect()
        return {
            "config": config,
            "vpn_importable": vpn_importable(protocol),
            **panel.config_payloads(config, server, protocol),
        }

    return await asyncio.to_thread(build)


def _used_peer_names(user_id: str, server_uid: str, protocol: str,
                     connections: list[dict]) -> set[str]:
    """Names already taken for this identity, including removed peers.

    ``peer_creates`` keys its journal on ``uuid5(user_id, server uid, protocol,
    name)`` and replays a finished operation, so a name whose peer was deleted
    can never be created again. Ignoring the journal would make the paid slot
    unrestorable ("Operation is blocked" every time).
    """
    used = {str(conn.get("name") or "") for conn in connections}
    try:
        journal = _panel().peer_creates.journal.read()
    except Exception:  # noqa: BLE001 - a missing journal must not block a payment
        logger.warning("billing: peer-create journal is unreadable")
        return used
    for operation in journal.values():
        identity = operation.get("identity") or []
        if (len(identity) == 4 and identity[0] == user_id
                and identity[1] == server_uid and identity[2] == protocol):
            used.add(str(identity[3]))
    return used


def _free_peer_name(used: set[str]) -> str:
    """Smallest free ``device-N`` name for one identity."""
    index = 1
    while f"device-{index}" in used:
        index += 1
    return f"device-{index}"


async def ensure_connections(user_id: str, *, device_limit: int, server_id: int,
                             protocol: str) -> list[dict]:
    """Create the paid number of devices; never removes extra ones.

    One plan slot is one registered device, not one VPN connection: several
    protocol profiles may hang off the same device without consuming slots.
    """
    panel = _panel()
    device_limit = max(1, int(device_limit or 1))
    existing = user_connections(user_id)
    from billing import store as billing_store

    known_devices = billing_store.ensure_devices(
        _panel_settings_db(), user_id, existing
    )
    missing = device_limit - len(known_devices)
    if missing <= 0:
        return existing

    servers = panel.load_data().get("servers") or []
    server_uid = (servers[server_id].get("uid") if 0 <= server_id < len(servers) else "") or ""
    for _ in range(missing):
        current = user_connections(user_id)
        name = _free_peer_name(
            _used_peer_names(user_id, server_uid, protocol, current)
        )
        try:
            await panel.peer_creates.create(user_id, server_id, protocol, name, None)
        except Exception as exc:  # noqa: BLE001 - one device failure must not sink the payment
            logger.exception("billing: failed to create device %s for %s: %s", name, user_id, exc)
            continue
        after = user_connections(user_id)
        billing_store.ensure_devices(_panel_settings_db(), user_id, after)
    return user_connections(user_id)


def user_devices(user_id: str) -> list[dict]:
    """Devices of one account, each with its connection (protocol) ids."""
    from billing import store as billing_store
    from billing.settings import db_path

    return billing_store.ensure_devices(
        db_path(), user_id, user_connections(user_id)
    )


# A device may run a couple of protocol profiles (for example AmneziaWG 3.1
# plus AmneziaWG 2.0 for an older phone). They share one plan slot, so the cap
# keeps the rule from turning into "unlimited peers for one payment".
MAX_PROFILES_PER_DEVICE = 3


# Links the Amnezia client can import directly. For any other protocol
# (Telemt's tg://, a raw config) the panel still returns a ``vpn://``-shaped
# string, but only as a QR fallback: the OS handler cannot import it, so the
# cabinet must not offer "open in Amnezia" for it.
IMPORTABLE_BASES = AWG_BASES + ("xray",)


def vpn_importable(protocol: str) -> bool:
    """Whether the config of this protocol can be opened by the Amnezia app."""
    return str(protocol or "").split("__", 1)[0] in IMPORTABLE_BASES


def profile_options(device: dict, connections: dict, servers: list[dict]) -> list[dict]:
    """AWG protocols installed on the device's server, minus the attached ones."""
    owned = [connections[cid] for cid in device.get("connection_ids", []) if cid in connections]
    if not owned:
        return []
    server_id = int(owned[0].get("server_id") or 0)
    if not (0 <= server_id < len(servers)):
        return []
    attached = {str(conn.get("protocol") or "") for conn in owned}
    options = []
    for protocol, record in (servers[server_id].get("protocols") or {}).items():
        if str(protocol).split("__", 1)[0] not in AWG_BASES:
            continue
        if protocol in attached or not (record or {}).get("installed", True):
            continue
        options.append({
            "id": protocol,
            "label": panel_protocol_name(protocol, record),
        })
    return options


def panel_protocol_name(protocol: str, record: Optional[dict] = None) -> str:
    from protocol_names import protocol_display_name

    record = record or {}
    return protocol_display_name(
        protocol, record.get("header_protection"), record.get("protocol_version"),
    )


@serialize_peer_lifecycle
async def add_device_profile(user_id: str, device_id: str,
                             protocol: Optional[str] = None) -> dict:
    """Create one more protocol profile on an existing device (same slot).

    The server is taken from the device itself, not from the request: a client
    must not be able to aim a profile at another host. The new peer still goes
    through ``peer_creates`` so its creation is journaled and recoverable.
    """
    panel = _panel()
    from billing import store as billing_store

    db = _panel_settings_db()
    devices = billing_store.ensure_devices(db, user_id, user_connections(user_id))
    device = next((item for item in devices if item["id"] == device_id), None)
    if not device:
        raise ProvisionError("Устройство не найдено")
    if len(device.get("connection_ids") or []) >= MAX_PROFILES_PER_DEVICE:
        raise ProvisionError(
            f"На одно устройство можно добавить не больше "
            f"{MAX_PROFILES_PER_DEVICE} подключений"
        )
    connections = {str(conn.get("id")): conn for conn in user_connections(user_id)}
    owned = [connections[cid] for cid in device["connection_ids"] if cid in connections]
    if not owned:
        raise ProvisionError("У устройства нет подключений")
    server_id = int(owned[0].get("server_id") or 0)
    servers = panel.load_data().get("servers") or []
    if not (0 <= server_id < len(servers)):
        raise ProvisionError("Сервер не найден")
    records = servers[server_id].get("protocols") or {}
    attached = {str(conn.get("protocol") or "") for conn in owned}
    protocol = str(protocol or "")
    if protocol:
        if protocol in attached:
            # The cabinet only offers free protocols; the API must not spend a
            # profile slot on a second peer with the same protocol either.
            raise ProvisionError("Этот протокол уже добавлен к устройству")
        if protocol not in records or protocol.split("__", 1)[0] not in AWG_BASES:
            raise ProvisionError("Этот протокол недоступен на сервере")
        if not (records[protocol] or {}).get("installed", True):
            raise ProvisionError("Этот протокол не установлен на сервере")
    else:
        # No protocol in the request: take the first one the device does not
        # have yet, in the same order the cabinet offers them.
        candidates = [
            name for name, record in records.items()
            if name.split("__", 1)[0] in AWG_BASES
            and name not in attached
            and (record or {}).get("installed", True)
        ]
        if not candidates:
            raise ProvisionError(
                "На этом сервере нет свободных протоколов для устройства"
            )
        protocol = candidates[0]

    name = _free_peer_name(
        _used_peer_names(
            user_id, str(servers[server_id].get("uid") or ""), protocol,
            user_connections(user_id),
        )
    )
    try:
        await panel.peer_creates.create(user_id, server_id, protocol, name, None)
    except Exception as exc:  # noqa: BLE001 - surface a readable message
        logger.exception("billing: could not add profile to device %s: %s", device_id, exc)
        raise ProvisionError("Не удалось создать подключение, попробуйте позже") from exc

    created = next(
        (conn for conn in user_connections(user_id)
         if str(conn.get("name") or "") == name
         and int(conn.get("server_id") or 0) == server_id
         and str(conn.get("protocol") or "") == protocol),
        None,
    )
    if not created:
        raise ProvisionError("Подключение не появилось, обновите страницу")
    try:
        billing_store.attach_connection(db, user_id, device_id, str(created["id"]))
    except ValueError as exc:
        # The peer exists and stays usable; the next page load adopts it as its
        # own device, so nothing is lost even though the grouping failed.
        logger.exception("billing: could not attach profile to device %s", device_id)
        raise ProvisionError("Подключение создано, обновите страницу") from exc
    return created


def _panel_settings_db() -> str:
    from billing.settings import db_path

    return db_path()
