# -*- coding: utf-8 -*-
"""Статический сторож качества разметки TTVPN.

Тесты не запускают браузер, не ходят в сеть и не читают DATA_FILE:
проверяются только файлы ``templates/*.html``.

Что ловим:
  1. Эмодзи в вёрстке (исключение — только флаги в переключателе языка,
     и только там, где рядом есть контекст выбора языка).
  2. Утечки Jinja: не компилируется, ``{% raw %}`` отдаёт наружу ``{{``,
     либо Jinja-выражение вставлено в JS-строку без ``|tojson``.
  3. Дочерний шаблон не объявляет заголовок страницы или объявляет его пустым.
  4. Дочерний шаблон переопределяет блок, которого нет у родителя
     (переименовали блок в base.html — страница молча теряет содержимое).
  5. Инлайновые ``style="..."`` с цветами/размерами-хаками — не больше
     зафиксированного baseline (защита от массового возврата хаков).
  6. Маркеры старого слоя (``app-header``, ``nav-tabs``, bootstrap-сетка).
  7. Внешние ресурсы (``<script src="http...">``, ``<link href="http...">``).

Запуск:
    pytest -q tests/test_ttvpn_ui_design_guard.py
"""

from __future__ import annotations

import re
import unicodedata
from bisect import bisect_right
from pathlib import Path

import pytest
from jinja2 import Environment, FileSystemLoader

TEMPLATES_DIR = Path(__file__).resolve().parents[1] / "templates"


def _template_names() -> list[str]:
    return sorted(p.name for p in TEMPLATES_DIR.glob("*.html"))


TEMPLATE_NAMES = _template_names()


def _read(name: str) -> str:
    return (TEMPLATES_DIR / name).read_text(encoding="utf-8")


def _line_at(text: str, lineno: int) -> str:
    lines = text.splitlines()
    if 1 <= lineno <= len(lines):
        return lines[lineno - 1].strip()[:160]
    return ""


def _report(problems: list[str], what: str) -> None:
    if problems:
        raise AssertionError(f"{what}:\n  " + "\n  ".join(problems))


# ---------------------------------------------------------------------------
# 1. Эмодзи
# ---------------------------------------------------------------------------

# Диапазоны символов со свойством Emoji=Yes из emoji-data.txt
# (unicode.org/Public/UCD/latest/ucd/emoji/emoji-data.txt, Unicode 18.0).
# Исключены ASCII-основы клавиш (0-9, #, *), (c), (r), (tm): в обычном тексте
# это не эмодзи. Таблица зафиксирована в коде, чтобы тест не зависел от
# установленных пакетов и версии Unicode в окружении.
EMOJI_RANGES = [
    (0x203C, 0x203C), (0x2049, 0x2049), (0x2139, 0x2139), (0x2194, 0x2199), (0x21A9, 0x21AA),
    (0x231A, 0x231B), (0x2328, 0x2328), (0x23CF, 0x23CF), (0x23E9, 0x23F3), (0x23F8, 0x23FA),
    (0x24C2, 0x24C2), (0x25AA, 0x25AB), (0x25B6, 0x25B6), (0x25C0, 0x25C0), (0x25FB, 0x25FE),
    (0x2600, 0x2604), (0x260E, 0x260E), (0x2611, 0x2611), (0x2614, 0x2615), (0x2618, 0x2618),
    (0x261D, 0x261D), (0x2620, 0x2620), (0x2622, 0x2623), (0x2626, 0x2626), (0x262A, 0x262A),
    (0x262E, 0x262F), (0x2638, 0x263A), (0x2640, 0x2640), (0x2642, 0x2642), (0x2648, 0x2653),
    (0x265F, 0x2660), (0x2663, 0x2663), (0x2665, 0x2666), (0x2668, 0x2668), (0x267B, 0x267B),
    (0x267E, 0x267F), (0x2692, 0x2697), (0x2699, 0x2699), (0x269B, 0x269C), (0x26A0, 0x26A1),
    (0x26A7, 0x26A7), (0x26AA, 0x26AB), (0x26B0, 0x26B1), (0x26BD, 0x26BE), (0x26C4, 0x26C5),
    (0x26C8, 0x26C8), (0x26CE, 0x26CF), (0x26D1, 0x26D1), (0x26D3, 0x26D4), (0x26E9, 0x26EA),
    (0x26F0, 0x26F5), (0x26F7, 0x26FA), (0x26FD, 0x26FD), (0x2702, 0x2702), (0x2705, 0x2705),
    (0x2708, 0x270D), (0x270F, 0x270F), (0x2712, 0x2712), (0x2714, 0x2714), (0x2716, 0x2716),
    (0x271D, 0x271D), (0x2721, 0x2721), (0x2728, 0x2728), (0x2733, 0x2734), (0x2744, 0x2744),
    (0x2747, 0x2747), (0x274C, 0x274C), (0x274E, 0x274E), (0x2753, 0x2755), (0x2757, 0x2757),
    (0x2763, 0x2764), (0x2795, 0x2797), (0x27A1, 0x27A1), (0x27B0, 0x27B0), (0x27BF, 0x27BF),
    (0x2934, 0x2935), (0x2B05, 0x2B07), (0x2B1B, 0x2B1C), (0x2B50, 0x2B50), (0x2B55, 0x2B55),
    (0x3030, 0x3030), (0x303D, 0x303D), (0x3297, 0x3297), (0x3299, 0x3299), (0x1F004, 0x1F004),
    (0x1F0CF, 0x1F0CF), (0x1F170, 0x1F171), (0x1F17E, 0x1F17F), (0x1F18E, 0x1F18E),
    (0x1F191, 0x1F19A), (0x1F1E6, 0x1F1FF), (0x1F201, 0x1F202), (0x1F21A, 0x1F21A),
    (0x1F22F, 0x1F22F), (0x1F232, 0x1F23A), (0x1F250, 0x1F251), (0x1F300, 0x1F321),
    (0x1F324, 0x1F393), (0x1F396, 0x1F397), (0x1F399, 0x1F39B), (0x1F39E, 0x1F3F0),
    (0x1F3F3, 0x1F3F5), (0x1F3F7, 0x1F4FD), (0x1F4FF, 0x1F53D), (0x1F549, 0x1F54E),
    (0x1F550, 0x1F567), (0x1F56F, 0x1F570), (0x1F573, 0x1F57A), (0x1F587, 0x1F587),
    (0x1F58A, 0x1F58D), (0x1F590, 0x1F590), (0x1F595, 0x1F596), (0x1F5A4, 0x1F5A5),
    (0x1F5A8, 0x1F5A8), (0x1F5B1, 0x1F5B2), (0x1F5BC, 0x1F5BC), (0x1F5C2, 0x1F5C4),
    (0x1F5D1, 0x1F5D3), (0x1F5DC, 0x1F5DE), (0x1F5E1, 0x1F5E1), (0x1F5E3, 0x1F5E3),
    (0x1F5E8, 0x1F5E8), (0x1F5EF, 0x1F5EF), (0x1F5F3, 0x1F5F3), (0x1F5FA, 0x1F64F),
    (0x1F680, 0x1F6C5), (0x1F6CB, 0x1F6D2), (0x1F6D5, 0x1F6D9), (0x1F6DC, 0x1F6E5),
    (0x1F6E9, 0x1F6E9), (0x1F6EB, 0x1F6EC), (0x1F6F0, 0x1F6F0), (0x1F6F3, 0x1F6FC),
    (0x1F7E0, 0x1F7EB), (0x1F7F0, 0x1F7F0), (0x1F90C, 0x1F93A), (0x1F93C, 0x1F945),
    (0x1F947, 0x1F9FF), (0x1FA70, 0x1FA7C), (0x1FA80, 0x1FAC6), (0x1FAC8, 0x1FAC8),
    (0x1FACC, 0x1FADD), (0x1FADF, 0x1FAEB), (0x1FAEF, 0x1FAFA),
]
_RANGE_STARTS = [a for a, _ in EMOJI_RANGES]

NOTE_EMOJI_CODES = {0x1F1E6, 0x1F1FF}  # границы блока региональных индикаторов


def _is_emoji_codepoint(cp: int) -> bool:
    idx = bisect_right(_RANGE_STARTS, cp) - 1
    return idx >= 0 and cp <= EMOJI_RANGES[idx][1]


def _is_flag_codepoint(cp: int) -> bool:
    """Региональный индикатор — половина эмодзи-флага страны."""
    return 0x1F1E6 <= cp <= 0x1F1FF


# Флаги языков — единственное разрешённое эмодзи. Они жёстко зашиты в
# base.html и login.html (в translations/*.json их нет: проверено, там только
# строки). Разрешение точечное: флаг допустим только на строке, которая
# относится к переключателю языка — рядом (в окне ±1 строка) есть `lang`,
# `lang_xx`, `select_lang` или `set_lang`. Декоративный флаг в другом месте
# страницы тест по-прежнему поймает.
_LANG_SWITCHER = re.compile(r"\blang\b|\blang_[a-z]{2}\b|select_lang|set_lang|langModal|langToggle")


def _emoji_problems(text: str) -> list[str]:
    lines = text.splitlines()
    problems = []
    for lineno, line in enumerate(lines, 1):
        for pos, ch in enumerate(line):
            cp = ord(ch)
            if not _is_emoji_codepoint(cp):
                continue
            if _is_flag_codepoint(cp) or cp == 0xFE0F:
                window = lines[max(0, lineno - 2): lineno + 1]
                if _is_flag_codepoint(cp) and any(_LANG_SWITCHER.search(w) for w in window):
                    continue
                if cp == 0xFE0F:
                    # VS16 лишь усиливает соседний символ; сам по себе не эмодзи.
                    prev = ord(line[pos - 1]) if pos else 0
                    if _is_flag_codepoint(prev) and any(_LANG_SWITCHER.search(w) for w in window):
                        continue
            try:
                uname = unicodedata.name(ch)
            except ValueError:  # pragma: no cover - для приватных зон
                uname = "UNKNOWN"
            problems.append(
                f"{_template_name}.{lineno}: эмодзи {ch!r} U+{cp:04X} ({uname}) — "
                f"замените на SVG-иконку. Строка: {line.strip()[:120]}"
            )
    return problems


_template_name = ""  # заполняется в тесте, чтобы сообщение содержало файл


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_emoji_in_markup(name: str) -> None:
    """В шаблонах нет эмодзи; флаги разрешены только в переключателе языка."""
    global _template_name
    _template_name = name
    text = _read(name)
    problems = _emoji_problems(text)
    _report(problems, f"{name}: найдены эмодзи в разметке")


# ---------------------------------------------------------------------------
# 2. Jinja: компиляция и утечки
# ---------------------------------------------------------------------------

_SCRIPT_BLOCK = re.compile(r"<script\b([^>]*)>(.*?)</script>", re.S | re.I)


def _jinja_in_js_strings(text: str, name: str = "") -> list[str]:
    """Jinja-вывод внутри кавычек JS-строки: ``'{{ x }}'`` без ``|tojson``.

    Такая вставка ломает скрипт, если значение содержит кавычку или перевод
    строки, поэтому допустима только через ``{{ x | tojson }}``.
    """
    problems: list[str] = []
    for block in _SCRIPT_BLOCK.finditer(text):
        attrs = block.group(1).lower()
        if "application/json" in attrs:
            continue
        body = block.group(2)
        base_line = text[: block.start(2)].count("\n")
        for match in re.finditer(r"\{\{", body):
            pos = match.start() - 1
            while pos >= 0 and body[pos] in " \t":
                pos -= 1
            if pos >= 0 and body[pos] in "\"'":
                lineno = base_line + body[: match.start()].count("\n") + 1
                snippet = _line_at(text, lineno)
                item = (
                    f"{name}.{lineno}: Jinja-выражение внутри JS-строки без |tojson — "
                    f"используйте {{{{ ... | tojson }}}}. Строка: {snippet}"
                )
                if item not in problems:
                    problems.append(item)
    return problems


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_jinja_delimiters_in_js_strings(name: str) -> None:
    """Jinja не вставляется в JS-строки без ``|tojson`` (иначе — поломка скрипта)."""
    text = _read(name)
    problems = _jinja_in_js_strings(text, name)
    _report(problems, f"{name}: Jinja-вывод без tojson внутри JS-строк")


_RAW_BLOCK = re.compile(r"\{%-?\s*raw\s*-?%\}(.*?)\{%-?\s*endraw\s*-?%\}", re.S)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_raw_block_leaking_jinja(name: str) -> None:
    """Внутри ``{% raw %}`` не должно быть Jinja-делимитеров — они утекут в HTML."""
    text = _read(name)
    problems = []
    for match in _RAW_BLOCK.finditer(text):
        body = match.group(1)
        if "{{" in body or "{%" in body:
            lineno = text[: match.start(1)].count("\n") + 1
            problems.append(f"{name}.{lineno}: внутри {{% raw %}} остались Jinja-делимитеры")
    _report(problems, f"{name}: {{% raw %}} отдаёт Jinja-делимитеры в разметку")


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_template_compiles(name: str) -> None:
    """Каждый шаблон компилируется Jinja2 — ловит синтаксические поломки правок."""
    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)))
    try:
        env.get_template(name)
    except Exception as exc:  # noqa: BLE001 - нужен любой сбой компиляции
        lineno = getattr(exc, "lineno", None)
        message = getattr(exc, "message", str(exc))
        where = f"{name}.{lineno}" if lineno else name
        snippet = _line_at(_read(name), lineno) if lineno else ""
        raise AssertionError(
            f"{where}: шаблон не компилируется: {type(exc).__name__}: {message}"
            + (f"\n  Строка: {snippet}" if snippet else "")
        ) from exc


# ---------------------------------------------------------------------------
# 3. Блоки и заголовок страницы
# ---------------------------------------------------------------------------

_EXTENDS = re.compile(r"""\{%-?\s*extends\s+["']([^"']+)["']""")
_BLOCK_TAG = re.compile(r"\{%-?\s*(block|endblock)\b([^%]*?)-?%\}")
_TITLE = re.compile(r"<title\b[^>]*>(.*?)</title>", re.S | re.I)


def _parent_name(text: str) -> str | None:
    match = _EXTENDS.search(text)
    return match.group(1) if match else None


def _top_level_blocks(text: str) -> list[tuple[str, int]]:
    """Блоки, объявленные в шаблоне на верхнем уровне (реальные override'ы).

    Блоки, вложенные в другой блок, родителю не передаются — их пропускаем.
    """
    result: list[tuple[str, int]] = []
    depth = 0
    for match in _BLOCK_TAG.finditer(text):
        kind = match.group(1)
        args = match.group(2).strip()
        name = args.split()[0] if args else ""
        if kind == "block":
            if depth == 0 and name:
                result.append((name, text[: match.start()].count("\n") + 1))
            depth += 1
        else:
            depth = max(0, depth - 1)
    return result


def _title_blocks(text: str) -> list[str]:
    match = _TITLE.search(text)
    if not match:
        return []
    return re.findall(r"\{%-?\s*block\s+([A-Za-z0-9_]+)", match.group(1))


def _parent_chain(name: str) -> list[str]:
    chain: list[str] = []
    current = name
    while True:
        parent = _parent_name(_read(current))
        if not parent or parent in chain:
            return chain
        chain.append(parent)
        current = parent


def _all_blocks(text: str) -> list[tuple[str, int]]:
    """Все блоки шаблона, включая вложенные.

    Jinja разрешает дочернему шаблону переопределять блок, вложенный в другой
    блок родителя (``content`` внутри ``shell``), поэтому для проверки
    «существует ли блок у родителя» глубина не важна.
    """
    result: list[tuple[str, int]] = []
    for match in _BLOCK_TAG.finditer(text):
        kind = match.group(1)
        args = match.group(2).strip()
        name = args.split()[0] if args else ""
        if kind == "block" and name:
            result.append((name, text[: match.start()].count("\n") + 1))
    return result


def _parent_blocks(name: str) -> set[str]:
    blocks: set[str] = set()
    for parent in _parent_chain(name):
        blocks.update(b for b, _ in _all_blocks(_read(parent)))
    return blocks


@pytest.mark.parametrize("name", ["base.html", "billing_base.html"])
def test_base_templates_expose_title_block(name: str) -> None:
    """Базовые шаблоны обязаны давать дочерним страницам блок заголовка в <title>."""
    blocks = _title_blocks(_read(name))
    assert blocks, (
        f"{name}: в <title> нет {{% block ... %}} — дочерние шаблоны не могут "
        f"задать заголовок страницы"
    )


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_title_block_declared_and_not_empty(name: str) -> None:
    """Каждый наследник объявляет блок заголовка родителя и не оставляет его пустым."""
    text = _read(name)
    chain = _parent_chain(name)
    if not chain:
        return  # самостоятельный шаблон (login.html, billing_base.html, base.html)
    expected: list[str] = []
    for parent in chain:
        expected.extend(_title_blocks(_read(parent)))
    if not expected:
        return  # за базовый шаблон отвечает test_base_templates_expose_title_block
    declared = _top_level_blocks(text)
    found = [(b, line) for b, line in declared if b in expected]
    if not found:
        raise AssertionError(
            f"{name}: наследует {chain[0]}, но не объявляет ни один блок заголовка "
            f"({', '.join(sorted(set(expected)))}). Заголовок страницы останется "
            f"общим для всех страниц."
        )
    for block_name, lineno in found:
        body = _block_body(text, block_name)
        if not re.sub(r"[\s{}\-%]", "", body):
            raise AssertionError(
                f"{name}.{lineno}: блок {block_name} пустой — заголовок страницы не задан"
            )


def _block_body(text: str, block_name: str) -> str:
    pattern = re.compile(
        r"\{%-?\s*block\s+" + re.escape(block_name) + r"\b[^%]*?-?%\}(.*?)\{%-?\s*endblock\b",
        re.S,
    )
    match = pattern.search(text)
    return match.group(1) if match else ""


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_child_blocks_exist_in_parent(name: str) -> None:
    """Дочерний шаблон не переопределяет блок, которого нет у родителя.

    Jinja молча игнорирует такой блок: страница теряет содержимое.
    """
    text = _read(name)
    chain = _parent_chain(name)
    if not chain:
        return
    known = _parent_blocks(name)
    problems = [
        f"{name}.{line}: блок {block} отсутствует в {chain[0]} — содержимое молча пропадёт"
        for block, line in _top_level_blocks(text)
        if block not in known
    ]
    _report(problems, f"{name}: переопределены несуществующие блоки")


# ---------------------------------------------------------------------------
# 4. Инлайновые стили: хаки не должны расти
# ---------------------------------------------------------------------------

_STYLE_ATTR = re.compile(r"""style\s*=\s*(["'])(.*?)\1""", re.S | re.I)

# «Хак» — инлайновый цвет литералом, !important, либо жёсткий размер в px.
_HACK_STYLE = re.compile(
    r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\("
    r"|!important"
    r"|\bfont-size\s*:\s*[0-9.]"
    r"|\b(?:min-|max-)?(?:width|height)\s*:\s*[0-9.]"
    r"|\b(?:margin|padding)(?:-[a-z]+)?\s*:\s*[0-9.]"
)
_COLOR_STYLE = re.compile(r"#[0-9a-fA-F]{3,8}\b|\brgba?\(|\bhsla?\(")

# Baseline: (хаки, цвет-литералы, !important) на файл.
# Замер сделан на develop после перевода страниц на .tt-* компоненты.
# Правило: значение не должно вырасти. Небольшой запас (см. _slack) позволяет
# другим агентам добавлять одну-две строки, но массовый возврат хаков ловится.
INLINE_STYLE_BASELINE: dict[str, tuple[int, int, int]] = {
    "_ssh_onboarding.html": (1, 0, 0),
    "base.html": (3, 0, 0),
    "billing_admin.html": (21, 0, 0),
    "billing_base.html": (0, 0, 0),
    "billing_cabinet.html": (16, 0, 0),
    "billing_landing.html": (3, 0, 0),
    "billing_legal_unavailable.html": (0, 0, 0),
    "billing_login.html": (6, 0, 0),
    "billing_mock.html": (1, 0, 0),
    "billing_my.html": (11, 3, 0),
    "billing_password.html": (4, 0, 0),
    "billing_plans.html": (8, 0, 0),
    "billing_register.html": (10, 0, 0),
    "billing_reset.html": (8, 0, 0),
    "billing_return.html": (0, 0, 0),
    "billing_tg.html": (7, 0, 0),
    "index.html": (2, 0, 0),
    "login.html": (5, 1, 0),
    "my_connections.html": (4, 0, 0),
    "server.html": (105, 15, 12),
    "server_new.html": (0, 0, 0),
    "settings.html": (4, 0, 0),
    "ttvpn_overview.html": (0, 0, 0),
    "user_share.html": (2, 0, 0),
    "users.html": (1, 0, 0),
}
# Общий бюджет: сумма по всем файлам + запас на текущую миграцию.
TOTAL_HACK_BUDGET = 222 + 20
TOTAL_COLOR_BUDGET = 19 + 3


def _slack(baseline: int) -> int:
    """Запас на безобидные правки: 20% от baseline, но не меньше 2."""
    return max(2, baseline // 5)


def _style_counts(text: str) -> tuple[int, int, int]:
    values = [match.group(2) for match in _STYLE_ATTR.finditer(text)]
    hacks = sum(1 for value in values if _HACK_STYLE.search(value))
    colors = sum(1 for value in values if _COLOR_STYLE.search(value))
    importants = sum(1 for value in values if "!important" in value)
    return hacks, colors, importants


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_inline_style_hacks_within_baseline(name: str) -> None:
    """Число инлайновых style-хаков в файле не выросло против baseline."""
    hacks, colors, importants = _style_counts(_read(name))
    base_hacks, base_colors, base_importants = INLINE_STYLE_BASELINE.get(name, (0, 0, 0))
    problems = []
    if hacks > base_hacks + _slack(base_hacks):
        problems.append(
            f"{name}: инлайновых style-хаков {hacks}, baseline {base_hacks} "
            f"(допустимо до {base_hacks + _slack(base_hacks)}). "
            f"Используйте классы .tt-* и переменные --* из static/css/ttvpn-ui.css"
        )
    if colors > base_colors + 1:
        problems.append(
            f"{name}: инлайновых цветов-литералов {colors}, baseline {base_colors} "
            f"(допустимо до {base_colors + 1}) — цвет должен идти через var(--*)"
        )
    if importants > base_importants:
        problems.append(
            f"{name}: !important в инлайновых стилях {importants}, baseline {base_importants}"
        )
    _report(problems, f"{name}: вернулись инлайновые style-хаки")


def test_inline_style_hack_total_budget() -> None:
    """Суммарный объём инлайновых хаков по всем шаблонам не растёт."""
    total_hacks = 0
    total_colors = 0
    for name in TEMPLATE_NAMES:
        hacks, colors, _ = _style_counts(_read(name))
        total_hacks += hacks
        total_colors += colors
    problems = []
    if total_hacks > TOTAL_HACK_BUDGET:
        problems.append(f"всего style-хаков {total_hacks}, бюджет {TOTAL_HACK_BUDGET}")
    if total_colors > TOTAL_COLOR_BUDGET:
        problems.append(f"всего цветов-литералов {total_colors}, бюджет {TOTAL_COLOR_BUDGET}")
    _report(problems, "templates/*.html: общий бюджет инлайновых стилей превышен")


# ---------------------------------------------------------------------------
# 5. Маркеры старого слоя вёрстки
# ---------------------------------------------------------------------------

LEGACY_MARKERS: list[tuple[re.Pattern[str], str]] = [
    (re.compile(r"""class\s*=\s*["'][^"']*\bapp-header\b"""), 'class="app-header" — старый каркас, нужен .tt-topbar'),
    (re.compile(r"""class\s*=\s*["'][^"']*\bapp-container\b"""), 'class="app-container" — старый каркас, нужен .tt-shell/.tt-workspace'),
    (re.compile(r"""class\s*=\s*["'][^"']*\bnavbar\b"""), 'class="navbar" — старый каркас, нужен .tt-nav'),
    (re.compile(r"""class\s*=\s*["'][^"']*\bnav-tabs\b"""), 'class="nav-tabs" как каркас страницы — нужен .tt-tabs'),
    (re.compile(r"""class\s*=\s*["'][^"']*\bmain-content\b"""), 'class="main-content" — старый каркас, нужен .tt-content'),
    (re.compile(r"""class\s*=\s*["'][^"']*\bcontent-wrapper\b"""), 'class="content-wrapper" — старый каркас, нужен .tt-content'),
    (re.compile(r"""class\s*=\s*["'][^"']*\bcontainer-fluid\b"""), 'class="container-fluid" — bootstrap-сетка'),
    (re.compile(r"""class\s*=\s*["'][^"']*\bcol-(?:xs|sm|md|lg|xl)-\d"""), "bootstrap-сетка col-* — используйте grid из ttvpn-ui.css"),
    (re.compile(r"""class\s*=\s*["'][^"']*\bmodal\s+fade\b"""), 'class="modal fade" — bootstrap-модалка, нужен .modal .modal-backdrop'),
    (re.compile(r"""class\s*=\s*["'][^"']*\b(?:glyphicon|fa)\b"""), "иконочный шрифт glyphicon/fa — нужны inline SVG"),
    (re.compile(r"\bdata-toggle\s*="), "data-toggle= — bootstrap-атрибут, нужен data-* своего компонента"),
]


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_legacy_layer_markers(name: str) -> None:
    """В шаблонах нет разметки старого слоя и bootstrap-каркаса."""
    problems = []
    for lineno, line in enumerate(_read(name).splitlines(), 1):
        for pattern, hint in LEGACY_MARKERS:
            if pattern.search(line):
                problems.append(f"{name}.{lineno}: {hint}. Строка: {line.strip()[:120]}")
    _report(problems, f"{name}: найдены маркеры старого слоя")


# ---------------------------------------------------------------------------
# 6. Внешние ресурсы
# ---------------------------------------------------------------------------

_EXTERNAL_ASSET = re.compile(
    r"""<\s*(?:script|link|img|iframe|source|video|audio|embed|object|track)\b[^>]*?"""
    r"""\b(?:src|href|data|poster)\s*=\s*["'](?:https?:)?//""",
    re.I,
)
_EXTERNAL_IMPORT = re.compile(r"""@import\s+(?:url\()?\s*["']?(?:https?:)?//""", re.I)
_EXTERNAL_CSS_URL = re.compile(r"""url\(\s*["']?(?:https?:)?//""", re.I)


@pytest.mark.parametrize("name", TEMPLATE_NAMES)
def test_no_external_assets(name: str) -> None:
    """Шаблоны не тянут ресурсы по сети: всё локальное (офлайн-панель).

    Ссылки ``<a href="https://...">`` разрешены — это переходы, а не ресурсы.
    """
    problems = []
    for lineno, line in enumerate(_read(name).splitlines(), 1):
        if _EXTERNAL_ASSET.search(line):
            problems.append(f"{name}.{lineno}: внешний ресурс в теге. Строка: {line.strip()[:120]}")
        elif _EXTERNAL_IMPORT.search(line) or _EXTERNAL_CSS_URL.search(line):
            problems.append(f"{name}.{lineno}: внешний url()/@import в стилях. Строка: {line.strip()[:120]}")
    _report(problems, f"{name}: найдены внешние ресурсы")


# ---------------------------------------------------------------------------
# Служебное: тест не должен «зеленеть» на пустом каталоге шаблонов
# ---------------------------------------------------------------------------


def test_templates_catalog_is_not_empty() -> None:
    """Страховка: если каталог шаблонов не найден, остальные тесты бессмысленны."""
    assert TEMPLATE_NAMES, f"не найдено ни одного шаблона в {TEMPLATES_DIR}"
    assert "base.html" in TEMPLATE_NAMES
    assert "billing_base.html" in TEMPLATE_NAMES
