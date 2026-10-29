from datetime import datetime, timedelta, timezone

import pytest

from jobauto import db
from jobauto.exclude import refresh_exclusions
from jobauto.maintenance import archive_stale, cleanup_files, compact_vacancies

NOW = datetime(2026, 10, 29, 12, tzinfo=timezone(timedelta(hours=3)))
OLD = (NOW - timedelta(days=40)).isoformat()
RECENT = (NOW - timedelta(days=2)).isoformat()


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "jobs.db")


def add(conn, vid, published, status=None, notes=None, updated=None, excluded=None, kind="main"):
    conn.execute(
        "INSERT INTO vacancies(id, kind, name, published_at, description, snippet, key_skills, detail_status, "
        "excluded, added_at, updated_at) VALUES(?, ?, ?, ?, 'текст', 's', '[\"x\"]', 'ok', ?, ?, ?)",
        (vid, kind, f"Вакансия {vid}", published, excluded, published, published),
    )
    if status or notes:
        conn.execute(
            "INSERT INTO tracking(item_type, item_id, status, notes, seen, updated_at) VALUES('vacancy', ?, ?, ?, 1, ?)",
            (vid, status or "новая", notes, updated or published),
        )
    conn.commit()


def state(conn, vid):
    return dict(conn.execute("SELECT description, snippet, key_skills, detail_status FROM vacancies WHERE id=?", (vid,)).fetchone())


def test_compact_old_rejected_and_stale_but_never_protected(conn):
    add(conn, "stale", OLD)                                        # старая без решения
    add(conn, "rej_old", RECENT, "не подходит", updated=OLD)       # давно отклонена
    add(conn, "rej_new", RECENT, "не подходит", updated=RECENT)    # отклонена недавно
    add(conn, "applied", OLD, "отклик")                      # отклик — не трогаем
    add(conn, "noted", OLD, notes="позвонить")                     # заметка — не трогаем
    add(conn, "fresh", RECENT)
    assert compact_vacancies(conn, now=NOW) == 2
    assert state(conn, "stale") == {"description": None, "snippet": None, "key_skills": "[]", "detail_status": "compacted"}
    assert state(conn, "rej_old")["detail_status"] == "compacted"
    for vid in ("rej_new", "applied", "noted", "fresh"):
        assert state(conn, vid)["description"] == "текст"
    assert compact_vacancies(conn, now=NOW) == 0  # повторно ничего


def test_compacted_vacancy_keeps_exclusion(conn):
    add(conn, "x", OLD, excluded='["правило"]')
    compact_vacancies(conn, now=NOW)
    # правила по тексту без описания ничего бы не нашли, но отсев не должен слететь
    refresh_exclusions(conn, [{"name": "по названию", "where": "title", "patterns": ["несуществующее"]}])
    assert conn.execute("SELECT excluded FROM vacancies WHERE id='x'").fetchone()[0] == '["правило"]'


def test_archive_stale_moves_to_rejected_with_reason(conn):
    add(conn, "old", (NOW - timedelta(days=20)).isoformat())
    add(conn, "new", RECENT)
    add(conn, "applied", (NOW - timedelta(days=20)).isoformat(), "отклик")
    add(conn, "hidden", (NOW - timedelta(days=20)).isoformat(), excluded='["r"]')
    assert archive_stale(conn, "main", now=NOW, apply=False) == 1
    assert archive_stale(conn, "main", now=NOW) == 1
    row = conn.execute("SELECT status, reject_reason FROM tracking WHERE item_id='old'").fetchone()
    assert tuple(row) == ("не подходит", "устарела")
    assert conn.execute("SELECT status FROM tracking WHERE item_id='applied'").fetchone()[0] == "отклик"
    assert conn.execute("SELECT COUNT(*) FROM tracking WHERE item_id IN ('new', 'hidden')").fetchone()[0] == 0


def test_cleanup_files_removes_only_old(tmp_path):
    import os, time

    d = tmp_path / "logs"
    d.mkdir()
    old, new = d / "old.txt", d / "new.txt"
    old.write_text("x"), new.write_text("x")
    past = time.time() - 40 * 86400
    os.utime(old, (past, past))
    assert cleanup_files([d, tmp_path / "missing"]) == 1
    assert not old.exists() and new.exists()


def test_backup_keeps_three(tmp_path):
    path = tmp_path / "jobs.db"
    db.connect(path).close()
    bdir = tmp_path / "b"
    bdir.mkdir()
    for i in range(6):
        (bdir / f"jobs-2026010{i}-000000.db").write_bytes(b"")
    db.backup(path, bdir)
    assert len(list(bdir.glob("jobs-*.db"))) == 3


def test_old_applied_status_is_migrated(tmp_path):
    import sqlite3

    path = tmp_path / "jobs.db"
    db.connect(path).close()
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO tracking(item_type, item_id, status, updated_at) VALUES('vacancy', '1', 'откликнулась', 'x')")
    raw.commit()
    raw.close()
    c = db.connect(path)
    assert c.execute("SELECT status FROM tracking WHERE item_id = '1'").fetchone()[0] == "отклик"
