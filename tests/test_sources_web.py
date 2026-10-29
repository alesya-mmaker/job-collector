"""Сайт hh.ru и площадки — на настоящих страницах, урезанных до нескольких вакансий."""

import json
import shutil
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest

from jobauto import db
from jobauto.config import EXAMPLE_DIR, load_config, load_yaml, save_yaml
from jobauto.duplicates import employer_key
from jobauto.marks import Marker
from jobauto.runner import Runner
from jobauto.sources import boards, hh, hh_web

FIX = Path(__file__).parent / "fixtures"
OLD = datetime(2000, 1, 1, tzinfo=timezone.utc)


class Resp:
    def __init__(self, status, text="", data=None):
        self.status_code, self.text, self._data = status, text, data

    def json(self):
        return self._data


class Session:
    """Отдаёт ответы по очереди и запоминает запросы."""

    def __init__(self, routes):
        self.routes = routes  # список (подстрока url, Resp) — берётся первый подходящий
        self.headers, self.calls = {}, []

    def get(self, url, params=None, **kw):
        self.calls.append((url, list(params.items()) if isinstance(params, dict) else params))
        for i, (part, resp) in enumerate(self.routes):
            if part in url:
                if isinstance(resp, list):
                    return resp.pop(0)
                return resp
        raise AssertionError(f"неожиданный запрос {url}")


def web(routes):
    return hh_web.HhWebSource(pause=0, session=Session(routes), sleep=lambda s: None)


def read(name):
    return (FIX / name).read_text(encoding="utf-8")


# ------------------------------------------------------------------ hh: сайт


def test_search_takes_only_exact_results_not_similar():
    src = web([("/search/vacancy", Resp(200, read("hhweb_search.html")))])
    got = list(src.search("no-code", [16, 113], [], None, since=OLD))
    # на странице 3 вакансии, но totalResults = 2: третья — «похожая», её не берём
    assert [v["name"] for v in got] == [
        "Senior Product Manager / Сеньор Продакт Менеджер",
        "Product manager/ Менеджер продукта (MVP)",
    ]
    v = got[0]
    assert v["id"] == "137877760" and v["url"] == "https://hh.ru/vacancy/137877760"
    assert v["work_format"] == "Удалённо" and v["employer_type"] == "company"
    assert v["published_at"].startswith("2026-09-28")
    params = dict(src.session.calls[0][1])
    assert params["work_format"] == "REMOTE" and params["order_by"] == "publication_time"
    assert ("area", 16) in src.session.calls[0][1] and ("area", 113) in src.session.calls[0][1]


def test_search_stops_at_since():
    src = web([("/search/vacancy", Resp(200, read("hhweb_search.html")))])
    since = datetime.fromisoformat("2026-09-28T17:56:00+03:00")
    assert [v["id"] for v in src.search("x", [16], [], None, since=since)] == ["137877760"]


def test_search_without_data_block_is_blocked():
    with pytest.raises(hh.HhBlocked, match="без данных"):
        list(web([("/search/vacancy", Resp(200, "<html>captcha</html>"))]).search("x", [16], [], None, since=OLD))
    with pytest.raises(hh.HhBlocked, match="451"):
        list(web([("/search/vacancy", Resp(451, ""))]).search("x", [16], [], None, since=OLD))


def test_vacancy_page():
    d = web([("/vacancy/", Resp(200, read("hhweb_vacancy.html")))]).detail("138710438")
    assert d["source_status"] == "active"
    assert "amoCRM" in d["key_skills"]
    assert d["description"].startswith("Цифра Девелопмент")
    assert d["schedule"] == "5/2" and d["employer_type"] == "company"


def test_removed_vacancy_and_block():
    assert web([("/vacancy/", Resp(403, read("hhweb_removed.html")))]).detail("8331228") is None
    with pytest.raises(hh.HhBlocked):
        web([("/vacancy/", Resp(403, "<html>ddos</html>"))]).detail("1")


def test_six_day_mark_from_schedule():
    cfg = load_config()
    m = Marker(cfg.marks)
    assert "6/1" in m.marks_for({"name": "x", "description": "", "schedule": "6/1", "salary_from": 1})
    assert "6/1" not in m.marks_for({"name": "x", "description": "", "schedule": "5/2", "salary_from": 1})


# ----------------------------------------------------------------- площадки


def test_habr_search_parses_ssr_state():
    b = boards.HabrBoard(pause=0, session=Session([("career.habr.com", Resp(200, read("habr_search.html")))]),
                         sleep=lambda s: None)
    got = b.search("python", OLD)
    assert len(got) == 3
    v = got[0]
    assert v["id"] == "habr:1000168937" and v["url"] == "https://career.habr.com/vacancies/1000168937"
    assert v["employer_name"] == "Джетлин" and "FastAPI" in v["key_skills"]
    assert v["work_format"] == "Удалённо"


def test_geekjob_feed_keeps_remote_only():
    data = json.loads(read("geekjob_feed.json"))
    b = boards.GeekjobBoard(pause=0, session=Session([("geekjob.ru", Resp(200, data=data))]), sleep=lambda s: None)
    got = b.feed(OLD)
    assert all(v["id"].startswith("geekjob:") for v in got)
    assert "Разработчик Vertica / Инженер данных (No" not in [v["name"][:40] for v in got]  # не удалёнка
    assert len(got) == sum(1 for v in data["data"] if v["jobFormat"]["remote"])


def test_ru_date_and_salary():
    assert boards.ru_date("21 октября", today=date(2026, 10, 21)).startswith("2026-10-21")
    assert boards.ru_date("30 декабря", today=date(2026, 1, 5)).startswith("2025-12-30")
    assert boards.parse_salary_text("от 250 000 до 270 000 ₽") == (250000, 270000, "RUR")
    assert boards.parse_salary_text("до 3 000 $") == (None, 3000, "USD")
    assert boards.parse_salary_text("") == (None, None, None)


def test_employer_key():
    assert employer_key("ООО «Ромашка»") == employer_key("Ромашка") == "ромашка"
    assert employer_key('"Цифра Девелопмент" LLC') == "цифрадевелопмент"
    assert employer_key("Аорта") == "аорта"


# ------------------------------------------------------- сбор целиком


class FakeBoard(boards.Board):
    name, label, server_search = "habr", "Habr Career", True

    def __init__(self, items):
        super().__init__(pause=0, session=Session([]), sleep=lambda s: None)
        self.items = items

    def search(self, term, since):
        self.requests += 1
        return [dict(v) for v in self.items if term.lower() in v["name"].lower()]

    def detail(self, vac_id):
        self.requests += 1
        return {"description": "Описание с Habr: работа на Bubble", "source_status": "active"}


@pytest.fixture
def cfg(tmp_path):
    d = tmp_path / "config"
    shutil.copytree(EXAMPLE_DIR, d)
    q = load_yaml("queries.yaml", d)
    q["queries"] = [{"id": "nocode", "text": "no-code", "direction": "продукт"}]
    q["side_jobs"]["enabled"] = False
    save_yaml("queries.yaml", q, d)
    b = load_yaml("boards.yaml", d)
    b["terms"] = [{"text": "Bubble", "direction": "продукт"}]
    save_yaml("boards.yaml", b, d)
    c = load_config(d)
    c.channels, c.boards = [], ["habr"]
    c.exclude = []
    return c


def test_run_with_hh_site_and_board(cfg, tmp_path):
    now = datetime(2026, 9, 29, 12, tzinfo=timezone(timedelta(hours=3)))
    site = web([
        ("/search/vacancy", Resp(200, read("hhweb_search.html"))),
        ("/vacancy/", Resp(200, read("hhweb_vacancy.html"))),
    ])
    habr = FakeBoard([
        {"id": "habr:1", "name": "Bubble-разработчик", "employer_name": "ООО Ромашка",
         "published_at": "2026-09-28T10:00:00+03:00", "url": "https://career.habr.com/vacancies/1",
         "key_skills": ["Bubble"], "source_status": "active"},
        {"id": "habr:2", "name": "Product manager / Менеджер продукта (MVP)", "employer_name": "",
         "published_at": "2026-09-29T10:00:00+03:00", "url": "https://career.habr.com/vacancies/2"},
    ])
    conn = db.connect(tmp_path / "jobs.db")
    r = Runner(conn, cfg, progress=lambda m: None, hh_web_source=site, board_sources={"habr": habr},
               sleep=lambda s: None, now=lambda: now, dumps_dir=tmp_path / "dumps").run()
    assert r.status == "ok", r.errors
    rows = {x["id"]: x for x in conn.execute("SELECT * FROM vacancies")}
    assert set(rows) == {"137877760", "137877654", "habr:1"}
    assert rows["habr:1"]["source"] == "habr" and rows["habr:1"]["direction"] == "продукт"
    assert rows["habr:1"]["description"].startswith("Описание с Habr")
    assert rows["habr:1"]["employer_key"] == "ромашка"
    assert rows["137877760"]["source"] == "hh" and rows["137877760"]["detail_status"] == "ok"
    assert r.requests >= 4
    assert db.get_state(conn, "board:habr") == now.isoformat()
    assert db.get_state(conn, "hh:nocode") == now.isoformat()


def test_board_failure_keeps_its_mark(cfg, tmp_path):
    class Broken(FakeBoard):
        def search(self, term, since):
            raise boards.BoardError("Habr Career: сеть недоступна")

    site = web([("/search/vacancy", Resp(200, read("hhweb_search.html"))),
                ("/vacancy/", Resp(200, read("hhweb_vacancy.html")))])
    conn = db.connect(tmp_path / "jobs.db")
    r = Runner(conn, cfg, progress=lambda m: None, hh_web_source=site, board_sources={"habr": Broken([])},
               sleep=lambda s: None, dumps_dir=tmp_path / "dumps").run()
    assert r.status == "partial" and any("Habr" in e for e in r.errors)
    assert db.get_state(conn, "board:habr") is None
    assert db.get_state(conn, "hh:nocode") is not None


def test_search_survives_unparsable_date():
    from jobauto.sources.hh_web import _older

    assert _older("вчера", datetime(2026, 10, 19).astimezone()) is False


def test_changed_board_terms_recheck_rejected(cfg, tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    r = Runner(conn, cfg, progress=lambda m: None, sleep=lambda s: None, dumps_dir=tmp_path / "dumps")
    r._forget_rejected_if_terms_changed([{"text": "bubble"}])
    conn.execute("INSERT INTO board_seen(id, checked_at) VALUES('habr:9', 'x')")
    r._forget_rejected_if_terms_changed([{"text": "bubble"}])
    assert conn.execute("SELECT COUNT(*) FROM board_seen").fetchone()[0] == 1  # слова те же — помним
    r._forget_rejected_if_terms_changed([{"text": "bubble"}, {"text": "n8n"}])
    assert conn.execute("SELECT COUNT(*) FROM board_seen").fetchone()[0] == 0  # слова изменились — проверим заново
