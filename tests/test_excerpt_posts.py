from jobauto import db
from jobauto.config import load_config
from jobauto.excerpt import excerpt
from jobauto.runner import refresh_post_keywords

FINANCE_POST = """Финансовый менеджер (автоматизация и развитие финансовых процессов) | Антиплагиат

✔️ Что делать:
Выстраивать финансовые процессы и бюджетирование, готовить финансовую и управленческую отчётность. Автоматизировать сверки и интеграции между системами с помощью AI-инструментов.

Ведущий бухгалтер | Моё дело
#финансы #AI"""


def test_excerpt_takes_tasks_section():
    text = "Aston — аккредитованная IT-компания.\n\nЗадачи:\n• Собирать MVP на Bubble\n• Настраивать n8n\n\nТребования:\n• опыт от года"
    assert excerpt(text) == "Собирать MVP на Bubble\nНастраивать n8n"


def test_excerpt_skips_company_intro_without_tasks_heading():
    text = "Aston — аккредитованная IT-компания.\nИщем разработчика на Bubble для внутренних сервисов."
    assert excerpt(text) == "Ищем разработчика на Bubble для внутренних сервисов."


def test_excerpt_is_short():
    text = "Задачи:\n" + "\n".join(f"пункт номер {i} " * 6 for i in range(20))
    out = excerpt(text)
    assert len(out) <= 330 and out.count("\n") <= 3 and out.endswith("…")
    assert excerpt(None) == "" and excerpt("") == ""


def test_finance_digest_is_hidden_by_current_keywords(tmp_path):
    conn = db.connect(tmp_path / "jobs.db")
    conn.execute(
        "INSERT INTO tg_posts(hash, channel, post_id, url, date, text, keywords, added_at) VALUES(?,?,?,?,?,?,?,?)",
        ("h1", "remotehunters", 1, "https://t.me/remotehunters/1", None, FINANCE_POST, '["автоматизаци\\\\w*"]', "x"),
    )
    conn.execute(
        "INSERT INTO tg_posts(hash, channel, post_id, url, date, text, keywords, added_at) VALUES(?,?,?,?,?,?,?,?)",
        ("h2", "nocode_jobs", 2, "https://t.me/nocode_jobs/2", None, "Ищем Bubble-разработчика, удалённо", "[]", "x"),
    )
    conn.commit()
    hidden = refresh_post_keywords(conn, load_config().keywords)
    rows = {r["hash"]: r["keywords"] for r in conn.execute("SELECT hash, keywords FROM tg_posts")}
    assert rows["h1"] == "[]" and hidden == 1        # финансы больше не проходят
    assert "bubble" in rows["h2"]                     # подходящий пост получил слово как в тексте


def test_excerpt_stops_at_next_section_and_skips_service_lines():
    text = "Задачи:\nсоздавать AI-агенты;\nстроить MLOps-инфраструктуру;\nЧего мы ждем от специалиста?\nопыт Python"
    assert excerpt(text) == "создавать AI-агенты;\nстроить MLOps-инфраструктуру;"
    habr = "Навыки: C#, .NET. Специализации: Бэкенд разработчик.\nПроект: Система лояльности\nОбязательные требования\nC# от 3 лет"
    assert excerpt(habr) == "Проект: Система лояльности"
