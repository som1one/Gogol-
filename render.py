"""Серверный разворот шаблона сайта.

Разметка написана под браузерный рантайм: списки заданы тегами <sc-for>,
условия — <sc-if>, значения — плейсхолдерами {{ ... }}. Раньше всё это
превращалось в готовый HTML только после загрузки React, то есть в ответе
сервера не было ни услуг, ни прейскуранта, ни адреса записи в ссылках.

Здесь те же данные подставляются до отдачи страницы. Рантайм после гидрации
рендерит уже развёрнутую разметку — плейсхолдеров в ней не остаётся, поэтому
результат совпадает с тем, что видит поисковик и человек с выключенным JS.

Правила сборки значений повторяют renderVals() из разметки: если меняете одно,
меняйте и другое.
"""

from __future__ import annotations

import math
import re
from html import escape
from typing import Any

# Ссылка записи по умолчанию — на случай пустой настройки в базе.
DEFAULT_BOOKING = "https://n24164.yclients.com/company/41786/personal/menu?o="

_TAG_RE = re.compile(r"<sc-(for|if)\b([^>]*)>")
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_PLACEHOLDER_RE = re.compile(r"\{\{\s*([A-Za-z_]\w*(?:\.\w+)*)\s*\}\}")


def build_vals(content: dict[str, Any]) -> dict[str, Any]:
    """Собирает значения для шаблона — зеркало renderVals() в разметке."""
    groups = [
        g for g in (content.get("priceGroups") or []) if g.get("visible", True) is not False
    ]
    half = math.ceil(len(groups) / 2)
    sections = content.get("sections") or {}
    booking = content.get("booking") or {}
    show = sections.get("showCosmetics")
    return {
        "bookingHref": booking.get("url") or DEFAULT_BOOKING,
        "showCosmetics": True if show is None else bool(show),
        "photos": content.get("photos") or {},
        "services": content.get("services") or [],
        "combo": content.get("combo") or {},
        "priceColA": groups[:half],
        "priceColB": groups[half:],
        "brands": content.get("brands") or [],
    }


def _attrs(raw: str) -> dict[str, str]:
    return {m.group(1): m.group(2) for m in _ATTR_RE.finditer(raw)}


def _resolve(expr: str, scope: dict[str, Any]) -> Any:
    """Достаёт значение по пути вида «svc.title» из текущей области видимости."""
    m = _PLACEHOLDER_RE.fullmatch(expr.strip())
    path = (m.group(1) if m else expr).strip()
    value: Any = scope
    for part in path.split("."):
        if isinstance(value, dict):
            value = value.get(part)
        else:
            return None
        if value is None:
            return None
    return value


def _close_block(html: str, start: int, kind: str) -> tuple[int, int]:
    """Ищет закрывающий тег с учётом вложенности. Возвращает (конец тела, конец блока)."""
    token = re.compile(rf"<sc-{kind}\b[^>]*>|</sc-{kind}>")
    depth = 1
    pos = start
    while True:
        m = token.search(html, pos)
        if not m:  # незакрытый блок — отдаём остаток, чтобы не потерять разметку
            return len(html), len(html)
        if m.group(0).startswith("</"):
            depth -= 1
            if depth == 0:
                return m.start(), m.end()
        else:
            depth += 1
        pos = m.end()


def _substitute(chunk: str, scope: dict[str, Any]) -> str:
    def one(m: re.Match[str]) -> str:
        value = _resolve(m.group(1), scope)
        if value is None or value is False:
            return ""
        if value is True:
            return "true"
        return escape(str(value), quote=True)

    return _PLACEHOLDER_RE.sub(one, chunk)


def expand(html: str, scope: dict[str, Any]) -> str:
    """Разворачивает циклы, условия и плейсхолдеры в готовый HTML."""
    out: list[str] = []
    pos = 0
    while True:
        m = _TAG_RE.search(html, pos)
        if not m:
            out.append(_substitute(html[pos:], scope))
            return "".join(out)

        out.append(_substitute(html[pos : m.start()], scope))
        kind = m.group(1)
        attrs = _attrs(m.group(2))
        body_end, block_end = _close_block(html, m.end(), kind)
        body = html[m.end() : body_end]

        if kind == "for":
            items = _resolve(attrs.get("list", ""), scope)
            name = attrs.get("as") or "item"
            for item in items or []:
                out.append(expand(body, {**scope, name: item}))
        else:
            if _resolve(attrs.get("value", ""), scope):
                out.append(expand(body, scope))

        pos = block_end
