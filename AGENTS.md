# AGENTS.md — TTVPN (Amnezia Web Panel fork)

Compact, repo-specific guidance. Read `TTVPN_TEAM.md` and `BILLING.md` for
deeper context; `TTVPN_TECHNICAL_AUDIT.md` for architecture.

## Product / scope
- Fork of Amnezia Web Panel plus a commercial billing layer (tariffs, payments,
  devices, cabinet, Telegram bot). VPN node management must keep working.
- Primary UI language: **Russian only**. Unified terminology; no English UI.
- Do not change production without owner approval. Never delete users, reissue
  keys, change routes/Caddy/volumes. Backups + rollback plan required.

## Image handling (hard rule)
- Do not generate, open, or send PNG/JPG/JPEG/WebP/GIF to model context.
- Do not use screenshots as input. No image-generation tools.
- Design UI with text specs, HTML, CSS and app components. SVG is allowed as
  source code. Keep the existing TTVPN logo untouched.

## Stack
- Python 3.11 in CI (`python:3.14-slim` in Docker), Flask + FastAPI, Jinja2,
  SQLite (WAL), paramiko, python-telegram-bot 20.7.
- Entry point: `python3 app.py` (port 5000). Compose files:
  `docker-compose.yml` (dev), `docker-compose.prod.yml` (prod).

## Environments
- System Python is 3.9 and has **no** deps. Use the isolated venv:
  `/tmp/ttvpn-venv/bin/python` (Python 3.12 + pytest 9).
- Jinja render check: `/tmp/jinjacheck/bin/python`.
- Env vars (read directly, no `.env` loader): `DATA_FILE`, `SECRET_KEY` (must
  stay stable — billing secrets become unreadable otherwise), `BILLING_DB_PATH`,
  `BILLING_TELEGRAM_SSH_SERVER_ID` (it is a **server index**, not a uid),
  `YOOKASSA_*`, `TELEGRAM_BOT_TOKEN`.

## Tests
- CI: `python -m unittest discover -s tests -v` (Python 3.11).
- Local pytest: `/tmp/ttvpn-venv/bin/python -m pytest tests/... -q -p no:cacheprovider`.
- Run a single file: `.../pytest tests/test_billing_devices.py -q -p no:cacheprovider`.
- New billing tests are pytest-style; they will not run under `unittest`.
- Isolated state for integration-style tests: set `DATA_FILE`, `SECRET_KEY`,
  `SSH_KNOWN_HOSTS_FILE`, `TUNNEL_STATE_FILE` to a temp dir (see CI).
- Never run tests or docker against production.

## Architecture (non-obvious)
- `app.py`: panel core, `DATA_LOCK`, `load_data`/`save_data`, peer_create
  journal (`data.json.peer-creates.json`).
- `billing/`: `store.py` (schema + migrations + devices), `provision.py`
  (device issuance under `DATA_LOCK`), `router.py` (cabinet/payments/webhook),
  `settings.py`, `telegram_tunnel.py` (SSH direct-tcpip CONNECT to
  api.telegram.org:443, bot only).
- `connection_service.py`: shared self-service guard used by web and Telegram.
- `telegram_bot.py`: billing-gated config delivery.
- Billing clients are identified by `description == "self-registered"`.
- Two identities on the client host: `billing_uid` (cabinet) and `user_id`
  (panel), synced via `_sync_panel_session`.

## Billing model (must not be broken)
- One plan slot = one row in `devices`. Multiple protocol profiles per device
  via `device_connections`; one connection belongs to exactly one device.
- Default device limit: **5 for all tariffs**, configurable 1..50 per tariff.
  Migration v1 raised legacy values to 5 (treated as test data).
- Do not auto-delete connections/keys when limits change.
- `provision.ensure_connections` counts slots by devices, not connections;
  peer names use `_free_peer_name` + `_used_peer_names` (avoids names already
  in the peer-create journal — a deleted peer can never be recreated).
- `MAX_PROFILES_PER_DEVICE = 3`.
- Self-service creation is blocked for billing clients in
  `connection_service._validate_create_request` via `_plan_owns_devices()`
  (applies to web and Telegram, not just host-gate).
- Config delivery requires an active subscription (403 `subscription_inactive`).
- Tariffs bind to a server by stable `server_uid` (+ index for compat).
- Payments store a snapshot: `plan_days`, `plan_device_limit`,
  `plan_server_uid`, `plan_protocol`, plus `provisioned`.
- `data.json` writes from billing go through `app.DATA_LOCK`
  (`_mutate` → `_mutate_blocking`).
- Webhook returns 503 if the term is not yet provisioned (YooKassa retries);
  `POST /api/billing/devices/sync` retries issuance.

## Coordination
- Work on branch `develop`; commits are local (no push).
- `TTVPN_TEAM.md` is the agent coordination file: ownership, journal, transfers.
  Update it before/after a block of work.
- `billing.db` is **not** in the standard panel archive (`panel_backup.py`);
  back it up separately with WAL in mind.
- Token leak: the bot token previously hit logs; rotate in BotFather and update.

## Performance / UX
- Prefer async mutations, avoid blocking the event loop.
- Reuse existing scroll-reveal (`.bc-scroll-reveal` / `.bc-is-scroll-pending`);
  do not duplicate observers.
- Mobile (390px): no horizontal scroll, hide `.bc-opbg`, logo not overlapped.
- Respect `prefers-reduced-motion`; never hide content without IntersectionObserver.
EOF
echo "written"; wc -l AGENTS.md
