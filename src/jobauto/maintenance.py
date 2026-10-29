"""Очистка: база и папка data не должны расти бесконечно.

Вакансия, которая больше не нужна (отклонена или устарела без решения), «сжимается»:
удаляются описание, сниппет и навыки, а название, компания, ссылка, статус и причина остаются.
Строка нужна, чтобы такая вакансия не собралась заново. Вакансии с откликом
или заметкой не сжимаются никогда.
"""

from __future__ import annotations

import shutil
import sqlite3
import time
from datetime import datetime, timedelta
from pathlib import Path

COMPACT_AFTER_DAYS = 30
STALE_AFTER_DAYS = 14  # «Убрать всё старше …» в ленте
FILES_KEEP_DAYS = 30
COMPACTED = "compacted"

_HAS_TEXT = "(COALESCE(v.description, '') != '' OR COALESCE(v.snippet, '') != '' OR v.key_skills != '[]')"
_NOT_PROTECTED = (
    "COALESCE(t.status, 'новая') != 'отклик' AND COALESCE(t.notes, '') = '' AND COALESCE(t.applied_at, '') = ''"
)


def compact_vacancies(conn: sqlite3.Connection, days: int = COMPACT_AFTER_DAYS, now: datetime | None = None) -> int:
    """Сжимает отклонённые (давно) и устаревшие без решения вакансии. Возвращает, сколько сжато."""
    cutoff = ((now or datetime.now().astimezone()) - timedelta(days=days)).isoformat()
    ids = [
        r[0]
        for r in conn.execute(
            "SELECT v.id FROM vacancies v LEFT JOIN tracking t ON t.item_type = 'vacancy' AND t.item_id = v.id "
            f"WHERE v.detail_status != ? AND {_HAS_TEXT} AND {_NOT_PROTECTED} AND ("
            "  (t.status = 'не подходит' AND t.updated_at < ?)"
            "  OR (COALESCE(t.status, 'новая') = 'новая' AND COALESCE(v.published_at, v.added_at) < ?))",
            (COMPACTED, cutoff, cutoff),
        )
    ]
    for i in range(0, len(ids), 500):
        chunk = ids[i : i + 500]
        conn.execute(
            "UPDATE vacancies SET description = NULL, snippet = NULL, key_skills = '[]', detail_status = ? "
            f"WHERE id IN ({', '.join('?' * len(chunk))})",
            [COMPACTED, *chunk],
        )
    conn.commit()
    return len(ids)


def archive_stale(conn: sqlite3.Connection, kind: str, days: int = STALE_AFTER_DAYS,
                  now: datetime | None = None, apply: bool = True) -> int:
    """Вакансии без решения старше `days` дней уходят в «Не подходит» с причиной «устарела».
    С apply=False только считает."""
    cutoff = ((now or datetime.now().astimezone()) - timedelta(days=days)).isoformat()
    where = (
        "FROM vacancies v LEFT JOIN tracking t ON t.item_type = 'vacancy' AND t.item_id = v.id "
        "WHERE v.kind = ? AND v.excluded IS NULL AND v.duplicate_of IS NULL AND v.source_status = 'active' "
        f"AND COALESCE(t.status, 'новая') = 'новая' AND {_NOT_PROTECTED} "
        "AND COALESCE(v.published_at, v.added_at) < ?"
    )
    ids = [r[0] for r in conn.execute(f"SELECT v.id {where}", (kind, cutoff))]
    if not apply:
        return len(ids)
    stamp = (now or datetime.now().astimezone()).isoformat(timespec="seconds")
    for i in ids:
        conn.execute(
            "INSERT INTO tracking(item_type, item_id, status, reject_reason, seen, updated_at) "
            "VALUES('vacancy', ?, 'не подходит', 'устарела', 1, ?) "
            "ON CONFLICT(item_type, item_id) DO UPDATE SET status = 'не подходит', reject_reason = 'устарела', "
            "seen = 1, updated_at = excluded.updated_at",
            (i, stamp),
        )
    conn.commit()
    return len(ids)


def cleanup_files(dirs: list[Path], days: int = FILES_KEEP_DAYS) -> int:
    """Удаляет из папок логов и пробных выгрузок файлы и папки старше `days` дней."""
    border = time.time() - days * 86400
    removed = 0
    for d in dirs:
        if not d.is_dir():
            continue
        for p in d.iterdir():
            try:
                if p.stat().st_mtime >= border:
                    continue
                shutil.rmtree(p) if p.is_dir() else p.unlink()
                removed += 1
            except OSError:
                continue
    return removed
