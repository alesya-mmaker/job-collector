"""Файл dumps/YYYY-MM-DD.md с новым за запуск.

В дамп не попадают: возможные повторы, закрытые и удалённые вакансии.
Подработка идёт только ссылками. Если нового нет, файл не создаётся.
Повторный запуск в тот же день дописывает в конец.
"""

from __future__ import annotations

import sqlite3
from datetime import date
from pathlib import Path

from .db import row_to_dict


def format_money(n: int) -> str:
    return f"{n:,}".replace(",", " ")


def format_salary(v: dict) -> str:
    lo, hi, cur = v.get("salary_from"), v.get("salary_to"), v.get("currency") or ""
    if lo is None and hi is None:
        return "не указана"
    if lo is not None and hi is not None:
        s = format_money(lo) if lo == hi else f"{format_money(lo)}–{format_money(hi)}"
    elif lo is not None:
        s = f"от {format_money(lo)}"
    else:
        s = f"до {format_money(hi)}"
    return f"{s} {cur}".strip()


def _dash(x: str | None) -> str:
    return x if x else "—"


def vacancy_block(v: dict) -> str:
    company = v.get("employer_name") or "компания не указана"
    lines = [
        f"## {v['name']} — {company}",
        f"Ссылка: {v.get('url') or '—'}",
        f"Зарплата: {format_salary(v)}",
        f"Регион: {_dash(v.get('area'))} | Опыт: {_dash(v.get('experience'))} | Занятость: {_dash(v.get('employment_form'))}",
        f"Навыки: {', '.join(v.get('key_skills') or []) or '—'}",
        f"Пометки: {', '.join(v.get('marks') or []) or '—'}",
        "",
        v.get("description") or "(полное описание не загружено)\n\n" + (v.get("snippet") or ""),
        "",
        "---",
        "",
    ]
    return "\n".join(lines)


def post_block(p: dict) -> str:
    when = (p.get("date") or "").replace("T", " ")[:16]
    return "\n".join([f"### @{p['channel']} — {when}", f"Ссылка: {p['url']}", "", p["text"], "", "---", ""])


def build_dump(conn: sqlite3.Connection, run_id: int) -> str | None:
    q = (
        "SELECT * FROM vacancies WHERE first_run_id = ? AND kind = ? "
        "AND duplicate_of IS NULL AND source_status = 'active' AND excluded IS NULL ORDER BY published_at"
    )
    main = [row_to_dict(r) for r in conn.execute(q, (run_id, "main"))]
    side = [row_to_dict(r) for r in conn.execute(q, (run_id, "side"))]
    posts = [
        row_to_dict(r)
        for r in conn.execute("SELECT * FROM tg_posts WHERE first_run_id = ? AND excluded IS NULL ORDER BY channel, post_id", (run_id,))
    ]
    if not (main or side or posts):
        return None

    out: list[str] = []
    if main:
        out.append("# Вакансии\n")
        out += [vacancy_block(v) for v in main]
    if side:
        out.append("# Подработка\n")
        out += [f"- [{v['name']} — {v.get('employer_name') or 'компания не указана'}]({v.get('url')})" for v in side]
        out.append("")
    if posts:
        out.append("## Телеграм\n")
        out += [post_block(p) for p in posts]
    return "\n".join(out).rstrip() + "\n"


def write_dump(conn: sqlite3.Connection, run_id: int, dumps_dir: Path, day: date | None = None) -> Path | None:
    content = build_dump(conn, run_id)
    if content is None:
        return None
    dumps_dir.mkdir(parents=True, exist_ok=True)
    path = dumps_dir / f"{(day or date.today()).isoformat()}.md"
    if path.exists() and path.stat().st_size:
        with path.open("a", encoding="utf-8") as f:
            f.write("\n\n")
            f.write(content)
    else:
        path.write_text(content, encoding="utf-8")
    return path
