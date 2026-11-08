"""Политика встроенного терминала TTVPN: классификация команд, маскировка.

Решение владельца:
- терминал для человека — полный доступ, **кроме критических систем**;
- для ИИ — полный доступ, **кроме API-ключей и секретов**.

Модуль ничего не выполняет: только решает, можно ли запускать команду, и
готовит безопасный текст для показа (вывод и журнал аудита). Это классификатор
и редактор секретов, а не песочница: он не защищает от ошибок в самой
разрешённой команде, поэтому критические пути дополнительно закрыты на уровне
прав файловой системы и монтирования данных в контейнер.

Правило отказа срабатывает до любого выполнения; ни одна функция здесь не
импортирует `app.py`, не обращается к сети и не трогает диск.
"""

from __future__ import annotations

import re
from dataclasses import dataclass


OWNER = 'owner'
AI = 'ai'
ACTORS = (OWNER, AI)

MAX_COMMAND_LENGTH = 2000

#: Сколько раз разворачивать `bash -c "…"` и подобные обёртки.
MAX_NESTING = 3


@dataclass(frozen=True)
class Decision:
    """Результат классификации одной команды."""

    allowed: bool
    reason: str
    rule: str
    category: str

    def to_dict(self) -> dict:
        return {
            'allowed': self.allowed,
            'reason': self.reason,
            'rule': self.rule,
            'category': self.category,
        }


def _allow(category: str) -> Decision:
    return Decision(True, 'Разрешено', 'allowlist', category)


def _deny(reason: str, rule: str, category: str = 'unknown') -> Decision:
    return Decision(False, reason, rule, category)


# --- 1. Разбор команды -----------------------------------------------------

#: Границы команд: `;`, `&&`, `||`, конвейер, перевод строки, подстановки.
_SEGMENT_SPLIT = re.compile(r'(?:\$\(|\)|`|\|\||&&|;|\n|\|)')

#: Обёртки, содержимое которых тоже нужно проверить.
_NESTED = re.compile(
    r'(?:^|[\s;&|(])(?:bash|sh|zsh|dash|ksh)\s+(?:-\S+\s+)*-c\s+(["\'])(.*?)\1',
    re.DOTALL,
)

_CONTROL = re.compile(r'[\x00-\x08\x0b-\x1f]')

#: Обёртки привилегий и префиксы, которые не меняют смысл команды.
_WRAPPERS = frozenset({
    'sudo', 'doas', 'env', 'time', 'nice', 'nohup', 'command', 'exec',
    'stdbuf', 'timeout', 'ionice', 'unbuffer', 'xargs',
})

_ASSIGNMENT = re.compile(r'^[A-Za-z_][A-Za-z0-9_]*=')


def _segments(command: str) -> list[str]:
    return [part.strip() for part in _SEGMENT_SPLIT.split(command) if part.strip()]


def _nested_commands(command: str) -> list[str]:
    return [match.group(2).strip() for match in _NESTED.finditer(command)]


def _command_word(segment: str) -> tuple[str, list[str]]:
    """Вернуть имя команды и аргументы, пропустив обёртки и присваивания."""
    tokens = segment.replace('\t', ' ').split()
    index = 0
    while index < len(tokens):
        token = tokens[index]
        if _ASSIGNMENT.match(token):
            index += 1
            continue
        if token in _WRAPPERS:
            index += 1
            # флаги обёртки: `sudo -E`, `timeout 30`, `nice -n 10`
            while index < len(tokens) and (tokens[index].startswith('-')
                                          or tokens[index].isdigit()):
                index += 1
            continue
        break
    if index >= len(tokens):
        return '', []
    return tokens[index].lower(), tokens[index + 1:]


# --- 2. Критические системы: запрещено всем ---------------------------------

_ROOT_TARGETS = frozenset({
    '/', '/*', '~', '~/*', '*', '/etc', '/etc/*', '/boot', '/boot/*', '/dev',
    '/dev/*', '/usr', '/usr/*', '/bin', '/bin/*', '/sbin', '/sbin/*',
    '/lib', '/lib/*', '/var', '/var/*', '/opt', '/opt/*', '/home', '/home/*',
    '/root', '/root/*', '/srv', '/proc', '/sys',
})

_CRITICAL_DIRS = (
    '/etc', '/boot', '/usr', '/bin', '/sbin', '/lib', '/var',
    '/opt/ttvpn', '/opt/amnezia', '/var/lib/docker', '/proc', '/sys',
)

_PROTECTED = re.compile(
    r'(?:/opt/amnezia-web-panel|/opt/ttvpn-panel|/opt/ttvpn-caddy|'
    r'/var/lib/docker|data\.json|billing\.db|\.lifecycle-uncertain)'
)

_VPN_CONF = re.compile(r'(?:/etc/amnezia|/etc/wireguard|/etc/amneziawg)')

_MUTATING = frozenset({
    'rm', 'rmdir', 'mv', 'cp', 'tee', 'truncate', 'dd', 'chmod', 'chown',
    'chgrp', 'ln', 'install', 'shred', 'unlink',
})

_SYSTEM_UNITS = re.compile(
    r'^(?:amnezia|ttvpn|docker|containerd|caddy|nginx|sshd|ssh|cron|systemd-journald)',
)

_CRITICAL_CONTAINERS = re.compile(r'^(?:amnezia_panel|ttvpn_monitor|ttvpn-caddy)\b')

_EDGE_LINK = re.compile(r'^(?:eth|ens|eno|enp|bond|br-|docker0|wg|awg)')

_FORK_BOMB = re.compile(r':\s*\(\s*\)\s*\{.*\}\s*;?\s*:')

_REDIRECT = re.compile(r'(?<![0-9])>>?\s*([^\s;&|>]+)')


def _is_root_target(path: str) -> bool:
    clean = path.rstrip('/') or '/'
    if path in _ROOT_TARGETS or clean in _ROOT_TARGETS:
        return True
    return any(path == prefix or path.startswith(prefix + '/')
               for prefix in ('/etc', '/boot', '/usr', '/bin', '/sbin', '/lib',
                              '/opt/ttvpn', '/opt/amnezia', '/var/lib/docker'))


def _rm_guard(word: str, args: list[str]) -> Decision | None:
    if word not in ('rm', 'rmdir', 'shred', 'unlink'):
        return None
    recursive = forced = False
    targets: list[str] = []
    for arg in args:
        if arg.startswith('--'):
            recursive = recursive or arg == '--recursive'
            forced = forced or arg == '--force'
        elif arg.startswith('-') and len(arg) > 1:
            flags = arg[1:]
            recursive = recursive or 'r' in flags or 'R' in flags
            forced = forced or 'f' in flags
        else:
            targets.append(arg)
    if any(_is_root_target(target) for target in targets):
        if recursive or forced or word != 'rm':
            return _deny('Удаление системного каталога запрещено', 'deny-critical')
    if word == 'rmdir' and any(_is_root_target(t) for t in targets):
        return _deny('Удаление системного каталога запрещено', 'deny-critical')
    return None


def _service_guard(word: str, args: list[str]) -> Decision | None:
    if word != 'systemctl':
        return None
    sub = ''
    units = []
    for arg in args:
        if arg.startswith('-') or arg.startswith('--'):
            continue
        if not sub:
            sub = arg.lower()
            continue
        units.append(arg)
    if sub in ('poweroff', 'reboot', 'halt', 'kexec', 'suspend'):
        return _deny('Остановка или перезагрузка хоста запрещена', 'deny-critical')
    if sub in ('stop', 'disable', 'mask', 'kill') and any(
            _SYSTEM_UNITS.match(unit) for unit in units):
        return _deny('Остановка критического сервиса запрещена', 'deny-critical')
    return None


def _docker_guard(word: str, args: list[str]) -> Decision | None:
    if word not in ('docker', 'docker-compose', 'podman'):
        return None
    plain = [arg.lower() for arg in args if not arg.startswith('-')]
    if not plain:
        return None
    if plain[0] in ('compose', 'stack') and any(
            item in ('down', 'kill', 'rm') for item in plain[1:]):
        return _deny('Остановка стека панели запрещена', 'deny-critical')
    if plain[0] == 'system' and 'prune' in plain[1:]:
        return _deny('Очистка docker system запрещена', 'deny-critical')
    if plain[0] == 'volume' and any(item in ('rm', 'prune') for item in plain[1:]):
        return _deny('Удаление docker-томов запрещено', 'deny-critical')
    if plain[0] in ('stop', 'kill', 'rm', 'restart', 'pause') and any(
            _CRITICAL_CONTAINERS.match(item) for item in plain[1:]):
        return _deny('Остановка контейнера панели запрещена', 'deny-critical')
    if plain[0] in ('stop', 'kill', 'rm') and '$(docker ps' in ' '.join(args):
        return _deny('Массовая остановка контейнеров запрещена', 'deny-critical')
    return None


def _network_guard(word: str, args: list[str]) -> Decision | None:
    joined = ' '.join(args)
    if word == 'iptables' or word == 'ip6tables':
        flags = {arg for arg in args if arg.startswith('-')}
        if flags & {'-F', '-X', '--flush', '--delete-chain'}:
            return _deny('Очистка правил firewall запрещена', 'deny-critical')
        if '-P' in flags and 'DROP' in joined.upper():
            return _deny('Политика DROP на цепочку запрещена', 'deny-critical')
        return None
    if word == 'nft' and 'flush' in args and 'ruleset' in args:
        return _deny('Очистка nftables запрещена', 'deny-critical')
    if word == 'ufw' and 'disable' in args:
        return _deny('Отключение firewall запрещено', 'deny-critical')
    if word == 'ip':
        plain = [arg.lower() for arg in args if not arg.startswith('-')]
        if plain[:2] == ['link', 'set'] and 'down' in plain and any(
                _EDGE_LINK.match(item) for item in plain[2:]):
            return _deny('Отключение внешнего интерфейса запрещено', 'deny-critical')
        if plain[:1] == ['route'] and any(
                item in ('del', 'delete', 'flush') for item in plain[1:]):
            return _deny('Удаление маршрутов запрещено', 'deny-critical')
        if plain[:1] == ['addr'] and 'flush' in plain:
            return _deny('Сброс адресов интерфейса запрещён', 'deny-critical')
        return None
    if word == 'ifdown':
        return _deny('Отключение интерфейса запрещено', 'deny-critical')
    return None


def _write_guard(word: str, args: list[str], segment: str) -> Decision | None:
    """Запись в данные панели, Caddy и VPN-конфигурации."""
    redirects = [match.group(1) for match in _REDIRECT.finditer(segment)]
    targets = list(args) + redirects
    joined = ' '.join(targets)
    mutating = (word in _MUTATING
                or (word == 'sed' and any(arg.startswith('-i') for arg in args))
                or word in ('tee', 'dd'))
    if _PROTECTED.search(joined) and (mutating or redirects):
        return _deny('Изменение данных панели запрещено', 'deny-critical')
    if _VPN_CONF.search(joined) and mutating and (
            word in ('rm', 'mv', 'shred', 'truncate', 'dd')
            or any(arg.startswith('-i') for arg in args)
            or word in ('tee', 'chmod', 'chown')):
        return _deny('Изменение VPN-конфигураций запрещено', 'deny-critical')
    if word in ('chmod', 'chown', 'chgrp'):
        recursive = any(arg in ('-R', '--recursive') or
                        (arg.startswith('-') and 'R' in arg) for arg in args)
        paths = [arg for arg in args if not arg.startswith('-')]
        if recursive and any(_is_root_target(path) or path.startswith('/opt/ttvpn')
                             for path in paths):
            return _deny('Смена прав на системный каталог запрещена', 'deny-critical')
    return None


def _critical(segment: str) -> Decision | None:
    lower = segment.lower()
    word, args = _command_word(segment)
    joined = ' '.join(args)

    if _FORK_BOMB.search(lower):
        return _deny('Fork-бомба запрещена', 'deny-critical')
    if word == 'mkfs' or word.startswith('mkfs.'):
        return _deny('Форматирование раздела запрещено', 'deny-critical')
    if word in ('wipefs', 'fdisk', 'sfdisk', 'parted', 'mkswap', 'cfdisk'):
        return _deny('Разметка и форматирование диска запрещены', 'deny-critical')
    if word in ('shutdown', 'reboot', 'halt', 'poweroff', 'kexec'):
        return _deny('Остановка или перезагрузка хоста запрещена', 'deny-critical')
    if word == 'init' and args and args[0] in ('0', '6'):
        return _deny('Остановка или перезагрузка хоста запрещена', 'deny-critical')
    if word == 'dd':
        if re.search(r'of=/dev/(?:sd|hd|nvme|vd|xvd|mmcblk|disk|loop)', joined):
            return _deny('Запись на блочное устройство запрещена', 'deny-critical')
    for guard in (_rm_guard, _service_guard, _docker_guard, _network_guard):
        decision = guard(word, args)
        if decision:
            return decision
    decision = _write_guard(word, args, segment)
    if decision:
        return decision
    return None


# --- 3. Секреты: запрещены только ИИ ---------------------------------------

_SECRET_PATHS = (
    'data.json', 'data.json.lifecycle-uncertain.json', 'billing.db', '.env',
    'id_rsa', 'id_ed25519', 'id_ecdsa', 'id_dsa', '.pem', '.key',
    '/proc/self/environ', '.bash_history', '.zsh_history', '.pgpass',
    '.netrc', 'credentials', 'kubeconfig',
)

_SECRET_NAMES = re.compile(
    r'(?i)(?:api[_-]?key|secret[_-]?key|secret|password|passwd|token|bearer|'
    r'authorization|private[_-]?key|presharedkey|psk|TTVPN_ASSISTANT_API_KEY|'
    r'TTVPN_CERT_ASK_TOKEN)'
)

_SECRET_COMMANDS = frozenset({'printenv', 'set', 'export', 'declare', 'compgen'})

_SECRET_FLAGS = (
    re.compile(r'(?i)\bprivate-key\b'),
    re.compile(r'(?i)\benv\b'),
)

_UPLOAD = re.compile(
    r'(?i)(?:-X\s*POST|--request\s+POST|--data|--post-data|--post-file|-d\s|'
    r'--form|-F\s|--upload-file|-T\s)'
)


def _secret(segment: str) -> Decision | None:
    lower = segment.lower()
    word, args = _command_word(segment)
    joined = ' '.join(args)

    if word in _SECRET_COMMANDS:
        return _deny('Просмотр переменных окружения запрещён', 'deny-secret')
    if word in ('curl', 'wget', 'http', 'https'):
        if _UPLOAD.search(joined):
            return _deny('Отправка данных во внешний сервис запрещена', 'deny-secret')
    if word == 'docker' and 'inspect' in args:
        formatted = '--format' in args or '-f' in args
        if not formatted or re.search(r'(?i)env', joined):
            return _deny('Выгрузка окружения контейнера запрещена', 'deny-secret')
    if word in ('docker', 'docker-compose') and 'compose' in args and 'config' in args:
        return _deny('Выгрузка конфигурации с секретами запрещена', 'deny-secret')

    for pattern in _SECRET_FLAGS:
        if pattern.search(lower):
            return _deny('Операция с секретом запрещена', 'deny-secret')

    for marker in _SECRET_PATHS:
        if marker in lower:
            return _deny('Доступ к файлу с секретами запрещён', 'deny-secret')

    if _SECRET_NAMES.search(joined):
        return _deny('Обращение к секрету запрещено', 'deny-secret')

    if re.search(r'/proc/\d+/(?:environ|cmdline)', lower):
        return _deny('Чтение окружения процесса запрещено', 'deny-secret')
    return None


# --- 4. Категории ----------------------------------------------------------

_CATEGORIES = (
    ('read', frozenset({
        'cat', 'head', 'tail', 'less', 'more', 'grep', 'egrep', 'wc', 'stat',
        'ls', 'pwd', 'find', 'df', 'du', 'free', 'uptime', 'uname', 'ps',
        'top', 'htop', 'dmesg', 'lsblk', 'journalctl', 'tree', 'file', 'sort',
        'uniq', 'awk', 'cut', 'tr', 'diff', 'md5sum', 'sha256sum', 'date',
        'whoami', 'id', 'hostname', 'lscpu', 'lsmod', 'iostat', 'vmstat',
        'getent', 'jq', 'watch', 'realpath', 'readlink', 'basename', 'dirname',
        'echo', 'printf',
    })),
    ('service', frozenset({
        'systemctl', 'service', 'update-rc.d', 'supervisorctl', 'crontab',
        'logrotate', 'timedatectl', 'hostnamectl',
    })),
    ('docker', frozenset({'docker', 'docker-compose', 'podman', 'ctr', 'nerdctl'})),
    ('network', frozenset({
        'ip', 'ss', 'netstat', 'ping', 'ping6', 'dig', 'nslookup', 'host',
        'traceroute', 'tracepath', 'curl', 'wget', 'mtr', 'nc', 'nmcli',
        'iptables', 'nft', 'ufw', 'resolvectl', 'tcpdump',
    })),
    ('vpn', frozenset({
        'awg', 'awg-quick', 'wg', 'wg-quick', 'amnezia', 'amneziawg',
        'xray', 'sing-box', 'openvpn',
    })),
    ('files', frozenset({
        'cp', 'mv', 'rm', 'mkdir', 'rmdir', 'touch', 'chmod', 'chown', 'chgrp',
        'ln', 'tar', 'rsync', 'zip', 'unzip', 'gzip', 'tee', 'truncate', 'dd',
        'sed', 'install', 'shred', 'namei', 'mount', 'umount',
    })),
    ('package', frozenset({
        'apt', 'apt-get', 'dpkg', 'yum', 'dnf', 'apk', 'pip', 'pip3', 'npm',
        'snap', 'add-apt-repository',
    })),
)

_CATEGORY_MAP = {word: name for name, words in _CATEGORIES for word in words}


def _category(segment: str) -> str:
    word, _ = _command_word(segment)
    if not word:
        return 'unknown'
    if word in _CATEGORY_MAP:
        return _CATEGORY_MAP[word]
    if word in ('bash', 'sh', 'zsh', 'dash'):
        return 'unknown'
    return 'unknown'


# --- 5. Публичный интерфейс ------------------------------------------------

def _shape(command: object) -> Decision | None:
    if not isinstance(command, str):
        return _deny('Команда должна быть строкой', 'deny-shape')
    # Управляющие символы проверяются до обрезки: `strip()` их скрывает.
    if _CONTROL.search(command) or '\r' in command:
        return _deny('Команда содержит управляющие символы', 'deny-shape')
    text = command.strip()
    if not text:
        return _deny('Пустая команда', 'deny-shape')
    if len(text) > MAX_COMMAND_LENGTH:
        return _deny(
            f'Команда длиннее {MAX_COMMAND_LENGTH} символов', 'deny-shape')
    return None


def classify(command: object, *, actor: str = OWNER, _depth: int = 0) -> Decision:
    """Решить, можно ли выполнять команду данному субъекту.

    Порядок правил: форма → критические системы → секреты (только ИИ) →
    разрешено. Отказ по критическому правилу действует и для владельца.
    """
    if actor not in ACTORS:
        return _deny('Неизвестный субъект команды', 'deny-actor')

    decision = _shape(command)
    if decision:
        return decision

    text = command.strip()

    # Fork-бомба содержит разделители сегментов внутри себя, поэтому
    # проверяется на всей строке до разбора.
    if _FORK_BOMB.search(text.lower()):
        return _deny('Fork-бомба запрещена', 'deny-critical')

    if _depth < MAX_NESTING:
        for nested in _nested_commands(text):
            inner = classify(nested, actor=actor, _depth=_depth + 1)
            if not inner.allowed:
                return inner

    for segment in _segments(text):
        decision = _critical(segment)
        if decision:
            return decision

    if actor == AI:
        for segment in _segments(text):
            decision = _secret(segment)
            if decision:
                return decision

    category = 'unknown'
    for segment in _segments(text):
        found = _category(segment)
        if found != 'unknown':
            category = found
            break
    return _allow(category)


def is_allowed(command: object, *, actor: str = OWNER) -> bool:
    return classify(command, actor=actor).allowed


# --- 6. Маскировка секретов ------------------------------------------------

_PEM = re.compile(
    r'-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?-----END [A-Z0-9 ]*PRIVATE KEY-----',
    re.DOTALL)
_AUTH_HEADER = re.compile(r'(?i)\b(authorization\s*:\s*)([^\r\n]+)')
_BEARER = re.compile(r'(?i)\b(bearer\s+)[A-Za-z0-9\-._~+/]{8,}=*')
_ENV_SECRET = re.compile(
    r'(?i)\b([A-Z0-9_]*(?:SECRET|PASSWORD|PASSWD|TOKEN|APIKEY|API_KEY|'
    r'PRIVATE_KEY|PSK)[A-Z0-9_]*\s*[=:]\s*)([^\s"\';&]+)')
_WG_KEY = re.compile(r'(?i)\b((?:privatekey|presharedkey)\s*=\s*)(\S+)')
_JWT = re.compile(r'\beyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}')
_PROVIDER_KEY = re.compile(r'\b(?:sk|pk|rk)-[A-Za-z0-9_-]{16,}')
_LONG_BLOB = re.compile(r'(?<![A-Za-z0-9+/])[A-Za-z0-9+/]{40,}={0,2}(?![A-Za-z0-9+/])')

REDACTED = '<redacted>'

#: Предел вывода, чтобы одна команда не раздувала память и журнал.
MAX_OUTPUT_CHARS = 20000


def redact_output(text: object) -> str:
    """Заменить секреты в выводе команды заглушкой `<redacted>`.

    Вывод обрезается до :data:`MAX_OUTPUT_CHARS`; обрезка выполняется до
    маскировки, потому что разбор очень длинного текста дороже, чем польза.
    """
    if text is None:
        return ''
    if not isinstance(text, str):
        try:
            text = str(text)
        except Exception:  # noqa: BLE001 - маскировка не должна падать
            return REDACTED
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + '\n… (вывод обрезан)'
    text = _PEM.sub(REDACTED, text)
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _ENV_SECRET.sub(lambda m: m.group(1) + REDACTED, text)
    text = _WG_KEY.sub(lambda m: m.group(1) + REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    text = _PROVIDER_KEY.sub(REDACTED, text)
    text = _LONG_BLOB.sub(REDACTED, text)
    return text


def sanitize_for_audit(command: object, *, limit: int = 500) -> str:
    """Обрезать команду для журнала аудита и убрать из неё секреты."""
    if not isinstance(command, str):
        return ''
    text = command.strip()
    if len(text) > limit:
        text = text[:limit] + '…'
    text = _AUTH_HEADER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _BEARER.sub(lambda m: m.group(1) + REDACTED, text)
    text = _ENV_SECRET.sub(lambda m: m.group(1) + REDACTED, text)
    text = _WG_KEY.sub(lambda m: m.group(1) + REDACTED, text)
    text = _PROVIDER_KEY.sub(REDACTED, text)
    text = _JWT.sub(REDACTED, text)
    return text
