from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import re
import secrets
import time
import uuid
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from typing import Optional
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import (HTMLResponse, JSONResponse, RedirectResponse,
                               Response)
from itsdangerous import BadSignature, TimestampSigner

from billing import (branding, crypto, mailer as billing_mailer, promo, provision,
                     store, telegram_auth, yookassa)
from billing.settings import (BillingSettings, get_settings, invalidate_cache,
                              new_csrf_token, save_overrides)

logger = logging.getLogger(__name__)

router = APIRouter()

EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
MIN_PASSWORD = 8

_RATE: dict[str, list[float]] = {}


# ----------------------------------------------------------------- utils

def _panel():
    import app as panel
    return panel


def _db() -> str:
    return get_settings().db_path


def _render(request: Request, template: str, **kwargs) -> HTMLResponse:
    panel = _panel()
    settings = get_settings()
    kwargs.setdefault("csrf", _csrf(request))
    kwargs.setdefault("billing", settings)
    kwargs.setdefault("format_money", _format_money)
    tg_ready = bool(_tg_token() and _tg_username())
    kwargs.setdefault("tg_bot", _tg_username() if tg_ready else "")
    kwargs.setdefault("tg_ready", tg_ready)
    kwargs.setdefault("tg_onetap", tg_ready)
    apex = os.environ.get("BILLING_APEX_HOSTS", "ttvpn.ru,www.ttvpn.ru").split(",")[0].strip()
    kwargs.setdefault("landing_url", f"https://{apex}/" if apex else "/landing")
    kwargs.setdefault("billing_user", _current(request))
    kwargs.setdefault("brand", branding.current())
    from ttvpn_assistant import assistant_enabled
    kwargs.setdefault("assistant_available", assistant_enabled())
    return panel.tpl(request, template, **kwargs)


def _format_money(kopecks: int) -> str:
    return f"{Decimal(int(kopecks)) / Decimal(100):.0f} ₽" if int(kopecks) % 100 == 0 \
        else f"{Decimal(int(kopecks)) / Decimal(100):.2f} ₽".replace(".", ",")


def _csrf(request: Request) -> str:
    token = request.session.get("billing_csrf")
    if not token:
        token = new_csrf_token()
        request.session["billing_csrf"] = token
    return token


def _check_csrf(request: Request, token: str) -> bool:
    expected = request.session.get("billing_csrf")
    return bool(expected and token and hmac.compare_digest(str(expected), str(token)))


def _uid(request: Request) -> Optional[str]:
    uid = request.session.get("billing_uid")
    return str(uid) if uid else None


def _current(request: Request) -> Optional[dict]:
    uid = _uid(request)
    return provision.find_user(uid) if uid else None


def _login(request: Request, user: dict) -> None:
    request.session["billing_uid"] = user["id"]
    # Keep the panel session in sync with the billing identity. Leaving a stale
    # user_id behind is how a client ends up authorized as somebody else: the
    # cabinet lists *their* devices, while /api/my/* answers for the old user.
    _sync_panel_session(request, user)
    request.session["billing_csrf"] = new_csrf_token()


def _sync_panel_session(request: Request, user: dict) -> None:
    """Grant (or revoke) the upstream panel session for this cabinet user.

    Two identities share one cookie on the client host, so they must always
    describe the same account: an inactive or different account must never
    keep a foreign ``user_id`` that /api/my/* would authorize as.
    """
    eligible = bool(user.get("enabled", True)
                    and user.get("role") in ("admin", "support", "user"))
    if eligible:
        request.session["user_id"] = user["id"]
    else:
        request.session.pop("user_id", None)


def _logout(request: Request) -> None:
    for key in ("billing_uid", "user_id", "billing_csrf"):
        request.session.pop(key, None)


def _rate_limit(request: Request, bucket: str, limit: int, window: int) -> bool:
    key = f"{bucket}:{_client_ip(request)}"
    now = time.time()
    hits = [t for t in _RATE.get(key, []) if now - t < window]
    if len(hits) >= limit:
        _RATE[key] = hits
        return False
    hits.append(now)
    _RATE[key] = hits
    return True


def _client_ip(request: Request) -> str:
    """Real client address behind the Caddy reverse proxy.

    The leftmost X-Forwarded-For entry is whatever the browser sent, so it is
    spoofable and useless for rate limits. Caddy appends the address it saw;
    with one trusted proxy in front of the panel that last entry is the client.
    """
    peer = request.client.host if request.client else "?"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded and _trust_proxy():
        try:
            address = ipaddress.ip_address(peer)
            networks = [ipaddress.ip_network(part.strip(), strict=False) for part in
                        os.environ.get("BILLING_TRUSTED_PROXY_CIDRS", "").split(",") if part.strip()]
            if any(address in network for network in networks):
                return str(ipaddress.ip_address(forwarded.split(",")[-1].strip()))
        except ValueError:
            pass
    return peer


def _trust_proxy() -> bool:
    raw = os.environ.get("BILLING_TRUST_PROXY", "0").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _trial_state(user: dict, settings: BillingSettings) -> dict:
    """What the cabinet may show about the free trial for this user."""
    used = store.get_trial(_db(), user["id"]) is not None
    has_payments = store.has_paid_payment(_db(), user["id"])
    configured = bool(settings.trial_enabled and settings.trial_days > 0)
    active = False
    try:
        active = provision.is_active(user)
    except Exception:  # noqa: BLE001
        active = False
    return {
        "configured": configured,
        "days": int(settings.trial_days),
        "devices": int(settings.trial_device_limit or 1),
        "used": used,
        "available": configured and not used and not has_payments and not active,
    }


# ----------------------------------------------------------------- pages

@router.post('/api/billing/assistant/message')
async def api_assistant_message(request: Request):
    """Customer-initiated support without tools or account data disclosure."""
    user = _current(request)
    if not user or not user.get('enabled', True):
        return JSONResponse({'error': 'Необходимо войти в кабинет'}, status_code=401)
    if not _rate_limit(request, 'assistant', 10, 600):
        return JSONResponse({'error': 'Слишком много запросов. Попробуйте позже'}, status_code=429)
    raw = bytearray()
    async for chunk in request.stream():
        if len(raw) + len(chunk) > 8192:
            return JSONResponse({'error': 'Сообщение слишком велико'}, status_code=413)
        raw.extend(chunk)
    try:
        body = json.loads(raw)
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({'error': 'Некорректный запрос'}, status_code=400)
    if not isinstance(body, dict):
        return JSONResponse({'error': 'Некорректный запрос'}, status_code=400)
    if not _check_csrf(request, body.get('csrf', '')):
        return JSONResponse({'error': 'Сессия устарела, обновите страницу'}, status_code=400)
    if body.get('external_processing_accepted') is not True:
        return JSONResponse({'error': 'Подтвердите отправку сообщения внешнему сервису'}, status_code=400)
    from ttvpn_assistant import answer_customer
    try:
        answer = await answer_customer(body.get('message'))
    except ValueError as exc:
        return JSONResponse({'error': str(exc)}, status_code=400)
    except RuntimeError:
        return JSONResponse({'error': 'Помощник временно недоступен'}, status_code=503)
    return {'answer': answer}

@router.get("/cabinet/login", response_class=HTMLResponse)
def page_login(request: Request, next: str = "", promo: str = ""):
    if _uid(request):
        return RedirectResponse(_safe_next(next), 303)
    settings = get_settings()
    return _render(
        request, "billing_login.html", error=None,
        trial_public=settings.trial_enabled and settings.trial_days > 0,
        trial_days=settings.trial_days,
        next_url=_safe_next(next),
        promo_code=store.normalize_code(promo),
    )


@router.get("/register", response_class=HTMLResponse)
def page_register(request: Request, next: str = "", promo: str = ""):
    if _uid(request):
        return RedirectResponse(_safe_next(next), 303)
    settings = get_settings()
    return _render(
        request, "billing_register.html", error=None,
        trial_public=settings.trial_enabled and settings.trial_days > 0,
        trial_days=settings.trial_days,
        next_url=_safe_next(next),
        promo_code=store.normalize_code(promo),
    )


@router.get("/plans", response_class=HTMLResponse)
def page_plans(request: Request, promo: str = "", plan: str = ""):
    user = _current(request)
    code = store.normalize_code(promo)
    wanted = str(plan or "").strip()
    if not user:
        params = [f"promo={quote(code)}"] if code else []
        if wanted:
            params.append(f"plan={quote(wanted)}")
        target = "/plans" + ("?" + "&".join(params) if params else "")
        return RedirectResponse(
            f"/cabinet/login?next={quote(target, safe='')}", 303
        )
    settings = get_settings()
    plans = store.list_plans(_db())
    active = provision.is_active(user)
    base, base_label = _renewal_base(user)
    return _render(
        request,
        "billing_plans.html",
        user=user,
        plans=plans,
        active=active,
        expiry=_fmt_date(user.get("expiration_date")),
        trial=_trial_state(user, settings),
        promo_code=code,
        selected_plan=(next((p["id"] for p in plans if p["id"] == wanted), "")),
        base_iso=base.isoformat(timespec="seconds"),
        base_label=base_label,
        plan_days={plan["id"]: int(plan["days"]) for plan in plans},
    )


@router.get("/cabinet", response_class=HTMLResponse)
def page_cabinet(request: Request):
    user = _current(request)
    if not user:
        return RedirectResponse("/cabinet/login", 303)
    # Heal a stale panel session left by an earlier login on this device.
    _sync_panel_session(request, user)
    settings = get_settings()
    payments = store.list_user_payments(_db(), user["id"])
    active = provision.is_active(user)
    base, base_label = _renewal_base(user)
    return _render(
        request,
        "billing_cabinet.html",
        user=user,
        active=active,
        expiry=_fmt_date(user.get("expiration_date")),
        devices=_flat_devices(user["id"]),
        device_limit=_device_limit(user),
        payments=_with_plan_names(payments),
        plans=store.list_plans(_db()),
        trial=_trial_state(user, settings),
        trial_record=store.get_trial(_db(), user["id"]),
        base_label=base_label,
        now=datetime.now(),
    )


@router.get("/cabinet/connections", response_class=HTMLResponse)
def page_connections(request: Request, device: str = ""):
    """Client-facing devices page: config text, vpn:// key and QR, in one place.

    The panel's own /my page is intentionally not used on the client host: it
    is rendered inside the admin chrome and looks like a cut-down admin panel.
    """
    user = _current(request)
    if not user:
        return RedirectResponse("/cabinet/login?next=%2Fcabinet%2Fconnections", 303)
    # Heal a stale panel session left by an earlier login on this device; the
    # cabinet lists billing-owned devices, so the panel identity must match.
    _sync_panel_session(request, user)
    settings = get_settings()
    devices = _named_devices(user["id"])
    try:
        import app as panel

        data = panel.load_data()
        servers = data.get("servers") or []
        owned = {
            str(item.get("id")): item
            for device in devices for item in device.get("connections", [])
        }
        for device in devices:
            for item in device.get("connections", []):
                sid = int(item.get("server_id") or 0)
                server = servers[sid] if 0 <= sid < len(servers) else {}
                item["server_name"] = (
                    server.get("name") or server.get("host") or "Сервер"
                )
                record = (server.get("protocols") or {}).get(item.get("protocol")) or {}
                item["protocol_label"] = panel.protocol_display_name(
                    item.get("protocol", ""), record.get("header_protection"),
                    record.get("protocol_version"),
                )
            device["protocol_options"] = provision.profile_options(device, owned, servers)
    except Exception:  # noqa: BLE001 - the page must render even with bad data
        logger.exception("billing: could not enrich connections")
        for device in devices:
            device["protocol_options"] = []
            for item in device.get("connections", []):
                item.setdefault("server_name", "Сервер")
                item.setdefault("protocol_label", str(item.get("protocol") or ""))

    active = provision.is_active(user)
    return _render(
        request,
        "billing_my.html",
        user=user,
        devices=devices,
        device_limit=_device_limit(user),
        active=active,
        expiry=_fmt_date(user.get("expiration_date")),
        focus_device=device,
        trial=_trial_state(user, settings),
        # Один источник правды о том, импортирует ли клиент ключ этого
        # протокола: список протоколов живёт в provision, а не в шаблоне.
        vpn_importable=provision.vpn_importable,
    )


@router.get("/cabinet/logout")
def page_logout(request: Request):
    _logout(request)
    return RedirectResponse("/", 303)


def _named_devices(user_id: str) -> list[dict]:
    """Registered devices, each with its connection (protocol profile) rows.

    ``name`` is the device name the client chose; a connection keeps its own
    label so two profiles of one device stay distinguishable.
    """
    labels = store.device_labels(_db(), user_id)
    connections = {
        str(conn.get("id")): conn for conn in provision.user_connections(user_id)
    }
    result: list[dict] = []
    for device in provision.user_devices(user_id):
        owned = []
        for connection_id in device.get("connection_ids", []):
            connection = connections.get(str(connection_id))
            if not connection:
                continue
            owned.append({
                **connection,
                "name": labels.get(str(connection.get("id"))) or connection.get("name"),
            })
        result.append({
            "id": device["id"],
            "name": device.get("name") or (owned[0].get("name") if owned else "Устройство"),
            "connections": owned,
            # ``provision.profile_options`` counts attached protocols through
            # ``connection_ids``. Without it the cabinet offered no protocols and
            # the "add profile" form never rendered, so the whole second-profile
            # feature was unreachable from the UI even though the API worked.
            "connection_ids": [str(item["id"]) for item in owned],
            "profiles_max": provision.MAX_PROFILES_PER_DEVICE,
        })
    return result


def _flat_devices(user_id: str) -> list[dict]:
    """One row per registered device for the cabinet overview.

    ``id`` stays the first connection id so existing ``?device=`` deep links
    keep opening the right QR code.
    """
    rows = []
    for device in _named_devices(user_id):
        connections = device.get("connections") or []
        first = connections[0] if connections else {}
        rows.append({
            "id": first.get("id") or device["id"],
            "device_id": device["id"],
            "name": device["name"],
            "protocol": first.get("protocol") or "",
            "protocol_label": first.get("protocol_label") or first.get("protocol") or "",
            "connection_count": len(connections),
            "connections": connections,
        })
    return rows


@router.post("/api/billing/devices/{device_id}/name")
async def api_registered_device_name(request: Request, device_id: str):
    """Rename a registered device (one plan slot), not a single profile."""
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход в кабинет"}, status_code=401)
    if not _rate_limit(request, "device_name", 30, 300):
        return JSONResponse({"error": "Слишком много запросов"}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 60 or any(ord(c) < 32 for c in name):
        return JSONResponse({"error": "Введите название длиной от 1 до 60 символов"}, status_code=400)
    if not store.rename_device(_db(), user["id"], device_id, name):
        return JSONResponse({"error": "Устройство не найдено"}, status_code=404)
    return {"status": "ok", "name": name}


@router.post("/api/billing/devices/{device_id}/profiles")
async def api_add_device_profile(request: Request, device_id: str):
    """Add a second protocol profile to a device without spending a slot."""
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход в кабинет"}, status_code=401)
    if not _rate_limit(request, "device_profile", 10, 300):
        return JSONResponse({"error": "Слишком много запросов"}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "") if isinstance(body, dict) else ""):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    if not provision.is_active(user):
        return JSONResponse(
            {"error": "Подписка не активна. Продлите тариф, чтобы добавить подключение"},
            status_code=403,
        )
    try:
        created = await provision.add_device_profile(
            user["id"], device_id, body.get("protocol")
        )
    except provision.ProvisionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {
        "status": "ok",
        "connection_id": str(created.get("id") or ""),
        "name": created.get("name") or "",
    }


@router.post("/api/billing/connections/{device_id}/name")
async def api_device_name(request: Request, device_id: str):
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход в кабинет"}, status_code=401)
    if not _rate_limit(request, "device_name", 30, 300):
        return JSONResponse({"error": "Слишком много запросов"}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    name = str(body.get("name") or "").strip()
    if not name or len(name) > 60 or any(ord(c) < 32 for c in name):
        return JSONResponse({"error": "Введите название длиной от 1 до 60 символов"}, status_code=400)
    if not any(str(d.get("id")) == device_id for d in provision.user_connections(user["id"])):
        return JSONResponse({"error": "Устройство не найдено"}, status_code=404)
    store.set_device_label(_db(), user["id"], device_id, name)
    return {"status": "ok", "name": name}


@router.post("/api/billing/connections/{device_id}/config")
async def api_device_config(request: Request, device_id: str):
    """Config, vpn:// key and QR frames for a device owned by this account.

    Kept in the billing namespace on purpose: the upstream endpoint resolves
    the owner from the panel session, which is not the identity the cabinet
    shows devices for.
    """
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход в кабинет"}, status_code=401)
    if not _rate_limit(request, "config", 90, 300):
        return JSONResponse(
            {"error": "Слишком много запросов — подождите минуту"}, status_code=429
        )
    if not provision.is_active(user):
        return JSONResponse(
            {"error": "Подписка не активна. Продлите доступ, чтобы получить подключение.",
             "code": "subscription_inactive"},
            status_code=403,
        )
    try:
        payload = await provision.connection_config(user["id"], device_id)
    except LookupError:
        return JSONResponse({"error": "Устройство не найдено"}, status_code=404)
    except Exception:  # noqa: BLE001 - SSH/manager failures are per-request
        logger.exception("billing: could not build config for device %s", device_id)
        return JSONResponse(
            {"error": "Сервер не отдал конфигурацию — попробуйте ещё раз"},
            status_code=502,
        )
    return JSONResponse(payload, headers={"Cache-Control": "no-store"})


@router.get("/billing/return", response_class=HTMLResponse)
async def page_return(request: Request, pid: str = ""):
    user = _current(request)
    if not pid or not user:
        return _render(request, "billing_return.html", status="unknown", plan=None,
                       payment=None)
    row = store.get_payment(_db(), pid)
    if not row or row["user_id"] != user["id"]:
        return _render(request, "billing_return.html", status="unknown", plan=None,
                       payment=None)
    status = await _refresh_payment(row)
    plan = store.get_plan(_db(), row["plan_id"], only_active=False)
    if status == "succeeded":
        fresh = provision.find_user(user["id"])
        if fresh:
            _sync_panel_session(request, fresh)
    return _render(request, "billing_return.html", status=status, plan=plan,
                   payment=row)


@router.get("/billing/mock/{payment_id}", response_class=HTMLResponse)
def page_mock(request: Request, payment_id: str):
    settings = get_settings()
    if not settings.mock:
        return HTMLResponse("Not available", status_code=404)
    user = _current(request)
    row = store.get_payment(_db(), payment_id)
    if not user or not row or row["user_id"] != user["id"]:
        return RedirectResponse("/cabinet/login", 303)
    plan = store.get_plan(_db(), row["plan_id"], only_active=False)
    return _render(request, "billing_mock.html", payment=row, plan=plan)


@router.get("/billing/legal/{document_type}", response_class=HTMLResponse)
def page_legal_unavailable(request: Request, document_type: str):
    if document_type not in ("privacy", "terms"):
        return HTMLResponse("Not Found", status_code=404)
    return _render(request, "billing_legal_unavailable.html", document_type=document_type)


@router.get("/landing", response_class=HTMLResponse)
def page_landing(request: Request):
    settings = get_settings()
    try:
        plans = store.list_plans(_db())
    except Exception:  # noqa: BLE001 - a landing must not fail on a broken DB
        logger.exception("billing: could not load plans for the landing page")
        plans = []
    return _render(
        request, "billing_landing.html",
        plans=plans,
        trial_public=settings.trial_enabled and settings.trial_days > 0,
        trial_days=settings.trial_days,
    )


# ----------------------------------------------------------------- auth api

@router.post("/api/billing/register")
async def api_register(request: Request):
    """Step 1: validate the form and send a confirmation code to the mailbox."""
    settings = get_settings()
    if not _rate_limit(request, "register", 20, 3600):
        return JSONResponse({"error": "Слишком много попыток. Попробуйте позже."}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    if not EMAIL_RE.match(email) or len(email) > 320:
        return JSONResponse({"error": "Укажите корректный email"}, status_code=400)
    if len(password) < MIN_PASSWORD:
        return JSONResponse({"error": f"Пароль должен быть не короче {MIN_PASSWORD} символов"}, status_code=400)
    if provision.find_user_by_email(email):
        return JSONResponse({"error": "Пользователь уже зарегистрирован"}, status_code=400)

    verification = _verification_active(settings)
    if not verification:
        try:
            user = await provision.create_customer(
                email=email, password=password, trial_days=0, email_verified=True
            )
        except provision.ProvisionError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        _login(request, user)
        return {"status": "ok", "redirect": "/plans"}

    sent, error, retry_after = await _send_code(request, settings, email, password)
    if error:
        return error
    return {
        "status": "verify",
        "email": email,
        "ttl_minutes": int(settings.code_ttl_minutes or 10),
        "resend_after": max(0, int(settings.code_resend_seconds or 0)),
    }


@router.post("/api/billing/register/verify")
async def api_register_verify(request: Request):
    """Step 2: check the code, then create the verified account."""
    settings = get_settings()
    if not _rate_limit(request, "verify", 30, 900):
        return JSONResponse({"error": "Слишком много попыток. Попробуйте позже."}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    email = str(body.get("email", "")).strip().lower()
    code = re.sub(r"\D+", "", str(body.get("code", "")))
    if not email or len(code) != CODE_LENGTH:
        return JSONResponse({"error": "Введите код из письма"}, status_code=400)

    db = _db()
    row = store.get_email_code(db, email)
    if not row:
        return JSONResponse(
            {"error": "Код не найден или устарел. Запросите новый."}, status_code=400
        )
    if _is_expired(row.get("expires_at")):
        store.delete_email_code(db, row["id"])
        return JSONResponse({"error": "Код истёк. Запросите новый."}, status_code=400)
    if int(row.get("attempts") or 0) >= MAX_CODE_ATTEMPTS:
        store.delete_email_code(db, row["id"])
        return JSONResponse(
            {"error": "Слишком много неверных попыток. Запросите новый код."}, status_code=400
        )
    if not hmac.compare_digest(str(row.get("code_hash") or ""), _code_hash(email, code)):
        attempts = store.bump_email_code_attempts(db, row["id"])
        left = max(0, MAX_CODE_ATTEMPTS - attempts)
        return JSONResponse(
            {"error": f"Неверный код. Осталось попыток: {left}"}, status_code=400
        )

    try:
        user = await provision.create_customer(
            email=email,
            password_hash=row.get("password_hash"),
            trial_days=0,
            email_verified=True,
        )
    except provision.ProvisionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    store.delete_email_code(db, row["id"])
    _login(request, user)
    logger.info("billing: email verified and account created for %s", email)
    return {"status": "ok", "redirect": "/plans"}


@router.post("/api/billing/register/resend")
async def api_register_resend(request: Request):
    settings = get_settings()
    if not _rate_limit(request, "resend", 10, 900):
        return JSONResponse({"error": "Слишком много попыток. Попробуйте позже."}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return JSONResponse({"error": "Укажите корректный email"}, status_code=400)

    pending = store.get_email_code(_db(), email)
    if not pending or _is_expired(pending.get("expires_at")):
        return JSONResponse(
            {"error": "Начните регистрацию заново: код больше не действует."}, status_code=400
        )
    cooldown = max(0, int(settings.code_resend_seconds or 0))
    elapsed = _seconds_since(pending.get("created_at"))
    if cooldown and elapsed is not None and elapsed < cooldown:
        return JSONResponse(
            {"error": f"Новый код можно запросить через {int(cooldown - elapsed)} с."},
            status_code=429,
        )
    sent, error, retry_after = await _send_code(
        request, settings, email, None, password_hash=pending.get("password_hash")
    )
    if error:
        return error
    return {"status": "ok", "ttl_minutes": int(settings.code_ttl_minutes or 10)}


CODE_LENGTH = 6
MAX_CODE_ATTEMPTS = 5
RESET_PURPOSE = "reset"


def _verification_active(settings: BillingSettings) -> bool:
    """Confirmation is required only when it can actually be delivered."""
    if not settings.verify_email:
        return False
    return billing_mailer.status().get("ready", False)


def _code_hash(email: str, code: str) -> str:
    secret = os.environ.get("SECRET_KEY", "")
    return hmac.new(secret.encode(), f"{email}:{code}".encode(), hashlib.sha256).hexdigest()


def _is_expired(value: Optional[str]) -> bool:
    if not value:
        return True
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return True
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment <= datetime.now(timezone.utc)


def _seconds_since(value: Optional[str]) -> Optional[float]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except ValueError:
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - moment).total_seconds()


async def _send_code(request: Request, settings: BillingSettings, email: str,
                     password: Optional[str], password_hash: Optional[str] = None,
                     purpose: str = "register"):
    """Create and deliver a new code. Returns (sent, error_response, retry_after)."""
    db = _db()
    ip = _client_ip(request)
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    if store.count_email_codes_from_ip(db, ip, since) >= 20:
        return False, JSONResponse(
            {"error": "Слишком много запросов кодов с этого адреса. Попробуйте позже."},
            status_code=429,
        ), None

    if password_hash is None and password is not None:
        password_hash = await asyncio.to_thread(_panel().hash_password, password or "")

    code = f"{secrets.randbelow(10 ** CODE_LENGTH):0{CODE_LENGTH}d}"
    ttl = max(1, int(settings.code_ttl_minutes or 10))
    expires_at = (datetime.now(timezone.utc) + timedelta(minutes=ttl)).isoformat()
    store.create_email_code(
        db, email=email, purpose=purpose, code_hash=_code_hash(email, code),
        expires_at=expires_at, password_hash=password_hash, ip=ip,
    )

    brand = "TTVPN"
    try:
        brand = str((_panel().load_data().get("settings", {})
                     .get("appearance", {}) or {}).get("title") or brand)
    except Exception:  # noqa: BLE001
        pass
    try:
        await asyncio.to_thread(
            billing_mailer.send_verification_code, email, code, ttl, brand,
            "password" if purpose == RESET_PURPOSE else "register",
        )
    except billing_mailer.BillingMailError as exc:
        pending = store.get_email_code(db, email, purpose=purpose)
        if pending:
            store.delete_email_code(db, pending["id"])
        logger.warning("billing: verification mail to %s failed: %s", email, exc)
        return False, JSONResponse({"error": str(exc)}, status_code=503), None
    logger.info("billing: %s code sent to %s", purpose, email)
    return True, None, None


@router.post("/api/billing/login")
async def api_login(request: Request):
    if not _rate_limit(request, "login", 20, 600):
        return JSONResponse({"error": "Слишком много попыток. Попробуйте позже."}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    email = str(body.get("email", "")).strip().lower()
    password = str(body.get("password", ""))
    user = provision.find_user_by_email(email) if email else None
    if not user or not provision.check_password(user, password):
        return JSONResponse({"error": "Неверный email или пароль"}, status_code=401)
    _login(request, user)
    return {"status": "ok", "redirect": _post_login_redirect(user)}


# ------------------------------------------------------ password recovery

@router.get("/cabinet/reset", response_class=HTMLResponse)
def page_reset(request: Request):
    if _uid(request):
        return RedirectResponse("/cabinet", 303)
    settings = get_settings()
    return _render(
        request, "billing_reset.html",
        reset_ready=_verification_active(settings),
        mail_ready=bool(billing_mailer.status().get("ready", False)),
        min_password=MIN_PASSWORD,
        code_ttl=int(settings.code_ttl_minutes or 10),
    )


@router.post("/api/billing/reset/start")
async def api_reset_start(request: Request):
    if not _rate_limit(request, "reset", 10, 900):
        return JSONResponse({"error": "Слишком много попыток. Попробуйте позже."}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    email = str(body.get("email", "")).strip().lower()
    if not EMAIL_RE.match(email):
        return JSONResponse({"error": "Укажите корректный email"}, status_code=400)
    settings = get_settings()
    if not _verification_active(settings):
        return JSONResponse(
            {"error": "Восстановление по почте недоступно — напишите в поддержку"},
            status_code=503,
        )
    # A registered address gets a code; an unknown one gets the same answer so
    # the form cannot be used to enumerate customers.
    user = provision.find_user_by_email(email)
    if user:
        sent, error, _retry = await _send_code(
            request, settings, email, None, purpose=RESET_PURPOSE
        )
        if error:
            return error
    return {"status": "sent", "ttl_minutes": int(settings.code_ttl_minutes or 10)}


@router.post("/api/billing/reset/confirm")
async def api_reset_confirm(request: Request):
    if not _rate_limit(request, "resetconfirm", 30, 900):
        return JSONResponse({"error": "Слишком много попыток. Попробуйте позже."}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    email = str(body.get("email", "")).strip().lower()
    code = re.sub(r"\D+", "", str(body.get("code", "")))
    password = str(body.get("password", ""))
    if not EMAIL_RE.match(email) or len(code) != CODE_LENGTH:
        return JSONResponse({"error": "Введите код из письма"}, status_code=400)
    if len(password) < MIN_PASSWORD:
        return JSONResponse(
            {"error": f"Пароль должен быть не короче {MIN_PASSWORD} символов"}, status_code=400
        )
    db = _db()
    row = store.get_email_code(db, email, purpose=RESET_PURPOSE)
    if not row:
        return JSONResponse(
            {"error": "Код не найден или устарел. Запросите новый."}, status_code=400
        )
    if _is_expired(row.get("expires_at")):
        store.delete_email_code(db, row["id"])
        return JSONResponse({"error": "Код истёк. Запросите новый."}, status_code=400)
    if int(row.get("attempts") or 0) >= MAX_CODE_ATTEMPTS:
        store.delete_email_code(db, row["id"])
        return JSONResponse(
            {"error": "Слишком много неверных попыток. Запросите новый код."}, status_code=400
        )
    if not hmac.compare_digest(str(row.get("code_hash") or ""), _code_hash(email, code)):
        attempts = store.bump_email_code_attempts(db, row["id"])
        left = max(0, MAX_CODE_ATTEMPTS - attempts)
        return JSONResponse(
            {"error": f"Неверный код. Осталось попыток: {left}"}, status_code=400
        )
    user = provision.find_user_by_email(email)
    if not user:
        store.delete_email_code(db, row["id"])
        return JSONResponse({"error": "Аккаунт не найден"}, status_code=400)
    try:
        await provision.set_password(user["id"], password)
    except provision.ProvisionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    store.delete_email_code(db, row["id"])
    _login(request, user)
    logger.info("billing: password reset for %s", user["id"])
    return {"status": "ok", "redirect": _post_login_redirect(user)}


@router.get("/cabinet/password", response_class=HTMLResponse)
def page_password(request: Request):
    user = _current(request)
    if not user:
        return RedirectResponse("/cabinet/login?next=%2Fcabinet%2Fpassword", 303)
    return _render(
        request, "billing_password.html", user=user,
        has_password=bool(user.get("password_hash")),
        min_password=MIN_PASSWORD,
    )


@router.post("/api/billing/password")
async def api_password(request: Request):
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход"}, status_code=401)
    if not _rate_limit(request, "password", 10, 900):
        return JSONResponse({"error": "Слишком много попыток"}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    password = str(body.get("password", ""))
    confirm = str(body.get("confirm", ""))
    if len(password) < MIN_PASSWORD:
        return JSONResponse(
            {"error": f"Пароль должен быть не короче {MIN_PASSWORD} символов"}, status_code=400
        )
    if password != confirm:
        return JSONResponse({"error": "Пароли не совпадают"}, status_code=400)
    if user.get("password_hash"):
        current = str(body.get("current", ""))
        if not provision.check_password(user, current):
            return JSONResponse({"error": "Текущий пароль указан неверно"}, status_code=400)
    try:
        await provision.set_password(user["id"], password)
    except provision.ProvisionError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    request.session["billing_csrf"] = new_csrf_token()
    logger.info("billing: password changed for %s", user["id"])
    return {"status": "ok", "message": "Пароль обновлён"}


def _post_login_redirect(user: dict) -> str:
    # The upstream panel has no '/admin' route: its admin UI is '/' (server
    # list) plus /users and /settings, all served on the admin host.
    if user.get("role") in ("admin", "support"):
        return f"{_admin_url()}/"
    return "/cabinet/connections" if provision.is_active(user) else "/cabinet"


@router.get("/api/billing/telegram/config")
def api_tg_config():
    username = _tg_username()
    ready = bool(_tg_token() and username)
    return {"enabled": ready, "bot_username": username if ready else ""}


# ---------------------------------------------------------- one-tap tg login
# Telegram Login Widget asks for a phone number on a device where telegram.org
# has no session, which is exactly the case on a new phone. A bot deep link
# does not: tap the button, press Start, come back — the browser polls and the
# account is linked by telegram_id.

TG_LOGIN_TTL_MINUTES = 10


def _telegram_bot_running() -> bool:
    try:
        return bool(_panel().tg_bot.is_running())
    except Exception:  # noqa: BLE001
        return False


def _panel_telegram() -> dict:
    """Telegram block from the upstream panel settings, used as a fallback."""
    try:
        return (( _panel().load_data().get("settings") or {}).get("telegram") or {})
    except Exception:  # noqa: BLE001
        return {}


def _tg_token() -> str:
    panel = _panel_telegram()
    if panel.get("token"):
        return str(panel["token"]).strip() if panel.get("enabled") else ""
    return (get_settings().telegram_bot_token or "").strip()


def _tg_username() -> str:
    panel = _panel_telegram()
    if panel.get("token"):
        if not panel.get("enabled"):
            return ""
        try:
            if not _telegram_bot_running():
                return ""
            return str(_panel().tg_bot.running_username() or "").strip().lstrip("@")
        except Exception:  # noqa: BLE001
            return ""
    return (get_settings().telegram_bot_username or "").strip().lstrip("@")


def _ensure_telegram_bot() -> bool:
    """Start polling if nobody does, otherwise a pressed Start is never read.

    The upstream panel starts the bot at boot when Telegram is enabled. The
    cabinet may be used on an installation where only the billing token is
    set, so the same bot is started on demand (once — the loop is a singleton).
    """
    panel = _panel()
    expected = _tg_username().lower()
    if _telegram_bot_running():
        try:
            running = str(panel.tg_bot.running_username() or "").lower()
        except Exception:  # noqa: BLE001
            running = ""
        # Unknown (bot still starting) is fine; a *different* bot is not, since
        # the deep link would open it and the confirmation would never arrive.
        return not (expected and running and running != expected)
    token = _tg_token()
    if not token:
        return False
    try:
        panel.tg_bot.launch_bot(
            token, panel.load_data, panel.generate_vpn_link, panel.save_data,
            self_service_svc=panel.self_service_connections,
        )
    except Exception:  # noqa: BLE001
        logger.exception("billing: could not start the telegram bot for login links")
        return False
    logger.info("billing: telegram bot started on demand for one-tap login")
    return True


def _tg_deep_link(token: str) -> str:
    return f"https://t.me/{_tg_username()}?start=lk_{token}"


@router.get("/cabinet/tg", response_class=HTMLResponse)
async def page_tg_start(request: Request, next: str = "", promo: str = ""):
    target = _safe_next(next)
    code = store.normalize_code(promo)
    if code and target == "/cabinet":
        target = f"/plans?promo={quote(code)}"
    elif code and target.startswith("/plans") and "promo=" not in target:
        target += ("&" if "?" in target else "?") + f"promo={quote(code)}"
    if _uid(request):
        return RedirectResponse(target, 303)
    if not (_tg_token() and _tg_username()) or not _ensure_telegram_bot():
        return RedirectResponse("/cabinet/login?tg=unavailable", 303)
    if not _rate_limit(request, "tgstart", 30, 600):
        return RedirectResponse("/cabinet/login?tg=slow", 303)
    token = secrets.token_urlsafe(24)
    expires = (datetime.now(timezone.utc)
               + timedelta(minutes=TG_LOGIN_TTL_MINUTES)).isoformat()
    store.create_login_token(_db(), token=token, expires_at=expires,
                             ip=_client_ip(request))
    request.session["billing_tg"] = token
    request.session["billing_tg_next"] = target
    return _render(
        request, "billing_tg.html",
        token=token, link=_tg_deep_link(token), ttl=TG_LOGIN_TTL_MINUTES,
        next_url=target,
    )


@router.get("/cabinet/tg/{token}", response_class=HTMLResponse)
def page_tg_wait(request: Request, token: str):
    if _uid(request):
        return RedirectResponse("/cabinet", 303)
    if request.session.get("billing_tg") != token:
        # The link was opened in another browser/profile; start over there.
        return RedirectResponse("/cabinet/tg", 303)
    row = store.get_login_token(_db(), token)
    if not row or _is_expired(row.get("expires_at")):
        return RedirectResponse("/cabinet/tg", 303)
    return _render(
        request, "billing_tg.html",
        token=token, link=_tg_deep_link(token), ttl=TG_LOGIN_TTL_MINUTES,
        next_url=request.session.get("billing_tg_next") or "",
    )


@router.get("/api/billing/telegram/poll/{token}")
async def api_tg_poll(request: Request, token: str):
    if request.session.get("billing_tg") != token:
        return JSONResponse({"status": "foreign"}, status_code=403)
    db = _db()
    row = store.get_login_token(db, token)
    if not row or _is_expired(row.get("expires_at")):
        return JSONResponse({"status": "expired"}, status_code=410)
    if not row.get("confirmed_at"):
        return {"status": "pending"}
    claimed = store.consume_login_token(db, token)
    if not claimed:
        return {"status": "pending"}
    telegram_id = str(claimed.get("telegram_id") or "")
    user = provision.find_user_by_telegram(telegram_id)
    if not user:
        try:
            user = await provision.create_customer(telegram_id=telegram_id, trial_days=0)
        except provision.ProvisionError as exc:
            logger.warning("billing: telegram account creation failed: %s", exc)
            return JSONResponse(
                {"status": "error", "error": "Не удалось создать аккаунт Telegram"},
                status_code=400,
            )
    _login(request, user)
    request.session.pop("billing_tg", None)
    target = request.session.pop("billing_tg_next", "") or ""
    if not target or target == "/cabinet":
        target = "/cabinet/connections" if provision.is_active(user) else "/cabinet"
    logger.info("billing: telegram one-tap login for %s", user.get("id"))
    return {"status": "ok", "redirect": target}


@router.get("/api/billing/telegram/login")
async def api_tg_login(request: Request):
    token = _tg_token()
    if not (token and _tg_username()):
        return RedirectResponse("/cabinet/login?tg=unconfigured", 303)
    params = {key: value for key, value in request.query_params.items()}
    try:
        profile = telegram_auth.verify_login_widget(params, token)
    except telegram_auth.TelegramAuthError as exc:
        logger.warning("billing: telegram login rejected: %s", exc)
        return RedirectResponse("/cabinet/login?tg=error", 303)

    user = provision.find_user_by_telegram(profile["telegram_id"])
    if not user:
        try:
            user = await provision.create_customer(
                telegram_id=profile["telegram_id"], trial_days=0
            )
        except provision.ProvisionError as exc:
            logger.warning("billing: telegram account creation failed: %s", exc)
            return RedirectResponse("/cabinet/login?tg=error", 303)
    _login(request, user)
    return RedirectResponse("/cabinet/connections" if provision.is_active(user) else "/cabinet", 303)


@router.post("/api/billing/logout")
def api_logout(request: Request):
    _logout(request)
    return {"status": "ok"}


# ----------------------------------------------------------------- payment

@router.post("/api/billing/checkout")
async def api_checkout(request: Request):
    settings = get_settings()
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход"}, status_code=401)
    if not _rate_limit(request, "checkout", 30, 600):
        return JSONResponse({"error": "Слишком много попыток"}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    plan = store.get_plan(_db(), str(body.get("plan_id", "")))
    if not plan:
        return JSONResponse({"error": "Тариф не найден"}, status_code=404)

    code = store.normalize_code(str(body.get("promo_code") or ""))
    try:
        offer = promo.quote(_db(), code, plan, user_id=user["id"]) if code else None
    except promo.PromoError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    amount = offer.final_kopecks if offer else int(plan["price_kopecks"])
    currency = settings.currency
    # Who actually takes the money. Mock checkouts and free orders used to be
    # stored as "yookassa", which made the admin revenue figure include money
    # that was never received.
    if amount <= 0:
        provider = "promo"
    elif settings.mock:
        provider = "mock"
    else:
        provider = "yookassa"
    row = store.create_payment(
        _db(), user_id=user["id"], plan_id=plan["id"], provider=provider,
        amount_kopecks=amount, currency=currency,
        idempotency_key=str(uuid.uuid4()),
        promo_code=offer.code if offer else "",
        discount_kopecks=offer.discount_kopecks if offer else 0,
        bonus_days=offer.bonus_days if offer else 0,
        days_granted=offer.days_granted if offer else 0,
        plan_days=int(plan["days"]),
        plan_device_limit=int(plan["device_limit"]),
        plan_server_uid=str(plan.get("server_uid") or ""),
        plan_protocol=str(plan.get("protocol") or ""),
    )
    # A 100% discount never needs a payment provider.
    if amount <= 0:
        store.mark_status(_db(), row["id"], "succeeded")
        await _apply_payment(row["id"])
        logger.info("billing: free checkout via promo %s for %s", row.get("promo_code"), user["id"])
        return {"free": True, "redirect": "/cabinet", "payment_id": row["id"]}

    if settings.mock or not settings.yookassa_ready:
        if not settings.mock:
            return JSONResponse(
                {"error": "Оплата временно недоступна: не настроены ключи YooKassa"},
                status_code=503,
            )
        store.set_provider_payment(
            _db(), row["id"], f"mock_{row['id'][:8]}", f"/billing/mock/{row['id']}", "pending"
        )
        return {"confirmation_url": f"/billing/mock/{row['id']}"}

    receipt = _build_receipt(settings, plan, user, amount, currency)
    try:
        created = yookassa.create_payment(
            shop_id=settings.yookassa_shop_id,
            secret_key=settings.yookassa_secret_key,
            amount_kopecks=amount,
            currency=currency,
            description="Подписка",
            idempotency_key=row["idempotency_key"],
            return_url=f"{settings.public_base}/billing/return?pid={row['id']}",
            internal_payment_id=row["id"],
            receipt=receipt,
        )
    except yookassa.YooKassaError as exc:
        logger.exception("billing: YooKassa create failed: %s", exc)
        store.mark_status(_db(), row["id"], "canceled")
        return JSONResponse({"error": "Не удалось создать платёж. Попробуйте позже."}, status_code=502)

    store.set_provider_payment(
        _db(), row["id"], created.provider_payment_id, created.confirmation_url,
        created.status, created.raw,
    )
    return {"confirmation_url": created.confirmation_url}


@router.post("/api/billing/promo/check")
async def api_promo_check(request: Request):
    """Validate a promo code, with or without a chosen tariff.

    Called from the cabinet main page (no plan yet) and from the plans page
    (plan selected). The response always includes the projected expiry date so
    the client sees what the code actually does to the term, not just a discount.
    """
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход"}, status_code=401)
    if not _rate_limit(request, "promo", 40, 300):
        return JSONResponse({"error": "Слишком много попыток"}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    code = store.normalize_code(str(body.get("promo_code") or ""))
    plan_id = str(body.get("plan_id") or "").strip()
    plan = store.get_plan(_db(), plan_id) if plan_id else None
    if not code:
        return JSONResponse({"error": "Введите промокод"}, status_code=400)
    if plan_id and not plan:
        return JSONResponse({"error": "Тариф не найден"}, status_code=404)
    try:
        offer = promo.quote(_db(), code, plan, user_id=user["id"])
    except promo.PromoError as exc:
        payload = {"error": str(exc)}
        bound = (store.get_promo_by_code(_db(), code) or {}).get("plan_id")
        if bound and plan is not None and bound != plan["id"]:
            # The page re-selects the tariff the code belongs to instead of
            # leaving the customer with a dead end.
            payload["plan_id"] = bound
            return JSONResponse(payload, status_code=409)
        return JSONResponse(payload, status_code=400)

    base, _ = _renewal_base(user)
    projection = None
    if not offer.needs_plan:
        # A "free N days" code defines the period on its own, so the new expiry
        # can be shown before any tariff is picked.
        if offer.days_granted:
            days = offer.days_granted + offer.bonus_days
        elif plan is not None:
            days = int(plan["days"]) + offer.bonus_days
        else:
            days = 0
        if days:
            projection = {
                "days": days,
                "expiry": (base + timedelta(days=days)).strftime("%d.%m.%Y"),
                "hint": _term_hint(plan, offer) if plan is not None else "",
            }
    return {
        "status": "ok",
        "code": offer.code,
        "kind": offer.kind,
        "label": offer.label,
        "needs_plan": offer.needs_plan,
        "discount_kopecks": offer.discount_kopecks,
        "bonus_days": offer.bonus_days,
        "days_granted": offer.days_granted,
        "final_kopecks": offer.final_kopecks,
        "final_label": _format_money(offer.final_kopecks) if not offer.needs_plan else "",
        "free": offer.free,
        "projection": projection,
        "can_activate": offer.kind == "free_days" and offer.days_granted > 0,
    }


def _term_hint(plan: dict, offer: promo.Quote) -> str:
    """One sentence explaining what the code does to the term."""
    if offer.kind == "free_days":
        return f"Доступ на {_days_label(offer.days_granted)} без оплаты"
    if offer.kind == "bonus_days":
        return f"+{_days_label(offer.bonus_days)} к тарифу, срок добавится после оплаты"
    return "Скидка применится при оплате этого тарифа"



@router.post("/api/billing/promo/activate")
async def api_promo_activate(request: Request):
    """Turn a "free N days" code into real access, right from the cabinet.

    Nothing is charged, so the code does not need a trip through the tariff
    page. The plan is only used for bookkeeping and the device limit.
    """
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход"}, status_code=401)
    if not _rate_limit(request, "activate", 10, 600):
        return JSONResponse({"error": "Слишком много попыток"}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    code = store.normalize_code(str(body.get("promo_code") or ""))
    if not code:
        return JSONResponse({"error": "Введите промокод"}, status_code=400)

    db = _db()
    record = store.get_promo_by_code(db, code)
    if not record:
        return JSONResponse({"error": "Промокод не найден"}, status_code=400)

    plan = None
    if record.get("plan_id"):
        plan = store.get_plan(db, record["plan_id"], only_active=False)
    if plan is None:
        active_plans = store.list_plans(db)
        if active_plans:
            plan = min(active_plans, key=lambda item: int(item["price_kopecks"]))
    if plan is None:
        return JSONResponse(
            {"error": "Тарифы не настроены — обратитесь в поддержку"}, status_code=400
        )

    try:
        offer = promo.quote(db, code, plan, user_id=user["id"])
    except promo.PromoError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)

    if offer.kind != "free_days" or offer.days_granted <= 0:
        return JSONResponse(
            {"error": "Этот промокод применяется при оплате тарифа"}, status_code=400
        )

    row = store.create_payment(
        db, user_id=user["id"], plan_id=plan["id"], provider="promo",
        amount_kopecks=0, currency=get_settings().currency,
        idempotency_key=str(uuid.uuid4()),
        promo_code=offer.code,
        discount_kopecks=offer.discount_kopecks,
        bonus_days=offer.bonus_days,
        days_granted=offer.days_granted,
        plan_days=int(plan["days"]),
        plan_device_limit=int(plan["device_limit"]),
        plan_server_uid=str(plan.get("server_uid") or ""),
        plan_protocol=str(plan.get("protocol") or ""),
    )
    store.mark_status(db, row["id"], "succeeded")
    if not await _apply_payment(row["id"]):
        logger.error("billing: promo activation failed to apply payment %s", row["id"])
        return JSONResponse(
            {"error": "Не удалось активировать промокод. Напишите в поддержку."},
            status_code=500,
        )
    logger.info(
        "billing: promo %s activated %s days for %s",
        offer.code, offer.days_granted, user["id"],
    )
    fresh = provision.find_user(user["id"]) or user
    return {
        "status": "ok",
        "days": offer.days_granted,
        "expiry": _fmt_date(fresh.get("expiration_date")),
        "redirect": "/cabinet/connections",
    }


@router.post("/api/billing/trial")
async def api_start_trial(request: Request):
    """Activate the free trial once per account."""
    settings = get_settings()
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход"}, status_code=401)
    if not settings.trial_enabled or settings.trial_days <= 0:
        return JSONResponse({"error": "Пробный период сейчас недоступен"}, status_code=400)
    if not _rate_limit(request, "trial", 10, 3600):
        return JSONResponse({"error": "Слишком много попыток"}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)

    if provision.is_active(user):
        return JSONResponse({"error": "У вас уже есть активная подписка"}, status_code=400)
    if store.get_trial(_db(), user["id"]):
        return JSONResponse({"error": "Пробный период уже был использован"}, status_code=400)
    if store.has_paid_payment(_db(), user["id"]):
        return JSONResponse({"error": "Пробный период доступен только новым клиентам"}, status_code=400)

    ip = _client_ip(request)
    days = int(settings.trial_days)
    expires_at = (datetime.now() + timedelta(days=days)).isoformat()
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    claim = await asyncio.to_thread(
        store.claim_trial, _db(), user_id=user["id"], days=days,
        expires_at=expires_at, ip=ip, since_iso=since,
        ip_limit=int(settings.trial_ip_limit or 0),
    )
    if claim != "claimed":
        messages = {
            "used": "Пробный период уже был использован",
            "paid": "Пробный период доступен только новым клиентам",
            "ip_limit": "Пробный период с этого адреса уже использован",
        }
        return JSONResponse({"error": messages.get(claim, "Пробный период недоступен")}, status_code=400)
    try:
        await provision.extend_subscription(user["id"], days)
    except Exception:  # An unknown result cannot safely be retried automatically.
        logger.exception("billing: trial claim requires reconciliation for %s", user["id"])
        return JSONResponse({"error": "Не удалось завершить выдачу пробного периода. Обратитесь в поддержку."}, status_code=503)

    if settings.provision_devices:
        try:
            server_id, protocol = await asyncio.to_thread(
                provision.pick_target, settings.server_id, settings.protocol or None
            )
            await provision.ensure_connections(
                user["id"],
                device_limit=max(1, int(settings.trial_device_limit or 1)),
                server_id=server_id,
                protocol=protocol,
            )
        except Exception:  # noqa: BLE001 - the trial itself is already granted
            logger.exception("billing: trial device provisioning failed for %s", user["id"])

    _trigger_peer_sync(user["id"])
    request.session["user_id"] = user["id"]
    logger.info("billing: trial of %s days started for %s", days, user["id"])
    return {"status": "ok", "days": days, "redirect": "/cabinet"}


@router.post("/api/billing/mock/{payment_id}/confirm")
async def api_mock_confirm(request: Request, payment_id: str):
    settings = get_settings()
    if not settings.mock:
        return JSONResponse({"error": "Not available"}, status_code=404)
    user = _current(request)
    row = store.get_payment(_db(), payment_id)
    if not user or not row or row["user_id"] != user["id"]:
        return JSONResponse({"error": "Платёж не найден"}, status_code=404)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    store.mark_status(_db(), row["id"], "succeeded")
    await _apply_payment(row["id"])
    refreshed = provision.find_user(user["id"])
    if refreshed and provision.is_active(refreshed):
        request.session["user_id"] = refreshed["id"]
    return {"status": "ok", "redirect": "/cabinet"}


@router.get("/api/billing/payment/{payment_id}")
async def api_payment_status(request: Request, payment_id: str):
    user = _current(request)
    row = store.get_payment(_db(), payment_id)
    if not user or not row or row["user_id"] != user["id"]:
        return JSONResponse({"error": "Платёж не найден"}, status_code=404)
    status = await _refresh_payment(row)
    return {"status": status}


@router.post("/api/billing/yookassa/webhook")
async def api_yookassa_webhook(request: Request):
    settings = get_settings()
    try:
        event = await request.json()
    except Exception:  # noqa: BLE001
        return JSONResponse({"error": "bad request"}, status_code=400)
    if not isinstance(event, dict) or not isinstance(event.get("object"), dict):
        return JSONResponse({"error": "bad request"}, status_code=400)
    obj = event["object"]
    provider_id = obj.get("id")
    if (not isinstance(provider_id, str) or not provider_id
            or len(provider_id) > 128 or provider_id != provider_id.strip()):
        return JSONResponse({"error": "no payment id"}, status_code=400)
    if not settings.yookassa_ready:
        return JSONResponse({"error": "not configured"}, status_code=503)

    try:
        verified = yookassa.verify_payment(
            shop_id=settings.yookassa_shop_id,
            secret_key=settings.yookassa_secret_key,
            provider_payment_id=provider_id,
        )
    except yookassa.YooKassaError as exc:
        logger.warning("billing: webhook verify failed: %s", exc)
        return JSONResponse({"error": "verification failed"}, status_code=400)

    row = store.get_payment_by_provider(_db(), provider_id)
    if not row:
        return {"ok": True}
    if (verified.amount_kopecks != row["amount_kopecks"]
            or verified.currency != row["currency"]
            or (verified.internal_payment_id and verified.internal_payment_id != row["id"])):
        logger.warning("billing: webhook amount/metadata mismatch for %s", provider_id)
        return JSONResponse({"error": "mismatch"}, status_code=400)

    if verified.status == "succeeded":
        store.mark_status(_db(), row["id"], "succeeded")
        applied = await _apply_payment(row["id"])
        if not applied:
            # Let YooKassa retry: the period was not granted yet. Returning 200
            # here used to lose the order when the JSON store was busy.
            logger.error("billing: payment %s confirmed but not applied yet", row["id"])
            return JSONResponse({"ok": False}, status_code=503)
    elif verified.status == "canceled":
        _cancel_payment(row["id"])
    return {"ok": True}


# ----------------------------------------------------------------- apply

async def _refresh_payment(row: dict) -> str:
    """Best-effort server-side verification when the user returns from YooKassa."""
    settings = get_settings()
    if row["status"] == "succeeded":
        applied = await _apply_payment(row["id"])
        return "succeeded" if applied else "pending"
    if row["status"] in ("canceled", "refunded"):
        return row["status"]
    if row.get("provider") != "yookassa":
        # Mock and free orders have no provider payment to verify: asking
        # YooKassa about a `mock_...` id only produced a failed API call.
        return row["status"]
    if not settings.yookassa_ready or not row.get("provider_payment_id"):
        return row["status"]
    try:
        verified = yookassa.verify_payment(
            shop_id=settings.yookassa_shop_id,
            secret_key=settings.yookassa_secret_key,
            provider_payment_id=row["provider_payment_id"],
        )
    except yookassa.YooKassaError:
        return row["status"]
    if verified.status == "succeeded":
        if (verified.amount_kopecks != row["amount_kopecks"]
                or verified.currency != row["currency"]
                or (verified.internal_payment_id
                    and verified.internal_payment_id != row["id"])):
            logger.warning(
                "billing: payment %s terms mismatch on refresh", row["id"]
            )
            return row["status"]
        store.mark_status(_db(), row["id"], "succeeded")
        applied = await _apply_payment(row["id"])
        return "succeeded" if applied else "pending"
    if verified.status == "canceled":
        _cancel_payment(row["id"])
        return "canceled"
    return verified.status


def _cancel_payment(payment_id: str) -> None:
    if not store.mark_status(_db(), payment_id, "canceled"):
        # A settled order must not be canceled by a late or replayed notice;
        # the refused transition keeps the granted term and the revenue.
        logger.warning(
            "billing: ignored cancel for settled payment %s", payment_id
        )


async def _apply_payment(payment_id: str) -> bool:
    row = store.get_payment(_db(), payment_id)
    if not row or row["status"] != "succeeded":
        return False

    # One webhook delivery wins the right to grant the period; every concurrent
    # replay must answer "not yet confirmed" so the provider retries instead of
    # receiving a 200 for a grant that has not happened.
    claim = store.claim_payment_application(_db(), payment_id)
    if claim.status == store.CLAIM_ALREADY_APPLIED:
        return True  # days are already granted, by this or an earlier handler
    if claim.status != store.CLAIM_GRANTED:
        logger.info(
            "billing: payment %s is being applied by another handler", payment_id
        )
        return False

    plan = store.get_plan(_db(), row["plan_id"], only_active=False)
    if not plan:
        store.release_payment_claim(_db(), payment_id, claim.token)
        logger.error("billing: payment %s references a missing plan", payment_id)
        return False

    # Extending the paid period is the critical step: if it fails we release the
    # claim so a retry/replay can apply it later. Device provisioning below is
    # best-effort and must never roll the paid period back.
    days_granted = int(row.get("days_granted") or 0)
    bonus_days = int(row.get("bonus_days") or 0)
    # A "free N days" promo defines the period itself; every other promo adds
    # its bonus days on top of the plan length.
    plan_days = int(row.get("plan_days") or 0) or int(plan["days"])
    total_days = (days_granted + bonus_days) if days_granted > 0 \
        else (plan_days + bonus_days)
    try:
        await provision.extend_subscription(row["user_id"], total_days)
    except Exception:  # noqa: BLE001
        logger.exception("billing: extending subscription for %s failed", payment_id)
        store.release_payment_claim(_db(), payment_id, claim.token)
        return False

    # The period is granted: settle the claim now, so a slow provisioning step
    # cannot keep the payment "in progress" for the next notification.
    if not store.confirm_payment_applied(_db(), payment_id, claim.token):
        logger.error(
            "billing: payment %s was granted while its claim was already lost",
            payment_id,
        )

    await _provision_paid_devices(row["user_id"], plan, row)

    # The code is consumed only now, once the period is really granted, so an
    # abandoned checkout cannot burn a limited promo.
    if row.get("promo_code"):
        promo_row = store.get_promo_by_code(_db(), row["promo_code"])
        if promo_row:
            store.record_redemption(
                _db(), promo_id=promo_row["id"], user_id=row["user_id"],
                payment_id=row["id"],
                discount_kopecks=int(row.get("discount_kopecks") or 0),
                bonus_days=int(row.get("bonus_days") or 0),
            )

    # Re-enable previously suspended peers immediately instead of waiting for
    # the panel's 10-minute traffic/state cycle.
    _trigger_peer_sync(row["user_id"])
    return True


async def _provision_paid_devices(user_id: str, plan: dict, payment: dict) -> bool:
    """Create the paid devices; retryable until the plan count is reached.

    The paid period is granted before this step and is never rolled back. A
    failed SSH round-trip therefore must not mark the order as provisioned —
    ``/api/billing/devices/sync`` (called when the cabinet is opened) retries it.
    """
    settings = get_settings()
    if not settings.provision_devices:
        logger.info("billing: device provisioning disabled (BILLING_PROVISION_DEVICES=0)")
        store.mark_provisioned(_db(), payment["id"])
        return True

    limit = int(payment.get("plan_device_limit") or 0) or int(plan.get("device_limit") or 0) or 1
    plan_uid = payment.get("plan_server_uid") or plan.get("server_uid") or None
    plan_protocol = payment.get("plan_protocol") or plan.get("protocol") or None
    try:
        server_id, protocol = await asyncio.to_thread(
            provision.pick_target,
            plan.get("server_id") if plan.get("server_id") is not None else settings.server_id,
            plan_protocol or settings.protocol or None,
            server_uid=plan_uid,
        )
        await provision.ensure_connections(
            user_id, device_limit=limit, server_id=server_id, protocol=protocol,
        )
        have = len(provision.user_devices(user_id))
        if have >= limit:
            store.mark_provisioned(_db(), payment["id"])
            return True
        logger.warning(
            "billing: %s has %s of %s paid devices after provisioning", user_id, have, limit
        )
    except Exception:  # noqa: BLE001 - paid period is already granted
        logger.exception("billing: device provisioning for payment %s failed", payment["id"])
    return False


@router.post("/api/billing/devices/sync")
async def api_devices_sync(request: Request):
    """Retry device creation for a paid order that never got its devices."""
    user = _current(request)
    if not user:
        return JSONResponse({"error": "Требуется вход в кабинет"}, status_code=401)
    if not _rate_limit(request, "devices_sync", 12, 300):
        return JSONResponse({"error": "Слишком много запросов"}, status_code=429)
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "") if isinstance(body, dict) else ""):
        return JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    if not provision.is_active(user):
        return {"status": "ok", "restored": 0}
    row = store.last_unprovisioned_payment(_db(), user["id"])
    if not row:
        return {"status": "ok", "restored": 0}
    plan = store.get_plan(_db(), row["plan_id"], only_active=False)
    if not plan:
        store.mark_provisioned(_db(), row["id"])
        return {"status": "ok", "restored": 0}
    before = len(provision.user_devices(user["id"]))
    await _provision_paid_devices(user["id"], plan, row)
    after = len(provision.user_devices(user["id"]))
    return {"status": "ok", "restored": max(0, after - before)}


def _trigger_peer_sync(user_id: str) -> None:
    try:
        panel = _panel()
        loop = asyncio.get_running_loop()
        loop.create_task(panel.perform_toggle_user({}, user_id, True))
    except Exception:  # noqa: BLE001 - periodic sync remains a fallback
        logger.warning("billing: could not schedule immediate peer sync for %s", user_id)


# ----------------------------------------------------------------- helpers

def _build_receipt(settings: BillingSettings, plan: dict, user: dict,
                   amount: int, currency: str) -> Optional[dict]:
    if not settings.receipt_enabled:
        return None
    email = str(user.get("email") or "").strip()
    if not email:
        return None
    value = f"{Decimal(amount) / Decimal(100):.2f}"
    return {
        "customer": {"email": email},
        "items": [{
            "description": "Подписка",
            "quantity": "1.00",
            "amount": {"value": value, "currency": currency},
            "vat_code": settings.vat_code,
        }],
        "send": True,
    }


async def _json(request: Request) -> dict:
    try:
        data = await request.json()
        return data if isinstance(data, dict) else {}
    except Exception:  # noqa: BLE001
        return {}


def _fmt_date(value: Optional[str]) -> str:
    if not value:
        return "—"
    try:
        dt = datetime.fromisoformat(str(value))
        return dt.strftime("%d.%m.%Y %H:%M")
    except (ValueError, TypeError):
        return str(value)


# --------------------------------------------------------------- branding

@router.get("/billing/brand/logo")
def brand_logo():
    payload = branding.logo_file()
    if not payload:
        return HTMLResponse("Not Found", status_code=404)
    body, media_type = payload
    return Response(
        content=body, media_type=media_type,
        headers={"Cache-Control": "public, max-age=600"},
    )


@router.get("/billing/brand/icon")
def brand_icon():
    payload = branding.icon_file()
    if not payload:
        return HTMLResponse("Not Found", status_code=404)
    body, media_type = payload
    return Response(
        content=body, media_type=media_type,
        headers={"Cache-Control": "public, max-age=600"},
    )


@router.get("/billing/brand/operator")
def brand_operator():
    payload = branding.operator_file()
    if not payload:
        return HTMLResponse("Not Found", status_code=404)
    body, media_type = payload
    return Response(
        content=body, media_type=media_type,
        headers={"Cache-Control": "public, max-age=600"},
    )


@router.get("/billing/brand/operator-small")
def brand_operator_small():
    payload = branding.operator_small_file()
    if not payload:
        # SVG/GIF uploads (or a Pillow failure) have no crop: serve the
        # original so the chip is never empty.
        payload = branding.operator_file()
    if not payload:
        return HTMLResponse("Not Found", status_code=404)
    body, media_type = payload
    return Response(
        content=body, media_type=media_type,
        headers={"Cache-Control": "public, max-age=600"},
    )


@router.post("/api/billing/admin/brand")
async def api_admin_brand_upload(request: Request):
    """Store the service logo sent as a base64 data URL from the admin page."""
    admin, error = await _admin_guard(request)
    if error:
        return error
    body = await _json(request)
    try:
        payload = branding.decode_upload(body.get("data") or "")
        state = await asyncio.to_thread(
            branding.save, str(body.get("filename") or ""), payload
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:  # noqa: BLE001
        logger.exception("billing: brand upload failed")
        return JSONResponse({"error": "Не удалось сохранить картинку"}, status_code=500)
    logger.info("billing: brand logo updated by %s", admin.get("username"))
    return {"status": "ok", "brand": state}


@router.post("/api/billing/admin/brand/delete")
async def api_admin_brand_delete(request: Request):
    admin, error = await _admin_guard(request)
    if error:
        return error
    await asyncio.to_thread(branding.delete)
    logger.info("billing: brand logo removed by %s", admin.get("username"))
    return {"status": "ok"}


@router.post("/api/billing/admin/brand/operator")
async def api_admin_operator_upload(request: Request):
    admin, error = await _admin_guard(request)
    if error:
        return error
    body = await _json(request)
    try:
        payload = branding.decode_upload(body.get("data") or "")
        state = await asyncio.to_thread(
            branding.save_operator, str(body.get("filename") or ""), payload
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    except Exception:  # noqa: BLE001
        logger.exception("billing: operator avatar upload failed")
        return JSONResponse({"error": "Не удалось сохранить картинку"}, status_code=500)
    logger.info("billing: operator avatar updated by %s", admin.get("username"))
    return {"status": "ok", "brand": state}


@router.post("/api/billing/admin/brand/operator/delete")
async def api_admin_operator_delete(request: Request):
    admin, error = await _admin_guard(request)
    if error:
        return error
    await asyncio.to_thread(branding.delete_operator)
    logger.info("billing: operator avatar removed by %s", admin.get("username"))
    return {"status": "ok"}


# ------------------------------------------------------------ admin cabinet

ADMIN_PAGE = "/billing-admin"


def _admin_session(request: Request) -> Optional[dict]:
    """Only a real panel admin may manage billing keys or tariffs."""
    try:
        user = _panel().get_current_user(request)
    except Exception:  # noqa: BLE001
        return None
    if user and user.get("role") == "admin":
        return user
    return None


def _admin_denied_redirect(request: Request) -> RedirectResponse:
    _client_hosts, admin_hosts, _apex = _hosts()
    if _host(request) in admin_hosts:
        return RedirectResponse("/login", 303)
    return RedirectResponse(f"{_admin_url()}/login?next={ADMIN_PAGE}", 303)


def _admin_denied_json() -> JSONResponse:
    return JSONResponse({"error": "Доступ только для администратора"}, status_code=403)


async def _admin_guard(request: Request):
    """Return (admin, error_response). Validates the session and CSRF."""
    admin = _admin_session(request)
    if not admin:
        return None, _admin_denied_json()
    body = await _json(request)
    if not _check_csrf(request, body.get("csrf", "")):
        return None, JSONResponse({"error": "Сессия устарела, обновите страницу"}, status_code=400)
    return admin, None


def _targets() -> list[dict]:
    try:
        servers = _panel().load_data().get("servers", [])
    except Exception:  # noqa: BLE001
        return []
    out: list[dict] = []
    for index, server in enumerate(servers):
        protocols = server.get("protocols") or {}
        names = list(protocols) if isinstance(protocols, dict) else []
        awg = [p for p in names if str(p).split("__", 1)[0] in provision.AWG_BASES]
        for proto in awg:
            out.append({
                # Stable server uid, not the array index: reordering servers
                # used to silently retarget an already sold plan.
                "value": f"{server.get('uid') or index}:{proto}",
                "label": f"{server.get('name') or server.get('host')} · {proto}",
            })
    return out


def _parse_target(raw: str) -> tuple[Optional[int], Optional[str], Optional[str]]:
    """Parse a plan target value into (index, stable uid, protocol)."""
    text = str(raw or "").strip()
    if not text:
        return None, None, None
    server_part, _, protocol = text.partition(":")
    protocol = protocol or None
    try:
        servers = _panel().load_data().get("servers", []) or []
    except Exception:  # noqa: BLE001
        servers = []
    for index, server in enumerate(servers):
        if server.get("uid") and str(server.get("uid")) == server_part:
            return index, str(server.get("uid")), protocol
    try:
        index = int(server_part)
    except ValueError:
        return None, None, protocol
    if 0 <= index < len(servers):
        return index, servers[index].get("uid"), protocol
    return index, None, protocol


def _users_overview() -> dict:
    """Counts for the admin header: cabinet clients vs. the whole panel.

    ``description == "self-registered"`` alone is too narrow — an account
    created before that marker existed, or one the operator re-created by hand,
    is still a client if it has a payment or a cabinet login. Counting payments
    keeps admin-created accounts that never touched the cabinet out of the
    number.
    """
    try:
        users = _panel().load_data().get("users", [])
    except Exception:  # noqa: BLE001
        return {"managed": 0, "active": 0, "total": 0}
    self_registered = {str(u.get("id")) for u in users
                       if str(u.get("description") or "") == "self-registered"}
    paid: set[str] = set()
    try:
        paid = {str(row["user_id"]) for row in store.list_payments(_db(), 5000)}
    except Exception:  # noqa: BLE001
        logger.exception("billing: could not list payments for the user overview")
    managed = [u for u in users
               if str(u.get("id")) in self_registered or str(u.get("id")) in paid]
    try:
        active = [u for u in managed if provision.is_active(u)]
    except Exception:  # noqa: BLE001
        active = []
    return {"managed": len(managed), "active": len(active), "total": len(users)}


def _users_by_id() -> dict[str, dict]:
    try:
        users = _panel().load_data().get("users", [])
    except Exception:  # noqa: BLE001
        return {}
    return {u.get("id"): u for u in users}


def _safe_next(target: str) -> str:
    """Only internal paths are allowed as post-login targets."""
    text = str(target or "").strip()
    if (not text.startswith("/") or text.startswith("//") or "\\" in text
            or any(ord(char) < 32 or ord(char) == 127 for char in text)):
        return "/cabinet"
    return text


def _renewal_base(user: dict) -> tuple[datetime, str]:
    """The date a new period is counted from: the later of now and the expiry.

    Mirrors ``provision.extend_subscription`` so the cabinet can promise the
    exact date the client will see after paying.
    """
    now = datetime.now()
    raw = user.get("expiration_date")
    if raw:
        try:
            current = datetime.fromisoformat(str(raw))
            if current.tzinfo is not None:
                current = current.astimezone().replace(tzinfo=None)
            if current > now:
                return current, current.strftime("%d.%m.%Y")
        except (ValueError, TypeError):
            pass
    return now, ""


def _days_label(days: int) -> str:
    days = int(days)
    if days % 10 == 1 and days % 100 != 11:
        return f"{days} день"
    if 2 <= days % 10 <= 4 and not 12 <= days % 100 <= 14:
        return f"{days} дня"
    return f"{days} дней"


def _device_limit(user: dict) -> int:
    """How many devices the client's current or last plan allows.

    The plan row can change after checkout, so the snapshot stored on the
    payment wins; the plan is only a fallback for older orders.
    """
    try:
        db = _db()
        payments = store.list_user_payments(db, user["id"], limit=50)
        for payment in payments:
            if payment.get("status") != "succeeded":
                continue
            snapshot = int(payment.get("plan_device_limit") or 0)
            if snapshot > 0:
                return snapshot
            plan = store.get_plan(db, payment["plan_id"], only_active=False)
            if plan:
                return max(1, int(plan.get("device_limit") or 1))
    except Exception:  # noqa: BLE001
        logger.exception("billing: could not resolve the device limit")
    return 1


def _with_plan_names(payments: list[dict]) -> list[dict]:
    """Attach the tariff name to a payment row for the cabinet history.

    Days and the device count come from the snapshot taken at checkout: an
    edited plan must not rewrite what an already paid order promised. Older
    rows without a snapshot fall back to the current plan.
    """
    try:
        plans = {p["id"]: p for p in store.list_plans(_db(), only_active=False)}
    except Exception:  # noqa: BLE001
        plans = {}
    for payment in payments:
        plan = plans.get(payment.get("plan_id")) or {}
        payment["plan_name"] = plan.get("name") or "Тариф удалён"
        payment["plan_days"] = (
            int(payment.get("plan_days") or 0) or int(plan.get("days") or 0)
        )
    return payments


def _mail_status(settings: BillingSettings) -> dict:
    """Mail channel state for the admin card: configured + actually required."""
    state = billing_mailer.status()
    state["required"] = bool(settings.verify_email and state.get("ready"))
    state["wanted"] = bool(settings.verify_email)
    return state


def _admin_context(request: Request) -> dict:
    settings = get_settings()
    db = settings.db_path
    payments = store.list_payments(db, 100)
    users = _users_by_id()
    for payment in payments:
        user = users.get(payment.get("user_id")) or {}
        payment["user_label"] = user.get("email") or user.get("username") or payment.get("user_id", "")[:8]
    summary = store.payments_summary(db)
    summary["revenue_rub"] = f"{Decimal(summary['paid_kopecks']) / Decimal(100):.0f}"
    promos = store.list_promos(db)
    plans = store.list_plans(db, only_active=False)
    servers = []
    try:
        servers = _panel().load_data().get("servers", []) or []
    except Exception:  # noqa: BLE001 - the admin page must still render
        logger.exception("billing: could not read servers for plan targets")
    for plan in plans:
        uid = plan.get("server_uid")
        if not uid and plan.get("server_id") is not None:
            try:
                index = int(plan["server_id"])
                uid = servers[index].get("uid") if 0 <= index < len(servers) else None
            except (TypeError, ValueError, IndexError):
                uid = None
        if uid or plan.get("protocol"):
            plan["target_value"] = f"{uid or ''}:{plan.get('protocol') or ''}"
        else:
            plan["target_value"] = ""
    plans_by_id = {plan["id"]: plan for plan in plans}
    for item in promos:
        item["label"] = promo.describe(item)
        item["plan_name"] = (plans_by_id.get(item.get("plan_id")) or {}).get("name")
        item["exhausted"] = bool(item.get("max_uses")) and \
            int(item.get("used_count") or 0) >= int(item["max_uses"])
    return {
        "settings": settings,
        "plans": plans,
        "targets": _targets(),
        "payments": payments,
        "summary": summary,
        "users_stats": _users_overview(),
        "promos": promos,
        "trials": store.trials_summary(db),
        "mail": _mail_status(settings),
        "brand": branding.current(),
        "promo_kinds": [
            ("percent", "Скидка, %"),
            ("amount", "Скидка, ₽"),
            ("bonus_days", "Бонусные дни к тарифу"),
            ("free_days", "Бесплатный доступ, дней"),
        ],
        "masked": {
            "yookassa_secret_key": crypto.mask(settings.yookassa_secret_key),
            "telegram_bot_token": crypto.mask(settings.telegram_bot_token),
        },
        "webhook_url": f"{settings.public_base}/api/billing/yookassa/webhook",
        "telegram_unconfigured_hint": not settings.telegram_ready,
    }


@router.get(ADMIN_PAGE, response_class=HTMLResponse)
def page_billing_admin(request: Request):
    if not _admin_session(request):
        return _admin_denied_redirect(request)
    return _render(request, "billing_admin.html", **_admin_context(request))


@router.post("/api/billing/admin/settings")
async def api_admin_settings(request: Request):
    admin, error = await _admin_guard(request)
    if error:
        return error
    body = await _json(request)
    values = body.get("values") or {}
    reset = [str(key) for key in (body.get("reset") or [])]
    if not isinstance(values, dict):
        return JSONResponse({"error": "Некорректные данные"}, status_code=400)

    errors: list[str] = []
    clean: dict = {}

    def take(key: str):
        if key in values:
            raw = values[key]
            clean[key] = "" if raw is None else str(raw).strip()

    take("yookassa_shop_id")
    take("yookassa_secret_key")
    take("telegram_bot_token")
    take("protocol")
    if "telegram_bot_username" in values:
        clean["telegram_bot_username"] = str(values["telegram_bot_username"] or "").strip().lstrip("@")
    if "public_base" in values:
        public_base = str(values["public_base"] or "").strip().rstrip("/")
        if public_base and not public_base.startswith(("http://", "https://")):
            errors.append("Публичный адрес должен начинаться с http:// или https://")
        clean["public_base"] = public_base
    if "currency" in values:
        clean["currency"] = "RUB"
    for key, label, low, high in (
        ("trial_days", "Пробный период", 0, 365),
        ("trial_device_limit", "Устройств в пробном периоде", 1, 50),
        ("trial_ip_limit", "Пробных периодов с одного IP", 0, 100),
        ("code_ttl_minutes", "Срок действия кода", 1, 120),
        ("code_resend_seconds", "Пауза между письмами", 0, 3600),
    ):
        if key not in values:
            continue
        try:
            clean[key] = int(values[key])
        except (TypeError, ValueError):
            errors.append(f"{label}: нужно целое число")
        else:
            if not low <= int(clean[key]) <= high:
                errors.append(f"{label}: от {low} до {high}")
    if "vat_code" in values:
        try:
            clean["vat_code"] = int(values["vat_code"])
        except (TypeError, ValueError):
            errors.append("Некорректный код НДС")
        else:
            if not 1 <= int(clean["vat_code"]) <= 6:
                errors.append("Код НДС: от 1 до 6")
    for flag in ("mock", "receipt_enabled", "provision_devices", "trial_enabled",
                 "verify_email"):
        if flag in values:
            clean[flag] = values[flag] in (True, 1, "1", "true", "on", "yes")
    if "server_id" in values:
        target = str(values.get("server_id") or "").strip()
        if target == "":
            reset.append("server_id")
        else:
            try:
                clean["server_id"] = int(target)
            except ValueError:
                errors.append("Некорректный сервер")
    if errors:
        return JSONResponse({"error": "; ".join(errors)}, status_code=400)

    try:
        save_overrides(clean, reset)
    except Exception:  # noqa: BLE001
        logger.exception("billing: saving admin settings failed")
        return JSONResponse({"error": "Не удалось сохранить настройки"}, status_code=500)
    logger.info("billing: settings updated by admin %s", admin.get("username"))
    return {"status": "ok", "settings": get_settings().as_dict()}


def _truthy(value) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _plan_fields(body: dict, *, creating: bool) -> dict:
    name = str(body.get("name") or "").strip()
    if not name:
        raise ValueError("Укажите название тарифа")
    if len(name) > 80:
        raise ValueError("Название слишком длинное")

    price_raw = str(body.get("price_rub") or "").replace(",", ".").strip()
    try:
        price = int((Decimal(price_raw) * 100).to_integral_value(rounding=ROUND_HALF_UP))
    except Exception:  # noqa: BLE001
        raise ValueError("Укажите цену числом, например 199") from None
    if price <= 0:
        raise ValueError("Цена должна быть больше нуля")
    if price > 10_000_000:
        raise ValueError("Слишком большая цена")

    try:
        days = int(str(body.get("days") or "").strip())
    except ValueError:
        raise ValueError("Срок должен быть числом дней") from None
    if not 1 <= days <= 3650:
        raise ValueError("Срок: от 1 до 3650 дней")

    try:
        device_limit = int(str(body.get("device_limit") or "5").strip())
    except ValueError:
        raise ValueError("Лимит устройств должен быть числом") from None
    if not 1 <= device_limit <= 50:
        raise ValueError("Устройств: от 1 до 50")

    slug = re.sub(r"[^a-z0-9-]+", "-", str(body.get("slug") or "").strip().lower()).strip("-")
    if creating and not slug:
        slug = f"plan-{secrets.token_hex(3)}"
    if slug and len(slug) > 40:
        raise ValueError("Короткий код тарифа слишком длинный")

    server_id, server_uid, protocol = _parse_target(str(body.get("target") or ""))
    try:
        sort = int(str(body.get("sort") or "100").strip())
    except ValueError:
        sort = 100

    fields = {
        "name": name,
        "description": str(body.get("description") or "").strip()[:200],
        "price_kopecks": price,
        "days": days,
        "device_limit": device_limit,
        "server_id": server_id,
        "server_uid": server_uid,
        "protocol": protocol,
        "active": 1 if _truthy(body.get("active", True)) else 0,
        "sort": sort,
    }
    if slug:
        fields["slug"] = slug
    return fields


@router.post("/api/billing/admin/plans")
async def api_admin_plan_create(request: Request):
    admin, error = await _admin_guard(request)
    if error:
        return error
    body = await _json(request)
    db = get_settings().db_path
    try:
        fields = _plan_fields(body, creating=True)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    slug = fields.get("slug") or f"plan-{secrets.token_hex(3)}"
    if store.plan_slug_exists(db, slug):
        return JSONResponse({"error": f"Тариф с кодом «{slug}» уже есть"}, status_code=400)
    plan = store.create_plan(
        db, slug=slug, name=fields["name"], description=fields["description"],
        price_kopecks=fields["price_kopecks"], days=fields["days"],
        device_limit=fields["device_limit"], server_id=fields["server_id"],
        server_uid=fields["server_uid"], protocol=fields["protocol"],
        active=bool(fields["active"]), sort=fields["sort"],
    )
    logger.info("billing: plan %s created by %s", slug, admin.get("username"))
    return {"status": "ok", "plan": plan}


@router.post("/api/billing/admin/plans/{plan_id}")
async def api_admin_plan_update(request: Request, plan_id: str):
    admin, error = await _admin_guard(request)
    if error:
        return error
    db = get_settings().db_path
    if not store.get_plan(db, plan_id, only_active=False):
        return JSONResponse({"error": "Тариф не найден"}, status_code=404)
    body = await _json(request)
    try:
        fields = _plan_fields(body, creating=False)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    slug = fields.get("slug")
    if slug and store.plan_slug_exists(db, slug, exclude_id=plan_id):
        return JSONResponse({"error": f"Тариф с кодом «{slug}» уже есть"}, status_code=400)
    store.update_plan(db, plan_id, fields)
    logger.info("billing: plan %s updated by %s", plan_id, admin.get("username"))
    return {"status": "ok"}


@router.post("/api/billing/admin/plans/{plan_id}/delete")
async def api_admin_plan_delete(request: Request, plan_id: str):
    admin, error = await _admin_guard(request)
    if error:
        return error
    db = get_settings().db_path
    if not store.delete_plan(db, plan_id):
        return JSONResponse(
            {"error": "По тарифу уже есть платежи. Снимите галочку «Активен», чтобы скрыть его."},
            status_code=400,
        )
    logger.info("billing: plan %s deleted by %s", plan_id, admin.get("username"))
    return {"status": "ok"}


def _promo_fields(body: dict, *, creating: bool) -> dict:
    code = store.normalize_code(str(body.get("code") or ""))
    if creating:
        if not code:
            raise ValueError("Укажите промокод")
        if len(code) < 3 or len(code) > 40:
            raise ValueError("Промокод: от 3 до 40 символов (латиница, цифры, - и _)")

    kind = str(body.get("kind") or "percent")
    if kind not in store.PROMO_KINDS:
        raise ValueError("Неизвестный тип промокода")

    raw_value = str(body.get("value") or "").replace(",", ".").strip()
    if kind == "percent":
        try:
            value = int(Decimal(raw_value).to_integral_value(rounding=ROUND_HALF_UP))
        except Exception:  # noqa: BLE001
            raise ValueError("Укажите процент скидки числом") from None
        if not 1 <= value <= 100:
            raise ValueError("Скидка: от 1 до 100%")
    elif kind == "amount":
        try:
            value = int((Decimal(raw_value) * 100).to_integral_value(rounding=ROUND_HALF_UP))
        except Exception:  # noqa: BLE001
            raise ValueError("Укажите сумму скидки числом") from None
        if value <= 0:
            raise ValueError("Сумма скидки должна быть больше нуля")
    else:
        try:
            value = int(raw_value)
        except ValueError:
            raise ValueError("Укажите количество дней числом") from None
        if not 1 <= value <= 3650:
            raise ValueError("Дней: от 1 до 3650")

    try:
        max_uses = int(str(body.get("max_uses") or "0").strip() or "0")
    except ValueError:
        raise ValueError("Лимит использований должен быть числом") from None
    if not 0 <= max_uses <= 1_000_000:
        raise ValueError("Некорректный лимит использований")

    try:
        per_user = int(str(body.get("per_user") or "1").strip() or "1")
    except ValueError:
        raise ValueError("Лимит на клиента должен быть числом") from None
    if not 1 <= per_user <= 100:
        raise ValueError("На клиента: от 1 до 100")

    valid_until = str(body.get("valid_until") or "").strip()
    if valid_until:
        try:
            moment = datetime.fromisoformat(valid_until)
        except ValueError:
            raise ValueError("Некорректная дата окончания") from None
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=timezone.utc)
        valid_until = moment.isoformat()

    plan_id = str(body.get("plan_id") or "").strip() or None
    if plan_id and not store.get_plan(get_settings().db_path, plan_id, only_active=False):
        raise ValueError("Выбранный тариф не найден")

    fields = {
        "kind": kind,
        "value": value,
        "plan_id": plan_id,
        "max_uses": max_uses,
        "per_user": per_user,
        "valid_until": valid_until or None,
        "active": 1 if _truthy(body.get("active", True)) else 0,
        "note": str(body.get("note") or "").strip()[:200],
    }
    if code:
        fields["code"] = code
    return fields


@router.post("/api/billing/admin/promos")
async def api_admin_promo_create(request: Request):
    admin, error = await _admin_guard(request)
    if error:
        return error
    body = await _json(request)
    db = get_settings().db_path
    try:
        fields = _promo_fields(body, creating=True)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if store.get_promo_by_code(db, fields["code"]):
        return JSONResponse({"error": f"Промокод «{fields['code']}» уже существует"}, status_code=400)
    promo_row = store.create_promo(
        db, code=fields["code"], kind=fields["kind"], value=fields["value"],
        plan_id=fields["plan_id"], max_uses=fields["max_uses"],
        per_user=fields["per_user"], valid_until=fields["valid_until"],
        active=bool(fields["active"]), note=fields["note"],
    )
    logger.info("billing: promo %s created by %s", fields["code"], admin.get("username"))
    return {"status": "ok", "promo": promo_row}


@router.post("/api/billing/admin/promos/{promo_id}")
async def api_admin_promo_update(request: Request, promo_id: str):
    admin, error = await _admin_guard(request)
    if error:
        return error
    db = get_settings().db_path
    if not store.get_promo(db, promo_id):
        return JSONResponse({"error": "Промокод не найден"}, status_code=404)
    body = await _json(request)
    try:
        fields = _promo_fields(body, creating=False)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    if fields.get("code"):
        existing = store.get_promo_by_code(db, fields["code"])
        if existing and existing["id"] != promo_id:
            return JSONResponse(
                {"error": f"Промокод «{fields['code']}» уже существует"}, status_code=400
            )
    store.update_promo(db, promo_id, fields)
    logger.info("billing: promo %s updated by %s", promo_id, admin.get("username"))
    return {"status": "ok"}


@router.post("/api/billing/admin/promos/{promo_id}/delete")
async def api_admin_promo_delete(request: Request, promo_id: str):
    admin, error = await _admin_guard(request)
    if error:
        return error
    db = get_settings().db_path
    promo_row = store.get_promo(db, promo_id)
    if not promo_row:
        return JSONResponse({"error": "Промокод не найден"}, status_code=404)
    if int(promo_row.get("used_count") or 0) > 0:
        store.update_promo(db, promo_id, {"active": 0})
        logger.info("billing: promo %s deactivated (already used) by %s",
                    promo_id, admin.get("username"))
        return {"status": "ok", "deactivated": True}
    store.delete_promo(db, promo_id)
    logger.info("billing: promo %s deleted by %s", promo_id, admin.get("username"))
    return {"status": "ok"}


@router.post("/api/billing/admin/mail/test")
async def api_admin_mail_test(request: Request):
    """Send a test message through the SMTP settings from the panel."""
    admin, error = await _admin_guard(request)
    if error:
        return error
    if not _rate_limit(request, "mailtest", 5, 600):
        return JSONResponse({"error": "Слишком много попыток"}, status_code=429)
    body = await _json(request)
    to = str(body.get("email") or "").strip().lower()
    if not EMAIL_RE.match(to):
        return JSONResponse({"error": "Укажите корректный email"}, status_code=400)
    brand = "TTVPN"
    try:
        brand = str((_panel().load_data().get("settings", {})
                     .get("appearance", {}) or {}).get("title") or brand)
    except Exception:  # noqa: BLE001
        pass
    try:
        await asyncio.to_thread(billing_mailer.send_test, to, brand)
    except billing_mailer.BillingMailError as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)
    logger.info("billing: test mail sent to %s by %s", to, admin.get("username"))
    return {"status": "ok", "email": to}


@router.post("/api/billing/admin/mail/status")
async def api_admin_mail_status(request: Request):
    admin, error = await _admin_guard(request)
    if error:
        return error
    return {"status": "ok", "mail": _mail_status(get_settings())}


@router.post("/api/billing/admin/yookassa/check")
async def api_admin_yookassa_check(request: Request):
    admin, error = await _admin_guard(request)
    if error:
        return error
    body = await _json(request)
    settings = get_settings()
    shop_id = str(body.get("shop_id") or settings.yookassa_shop_id or "").strip()
    secret = str(body.get("secret_key") or settings.yookassa_secret_key or "").strip()
    if not shop_id or not secret:
        return JSONResponse({"error": "Сначала введите shopId и секретный ключ"}, status_code=400)
    try:
        info = await asyncio.to_thread(
            yookassa.check_credentials, shop_id=shop_id, secret_key=secret
        )
    except yookassa.YooKassaError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {"status": "ok", **info}


# ----------------------------------------------------------------- host gate

CLIENT_PATH_PREFIXES = (
    "/static/", "/api/billing/", "/api/my/", "/api/share/",
    "/my", "/share/", "/cabinet", "/plans", "/register", "/billing/",
    "/manifest.webmanifest", "/sw.js", "/favicon",
)
# Everything the public landing page needs to load. Without these the apex
# host redirected its own CSS, JS and logo to lk and the page rendered bare.
APEX_ASSET_PREFIXES = (
    "/static/", "/billing/brand/", "/manifest.webmanifest", "/sw.js", "/favicon",
)

DEFAULT_CLIENT_HOST = "lk.ttvpn.ru"
DEFAULT_ADMIN_URL = "https://admin.ttvpn.ru"
DEFAULT_APEX_HOSTS = "ttvpn.ru,www.ttvpn.ru"


def _admin_url() -> str:
    return os.environ.get("BILLING_ADMIN_URL", DEFAULT_ADMIN_URL).strip().rstrip("/") \
        or DEFAULT_ADMIN_URL


def _hosts() -> tuple[set[str], set[str], set[str]]:
    """client / admin / apex hosts actually served by this deployment.

    The client and admin hosts follow the configured public base and the
    BILLING_ADMIN_URL, so changing the domain never leaves redirects pointing
    at the old one. The apex list stays a plain env setting.
    """
    client_host = (urlparse(get_settings().public_base).hostname
                   or DEFAULT_CLIENT_HOST).lower()
    admin_host = (urlparse(_admin_url()).hostname
                  or urlparse(DEFAULT_ADMIN_URL).hostname).lower()
    raw_apex = os.environ.get("BILLING_APEX_HOSTS", DEFAULT_APEX_HOSTS)
    client = {client_host}
    apex = {part.strip().lower() for part in raw_apex.split(",") if part.strip()}
    # Domains authorized through Settings → Domains and certificates. The
    # panel only serves roles it already knows; arbitrary admin hosts are
    # never accepted here.
    try:
        custom_client, custom_apex = _panel().certificate_domain_hosts()
        client.update(custom_client)
        apex.update(custom_apex)
    except Exception:  # noqa: BLE001 - never widen the gate on a bad read
        pass
    return client, {admin_host}, apex



def _host(request: Request) -> str:
    # Host is supplied by the reverse proxy; forwarded-host is user spoofable.
    host = request.headers.get("host") or ""
    return host.split(":", 1)[0].strip().lower()


_SESSION_MAX_AGE = 60 * 60 * 24 * 14


def _session_uid(request: Request) -> Optional[str]:
    """Read the panel's signed session cookie without depending on middleware
    order. The host gate runs outside SessionMiddleware, so it has to verify
    and decode the cookie itself using the same secret and signer."""
    cookie = request.cookies.get("session")
    secret = os.environ.get("SECRET_KEY", "")
    if not cookie or not secret:
        return None
    try:
        # Starlette stores base64(json) signed by itsdangerous TimestampSigner,
        # i.e. "<b64 payload>.<ts>.<sig>". Unsign first, then base64-decode.
        payload = TimestampSigner(secret).unsign(
            cookie.encode(), max_age=_SESSION_MAX_AGE
        )
        data = json.loads(base64.b64decode(payload).decode())
        uid = data.get("user_id")
        return str(uid) if uid else None
    except (BadSignature, ValueError, TypeError, json.JSONDecodeError):
        return None


def _is_billing_managed(user_id: str) -> bool:
    """Self-registered customers are provisioned by the billing flow only.

    Admin-created accounts keep the panel's original self-service behaviour so
    this add-on does not break existing manual users.
    """
    try:
        panel = _panel()
        for user in panel.load_data().get("users", []):
            if user.get("id") == user_id:
                return str(user.get("description") or "") == "self-registered"
    except Exception:  # noqa: BLE001
        return False
    return False


def install_host_gate(app) -> None:
    # NOTE: this middleware is registered last, so Starlette runs it *outside*
    # SessionMiddleware. It must not use request.session — it only redirects or
    # rejects; rendering happens downstream where the session exists.
    @app.middleware("http")
    async def host_gate(request: Request, call_next):
        host = _host(request)
        path = request.url.path
        # Caddy calls this endpoint over the internal Docker network with the
        # service hostname. It is protected by the per-deployment ask token,
        # not by the public host gate.
        if path.startswith('/api/certificates/ask/'):
            return await call_next(request)
        client_hosts, admin_hosts, apex_hosts = _hosts()
        client_base = get_settings().public_base.rstrip("/")

        production = os.environ.get("TTVPN_PRODUCTION", "").strip().lower() in ("1", "true", "yes", "on")
        if production and host not in client_hosts | admin_hosts | apex_hosts:
            return HTMLResponse("Not Found", status_code=404)

        if host in apex_hosts:
            if path in ("/", "/index.html"):
                return RedirectResponse("/landing", 302)
            if path in ("/landing", "/billing/legal/privacy", "/billing/legal/terms"):
                return await call_next(request)
            if any(path.startswith(prefix) for prefix in APEX_ASSET_PREFIXES):
                return await call_next(request)
            query = ("?" + request.url.query) if request.url.query else ""
            # Everything the landing page links to (cabinet, plans, register,
            # password reset, API) lives on the client host. Redirecting by
            # prefix keeps new routes working without touching this list.
            if any(path.startswith(prefix) for prefix in CLIENT_PATH_PREFIXES):
                code = 302 if request.method in ("GET", "HEAD") else 307
                return RedirectResponse(client_base + path + query, code)
            return RedirectResponse(client_base + "/", 302)

        if host in client_hosts:
            if path == "/":
                return RedirectResponse("/cabinet", 303)
            if path == "/login":
                return RedirectResponse("/cabinet/login", 302)
            if path == "/logout":
                return RedirectResponse("/cabinet/logout", 302)
            # The panel's /my page is rendered inside the admin chrome (nav bar,
            # admin tabs) and reads like a cut-down admin panel. Clients get the
            # billing-styled devices page instead; the query string survives, so
            # a link to ?device=... still opens that device's QR.
            if path == "/my":
                query = ("?" + request.url.query) if request.url.query else ""
                return RedirectResponse("/cabinet/connections" + query, 303)
            allowed = any(path.startswith(prefix) for prefix in CLIENT_PATH_PREFIXES)
            if not allowed:
                return HTMLResponse("Not Found", status_code=404)
            # Devices are sold by the tariff. Letting a billing customer mint
            # extra peers for free would leak revenue; legacy manual accounts
            # are unaffected.
            if path == "/api/my/connections/add":
                uid = _session_uid(request)
                if uid and _is_billing_managed(uid):
                    return JSONResponse(
                        {"error": "Устройства выдаются по тарифу автоматически"},
                        status_code=403,
                    )

        return await call_next(request)
