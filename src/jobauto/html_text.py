"""HTML описания вакансии → читаемый текст с абзацами и списками."""

from __future__ import annotations

import re

from bs4 import BeautifulSoup, NavigableString, Tag

BLOCK_TAGS = {"p", "div", "h1", "h2", "h3", "h4", "h5", "h6", "ul", "ol", "table", "tr", "blockquote", "section"}


def html_to_text(html: str | None) -> str:
    if not html:
        return ""
    soup = BeautifulSoup(html, "html.parser")
    parts: list[str] = []
    _walk(soup, parts, list_stack=[])
    text = "".join(parts).replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _walk(node: Tag, parts: list[str], list_stack: list[list[int]]) -> None:
    for child in node.children:
        if isinstance(child, NavigableString):
            if child.__class__.__name__ in ("Comment", "Doctype"):
                continue
            parts.append(str(child).replace("\n", " "))
            continue
        if not isinstance(child, Tag):
            continue
        name = child.name.lower()
        if name in ("script", "style"):
            continue
        if name == "br":
            parts.append("\n")
        elif name in ("ul", "ol"):
            parts.append("\n")
            # для ol храним счётчик, для ul — None-маркер [-1]
            list_stack.append([0] if name == "ol" else [-1])
            _walk(child, parts, list_stack)
            list_stack.pop()
            parts.append("\n\n" if not list_stack else "\n")
        elif name == "li":
            counter = list_stack[-1] if list_stack else [-1]
            indent = "  " * max(len(list_stack) - 1, 0)
            if counter[0] >= 0:
                counter[0] += 1
                bullet = f"{counter[0]}. "
            else:
                bullet = "• "
            parts.append(f"\n{indent}{bullet}")
            _walk(child, parts, list_stack)
        elif name in ("td", "th"):
            _walk(child, parts, list_stack)
            parts.append(" ")
        elif name in BLOCK_TAGS:
            parts.append("\n\n")
            _walk(child, parts, list_stack)
            parts.append("\n\n")
        else:
            _walk(child, parts, list_stack)


def strip_highlight(text: str | None) -> str:
    """Сниппет поиска hh содержит <highlighttext>; убираем любые теги."""
    if not text:
        return ""
    return re.sub(r"<[^>]+>", "", text)
