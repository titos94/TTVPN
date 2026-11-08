# Встроенный терминал TTVPN: политика доступа

Документ фиксирует решение владельца и то, как оно реализовано в коде.
Реализация политики — `terminal_policy.py`; тесты — `tests/test_terminal_policy.py`.

## Решение владельца

- **Человек-администратор:** полный доступ, **кроме критических систем**.
- **Встроенный ИИ:** полный доступ, **кроме API-ключей и секретов**.

«Полный доступ» означает, что незнакомая команда разрешается: панель не
превращается в клетку из белого списка. Ограничения точечные — только на то,
что способно необратимо разрушить сервис или раскрыть секреты.

## Что запрещено всем, включая владельца

Правило `deny-critical`. Отказ возвращается до выполнения команды.

| Категория | Что блокируется |
|---|---|
| Форматирование и разметка диска | `mkfs.*`, `wipefs`, `fdisk`, `sfdisk`, `parted`, `mkswap`, `cfdisk`, `dd of=/dev/...` |
| Удаление корня | `rm -r -f` по `/`, `/*`, `/etc`, `/usr`, `/boot`, `/opt/ttvpn*`, `/opt/amnezia*`, `~`, `/var/lib/docker` |
| Остановка хоста | `shutdown`, `reboot`, `halt`, `poweroff`, `kexec`, `init 0`, `init 6`, `systemctl reboot/poweroff` |
| Остановка критических сервисов | `systemctl stop/disable/mask` для `amnezia*`, `ttvpn*`, `docker`, `containerd`, `caddy`, `nginx`, `sshd`, `cron` |
| Контейнеры панели | `docker stop/kill/rm/restart` для `amnezia_panel`, `ttvpn_monitor`, `ttvpn-caddy`; `docker compose down`; `docker system prune`; `docker volume rm/prune` |
| Сеть и firewall | `iptables -F`, `iptables -X`, `iptables -P ... DROP`, `nft flush ruleset`, `ufw disable`, `ip link set eth0 down`, `ip route del`, `ip addr flush`, `ifdown` |
| Данные панели | запись, удаление, `sed -i`, `truncate`, `chmod` по `/opt/amnezia-web-panel`, `/opt/ttvpn-panel`, `/opt/ttvpn-caddy`, `data.json`, `billing.db`, `.lifecycle-uncertain` |
| VPN-конфигурации | удаление и перезапись `/etc/amnezia`, `/etc/wireguard`, `/etc/amneziawg` |
| Права на системные каталоги | `chmod -R`, `chown -R` по `/` и системным путям |
| Отказ в обслуживании | fork-бомба `:(){ :\|:& };:` |

Обходы закрыты: команда разбивается на сегменты по `;`, `&&`, `\|\|`, `\|`,
переводу строки, `$(...)` и обратным кавычкам; содержимое
`bash -c "…"` / `sh -c '…'` проверяется рекурсивно (до трёх уровней);
ведущие `sudo`, `env`, `nice`, `timeout`, `xargs` и присваивания переменных
пропускаются при определении имени команды. Поэтому `sudo  rm -r -f  /`,
`echo x; rm -rf /` и `bash -c "reboot"` отклоняются так же, как прямая запись.

## Что запрещено только ИИ

Правило `deny-secret`. Владельцу эти же действия разрешены, потому что он и
так имеет доступ к серверу.

- Чтение `data.json`, `billing.db`, `.env`, `/opt/ttvpn-panel/.env`,
  `~/.ssh/id_*`, `*.pem`, `*.key`, `~/.bash_history`, `~/.netrc`, kubeconfig.
- `printenv`, `env`, `export`, `set`, `declare`, `/proc/*/environ`,
  `/proc/*/cmdline`.
- `docker inspect` (выгружает `Config.Env` целиком) и `docker compose config`.
- `awg show ... private-key`, `wg show ... private-key`.
- Любое упоминание `Authorization`, `Bearer`, `API_KEY`, `SECRET_KEY`,
  `TTVPN_ASSISTANT_API_KEY`, `TTVPN_CERT_ASK_TOKEN`, `password`, `token`, `psk`.
- `curl`/`wget` с `-X POST`, `--data`, `--post-data`, `--upload-file`, `-F`:
  запрещена отправка данных во внешний сервис.

## Что разрешено ИИ

Диагностика и управление: `docker ps`, `docker logs --tail N`, `docker stats`,
`systemctl status/is-active/list-units`, `journalctl`, `awg show`, `wg show`,
`ip a`, `ip route`, `ss`, `ping -c N`, `dig`, `nslookup`, `df`, `du`, `free`,
`uptime`, `ps`, `dmesg`, `ls`, `cat` (кроме секретных путей), `grep`, `tail`,
`head`, `curl -s GET` без заголовков авторизации.

## Маскировка вывода

`redact_output` применяется к **любому** выводу до показа в интерфейсе, ИИ и
журнале. Заменяется на `<redacted>`:

- блоки `-----BEGIN … PRIVATE KEY----- … -----END … -----END`;
- `Authorization: …` и `Bearer …`;
- значения переменных с `SECRET`, `PASSWORD`, `TOKEN`, `API_KEY`, `PSK`;
- `PrivateKey = …`, `PresharedKey = …` (`PublicKey` сохраняется);
- JWT (`eyJ…`), ключи провайдеров (`sk-…`, `pk-…`, `rk-…`);
- длинные base64/hex-последовательности от 40 символов.

Вывод обрезается до 20 000 символов с пометкой «вывод обрезан».
`sanitize_for_audit` обрезает команду до 500 символов и вычищает из неё
`Authorization`, `Bearer`, `KEY=value` с секретами и ключи провайдеров —
именно эта строка попадает в журнал аудита.

## Журнал аудита

Каждый запуск фиксируется: время, субъект (`owner` / `ai`), `sanitize_for_audit(команда)`,
решение (`allowed`, `rule`, `reason`), код возврата и длительность. Отклонённые
команды тоже пишутся в журнал — это важно для разбора инцидентов.

## Границы: что политика не делает

Это **классификатор, а не песочница**. Он не защищает от ошибки в разрешённой
команде: `dd if=/dev/zero of=/tmp/file` разрешён и может занять диск;
`kill -9` разрешён и может убить процесс панели; `apt-get upgrade` разрешён и
может обновить ядро. Поэтому обязательны внешние уровни защиты:

1. Терминал доступен только роли `admin` с подтверждённым вторым фактором.
2. Контейнер терминала монтирует данные панели **только для чтения**;
   запись в `/opt/ttvpn-panel` физически недоступна.
3. Отдельный пользователь без `CAP_SYS_ADMIN`, без доступа к docker-сокету
   там, где он не нужен.
4. Все команды выполняются с таймаутом и ограничением вывода.
5. Резервная копия `data.json` и `/data/caddy` перед крупными изменениями.

## Ручная проверка на сервере

```bash
# 1. Классификатор не пускает разрушение
python3 -c "import terminal_policy as t; print(t.classify('rm -rf /'))"
# 2. ИИ не видит ключи
python3 -c "import terminal_policy as t; print(t.classify('cat /opt/ttvpn-panel/.env', actor='ai'))"
# 3. Секреты скрываются в выводе
python3 -c "import terminal_policy as t; print(t.redact_output('SECRET_KEY=abc123'))"
```

## Что стоит уточнить у владельца

- Нужен ли второй фактор для терминала или достаточно роли `admin`.
- Разрешать ли владельцу `apt-get upgrade` и обновление ядра — сейчас разрешено.
- Нужен ли запрет на `kill`/`systemctl restart` панели для владельца
  (сейчас разрешено, останавливать контейнер панели — нет).
- Хранить ли вывод команд в журнале (сейчас — только команда и результат).
