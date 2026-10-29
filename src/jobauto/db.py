"""SQLite: схема, подключение, резервные копии.

Собранные данные (vacancies, employers, tg_posts) и поля трекера (tracking)
лежат в разных таблицах. Сборщик в tracking не пишет никогда.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS vacancies (
    id               TEXT PRIMARY KEY,
    kind             TEXT NOT NULL,              -- main | side
    name             TEXT NOT NULL,
    employer_id      TEXT,
    employer_name    TEXT,
    employer_type    TEXT,                       -- employer_type из справочника hh
    salary_from      INTEGER,
    salary_to        INTEGER,
    currency         TEXT,
    salary_gross     INTEGER,
    area             TEXT,
    experience       TEXT,
    employment_form  TEXT,
    work_format      TEXT,
    url              TEXT,
    published_at     TEXT,
    direction        TEXT,
    query_ids        TEXT NOT NULL DEFAULT '[]', -- json: id запросов, которыми найдена
    queries          TEXT NOT NULL DEFAULT '[]', -- json: тексты этих запросов
    key_skills       TEXT NOT NULL DEFAULT '[]', -- json
    snippet          TEXT,
    description      TEXT,                       -- полный текст без HTML
    detail_status    TEXT NOT NULL DEFAULT 'pending', -- pending | ok | failed
    detail_error     TEXT,
    source_status    TEXT NOT NULL DEFAULT 'active',  -- active | archived | deleted
    source_checked_at TEXT,
    archived_at      TEXT,
    previous_id      TEXT,                       -- от hh: из какой архивной вакансии создана
    duplicate_of     TEXT,                       -- возможный повтор этой вакансии
    source           TEXT NOT NULL DEFAULT 'hh', -- hh | habr | geekjob | getmatch
    schedule         TEXT,                       -- график: 5/2, 6/1 …
    employer_key     TEXT,                       -- нормализованное название компании
    marks            TEXT NOT NULL DEFAULT '[]', -- json
    first_run_id     INTEGER,
    added_at         TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_vac_employer ON vacancies(employer_id);
CREATE INDEX IF NOT EXISTS ix_vac_run ON vacancies(first_run_id);
CREATE INDEX IF NOT EXISTS ix_vac_status ON vacancies(source_status, source_checked_at);

CREATE TABLE IF NOT EXISTS employers (
    id         TEXT PRIMARY KEY,
    name       TEXT,
    type       TEXT,
    fetched_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS tg_posts (
    hash         TEXT PRIMARY KEY,               -- sha1(канал + нормализованный текст)
    channel      TEXT NOT NULL,
    post_id      INTEGER NOT NULL,
    url          TEXT NOT NULL,
    date         TEXT,
    text         TEXT NOT NULL,
    keywords     TEXT NOT NULL DEFAULT '[]',
    first_run_id INTEGER,
    added_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS ix_tg_run ON tg_posts(first_run_id);

-- Вакансии площадок, проверенные и отклонённые: наших слов нет ни в названии, ни в описании.
-- Чтобы не загружать их описание при каждом сборе.
CREATE TABLE IF NOT EXISTS board_seen (
    id         TEXT PRIMARY KEY,
    checked_at TEXT NOT NULL
);

-- Мои поля. item_type: vacancy | post; item_id: id вакансии или hash поста.
CREATE TABLE IF NOT EXISTS tracking (
    item_type  TEXT NOT NULL,
    item_id    TEXT NOT NULL,
    status     TEXT NOT NULL DEFAULT 'новая',
    applied_at TEXT,
    notes      TEXT,
    reject_reason TEXT,                      -- почему «не подходит» (необязательно)
    seen       INTEGER NOT NULL DEFAULT 0,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (item_type, item_id)
);

CREATE TABLE IF NOT EXISTS runs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at       TEXT NOT NULL,
    finished_at      TEXT,
    status           TEXT NOT NULL,              -- running | ok | partial | failed
    requests         INTEGER NOT NULL DEFAULT 0,
    new_main         INTEGER NOT NULL DEFAULT 0,
    new_side         INTEGER NOT NULL DEFAULT 0,
    new_posts        INTEGER NOT NULL DEFAULT 0,
    skipped_channels TEXT NOT NULL DEFAULT '[]',
    errors           TEXT NOT NULL DEFAULT '[]',
    dump_path        TEXT
);

-- Отметки прогресса: hh:<id запроса> → время, с которого брать в следующий раз;
-- tg:<канал> → id последнего прочитанного поста.
CREATE TABLE IF NOT EXISTS state (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""

JSON_COLUMNS = {"query_ids", "queries", "key_skills", "marks", "keywords", "skipped_channels", "errors", "excluded"}


def now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.executescript(SCHEMA)
    _migrate(conn)
    return conn


# Колонки, добавленные после первой версии базы: (имя, определение)
ADDED_COLUMNS = [
    ("source", "TEXT NOT NULL DEFAULT 'hh'"),
    ("schedule", "TEXT"),
    ("employer_key", "TEXT"),
    ("excluded", "TEXT"),  # json: сработавшие правила отсева, NULL — не отсеяна
]


def _migrate(conn: sqlite3.Connection) -> None:
    have = {r["name"] for r in conn.execute("PRAGMA table_info(vacancies)")}
    for name, ddl in ADDED_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE vacancies ADD COLUMN {name} {ddl}")
    if "excluded" not in {r["name"] for r in conn.execute("PRAGMA table_info(tg_posts)")}:
        conn.execute("ALTER TABLE tg_posts ADD COLUMN excluded TEXT")
    if "reject_reason" not in {r["name"] for r in conn.execute("PRAGMA table_info(tracking)")}:
        conn.execute("ALTER TABLE tracking ADD COLUMN reject_reason TEXT")
    # статус «откликнулась» переименован в нейтральный «отклик»
    conn.execute("UPDATE tracking SET status = 'отклик' WHERE status = 'откликнулась'")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_vac_employer_key ON vacancies(employer_key)")
    conn.execute("CREATE INDEX IF NOT EXISTS ix_vac_source ON vacancies(source)")
    conn.commit()


def row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
    if row is None:
        return None
    d = dict(row)
    for k in JSON_COLUMNS & d.keys():
        d[k] = json.loads(d[k]) if d[k] else []
    return d


def get_state(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM state WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_state(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO state(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    conn.commit()


def backup(db_path: Path, backup_dir: Path, keep: int = 3) -> Path | None:
    """Копия базы перед сбором. Хранит последние `keep` копий."""
    if not db_path.exists():
        return None
    backup_dir.mkdir(parents=True, exist_ok=True)
    target = backup_dir / f"jobs-{datetime.now():%Y%m%d-%H%M%S}.db"
    src = sqlite3.connect(db_path)
    dst = sqlite3.connect(target)
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()
    for old in sorted(backup_dir.glob("jobs-*.db"))[:-keep]:
        old.unlink(missing_ok=True)
    return target
