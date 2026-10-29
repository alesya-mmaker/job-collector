import shutil
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from fakes import TZ, FakeHh, item
from jobauto import db
from jobauto.config import load_config, save_yaml, load_yaml, EXAMPLE_DIR
from jobauto.duplicates import title_similarity
from jobauto.runner import Runner

NOW = datetime(2026, 10, 19, 12, tzinfo=TZ)


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(EXAMPLE_DIR, d)
    s = load_yaml("settings.yaml", d)
    s["hh"]["contact_email"] = "test@test.dev"
    save_yaml("settings.yaml", s, d)
    q = load_yaml("queries.yaml", d)
    # для тестов два запроса из разных направлений и подработка
    q["queries"] = [
        {"id": "bubble", "text": "Bubble", "direction": "продукт"},
        {"id": "n8n", "text": "n8n", "direction": "автоматизация"},
    ]
    q["side_jobs"]["text"] = ""
    save_yaml("queries.yaml", q, d)
    ch = load_yaml("channels.yaml", d)
    ch["channels"] = []  # без обращений к t.me в тестах
    save_yaml("channels.yaml", ch, d)
    c = load_config(d)
    c.boards = []  # площадки проверяются отдельными тестами, без сети
    c.exclude = []  # отсев проверяется в test_exclude.py
    return c


@pytest.fixture
def conn(tmp_path):
    return db.connect(tmp_path / "jobs.db")


def run(conn, cfg, fake, tmp_path, now=NOW):
    return Runner(conn, cfg, progress=lambda m: None, hh_client=fake, sleep=lambda s: None,
                  now=lambda: now, dumps_dir=tmp_path / "dumps").run()


def vac(conn, vid):
    return db.row_to_dict(conn.execute("SELECT * FROM vacancies WHERE id=?", (str(vid),)).fetchone())


def test_full_run(conn, cfg, tmp_path):
    t = NOW - timedelta(hours=5)
    fake = FakeHh(
        [
            item(1, "Bubble-разработчик", t, salary={"from": 1000, "to": 2000, "currency": "USD"},
                 key_skills=[{"name": "Bubble"}], description="<p>График 6/1</p>"),
            item(2, "Bubble разработчик (удалённо)", t + timedelta(hours=1)),       # повтор 1
            item(3, "No-code автоматизатор", t, employer_id="e2", query="n8n"),
            item(4, "Bubble dev", t, employer_id="e3", query="n8n"),               # найдётся и по Bubble
            item(5, "Ассистент", t, employer_id="e4", query="", forms=("PART",)),    # подработка
            item(6, "Закрытая", t, employer_id="e5"),
            item(7, "Bubble lead", t, employer_id="e6", previous_id="1"),           # перевыложена из 1
        ],
        employers={"e2": "private_individual"},
        archived={"6"},
    )
    fake.items.append(dict(item(4, "Bubble dev", t, employer_id="e3", query="Bubble")))
    r = run(conn, cfg, fake, tmp_path)

    assert r.status == "ok", r.errors
    assert r.new_main == 6 and r.new_side == 1

    v1 = vac(conn, 1)
    assert v1["direction"] == "продукт" and v1["key_skills"] == ["Bubble"]
    assert v1["description"] == "График 6/1" and "6/1" in v1["marks"]
    assert v1["duplicate_of"] is None
    assert vac(conn, 2)["duplicate_of"] == "1"
    assert vac(conn, 7)["duplicate_of"] == "1"
    assert "работодатель физлицо" in vac(conn, 3)["marks"] and "зп не указана" in vac(conn, 3)["marks"]
    v4 = vac(conn, 4)
    assert set(v4["query_ids"]) == {"bubble", "n8n"} and v4["direction"] == "продукт"
    assert vac(conn, 5)["kind"] == "side"
    assert vac(conn, 6)["source_status"] == "archived"

    dump = r.dump_path.read_text(encoding="utf-8")
    assert "## Bubble-разработчик — Ромашка" in dump
    assert "Зарплата: 1 000–2 000 USD" in dump
    assert "Пометки: 6/1" in dump
    assert "(удалённо)" not in dump and "Bubble lead" not in dump    # повторы не в дампе
    assert "Закрытая" not in dump                                     # архивные не в дампе
    assert "# Подработка\n\n- [Ассистент — Ромашка](https://hh.ru/vacancy/5)" in dump
    assert "Описание" not in dump.split("# Подработка")[1]            # подработка только ссылками

    assert db.get_state(conn, "hh:bubble") == NOW.isoformat()
    assert db.get_state(conn, "hh:side") == NOW.isoformat()


def test_second_run_uses_watermark_and_no_dump_when_nothing_new(conn, cfg, tmp_path):
    fake = FakeHh([item(1, "Bubble", NOW - timedelta(hours=2))])
    run(conn, cfg, fake, tmp_path)
    later = NOW + timedelta(days=2)
    fake2 = FakeHh(list(fake.items))
    r = run(conn, cfg, fake2, tmp_path, now=later)
    assert r.new_main == 0 and r.dump_path is None
    search_calls = [c for p, c in fake2.calls if p == "/vacancies"]
    # окно начинается с прошлого успешного сбора минус запас 60 минут
    assert search_calls[0]["date_from"][0] == (NOW - timedelta(minutes=60)).strftime("%Y-%m-%dT%H:%M:%S%z")


def test_watermark_does_not_move_when_run_breaks(conn, cfg, tmp_path):
    t = NOW - timedelta(hours=2)
    items = [item(1, "Bubble", t), item(2, "n8n", t, employer_id="e2", query="n8n")]
    # 1-й запрос — поиск Bubble проходит, на поиске n8n hh просит капчу
    r = run(conn, cfg, FakeHh(items, block_after=1), tmp_path)
    assert r.status == "partial" and any("капчу" in e for e in r.errors)
    assert db.get_state(conn, "hh:bubble") == NOW.isoformat()
    assert db.get_state(conn, "hh:n8n") is None
    assert vac(conn, 1)["detail_status"] == "pending"

    # следующий сбор: n8n берётся с начала, описание 1 догружается
    later = NOW + timedelta(days=1)
    fake = FakeHh(items)
    r2 = run(conn, cfg, fake, tmp_path, now=later)
    assert r2.status == "ok"
    n8n_call = next(c for p, c in fake.calls if p == "/vacancies" and c.get("text") == ["n8n"])
    assert n8n_call["date_from"][0] == (later - timedelta(days=7)).strftime("%Y-%m-%dT%H:%M:%S%z")
    assert vac(conn, 1)["detail_status"] == "ok" and vac(conn, 2)["detail_status"] == "ok"


def test_recheck_marks_closed_and_keeps_tracking(conn, cfg, tmp_path):
    fake = FakeHh([item(1, "Bubble", NOW - timedelta(hours=2)), item(2, "Bubble 2", NOW, employer_id="e2")])
    run(conn, cfg, fake, tmp_path)
    conn.execute("INSERT INTO tracking(item_type, item_id, status, notes, updated_at) VALUES('vacancy','1','отклик','звонок','x')")
    conn.commit()

    later = NOW + timedelta(days=4)
    fake2 = FakeHh(list(fake.items), deleted={"1"}, archived={"2"})
    run(conn, cfg, fake2, tmp_path, now=later)
    assert vac(conn, 1)["source_status"] == "deleted"
    assert vac(conn, 2)["source_status"] == "archived"
    t = conn.execute("SELECT status, notes FROM tracking WHERE item_id='1'").fetchone()
    assert (t["status"], t["notes"]) == ("отклик", "звонок")


def test_marks_follow_config_changes(conn, cfg, tmp_path):
    fake = FakeHh([item(1, "Bubble", NOW - timedelta(hours=2), description="<p>Нужен SEO</p>")])
    run(conn, cfg, fake, tmp_path)
    assert "не разработка" in vac(conn, 1)["marks"]
    cfg.marks = [m for m in cfg.marks if m["name"] != "не разработка"]
    run(conn, cfg, FakeHh(list(fake.items)), tmp_path, now=NOW + timedelta(hours=1))
    assert "не разработка" not in vac(conn, 1)["marks"]


def test_no_email_reports_error(conn, cfg, tmp_path):
    cfg.settings["hh"]["contact_email"] = ""
    cfg.settings["hh"]["mode"] = "api"
    r = Runner(conn, cfg, progress=lambda m: None, sleep=lambda s: None, now=lambda: NOW,
               dumps_dir=tmp_path / "d").run()
    assert r.status == "partial" and "contact_email" in r.errors[0]


def test_title_similarity():
    assert title_similarity("Bubble-разработчик", "Bubble разработчик (удалённо)") == 1.0
    assert title_similarity("Разработчик Bubble", "Bubble разработчик") == 1.0
    assert title_similarity("No-code разработчик", "No-code разработчик (Junior)") >= 0.85
    assert title_similarity("Bubble-разработчик", "Менеджер по продажам") < 0.5
    assert title_similarity("Python разработчик", "Java разработчик") < 0.85
    assert title_similarity("No-code разработчик junior", "No-code разработчик senior") < 0.85
