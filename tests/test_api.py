import shutil
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from fakes import TZ, FakeHh, item
from jobauto import api, db, settings_store
from jobauto.config import EXAMPLE_DIR, load_config, load_yaml, save_yaml
from jobauto.runner import Runner

NOW = datetime(2026, 10, 19, 12, tzinfo=TZ)


@pytest.fixture
def env(tmp_path, monkeypatch):
    cfg_dir = tmp_path / "config"
    shutil.copytree(EXAMPLE_DIR, cfg_dir)
    db_path = tmp_path / "jobs.db"
    monkeypatch.setattr(api, "DB_PATH", db_path)
    monkeypatch.setattr(api, "DUMPS_DIR", tmp_path / "dumps")
    monkeypatch.setattr(api, "load_config", lambda: load_config(cfg_dir))
    monkeypatch.setattr(settings_store, "CONFIG_DIR", cfg_dir)
    save_yaml("skills.yaml", {"skills": [{"name": "Bubble", "level": "знаю", "variants": ["bubble.io"]}]}, cfg_dir)

    cfg = load_config(cfg_dir)
    cfg.settings["hh"]["contact_email"] = "t@t.dev"
    cfg.channels = []
    cfg.boards = []
    cfg.exclude = []  # отсев проверяется в test_exclude.py
    t = NOW - timedelta(hours=3)
    fake = FakeHh([
        item(1, "Bubble-разработчик", t, query='Bubble', key_skills=[{"name": "Bubble"}],
             description="<p>Делать MVP на Bubble.io и n8n. SEO не нужно</p>"),
        item(2, "Bubble разработчик", t + timedelta(hours=1)),
        item(3, "Ассистент", t, employer_id="e9", query='x', forms=("PART",)),
    ])
    c = db.connect(db_path)
    # подработка в тестовом конфиге ищется с текстом; подставим его в поддельный hh
    for q in cfg.queries:
        if q.is_side:
            fake.items[2]["_query"] = q.text
    Runner(c, cfg, progress=lambda m: None, hh_client=fake, sleep=lambda s: None, now=lambda: NOW,
           dumps_dir=tmp_path / "dumps").run()
    c.close()
    return TestClient(api.app), cfg_dir


def test_lists_and_filters(env):
    client, _ = env
    r = client.get("/api/vacancies", params={"kind": "main"}).json()
    assert r["total"] == 2
    assert {v["status"] for v in r["items"]} == {"новая"}
    assert client.get("/api/vacancies", params={"kind": "main", "hide_dups": "true"}).json()["total"] == 1
    assert client.get("/api/vacancies", params={"kind": "main", "mark": "не разработка"}).json()["total"] == 1
    assert client.get("/api/vacancies", params={"kind": "main", "nomark": "не разработка"}).json()["total"] == 1
    assert client.get("/api/vacancies", params={"kind": "main", "q": "MVP"}).json()["total"] == 1
    assert client.get("/api/vacancies", params={"kind": "side"}).json()["total"] == 1


def test_detail_highlights_skills_and_repeats(env):
    client, _ = env
    v = client.get("/api/vacancies/1").json()
    hl = [s for s in v["segments"] if s.get("level")]
    assert [s["t"] for s in hl] == ["Bubble.io"]
    assert v["my_skills"] == [{"skill": "Bubble", "level": "знаю"}]
    assert [r["id"] for r in v["repeats"]] == ["2"]
    v2 = client.get("/api/vacancies/2").json()
    assert v2["original"]["id"] == "1"


def test_tracking_roundtrip_and_meta(env):
    client, _ = env
    assert client.get("/api/meta").json()["unseen"]["main"] == 2
    r = client.patch("/api/tracking/vacancy/1", json={"status": "отклик", "applied_at": "2026-10-19",
                                                      "notes": "написано", "seen": True})
    assert r.status_code == 200
    v = client.get("/api/vacancies/1").json()
    assert (v["status"], v["applied_at"], v["notes"], v["seen"]) == ("отклик", "2026-10-19", "написано", 1)
    assert client.get("/api/meta").json()["unseen"]["main"] == 1
    assert client.patch("/api/tracking/vacancy/1", json={"status": "архив"}).status_code == 400
    assert client.patch("/api/tracking/vacancy/999", json={"seen": True}).status_code == 404
    assert client.get("/api/vacancies", params={"kind": "main", "status": "отклик"}).json()["total"] == 1


def test_config_save_validates_and_keeps_header(env):
    client, cfg_dir = env
    data = client.get("/api/config/channels").json()
    data["channels"].append({"name": "https://t.me/some_channel", "enabled": True})
    data["keywords"].append("re:(")
    r = client.put("/api/config/channels", json=data)
    assert r.status_code == 422 and "регулярке" in r.json()["detail"][0]
    data["keywords"].pop()
    assert client.put("/api/config/channels", json=data).status_code == 200
    saved = (cfg_dir / "channels.yaml").read_text(encoding="utf-8")
    assert saved.startswith("# Публичные телеграм-каналы")
    assert [c["name"] for c in load_yaml("channels.yaml", cfg_dir)["channels"]][-1] == "some_channel"


def test_config_roundtrip_all_sections(env):
    client, cfg_dir = env
    for name in settings_store.FILES:
        data = client.get(f"/api/config/{name}").json()
        r = client.put(f"/api/config/{name}", json=data)
        assert r.status_code == 200, (name, r.json())
    cfg = load_config(cfg_dir)
    assert len(cfg.queries) == len(load_yaml("queries.yaml", cfg_dir)["queries"]) + 1 and cfg.queries[-1].search_field == "name"
    assert cfg.marks[2]["source"] == "no_salary"


def test_export_and_runs(env):
    client, _ = env
    r = client.get("/api/export.xlsx")
    assert r.status_code == 200 and r.content[:2] == b"PK"
    runs = client.get("/api/runs").json()
    assert runs[0]["status"] == "ok"
    dumps = client.get("/api/dumps").json()
    assert dumps == ["2026-10-19.md"]
    assert "Bubble-разработчик" in client.get(f"/api/dumps/{dumps[0]}").text
    assert client.get("/api/dumps/..%2Fjobs.db").status_code == 404


def test_static_index(env):
    client, _ = env
    r = client.get("/")
    assert r.status_code == 200 and "app.js" in r.text


def test_views_do_not_overlap(env):
    client, _ = env
    get = lambda view: client.get("/api/vacancies", params={"kind": "main", "view": view}).json()
    assert get("new")["total"] == 2 and get("new")["view_counts"] == {"new": 2}
    client.patch("/api/tracking/vacancy/1", json={"seen": True})
    client.patch("/api/tracking/vacancy/2", json={"status": "не подходит", "seen": True})
    r = get("new")
    assert r["total"] == 0 and r["view_counts"] == {"seen": 1, "rejected": 1}
    assert [v["id"] for v in get("seen")["items"]] == ["1"]
    assert [v["id"] for v in get("rejected")["items"]] == ["2"] and get("applied")["total"] == 0
    assert client.get("/api/meta").json()["unseen"]["main"] == 0


def test_export_survives_control_chars_and_formulas(env):
    from io import BytesIO

    from openpyxl import load_workbook

    client, db_path = env
    c = db.connect(api.DB_PATH)
    c.execute("UPDATE vacancies SET name = ?, description = ? WHERE id = '1'", ("=SUM(1,1)", "плохой\x0bсимвол"))
    c.commit()
    r = client.get("/api/export.xlsx")
    assert r.status_code == 200
    ws = load_workbook(BytesIO(r.content))["Вакансии"]
    names = [row[3].value for row in ws.iter_rows(min_row=2)]
    assert "=SUM(1,1)" in names  # текст, а не формула


def test_foreign_site_cannot_change_things(env):
    client, _ = env
    assert client.post("/api/run", headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.get("/api/meta", headers={"Host": "evil.example"}).status_code == 403
    r = client.patch("/api/tracking/vacancy/1", json={"seen": True}, headers={"Origin": "http://127.0.0.1:8000"})
    assert r.status_code == 200


def test_tracking_rejects_bad_types(env):
    client, _ = env
    assert client.patch("/api/tracking/vacancy/1", json={"applied_at": 5}).status_code == 400
    assert client.patch("/api/tracking/vacancy/1", json={"notes": ["x"]}).status_code == 400


def test_reject_reason_and_new_filters(env):
    client, _ = env
    r = client.patch("/api/tracking/vacancy/1", json={"status": "не подходит", "reject_reason": "стек"})
    assert r.status_code == 200 and r.json()["reject_reason"] == "стек"
    assert client.patch("/api/tracking/vacancy/1", json={"reject_reason": "выдумка"}).status_code == 400
    # причина живёт только у «не подходит»
    assert client.patch("/api/tracking/vacancy/1", json={"status": "новая"}).json()["reject_reason"] is None
    got = client.get("/api/vacancies", params={"kind": "main", "view": "new", "no_experience": "true"}).json()
    assert "stale_count" in got
    c = db.connect(api.DB_PATH)
    c.execute("UPDATE vacancies SET experience = 'Нет опыта' WHERE id = '1'")
    c.execute("UPDATE vacancies SET experience = 'От 1 года до 3 лет' WHERE id = '2'")
    c.commit()
    ids = [v["id"] for v in client.get("/api/vacancies", params={"kind": "main", "no_experience": "true"}).json()["items"]]
    assert ids == ["1"]
    assert client.get("/api/vacancies", params={"kind": "main", "has_salary": "true"}).status_code == 200


def test_archive_stale_endpoint(env):
    client, _ = env
    assert client.post("/api/tracking/vacancy/archive-stale?kind=main").json() == {"archived": 0}
    assert client.post("/api/tracking/vacancy/archive-stale?kind=zzz").status_code == 400
