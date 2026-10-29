import json
from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from fakes import TZ, FakeHh, item
from jobauto import api, db
from jobauto.config import load_config
from jobauto.exclude import Excluder, refresh_exclusions, requirements_text
from jobauto.patterns import compile_pattern
from jobauto.runner import Runner
from jobauto.sources.boards import older_than

NOW = datetime(2026, 10, 21, 12, 0, tzinfo=TZ)
RULES = load_config().exclude  # настоящие правила из config/exclude.yaml


def reasons(name, kind="main", **kw):
    return Excluder(RULES).reasons({"name": name, "kind": kind, **kw})


def test_star_matches_word_start_only():
    p = compile_pattern("юрист*")
    assert p.search("Помощник юриста") and p.search("Юрист") and not p.search("LLM-разработчик в сервис Нейроюрист")


def test_titles_from_my_no_list_are_excluded():
    for title in [
        "Backend Developer", "Менеджер по продажам", "Personal brand producer / Контент-менеджер",
        "Бизнес-ассистент руководителя", "Программист", "Программист 1С", "Системный аналитик",
        "Руководитель проектов", "Project Manager (GameDev)", "Экономист-аналитик", "CEO / Исполнительный директор",
        "Sales & Recruitment Manager", "Fullstack-разработчик Angular + C#/.NET", "Commercial & Pricing Analyst",
        "Head of Support", "QA Engineer", "Лидогенератор / Менеджер по назначению встреч", "Консультант 1С",
        "Юрисконсульт", "Аккаунт менеджер Google ADS", "SEO-специалист", "Маркетолог", "Генеральный директор",
        "Backend-разработчик", "Администратор-разработчик Битрикс24",
    ]:
        assert reasons(title), title


def test_my_profile_and_exceptions_stay():
    for title in [
        "Вайб-кодер / AI-разработчик", "Low-Code / No-Code Automation Engineer", "Промпт-инженер",
        "AI-инженер по автоматизации бизнес-процессов", "Fullstack-разработчик (AI-native)",
        "Старший LLM-разработчик в сервис Нейроюрист",
    ]:
        assert reasons(title) == [], title
    assert reasons("Fullstack-разработчик") == ["fullstack без AI-assisted"]
    # CRM-специалист — только без опыта
    assert reasons("CRM-специалист", experience="Нет опыта") == []
    assert reasons("CRM-специалист", experience="От 1 года до 3 лет")
    # «менеджер» и «консультант» — только в основной работе, в подработке поддержка остаётся
    assert reasons("Менеджер поддержки клиентов", kind="side") == []
    assert reasons("Консультант поддержки клиентов", kind="side") == []
    assert reasons("Бизнес-ассистент", kind="side")


def test_requirements_skip_nice_to_have():
    desc = "Требования:\nопыт с n8n\nБудет плюсом:\nKafka\nGrafana\nУсловия:\nудалёнка\nЖелательно знание Lua"
    req = requirements_text(desc)
    assert "Kafka" not in req and "Lua" not in req and "n8n" in req and "удалёнка" in req
    assert reasons("Специалист по автоматизации", description=desc) == []
    hard = "Требования:\nОпыт работы с Kafka\nУверенное владение любым из языков программирования"
    assert set(reasons("Специалист по автоматизации", description=hard)) == {
        "стек, которого у меня нет", "требуют программирования"}
    # без описания правила по требованиям смотрят сниппет
    assert reasons("Инженер", snippet="Практический боевой опыт с Ozon Seller API")


def test_refresh_hides_returns_and_protects(tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    for vid, name in [("1", "Менеджер проектов"), ("2", "Вайб-кодер"), ("3", "Маркетолог")]:
        conn.execute(
            "INSERT INTO vacancies(id, kind, name, url, query_ids, queries, detail_status, source_status, added_at, updated_at) "
            "VALUES(?, 'main', ?, 'u', '[]', '[]', 'pending', 'active', 'x', 'x')", (vid, name))
    conn.execute("INSERT INTO tracking(item_type, item_id, status, updated_at) VALUES('vacancy', '3', 'отклик', 'x')")
    conn.commit()
    stats = refresh_exclusions(conn, RULES)
    rows = {r["id"]: dict(r) for r in conn.execute("SELECT id, excluded, detail_status FROM vacancies")}
    assert stats["vacancies"] == 1 and json.loads(rows["1"]["excluded"]) == ["менеджер"]
    assert rows["1"]["detail_status"] == "skipped"          # описание отсеянной не грузим
    assert rows["2"]["excluded"] is None and rows["3"]["excluded"] is None  # отклик — не трогаем
    # правило выключили — вакансия вернулась и снова ждёт описание
    stats = refresh_exclusions(conn, [r for r in RULES if r["name"] != "менеджер"])
    row = conn.execute("SELECT excluded, detail_status FROM vacancies WHERE id = '1'").fetchone()
    assert stats["returned"] == 1 and row["excluded"] is None and row["detail_status"] == "pending"


def test_run_skips_details_of_excluded_and_dump(tmp_path, monkeypatch):
    cfg = load_config()
    cfg.settings["hh"]["contact_email"] = "t@t.dev"
    cfg.channels, cfg.boards = [], []
    cfg.queries = [q for q in cfg.queries if q.id == "bubble"]
    t = NOW - timedelta(hours=3)
    fake = FakeHh([
        item(1, "Bubble-разработчик", t, description="<p>Собирать MVP на Bubble</p>"),
        item(2, "Менеджер проектов Bubble", t, description="<p>Управлять</p>"),
        item(3, "Bubble no-code разработчик", t, description="<p>Требования: опыт работы с Kafka</p>"),
    ])
    db_path = tmp_path / "jobs.db"
    conn = db.connect(db_path)
    r = Runner(conn, cfg, progress=lambda m: None, hh_client=fake, sleep=lambda s: None, now=lambda: NOW,
               dumps_dir=tmp_path / "dumps").run()
    assert r.status == "ok", r.errors
    rows = {x["id"]: dict(x) for x in conn.execute("SELECT id, excluded, detail_status FROM vacancies")}
    assert rows["2"]["detail_status"] == "skipped" and json.loads(rows["2"]["excluded"]) == ["менеджер"]
    assert json.loads(rows["3"]["excluded"]) == ["стек, которого у меня нет"]  # по описанию, после загрузки
    assert rows["1"]["excluded"] is None
    assert "/vacancies/2" not in [path for path, _ in fake.calls]
    dump = r.dump_path.read_text(encoding="utf-8")
    assert "Bubble-разработчик" in dump and "Менеджер проектов" not in dump and "no-code разработчик" not in dump
    conn.close()

    monkeypatch.setattr(api, "DB_PATH", db_path)
    client = TestClient(api.app)
    lst = client.get("/api/vacancies", params={"kind": "main"}).json()
    assert [v["id"] for v in lst["items"]] == ["1"] and lst["excluded_count"] == 2
    ex = client.get("/api/vacancies", params={"kind": "main", "excluded": "true"}).json()
    assert {v["id"] for v in ex["items"]} == {"2", "3"} and ex["items"][0]["excluded"]
    assert client.get("/api/meta").json()["unseen"]["main"] == 1


def test_board_dates_without_timezone():
    since = datetime(2026, 10, 14, 15, 53, tzinfo=TZ)
    assert older_than("2026-10-01T10:00:00", since) and not older_than("2026-10-20", since)
    assert not older_than(None, since) and not older_than("мусор", since)


def test_second_round_of_my_no():
    assert reasons("3D-разработчик / Технический партнер (Vibe-coder)")
    assert reasons("AI-инженер: Marketing OS")
    for req in [
        "3+ года коммерческой разработки на Node.js + TypeScript",
        "Глубокое понимание LLM под капотом",
        "Опыт с Redis, ClickHouse, векторные БД (Qdrant / pgvector)",
        "Опыт высоких нагрузок",
        "Профильное образование в социологии, маркетинговых исследованиях или смежной области",
        "Профильное техническое образование",
        "Django 5, Django REST Framework",
        "Глубокое понимание Java Core, включая многопоточность, память, сборку мусора;",
        "Опыт работы с Spring Framework и Spring Boot.",
        "Уверенное владение SQL и реляционными СУБД (предпочтительно PostgreSQL), либо опыт работы с NoSQL (например, MongoDB);",
        "Понимание принципов построения и использования индексов;",
        "Знание и применение архитектурных паттернов и шаблонов проектирования;",
        "Опыт в 2D и 3D разработке",
    ]:
        assert reasons("Специалист по автоматизации", description="Требования:\n" + req), req
    # в «будет плюсом» — не отсеивается
    assert reasons("Специалист по автоматизации", description="Требования:\nn8n\nБудет плюсом — опыт с Redis") == []


def test_third_round_of_my_no():
    for req in [
        "Обучение моделей",
        "Свободный или уверенный английский язык",
        "Знаете Python и SQL на уровне, достаточном, чтобы разобраться в чужом коде",
        "Коммерческий опыт разработки на Python",
        "Опыт с BPMSoft, Naumen Erudite, BI",
        "анализ рынка → поиск товарных возможностей → расчёт экономики → поиск поставщиков",
        "Хорошее знание правил футбола",
        "Свободное владение киргизским языком",
        "Проводить эфиры",
        "Настраивать и сопровождать GetCourse",
        "Строить автоворонки",
        "Настраивать Salebot",
        "Высшее техническое/математическое образование",
        "Пишете код (Python)",
        "Strong Python skills",
        "Основы React, JavaScript / TypeScript",
        "Понимание минимум одного из языков: JavaScript, Python, Groovy",
        "Нужно жить в Азербайджане или Узбекистане",
        "Готовность к регулярным командировкам",
        "Основной стек — Langflow",
    ]:
        assert reasons("Специалист по автоматизации", description="Требования:" + chr(10) + req), req
    for ok in ["Командировок нет, полностью удалённо", "Английский на уровне чтения документации",
               "Уверенное владение русским языком"]:
        assert reasons("Специалист по автоматизации", description="Требования:" + chr(10) + ok) == [], ok
    assert reasons("Специалист по сбору данных на киргизском языке (AI/LLM)")
    assert reasons("Технический специалист GetCourse / Salebot в онлайн-школу")


def test_posts_and_fourth_round():
    ex = Excluder(RULES)
    nl = chr(10)
    pm = nl.join(["🙂 Новая вакансия", "📍Менеджмент", "Product Manager / Питер, офис", "Нужен AI-агент"])
    assert "менеджер" in ex.post_reasons(pm)
    course = nl.join(["🔥 Идёт набор в XI поток Hello New Job!", "За 7 недель обучения ты:", "AI-агент"])
    assert "реклама курсов и вебинаров" in ex.post_reasons(course)
    assert "реклама курсов и вебинаров" in ex.post_reasons("Последний паблик вебинар о поиске работы")
    stack = nl.join(["Вайб-кодер", "Требования:", "React, TypeScript, Next.js"])
    assert "стек, которого у меня нет" in ex.post_reasons(stack)
    assert ex.post_reasons(nl.join(["Вайб-кодер в стартап", "Делать MVP на Bubble и n8n", "Удалённо"])) == []
    assert reasons("Founding Forward Deployed Engineer")
    for req in ["English B1", "B2+ level", "Английский язык на уровне Intermediate", "Работа в офисе в Минске"]:
        assert reasons("Специалист по автоматизации", description="Требования:" + nl + req), req
    assert reasons("Специалист по автоматизации", description="Удалённо, офис по желанию")  # офис ни в каком виде
    assert reasons("Специалист по автоматизации", description="Удалённо, без офиса") == []


def test_side_jobs_round():
    nl = chr(10)
    for title in ["HR-специалист", "Ассистент в онлайн-школу", "Помощник рекрутера", "Контент-менеджер", "Ночной Модератор"]:
        assert reasons(title, kind="side"), title
    for req in ["Вечерняя смена", "График 2/2", "Ведение бухгалтерского учёта", "Переговоры с клиентами",
                "Поиск людей в соцсетях", "Работа в 1С", "Опыт работы в системах мониторинга BrandAnalytics и/или Медиалогия",
                "Вечерние или ночные смены"]:
        assert reasons("Оператор чата", kind="side", description="Требования:" + nl + req), req
    assert reasons("Оператор чата", kind="side", schedule="2/2")
    assert reasons("Оператор чата", kind="side", schedule="5/2") == []
    assert reasons("Оператор чата", kind="side", salary_from=27000, currency="RUR")
    assert reasons("Оператор чата", kind="side", salary_from=27000, salary_to=35000, currency="RUR") == []
    assert reasons("Оператор чата", kind="side", salary_from=600, currency="BYR")   # ≈ 16 000 ₽
    assert reasons("Оператор чата", kind="side") == []                            # без зарплаты — остаётся
    assert reasons("Оператор чата", kind="main", salary_from=27000, currency="RUR") == []  # только подработка


def test_round_six():
    nl = chr(10)
    side = lambda **kw: reasons("Оператор чата", kind="side", **kw)
    assert reasons("Специалист поддержки amoCRM", kind="side", experience="От 1 года до 3 лет")
    assert reasons("Специалист поддержки amoCRM", kind="side", experience="Нет опыта") == []
    assert reasons("Веб-дизайнер (поддержка сайта)", kind="side", experience="От 1 года до 3 лет")
    assert reasons("Начинающий веб-дизайнер", kind="side", experience="От 1 года до 3 лет") == []
    assert reasons("Веб-дизайнер", kind="side", experience="Нет опыта") == []
    for req in ["Изучение рынка и анализ конкурентов", "Рассказывать коллегам и знакомым представителям клиник о продуктах",
                "Рабочее время: с 17:00 до 00:00", "Формат работы онлайн/офлайн", "Первые две недели в офисе"]:
        assert side(description="Задачи:" + nl + req), req
    assert side(work_format="На месте работодателя")
    assert side(work_format="На месте работодателя, Удалённо") == []  # удалёнка на выбор
    assert side(work_format="Удалённо") == []
    assert side(duplicate_of="123") and side(source_status="archived")
    assert side(description="Рабочее время: с 9:00 до 18:00") == []


def test_city_only():
    nl = chr(10)
    for req in ["Рассматриваем кандидатов только с г.Новосибирск", "Только из Москвы", "Кандидаты из Казани",
                "Для проживающих в Екатеринбурге", "Привязка к региону"]:
        assert reasons("Оператор чата", kind="side", description="Условия:" + nl + req), req
    for ok in ["Можно из любого города", "Только из Минска", "Будем сочинять тексты только вместе",
               "Консультирование жителей Москвы по вопросам выплат"]:
        assert reasons("Оператор чата", kind="side", description="Условия:" + nl + ok) == [], ok
