# Billing / client cabinet add-on (fork)

Adds a public client cabinet, email or Telegram registration, tariffs and
YooKassa payments on top of the upstream Amnezia Web Panel. Core panel code is
untouched except three small hooks:

1. `app.py` (bottom): mounts `billing.router` and the host gate, wrapped in
   `try/except` so a billing failure can never stop the panel from starting.
2. `app.py` `my_connections_page`: passes `billing_managed` to the template so
   paying customers don't get a free "create device" button.
3. `templates/my_connections.html`: hides that button when `billing_managed`.

Everything else lives in `billing/`.

> Обновление DOCS-агента, 2026-11-06. Новые и исправленные разделы ниже написаны
> по исходникам (`billing/*`, `telegram_bot.py`, `app.py`, `panel_backup.py`,
> compose-файлы, `.dockerignore`/`.gitignore`). Формулировка «код делает»
> означает утверждение, снятое с кода; «проверено на стенде» ставится только
> там, где проверка действительно выполнялась. В рамках этой правки ничего не
> запускалось: ни контейнеров, ни тестов, ни обращений к production, поэтому
> все утверждения ниже — статические. Более старые разделы на английском
> сохранены; они не противоречат новым, кроме мест, исправленных явно.
> `billing/store.py`, `billing/provision.py` и `billing/router.py` в это же время
> правят другие агенты, поэтому ссылки на них даны по именам функций и таблиц,
> а не по номерам строк: номера успевают устареть между правками.

## Hosts

| Host | Purpose |
|------|---------|
| `ttvpn.ru` | landing page (tariffs, promo, registration) |
| `lk.ttvpn.ru` | client cabinet, tariffs, payment, `/cabinet/connections` |
| `admin.ttvpn.ru` | admin panel + `/billing-admin` (unchanged upstream UI) |

The gate keys off the `Host` header, so all three vhosts can point at the same
container. The landing host also serves `/static/*` and `/billing/brand/*` so
its CSS, JS, logo and favicon load without a redirect.

Host names are not hardcoded: the client host comes from
`BILLING_PUBLIC_BASE`, the admin host from `BILLING_ADMIN_URL`, and the apex
list from `BILLING_APEX_HOSTS`. Changing the domain is therefore a `.env`
change plus the Caddyfile, with no code edit and no stale redirects.

## Client pages

Clients never see the upstream admin chrome. All of these extend
`templates/billing_base.html`, which carries one set of styles and the header
logo:

| Path | Page |
|------|------|
| `/landing` | landing page (apex only) |
| `/cabinet/login`, `/register` | sign in / two-step sign up |
| `/cabinet/tg`, `/cabinet/tg/<token>` | one-tap Telegram sign in (polls until confirmed) |
| `/cabinet/reset` | password recovery: email code → new password |
| `/cabinet/password` | set/change the password while signed in |
| `/cabinet` | status, expiry, promo code, devices, payments |
| `/plans?promo=CODE&plan=ID` | tariffs, price and new expiry recalculation, checkout |
| `/cabinet/connections?device=ID&tab=qr\|key\|conf` | QR, `vpn://` key, config file |

`/my` on `lk.ttvpn.ru` is redirected (303, query string preserved) to
`/cabinet/connections`, because the upstream page renders inside the admin
frame. The `device` link from the cabinet and the "Ключ и QR-код" button land
here, and the page opens that device with the requested tab already active.

Device configs come from `POST /api/billing/connections/{id}/config`, not from
the upstream `/api/my/...`. The upstream endpoint authorizes the *panel*
session, and the panel session can lag behind the cabinet (an expired account
has no panel session at all, and a re-login in the same browser replaces it).
That mismatch is what produced "Устройство не найдено" for a device the client
could see in the list. The billing endpoint resolves the owner from
`billing_uid`, so the identity that renders the list is the identity that
authorizes the config.

## Branding

`/billing-admin` → «Логотип» uploads a single picture (PNG, JPEG, WebP, GIF or
SVG, up to 4 MB). It is stored next to `data.json` in `branding/logo.<ext>`; a
square, transparent-padded `branding/icon.png` is generated with Pillow so the
browser tab icon is not a squashed rectangle. Served as `/billing/brand/logo`
and `/billing/brand/icon` (cache-busted with the file mtime). No logo yet means
the shield/text fallback and the upstream `/static/favicon.svg`.

Raster uploads are downscaled and re-encoded as WebP on save (logo ≤ 512 px,
operator photo ≤ 1024 px), because the cabinet is opened on mobile data. The
operator photo also gets a `branding/operator_small.webp` square crop
(`/billing/brand/operator-small`) used by the phone-sized support chip.

## Environment

| Variable | Default | Meaning |
|----------|---------|---------|
| `BILLING_ENABLED` | `1` | mount the add-on |
| `BILLING_PUBLIC_BASE` | `https://lk.ttvpn.ru` | used in redirects, defines the client host |
| `BILLING_ADMIN_URL` | `https://admin.ttvpn.ru` | admin host; `next` target after an admin login |
| `BILLING_APEX_HOSTS` | `ttvpn.ru,www.ttvpn.ru` | comma-separated landing hosts |
| `BILLING_TRUST_PROXY` | `0` | use the last `X-Forwarded-For` address for rate limits only if the socket peer matches `BILLING_TRUSTED_PROXY_CIDRS` |
| `BILLING_TRUSTED_PROXY_CIDRS` | empty | comma-separated exact proxy IPs or CIDRs; empty or invalid configuration trusts no proxy |
| `TTVPN_SECURE_COOKIE` | `0` | set to `1` behind HTTPS to require Secure session cookies; HTTP logins will not work |
| `TTVPN_PRODUCTION` | `0` | set to `1` to refuse startup with an empty user database, force Secure session cookies and reject unknown hosts when billing is enabled |

The host gate uses the `Host` header, not `X-Forwarded-Host`. With
`TTVPN_PRODUCTION=1` it rejects unknown hosts; without that setting unknown
hosts retain legacy panel access, so never expose that mode publicly. Configure
the proxy to pass the original host and configure all
public hosts via `BILLING_PUBLIC_BASE`, `BILLING_ADMIN_URL` and
`BILLING_APEX_HOSTS`. Protect the backend from direct access. On a new
installation provision an administrator securely before enabling production;
do not expose the legacy admin/admin bootstrap to a network. Set a persistent,
strong `SECRET_KEY` and enable secure cookies for HTTPS deployments. Production
startup requires `SECRET_KEY`, enabled billing and successful billing/host-gate
initialization. The production Compose file enables this mode. Configure the
proxy CIDRs narrowly and only enable forwarded IPs when a single trusted proxy
appends the actual client IP and direct access is blocked. The supplied app.py
disables Uvicorn proxy-header rewriting so the gate sees the socket peer; if
running another ASGI launcher, disable its proxy-header rewriting too.
| `BILLING_MOCK` | `0` | `1` = free test payments (never use in prod) |
| `BILLING_CURRENCY` | `RUB` | |
| `BILLING_TRIAL_ENABLED` | `0` | offer a self-service free trial |
| `BILLING_TRIAL_DAYS` | `0` | trial length, days |
| `BILLING_TRIAL_DEVICE_LIMIT` | `5` | devices included in the trial (`settings.py:190`; admin range 1..50) |
| `BILLING_TRIAL_IP_LIMIT` | `2` | trials per IP / 30 days (`0` = unlimited) |
| `BILLING_VERIFY_EMAIL` | `1` | require an email confirmation code on signup |
| `BILLING_CODE_TTL_MINUTES` | `10` | how long a code stays valid |
| `BILLING_CODE_RESEND_SECONDS` | `60` | pause before "resend code" (`0` = no pause) |
| `BILLING_DB_PATH` | `<data>/billing.db` | SQLite биллинга: `plans`, `payments`, `promos`, `promo_redemptions`, `trials`, `email_codes`, `login_tokens`, `device_labels`, `settings` (секреты в зашифрованном виде) |
| `BILLING_SERVER_ID` | auto | индекс `servers[n]` в `data.json`; используется, только если у тарифа не задана цель (см. «Цель выдачи») |
| `BILLING_PROTOCOL` | auto (awg3) | протокол по умолчанию, если у тарифа он не задан |
| `BILLING_TELEGRAM_SSH_SERVER_ID` | – | индекс `servers[n]`, через который бот ходит в `api.telegram.org` по SSH-туннелю; пусто — прямое подключение (см. «Telegram через SSH-туннель») |
| `BILLING_PROVISION_DEVICES` | `1` | create peers after payment |
| `BILLING_RECEIPT_ENABLED` | `0` | send 54-FZ receipt to YooKassa |
| `BILLING_VAT_CODE` | `1` | receipt VAT code |
| `YOOKASSA_SHOP_ID` / `YOOKASSA_SECRET_KEY` | – | required for real payments |
| `TELEGRAM_BOT_TOKEN` / `TELEGRAM_BOT_USERNAME` | – | enable Telegram login |

`SECRET_KEY` must be the same value the panel already uses, otherwise existing
sessions are invalidated (users simply re-login). Он же — единственный источник
ключа шифрования секретов в `billing.db`; см. «Безопасность и эксплуатация».

## Telegram через SSH-туннель

`BILLING_TELEGRAM_SSH_SERVER_ID` (индекс `servers[n]` в `data.json`) включает
локальный HTTPS-CONNECT-туннель для бота: `telegram_bot._run_bot` читает
переменную и вызывает `billing.telegram_tunnel.start(index)`
(`telegram_bot.py:1742–1751`). Как это устроено:

* `billing/telegram_tunnel.py` поднимает `ThreadingTCPServer` только на
  `127.0.0.1` со случайным портом (`telegram_tunnel.py:59–67`) и возвращает
  `http://127.0.0.1:<port>`; этот адрес передаётся как `proxies=` в
  `httpx.AsyncClient` **только клиента бота** (`telegram_bot.py:1751`). Других
  вызовов `telegram_tunnel.start` в дереве нет.
* Обработчик принимает ровно одну строку запроса —
  `CONNECT api.telegram.org:443 HTTP/1.1`; на любой другой `CONNECT` отвечает
  `403 Forbidden` (`telegram_tunnel.py:21–23`). Это не общий HTTP/HTTPS-прокси:
  произвольный хост или порт через него не открыть.
* Исходящее соединение идёт SSH-каналом `direct-tcpip` к
  `api.telegram.org:443` через пул панели (`panel.get_ssh(...)`,
  `telegram_tunnel.py:26–35`), то есть поверх уже существующего доверенного
  SSH-доступа к серверу панели.
* Клиентского VPN-трафика это не касается: модуль не меняет конфигурации
  AmneziaWG/Xray, не настраивает маршрутизацию/NAT и не слушает внешний
  интерфейс. Туннель используют только HTTP-запросы процесса бота, поэтому
  маршруты VPN-клиентов остаются прежними.

Ограничения, которые видны из кода:

* `BILLING_TELEGRAM_SSH_SERVER_ID` — **индекс**, а не uid: при
  переупорядочивании `servers` в `data.json` туннель молча переключится на
  другой сервер. Индекс вне диапазона не мешает старту — туннель поднимется,
  но каждый `CONNECT` будет падать с `ValueError("Telegram tunnel server not
  found")` и записью в журнал (`telegram_tunnel.py:29–30,47–48`).
* Один экземпляр туннеля на процесс (`_server` в модуле); порт выбирает ОС при
  первом вызове `start`.
* Использование `proxies=` в `httpx.AsyncClient` опирается на
  зафиксированную версию `httpx==0.25.2` (`requirements.txt`): в более новых
  выпусках httpx аргумент переименован/удалён.

Эксплуатационный вывод (на стенде в рамках этой правки **не проверялся**): если
сервер панели находится в РФ и прямой доступ к `api.telegram.org` заблокирован,
без туннеля бот не запустится — `getMe`/`getUpdates` не доходят, вход через
Telegram (`/cabinet/tg`) и выдача конфигурации в боте не работают. Что маршрут
рабочий, зависит ещё и от того, что выбранный сервер действительно имеет сетевой
доступ к Telegram.

Токен бота: по данным аудита (`TTVPN_TECHNICAL_AUDIT.md`, раздел «Платежи и
Telegram») токен ранее попадал в журнал HTTP-клиента. Такой токен нужно
**перевыпустить в BotFather**, вписать новый в админку биллинга (или `.env`) и
перезапустить бота; старый токен после перевыпуска недействителен. В текущем
коде уровень журнала `httpx` принудительно поднят до `WARNING`, потому что URL
Telegram содержит токен (`telegram_bot.py:1748–1750`). Токен нельзя пересылать
в чат, коммиты или тикеты.

## Sign in

Three ways, none of which needs a password typed on a phone:

1. **Email + password** — the classic form; the password can be set or changed
   at `/cabinet/password`.
2. **One-tap Telegram** — `/cabinet/tg` creates a single-use token, shows
   `https://t.me/<bot>?start=lk_<token>`, and the page polls
   `/api/billing/telegram/poll/<token>` every 2.5 s. Pressing Start in the bot
   writes `telegram_id` into `login_tokens` (bot side:
   `telegram_bot._handle_billing_login`), the poll claims the token, finds or
   creates the account by `telegramId`, and redirects into the cabinet. Nothing
   is typed, no phone number is requested, and no third-party widget is loaded.
   Tokens live 10 minutes and can be claimed exactly once.
3. **Legacy Login Widget** — `/api/billing/telegram/login` still verifies the
   signed widget payload (HMAC keyed by the bot token) for links already
   published. It is no longer rendered by the templates.

For (2) the bot must actually be polling: the panel starts it at boot when the
Telegram block is enabled, and the cabinet starts it on demand if the billing
token is configured but the bot is idle. If a *different* bot is polling, the
deep link would never be confirmed, so the cabinet falls back to the password
form with an explanatory message.

## Password recovery

`/cabinet/reset` → `POST /api/billing/reset/start` sends a 6-digit code (same
transport and rate limits as the registration code, `purpose="reset"`), then
`POST /api/billing/reset/confirm` verifies it and writes the new password. An
unknown address gets the same "код отправлен" answer, so the form cannot be
used to enumerate customers. Without a working SMTP setup the page says so and
offers the Telegram support link instead of failing silently.

## Admin UI

Everything below is edited in the browser at **https://admin.ttvpn.ru/billing-admin**
(link "Биллинг" in the panel header). Values are stored in `billing.db` and
override `.env`, so no restart is needed.

| Section | What it controls |
|---------|------------------|
| ЮKassa | shopId + secret key, "Проверить ключи" calls `/v3/me` |
| Telegram | bot username + token for one-tap sign in |
| Почта | email confirmation on/off, code TTL, resend pause, test send |
| Тарифы | name, price, days, device limit, server/protocol, sorting, on/off |
| Промокоды | create/disable codes, see redemptions |
| Пробный период | on/off, days, devices, per-IP limit |
| Платежи | last 100 payments with promo and status |
| Прочее | VAT code, public base URL, device provisioning, receipts, mock |

Secrets are encrypted with a key derived from `SECRET_KEY` (Fernet) before they
are written to SQLite, and are never rendered back into HTML.

## Plans

Seeded once in `billing.db` on first start and then fully editable in the admin
UI. For scripted changes:

```bash
sqlite3 /opt/amnezia-web-panel/data/billing.db \
  "UPDATE plans SET price_kopecks=29900, days=30 WHERE slug='month';"
```

`price_kopecks` is in kopecks (19900 = 199 ₽).

### Цель выдачи: индекс, uid и протокол

Тариф хранит цель тремя полями: `plans.server_id` (индекс в `data.json`),
`plans.server_uid` (стабильный uid сервера) и `plans.protocol`
(`billing/store.py`, схема `plans`). В админке цель выбирается строкой
`<uid>:<protocol>` (`router._targets`), а `router._parse_target` разбирает её
обратно в `(index, uid, protocol)` и заодно записывает текущий индекс.

* **uid стабилен при переупорядочивании.** `provision.pick_target` ищет сервер
  по `server_uid` и переопределяет сохранённый индекс, поэтому перестановка
  серверов не перенацеливает уже проданный тариф. Индекс остаётся в базе для
  совместимости и как резерв.
* **Backfill на старте.** `app.py:9467–9479` вызывает
  `store.backfill_plan_uids(...)` с картой «индекс → uid» по текущему порядку
  серверов. Функция трогает только планы, у которых uid пуст, поэтому
  повторный запуск безопасен: уже заполненные uid не перезаписываются, и
  backfill после переупорядочивания ничего не портит.
* **Если uid исчез** (сервер удалён или пересоздан): `pick_target` пишет
  `logger.error("billing: plan target server uid ... no longer exists")` и
  оставляет сохранённый `server_id` (`billing/provision.py`). Если индекс вне
  диапазона — берётся первый сервер с `self_service_enabled`, иначе индекс 0.
  Если сохранённого протокола на этом сервере нет, выбирается первый доступный
  базовый AWG. В админке индикатора «uid не найден» нет — остаётся только
  строка в журнале, а оплаченный заказ уходит на сервер по умолчанию.
* Платёж фиксирует цель на момент оплаты: `plan_server_uid` и `plan_protocol`
  имеют приоритет над текущими полями тарифа (`router._provision_paid_devices`).
  Это значит, что правка тарифа после оплаты не перенацеливает уже купленный
  заказ.

## Устройства и лимит

Состояние на момент правки: раздел приведён в соответствие с кодом ветки
`ttvpn-modernization` (коммиты `61512e5`, `2c0d23c` и последующие). Модель
устройств дописана ORCHESTRATOR/BACKEND — см. журнал `TTVPN_TEAM.md`.

* **Слот тарифа = запись в `devices`, а не отдельный peer.** Устройство — это
  учётная запись в `billing.db` (`devices` + связь `device_connections`),
  которая может нести несколько VPN-подключений (профилей протокола); одно
  подключение принадлежит ровно одному устройству (`billing/store.py`:
  `devices`, `device_connections`, `list_devices`). Связь хранится в SQLite,
  `data.json` при этом не переписывается.
* **Лимит считается по устройствам.** `provision.ensure_connections` сначала
  вызывает `store.ensure_devices`, затем создаёт недостающие peer'ы по формуле
  `missing = device_limit - len(known_devices)`. `ensure_devices` заводит по
  одному устройству на каждое «бесхозное» подключение и удаляет устройство, у
  которого не осталось живых peer'ов. Для уже выданных клиентов соответствие
  1:1, то есть число слотов сейчас совпадает с числом peer'ов, но модель
  допускает несколько подключений на одно устройство.
* **Физическая идентификация не гарантируется.** «Устройство» — запись в базе,
  а не отпечаток телефона: скопированный конфиг работает на нескольких
  устройствах, а несколько физических устройств могут попасть в один слот,
  если оператор или кабинет свяжет их с одной записью `devices`. Обещать
  защиту от копирования конфига нельзя (то же отмечено в
  `TTVPN_TECHNICAL_AUDIT.md`, раздел «Пользователи, устройства, подписки»).
* **Лимит по умолчанию — 5 для всех тарифов.** У трёх тарифов-заготовок
  `device_limit = 5` (`store.DEFAULT_PLANS`), в схеме
  `plans.device_limit ... DEFAULT 5`, а миграция схемы v1 (`store._migrate`)
  поднимает значения меньше 5 до 5. Планку можно менять в тарифе: в админке
  поле принимает 1..50 (`router._plan_fields`), пустое значение приводится к
  5, а `0` или число вне диапазона — ошибка 400.
* **Лимит в кабинете берётся из снимка платежа.** `router._device_limit()`
  сначала использует `payments.plan_device_limit` и падает обратно на текущий
  тариф только для старых заказов без снимка, то есть показывает то же число,
  которое применяет выдача (`_provision_paid_devices`). Поэтому правка тарифа
  задним числом уже проданный лимит не меняет.
* **Имена.** `device_labels` (`store.device_labels`,
  `store.set_device_label`) хранит подписи отдельных подключений, а имя
  устройства лежит в `devices.name`. Переименование устройства
  (`POST /api/billing/devices/{device_id}/name`) меняет только `billing.db`:
  имя peer в панели (`device-N`, создаётся в `ensure_connections`) остаётся
  прежним, поэтому в списках панели клиент виден под старым именем.
  Переименование подключения (`POST /api/billing/connections/{id}/name`)
  пишет `device_labels`. На лимит и на конфиг переименование не влияет.
* **Сверх лимита.** `ensure_connections` только добавляет недостающие
  подключения и никогда не удаляет лишние, поэтому у клиента, которому раньше
  выдали больше, количество не уменьшится автоматически.
* **Второй профиль на устройство добавляется из кабинета.**
  `POST /api/billing/devices/{device_id}/profiles` →
  `provision.add_device_profile`. Сервер берётся из самого устройства (клиент
  не может указать чужой хост), протокол — из установленных AWG-протоколов
  этого сервера, ещё не привязанных к устройству
  (`provision.profile_options`). API проверяет то же самое, что и интерфейс:
  уже привязанный протокол (`«Этот протокол уже добавлен к устройству»`) и
  протокол, который не установлен на сервере, отклоняются, а запрос без
  протокола берёт первый свободный установленный. Потолок —
  `MAX_PROFILES_PER_DEVICE = 3` профиля на устройство, иначе «одно место в
  лимите» превратилось бы в неограниченное число peer'ов. Новый peer
  создаётся через тот же `peer_creates.create`, поэтому его появление
  восстановимо по журналу, и сразу связывается с устройством через
  `attach_connection`. Требуется активная подписка (иначе 403).
* **Имя peer'а выбирается с учётом журнала создания.** `_free_peer_name` берёт
  минимальный свободный `device-N`, а `_used_peer_names` добавляет к занятым
  именам те, что уже встречались в журнале `peer_creates`
  (`data.json.peer-creates.json`, ключ — `uuid5(user_id, uid сервера,
  протокол, имя)`). Без этого имя, чей peer был удалён, никогда не создать
  повторно: журнал считает операцию завершённой и отвечает 409, а оплаченное
  место осталось бы пустым. Поэтому в панели имя может быть `device-7` — это
  нормально, клиент видит имя устройства из `devices.name`.
* **Конфиг адресуется подключением, а не устройством:**
  `POST /api/billing/connections/{connection_id}/config`; кабинет подставляет
  id конкретного подключения из группы устройства (`templates/billing_my.html`).
* **Открытие кабинета само заводит отсутствующие записи.**
  `provision.user_devices` вызывает `store.ensure_devices`, поэтому просмотр
  списка устройств «усыновляет» уже существующие peer'ы в `devices`. Новых
  peer'ов это не создаёт — только записи в `billing.db`.
* **Самодобавление подключений** для billing-клиентов блокирует общий
  `connection_service._validate_create_request()` →
  `_plan_owns_devices()`: 403 для аккаунтов с
  `description == "self-registered"`, у которых есть активная запись в
  биллинге. Проверка стоит в сервисе, поэтому действует и для веб-формы, и для
  Telegram-бота, а не только для одного URL (host-gate остаётся
  дополнительным уровнем). Операторские аккаунты не затронуты; при недоступном
  биллинге проверка не блокирует пользователя.

## Free trial

Disabled by default. When enabled, a registered user without any payment sees
"Попробовать бесплатно" in the cabinet and can claim it **once**:

* one trial per account (`trials` table) and never for a user who already paid;
* `BILLING_TRIAL_IP_LIMIT` limits how many trials one IP can start in 30 days;
* the trial grants `trial_device_limit` device slots (default 5) and the same
  auto-expiry as a paid plan, so nothing needs manual cleanup.

Registration itself does **not** grant days — without a trial or a payment the
account stays inactive, which keeps `/my` and self-service closed.

## Email verification

Registration is two-step when **both** of these hold:

1. `BILLING_VERIFY_EMAIL=1` (Billing → Почта → "Требовать подтверждение");
2. the panel's mail block (`data.json` → `settings.mail`, edited in
   Settings → Почта) passes the panel's own validation — enabled, host,
   sender address, security mode.

That way a shop without an SMTP server is never locked out of signups: the
second step is skipped automatically and the account is created immediately.

With verification on, `POST /api/billing/register` validates the form and
sends a 6-digit code instead of creating anything; `POST
/api/billing/register/verify` creates the account only after the correct code
arrives, and `POST /api/billing/register/resend` re-sends it with a cooldown.

* code hashed with HMAC-SHA256 keyed by `SECRET_KEY`, never stored in clear;
* TTL `BILLING_CODE_TTL_MINUTES`, at most 5 wrong attempts, one live code per
  address (a resend replaces the previous one);
* at most 20 codes per IP per hour;
* the password is hashed at step 1 and kept with the pending code, so the
  account row is written once, already verified (`email_verified: true`);
* existing address → "Пользователь уже зарегистрирован", no mail is sent.

Billing → Почта shows whether SMTP is ready, lets you tune the TTL and the
resend pause, and has a "Проверка отправки" button that sends a real test
message through exactly the same transport the codes use.

## Promo codes

Four kinds:

| Kind | Effect |
|------|--------|
| `percent` | discount by percent |
| `amount` | discount by fixed rubles (never below zero) |
| `bonus_days` | +N days on top of the plan, full price |
| `free_days` | free access for N days, no payment at all |

Limits: `max_uses` (0 = unlimited), `per_user`, optional `plan_id` and
`valid_until`. A 100% discount (or `free_days`) is applied immediately without
touching YooKassa.

`POST /api/billing/promo/check` works with or without a chosen plan. It returns
`label`, `days_granted`/`bonus_days`, and a `projection` with the expiry date the
code produces (`base_iso` + days, computed exactly like
`provision.extend_subscription`). When a code is tied to another plan the answer
is `409` with `plan_id`, and the plans page selects that tariff and re-checks.

Codes of kind `free_days` can be activated straight from the cabinet with
`POST /api/billing/promo/activate`: it creates a zero-amount `promo` payment and
applies it, so the term changes immediately. Discount and bonus-day codes are
only applied at checkout.

A code is consumed only after the paid period is actually granted, so an
abandoned checkout does not burn a limited code. Codes with redemptions can be
disabled but not deleted — payment history keeps its reference.

## Payment flow

1. `POST /api/billing/checkout` picks the plan **from the DB** (never trusts the
   client's amount), creates a local payment row and a YooKassa payment with an
   idempotency key. Провайдер записывается сразу: `promo` при нулевой сумме,
   `mock` в тестовом режиме (`BILLING_MOCK`), иначе `yookassa`.
2. User returns to `/billing/return?pid=...`; the server re-fetches the payment
   from YooKassa and applies it.
3. `POST /api/billing/yookassa/webhook` (event notification URL in the YooKassa
   dashboard) re-verifies id, amount, currency and internal id, then applies.
4. Применение идемпотентно (`payments.applied` захватывается атомарно),
   продлевает срок от более поздней из дат «сейчас / текущий срок», включает
   аккаунт и **пытается** создать оплаченное число устройств. Неудачная выдача
   уже не откатывает срок: заказ остаётся с `provisioned = 0` и повторяется
   через `POST /api/billing/devices/sync` (см. «Снимок условий тарифа и
   повторная выдача»).

Configure the webhook URL in YooKassa: `https://lk.ttvpn.ru/api/billing/yookassa/webhook`.

### Статусы платежа, доход и возвраты

`payments.status` — движение только вперёд (`billing/store.py`,
`next_payment_status`, `mark_status`): `pending` → `succeeded` | `canceled`,
`succeeded` → `refunded`; `canceled` и `refunded` не переоткрываются —
повторная покупка создаёт новую запись. Запоздавшее уведомление не переводит
оплаченный заказ назад (`router._cancel_payment` пишет предупреждение в
журнал), а `paid_at` пишется один раз, при первом переходе в `succeeded`.
Раньше повторный webhook перезаписывал `paid_at` временем повтора, поэтому
доход по месяцам мог попадать не в тот месяц; восстановить уже сдвинутые даты
миграцией нельзя.

`payments.provider` показывает, кто фактически принял деньги: `yookassa`
(реальный платёж), `mock` (тестовый режим `BILLING_MOCK`), `promo` (промокод
или скидка 100 % — сумма 0). Раньше все заказы писались как `yookassa`.
Миграция v2 размечает старые записи: `provider_payment_id` вида `mock_…` →
`mock`, успешные платежи с нулевой суммой и без id провайдера → `promo`.
Счётчики админки (`store.payments_summary`) считают доход только по
`succeeded` и `provider <> 'mock'`; бесплатные и тестовые заказы выводятся
отдельно, возвраты — в `refunded_count`/`refunded_kopecks`.

**Возвраты.** `store.mark_refunded` переводит `succeeded` → `refunded`,
сохраняет `paid_at` и убирает платёж из дохода. Оплаченный срок при этом не
отзывается: вернуть деньги и отключить доступ — разные решения, и откат срока
здесь не автоматизирован. Кнопки в админке и приёма уведомлений о возвратах
(это отдельный объект API ЮKassa, не событие платежа) нет: примитив в store
нужен для сверки и для будущего эндпоинта, включать его в интерфейс — только
после согласования с владельцем.

## Снимок условий тарифа и повторная выдача

Оплата не должна зависеть от того, что оператор позже изменил в тарифе. Поэтому
checkout и активация промокода пишут в `payments` снимок условий: `plan_days`,
`plan_device_limit`, `plan_server_uid`, `plan_protocol` (`billing/store.py`:
`_ADDED_COLUMNS` и `create_payment`; вызовы — `router.api_checkout` и
`router.api_promo_activate`). При применении платежа снимок имеет приоритет:
дни берутся из `plan_days`, пустое значение дополняется текущим тарифом
(`router._apply_payment`); лимит устройств и цель выдачи —
`router._provision_paid_devices`.

* **`provisioned`.** Флаг `payments.provisioned` означает «оплаченные
  устройства действительно созданы». Он ставится только когда число устройств
  достигло лимита (`router._provision_paid_devices`), а при
  `BILLING_PROVISION_DEVICES=0` — сразу.
* **Повтор выдачи.** `POST /api/billing/devices/sync` (`router.api_devices_sync`)
  берёт последний оплаченный и ещё не выданный по устройствам заказ
  (`store.last_unprovisioned_payment`) и повторяет выдачу. Вызывается
  кабинетом: на странице устройств один раз за сессию при активной подписке
  (`templates/billing_my.html`). Ограничения: нужен вход в кабинет,
  CSRF-токен, активная подписка и не больше 12 запросов за 5 минут; если тариф
  удалён, платёж помечается `provisioned` и выдача не повторяется.
* **Webhook отвечает 503, если срок ещё не выдан.** После проверки платежа
  webhook вызывает `_apply_payment`; при неудаче возвращает `{"ok": false}` с
  кодом 503, чтобы ЮKassa повторила уведомление (`router.api_yookassa_webhook`).
  Если платёж уже применил параллельный обработчик, `claim_applied` не
  срабатывает и ответ считается успешным.
* **Сверка суммы, валюты и metadata.** И в webhook
  (`router.api_yookassa_webhook`), и в `router._refresh_payment` сумма в
  копейках и валюта сравниваются с локальной записью, а
  `metadata.internal_payment_id` — с `payments.id`; при расхождении webhook
  отвечает 400, а refresh не применяет платёж и возвращает текущий статус.
  Подпись уведомления не проверяется: вместо неё платёж заново запрашивается у
  ЮKassa по API (`billing/yookassa.py`, `verify_payment`), поэтому доверять
  телу webhook не требуется.
* **Ограничение отображения.** История платежей в кабинете берёт дни из
  текущего тарифа, а не из снимка (`router._with_plan_names` перезаписывает
  `plan_days`), поэтому после правки тарифа в истории видно новое число дней,
  хотя выдано прежнее.

## Доступ к конфигурации: только при активной подписке

* Кабинет: `POST /api/billing/connections/{id}/config` сначала проверяет
  владельца через `billing_uid`, затем `provision.is_active(user)`; при
  неактивной подписке — `403` с `code: "subscription_inactive"`
  (`router.api_device_config`). Владелец конфигурации разрешается по
  billing-идентичности, а не по сессии панели (`provision.connection_config`).
* Telegram: `_handle_get_config` проверяет
  `_billing_subscription_active(panel_user)` и при неактивной подписке отвечает
  сообщением «Подписка не активна» вместо конфигурации
  (`telegram_bot.py:925–938`, сама проверка — `telegram_bot.py:555–570`).
  Проверка применяется только к аккаунтам с
  `description == "self-registered"`; аккаунты, созданные администратором,
  ведут себя как раньше.
* `is_active` = `enabled` и нет `disable_reasons` и `expiration_date` в
  будущем (`billing/provision.py`, `is_active`).
* Ограничение стоит именно на выдаче ключа/QR: список устройств и
  переименование доступны и без активной подписки (страница
  `/cabinet/connections` рендерится без проверки `is_active`).
* «Лимит устройств» считается по записям `devices` в `billing.db` (слот тарифа),
  а не по числу физических устройств; см. «Устройства и лимит».

## Безопасность и эксплуатация

### Что исключено из образа и Git

* `.dockerignore` исключает `.env`, `.env.*`, `*.db`, `*.db-wal`, `*.db-shm`
  (а также `data.json*`, `tests`, `scripts`). `Dockerfile` по-прежнему делает
  `COPY . .`, поэтому защита держится именно на `.dockerignore`: при его
  изменении секреты и база могут попасть в слой образа. Уже собранные образы
  это не исправляет — их нужно пересобрать и проверить отдельно.
* `.gitignore` исключает `billing.db`, `billing.db-wal`, `billing.db-shm`,
  `.env`, `.env.*`, `data.json`, `branding/`.
* `billing.db` лежит в каталоге данных (`BILLING_DB_PATH`, по умолчанию
  `<data>/billing.db`), который в production монтируется томом
  (`docker-compose.prod.yml`: `/opt/amnezia-web-panel/data:/app/data`).

### Резервная копия billing.db

* Штатный архив панели (`panel_backup.py`) содержит только `state/data.json`,
  его sidecar-файлы, `data.json.recovery/` и `trust/*`
  (`panel_backup.py:124–148,244–246`). `billing.db` в него **не входит**:
  тарифы, платежи, промокоды, пробные периоды и `device_labels` этим архивом
  не сохраняются и не восстанавливаются.
* `PANEL_BACKUP.md` («Save deployment material separately») относит `.env`,
  `SECRET_KEY` и прочие материалы развёртывания к отдельной резервной копии
  уровня хоста — это же касается и `billing.db`.
* `billing/store.py` (`connect`) включает `PRAGMA journal_mode=WAL` на каждом
  соединении, поэтому рядом с базой живут `billing.db-wal` и `billing.db-shm`.
  Копия одного файла `billing.db` может не содержать последних
  зафиксированных транзакций. Согласованный способ — `sqlite3
  /path/billing.db ".backup '/path/billing-YYYY-MM-DD.db'"` (или `VACUUM
  INTO`, или остановка контейнера перед копированием); копию хранить вместе с
  `.env`/`SECRET_KEY`. Проверка восстановления в рамках этой правки **не
  выполнялась**.

### SECRET_KEY

* Секреты в таблице `settings` (ключ ЮKassa, токен бота) шифруются
  Fernet-ключом, производным от `SECRET_KEY` (SHA-256 от значения,
  `billing/crypto.py`).
* Если `SECRET_KEY` не задан в момент сохранения, `encrypt()` возвращает
  строку без префикса `enc:` — секрет лежит в базе открытым текстом (и
  продолжает читаться). Если `SECRET_KEY` потом изменить, `decrypt()` вернёт
  пустую строку и запишет предупреждение в журнал: сохранённые ключ ЮKassa и
  токен бота станут недоступны, их придётся вводить заново. Смена
  `SECRET_KEY` также инвалидирует сессии панели и кабинета.
* Поэтому `SECRET_KEY` должен быть стабильным и храниться вместе с копией
  `billing.db`.

### Локальный туннель

Порт туннеля слушает только `127.0.0.1` и не имеет аутентификации; это
приемлемо, пока контейнер не публикует порт наружу и на хосте нет
недоверенных процессов. Подробности — в разделе «Telegram через SSH-туннель».

## Rollback

```bash
docker compose -p ttvpn-panel -f /opt/ttvpn-panel-fork/docker-compose.prod.yml down
docker compose -p amnezia-web-panel -f /opt/amnezia-web-panel/docker-compose.yml up -d
cp /root/caddy-backup/Caddyfile /opt/ttvpn-caddy/Caddyfile
docker exec ttvpn-caddy caddy reload --config /etc/caddy/Caddyfile
```

Откат возвращает upstream-образ, но **не** «просто данные из общего
bind-mount». Что известно из кода и compose-файлов:

* **`data.json` может оказаться другим.** `docker-compose.prod.yml` форка
  монтирует bind-mount `/opt/amnezia-web-panel/data:/app/data`, а
  `docker-compose.yml` upstream (он же в дереве форка) определяет named volume
  `amnezia_data`. Запуск второго файла без правки `volumes:` поднимет панель с
  другим (пустым) томом. Перед откатом нужно сверить `volumes:` и, если нужно,
  прописать тот же bind-mount.
* **Исчезает host-gate и общая проверка квоты.** `install_host_gate`
  вызывается только из billing-блока `app.py:9481`, поэтому после отката
  клиентский хост снова отдаёт страницы панели, `/my` не редиректится на
  `/cabinet/connections`, а `POST /api/my/connections/add` больше не
  возвращает 403 для billing-клиентов. Вместе с host-gate пропадает и
  `connection_service._plan_owns_devices()`, потому что она вызывает
  `billing.provision.is_billing_managed` и при выключенном биллинге всегда
  возвращает `False`. То есть **ограничение самостоятельного создания
  подключений снимается**: клиент может создать peer сверх тарифа, пока это не
  запрещено настройками self-service самой панели (их поведение после отката в
  рамках этой правки не проверялось). Перед откатом нужно отдельно выключить
  self-service или запретить создание подключений в настройках панели.
* **Маршруты биллинга пропадают.** `/cabinet/*`, `/plans`, `/landing`,
  `/api/billing/*`, `/billing-admin` существуют только в форке; точный код
  ответа после отката не проверялся. В частности, перестанет обрабатываться
  webhook ЮKassa: платежи, оплаченные в этот период, не будут применены — срок
  не продлится, устройства не выдадутся.
* **`plans`/`payments`/`devices` остаются сиротами.** `billing.db` лежит
  в каталоге данных и откатом не удаляется; upstream её не читает. Если позже
  вернуть форк с той же базой, старые тарифы, платежи, промокоды, имена
  устройств и связи `device_connections` «оживут» и снова начнут применяться.
* **Сроки и peers не откатываются.** Они записаны в `data.json`, общий для
  обоих образов: оплаченные периоды и созданные подключения сохраняются (при
  условии, что откат смотрит на тот же файл). Пользователи с
  `description == "self-registered"` остаются обычными пользователями панели.
* SQLite остаётся в режиме WAL с возможными непустыми `billing.db-wal`/
  `billing.db-shm`; это нужно учитывать перед копированием или удалением базы
  (см. «Безопасность и эксплуатация»).

### Runbook отката

1. **Сохранить `billing.db`** согласованной копией с учётом WAL
   (`sqlite3 ... ".backup ..."`), рядом — `.env` и значение `SECRET_KEY` (не
   выводить секреты в чат, коммиты и тикеты). Копия нужна на случай, если
   каталог данных при откате окажется другим (см. пункт про `volumes:`), базу
   удалят или придётся возвращать форк: без неё тарифы, платежи, промокоды и
   имена устройств восстановить нечем.
2. **Остановить приём денег:** отключить платёжные ссылки/тарифы, отключить
   или перенаправить уведомления ЮKassa (webhook форка после отката не
   работает). Уже созданные, но неоплаченные заказы разобрать вручную.
3. **Закрыть self-service:** снять `self_service_enabled` у серверов, скрыть
   кнопку создания подключения (после отката шаблон берётся из upstream-образа,
   поэтому hook `templates/my_connections.html` исчезнет сам), проверить, что
   клиент не может создать peer сверх тарифа через
   `/api/my/connections/add`.
4. **Сверить `volumes:` в compose-файлах** и только затем выполнять `up -d`.
5. **Зафиксировать последствия:** лимиты тарифов и автоматическая выдача после
   отката не работают; продление срока и учёт устройств — вручную, до возврата
   форка. Перед возвратом восстановить `billing.db` из копии и проверить, что
   `SECRET_KEY` тот же, иначе секреты в базе не расшифруются.

## Чек-лист приёмки перед коммерческим запуском

Ни один пункт ниже в рамках этой правки не выполнялся — это список проверок,
которые нужно пройти на стенде и затем на production (с согласованием), а не
отчёт о тестировании.

- [ ] **Реальная оплата** минимальным платежом: `/plans` → checkout → редирект
      ЮKassa → возврат на `/billing/return`; в `payments` строка
      `status=succeeded`, `applied=1`, `provisioned=1`; срок увеличился ровно на
      `days` тарифа (или на дни промокода); уведомление ЮKassa доставлено и
      получило 200.
- [ ] **Выдача ключа/QR на реальном аккаунте:** `/cabinet/connections` →
      `POST /api/billing/connections/{id}/config` возвращает config и `vpn://`;
      QR сканируется телефоном и подключение реально работает; в боте тот же
      конфиг приходит файлом/ссылкой.
- [ ] **Вход через Telegram с телефона** (не из браузера, где уже есть сессия):
      `/cabinet/tg` → Start в боте → поллинг подтверждает вход; проверить, что
      открывается именно бот из `TELEGRAM_BOT_USERNAME`.
- [ ] **Повторная выдача устройств:** смоделировать сбой (недоступный SSH) так,
      чтобы оплата прошла, а устройства не создались; затем открыть кабинет —
      `POST /api/billing/devices/sync` досоздаёт недостающие, `provisioned`
      становится 1, повторный вызов лишних подключений не создаёт.
- [ ] **Ротация токена бота:** перевыпустить токен в BotFather, вписать новый в
      админку/`.env`, перезапустить бота, проверить вход и выдачу конфигурации;
      убедиться, что старый токен больше не работает.
- [ ] **Просрочка:** с истёкшей подпиской конфигурация в кабинете отдаёт 403
      `subscription_inactive`, а бот отвечает «Подписка не активна»; после
      оплаты доступ возвращается без ручных действий.
- [ ] **Лимит:** на тарифе с лимитом 5 выдать 5 устройств, проверить, что
      шестое не создаётся через кабинет, что счётчик в кабинете совпадает с
      числом устройств и что просмотр кабинета не увеличивает число слотов.
- [ ] **Второй профиль на устройстве:** в кабинете «Добавить профиль» на
      устройстве с местом в лимите; проверить, что счётчик устройств не
      изменился, у нового профиля свой QR/ключ, он подключается, а после
      удаления peer'а в панели слот освобождается при следующем открытии
      кабинета (имя peer'а при этом не переиспользуется — см. `_used_peer_names`).
      Повторный запрос с уже привязанным или неустановленным протоколом должен
      вернуть 400 и не увеличивать число профилей.
- [ ] **Платёжный журнал:** после успешной оплаты повторно отправить то же
      уведомление ЮKassa (и открыть `/billing/return?pid=…`) — `status`
      остаётся `succeeded`, `paid_at` не меняется; при `BILLING_MOCK=1` в
      `payments.provider` стоит `mock`, а в шапке админки тестовые и бесплатные
      заказы не попадают в «Оплаченных платежей» и в сумму дохода.
- [ ] **Бэкап:** снять копию `billing.db` с учётом WAL, восстановить на стенде,
      сверить планы/платежи/метки; отдельно убедиться, что без исходного
      `SECRET_KEY` секреты не читаются (ожидаемое поведение).
- [ ] **Откат:** прогнать runbook выше на стенде (включая сверку `volumes:`)
      и убедиться, что с закрытым self-service клиент не может создать лишние
      подключения, а оплата в период отката не теряется молча.
