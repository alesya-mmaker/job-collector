"""Короткая выжимка вакансии для карточки в списке: что предстоит делать.

Ищет в описании раздел «Задачи / Обязанности / Что делать…» и берёт первые
строки после заголовка, до следующего раздела («Требования», «Чего мы ждём»…).
Если раздела задач нет — первые строки описания, пропуская вступление
о компании и служебные строки площадок («Навыки: …», «Специализации: …»).
"""

from __future__ import annotations

import re

TASKS_HEAD = re.compile(
    r"(?im)^[^\n\w]*(задачи|обязанности|чем (?:предстоит|вам предстоит|будешь|будете|ты будешь|вы будете) занима\w+|"
    r"что (?:нужно |предстоит |надо |будет нужно )?делать|что (?:вы будете|ты будешь) делать|ваши задачи|твои задачи|"
    r"responsibilities|what you(?:'|’)?ll do|your role)\b[^\n]{0,40}\n"
)
# начало следующего раздела — на нём выжимка заканчивается
NEXT_HEAD = re.compile(
    r"(?i)^[^\w]*(обязательные требования|требования|мы ожидаем|ожидаем|наши ожидания|что мы ж[её]м|чего мы ж[её]м|"
    r"чего ж[её]м|кого мы ищем|кого ищем|мы ж[её]м|ты нам подходишь|вы нам подходите|условия|мы предлагаем|"
    r"предлагаем|что мы предлагаем|будет плюсом|плюсом будет|о компании|о нас|стек|технологии|"
    r"requirements|what we offer|benefits|nice to have)\b"
)
SERVICE_LINE = re.compile(r"(?i)^(навыки|специализации|специализация|уровень|квалификация|местоположение)\s*:")
COMPANY_INTRO = re.compile(r"(?i)^(о компании|о нас|мы\s|компания\s|наша компания|[«\"]?[\w\- ]{2,40}[»\"]?\s—\s)")


def _looks_like_heading(line: str) -> bool:
    """Короткая строка без точки в конце: «Язык и платформа», «Чего мы ждём от кандидата?»."""
    return len(line) <= 60 and (line.endswith((":", "?")) or not re.search(r"[.!;,…]$", line)) and len(line.split()) <= 7


def excerpt(text: str | None, max_chars: int = 320, max_lines: int = 4) -> str:
    if not text:
        return ""
    m = TASKS_HEAD.search(text)
    part = text[m.end():] if m else text
    lines = [ln.strip().lstrip("•-–—*·✔️✅▪️❗🔹🔸 \t").strip() for ln in part.splitlines()]
    lines = [ln for ln in lines if ln and not SERVICE_LINE.match(ln)]
    if not m:
        # без раздела «Задачи» — пропускаем вступление о компании
        while len(lines) > 1 and COMPANY_INTRO.match(lines[0]):
            lines.pop(0)
    out: list[str] = []
    used = 0
    for ln in lines:
        if NEXT_HEAD.match(ln) or (out and _looks_like_heading(ln) and ln.endswith((":", "?"))):
            break
        if used + len(ln) > max_chars:
            if not out:
                out.append(ln[:max_chars].rsplit(" ", 1)[0] + "…")
            else:
                out[-1] = out[-1].rstrip(".;,") + "…"
            break
        out.append(ln)
        used += len(ln)
        if len(out) >= max_lines:
            break
    return "\n".join(out)
