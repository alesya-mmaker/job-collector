import json
from pathlib import Path

from jobauto.config import load_config
from jobauto.html_text import html_to_text, strip_highlight
from jobauto.marks import Marker
from jobauto.patterns import PatternSet, compile_pattern, validate

FIX = Path(__file__).parent / "fixtures"


def test_phrase_is_whole_word_and_case_insensitive():
    ps = PatternSet.from_strings("seo", ["seo"])
    assert ps.search("Нужен SEO-специалист")
    assert not ps.search("seoul")


def test_yo_equals_e():
    assert PatternSet.from_strings("v", ["вёрстка"]).search("Верстка лендингов")
    assert PatternSet.from_strings("v", ["re:в[её]рстк\\w*"]).search("вёрстку")


def test_phrase_with_spaces_and_special_chars():
    assert PatternSet.from_strings("m", ["make.com"]).search("сценарии в Make.com")
    assert PatternSet.from_strings("m", ["code review"]).search("проводить code\n review")
    assert PatternSet.from_strings("1c", ["1С"]).search("опыт с 1С обязателен")


def test_phrase_hyphen_and_space_are_interchangeable():
    ps = PatternSet.from_strings("ai", ["AI-разработчик"])
    assert ps.search("Вакансия: AI разработчик")
    assert ps.search("AI-разработчик в стартап")
    assert ps.search("AIразработчик")
    nc = PatternSet.from_strings("nc", ["no-code"])
    assert nc.search("nocode") and nc.search("No code") and nc.search("no-code")
    assert not nc.search("unicode")


def test_validate():
    assert validate("re:(") is not None
    assert validate("re:\\w+") is None
    assert compile_pattern("  bubble ").search("bubble")


def _marks(text, **vac):
    cfg = load_config()
    base = {"name": "", "description": text, "salary_from": 1, "salary_to": None, "employer_type": "company"}
    base.update(vac)
    return Marker(cfg.marks).marks_for(base)


def test_default_marks():
    assert "нужно читать код" in _marks("Нужно уметь разбираться в сгенерированном коде")
    assert "нужно читать код" in _marks("Знание JavaScript/TypeScript на уровне чтения кода")
    assert "нужно читать код" in _marks("участие в code review")
    assert "6/1" in _marks("график 6/1")
    assert "6/1" in _marks("шестидневная рабочая неделя")
    assert "6/1" not in _marks("зарплата 16/18 тыс") and "6/1" not in _marks("версия 2.6/10")
    assert "внутренняя автоматизация" in _marks("обучение сотрудников работе с ИИ")
    assert "маркетплейсы" in _marks("опыт работы с Wildberries и Ozon")
    assert "маркетплейсы" in _marks("интеграция с Битрикс24")
    assert "не разработка" in _marks("вёрстка писем")
    assert "не разработка" not in _marks("Ищем менеджера проектов")  # убрано: руководство проектами — одна из целевых ролей
    assert _marks("Пишем на Bubble и n8n") == []


def test_api_marks():
    assert "зп не указана" in _marks("", salary_from=None, salary_to=None)
    assert "работодатель физлицо" in _marks("", employer_type="private_individual")
    assert "работодатель физлицо" in _marks("", employer_type="self_employed")
    assert "работодатель физлицо" not in _marks("", employer_type="agency")


def test_html_to_text_keeps_structure():
    html = "<p>Задачи:</p><ul><li>делать <strong>MVP</strong></li><li>n8n</li></ul><p>Условия<br/>удалёнка&nbsp;полная</p>"
    assert html_to_text(html) == "Задачи:\n\n• делать MVP\n• n8n\n\nУсловия\nудалёнка полная"
    assert html_to_text("<ol><li>раз</li><li>два</li></ol>") == "1. раз\n2. два"


def test_strip_highlight():
    assert strip_highlight("Опыт <highlighttext>Bubble</highlighttext>") == "Опыт Bubble"


def test_fixture_description():
    v = json.loads((FIX / "hh_vacancy.json").read_text(encoding="utf-8"))
    assert html_to_text(v["description"]) == "Работа хороша"
