"""Отсев: вакансии, которые точно не нужны, по exclude.yaml.

В отличие от пометок, отсеянная вакансия скрывается из ленты и дампа. Из базы
она не удаляется: так она не соберётся заново, а при правке правил вернётся.
Вакансии, где уже есть отклик или заметка, не отсеиваются никогда.

Правило:
  where: title        — ищется в названии вакансии (у поста Telegram — в первых строках);
         requirements — в описании без разделов «Будет плюсом / Желательно»
                        (пока полного описания нет — в сниппете; у поста — в его тексте);
         post         — только для постов Telegram, во всём тексте (реклама курсов, вебинары);
         salary       — зарплата указана и меньше salary_below рублей (без слов);
         duplicate    — возможный повтор уже собранной вакансии (без слов);
         closed       — вакансия в архиве или удалена на сайте (без слов);
  scope: main | side | all — основная работа, подработка или везде;
  patterns            — слова и фразы (формат как у пометок);
  unless              — исключения: если найдены там же, где правило, оно не срабатывает;
  unless_no_experience — не срабатывает, если опыт не требуется.
"""

from __future__ import annotations

import json
import re
import sqlite3
from typing import Any

from .patterns import PatternSet

# правила без слов, по полям вакансии
CONDITIONS = ("salary", "duplicate", "closed")

# примерный курс к рублю — только чтобы сравнить зарплату с порогом
RUB_RATE = {"RUR": 1, "RUB": 1, "BYR": 27, "BYN": 27, "KZT": 0.16, "USD": 80, "EUR": 90, "UZS": 0.0065}

NO_EXPERIENCE = {"нет опыта", "noexperience", "без опыта", "стажёр (intern)", "стажер (intern)"}

# разделы и строки «будет плюсом» — это не требования
PLUS = re.compile(
    r"(?i)(будет плюсом|плюсом будет|преимуществ\w*|будет большим плюсом|"
    r"было бы плюсом|желательно|приветствуется|как преимущество|nice to have|is a plus|would be a plus|"
    r"preferred|бонусом)"
)
# заголовок следующего раздела: после «Будет плюсом» снова начинаются требования, условия и т.п.
HEADING = re.compile(
    r"(?i)^[^\w]*(обязательные требования|требования|мы ожидаем|ожидаем|наши ожидания|что мы ж[её]м|"
    r"чего мы ж[её]м|чего ж[её]м|кого мы ищем|кого ищем|мы ж[её]м|условия|мы предлагаем|предлагаем|"
    r"что мы предлагаем|о компании|о нас|задачи|обязанности|чем предстоит|что нужно делать|"
    r"requirements|responsibilities|what we offer|benefits)\b"
)


def requirements_text(text: str | None) -> str:
    """Описание без разделов и строк «будет плюсом»."""
    out: list[str] = []
    skipping = False
    for line in (text or "").splitlines():
        s = line.strip()
        if not s:
            continue
        short = len(s) <= 60 and len(s.split()) <= 7
        if short and PLUS.search(s) and (s.endswith(":") or len(s.split()) <= 4):
            skipping = True  # заголовок раздела «Будет плюсом:»
            continue
        if skipping and (HEADING.match(s) or (short and s.endswith(":"))):
            skipping = False
        if skipping or PLUS.search(s):
            continue
        out.append(s)
    return "\n".join(out)


class Rule:
    def __init__(self, cfg: dict[str, Any]):
        self.name = str(cfg["name"])
        self.where = cfg.get("where") or "title"
        self.scope = cfg.get("scope") or "all"
        self.patterns = PatternSet.from_strings(self.name, cfg.get("patterns") or [])
        self.unless = PatternSet.from_strings(self.name, cfg.get("unless") or [])
        self.unless_no_experience = bool(cfg.get("unless_no_experience"))
        self.salary_below = float(cfg.get("salary_below") or 0)

    def applies_to(self, kind: str) -> bool:
        return self.scope == "all" or self.scope == kind

    def condition_hit(self, vac: dict[str, Any]) -> bool:
        """Правила без слов — по полям вакансии."""
        if self.where == "duplicate":
            return bool(vac.get("duplicate_of"))
        if self.where == "closed":
            return (vac.get("source_status") or "active") != "active"
        # salary: зарплата указана и даже верхняя граница меньше порога
        top = vac.get("salary_to") or vac.get("salary_from")
        rate = RUB_RATE.get((vac.get("currency") or "").upper())
        return bool(self.salary_below and top and rate) and top * rate < self.salary_below

    def hit(self, text: str, experience: str | None) -> bool:
        if not text or not self.patterns.search(text):
            return False
        if self.unless.patterns and self.unless.search(text):
            return False
        if self.unless_no_experience and (experience or "").strip().lower() in NO_EXPERIENCE:
            return False
        return True


class Excluder:
    def __init__(self, rules_config: list[dict[str, Any]]):
        self.rules = [Rule(r) for r in rules_config if r.get("enabled", True) and r.get("name")]

    def reasons(self, vac: dict[str, Any], kind: str | None = None, title_only: bool = False) -> list[str]:
        """Названия сработавших правил. title_only — только правила по названию (до загрузки описания)."""
        kind = kind or vac.get("kind") or "main"
        title = vac.get("name") or ""
        req: str | None = None
        out = []
        for r in self.rules:
            if not r.applies_to(kind):
                continue
            if r.where == "post":
                continue
            if r.where in CONDITIONS:
                if r.condition_hit(vac):
                    out.append(r.name)
                continue
            if r.where == "title":
                text = title
            elif title_only:
                continue
            else:
                if req is None:
                    # график из карточки hh («2/2», «6/1») проверяется вместе с текстом
                    req = requirements_text(vac.get("description") or vac.get("snippet"))
                    if vac.get("schedule"):
                        req += "\nГрафик: " + vac["schedule"]
                    # «На месте работодателя, Удалённо» на hh — можно выбрать удалёнку; считаем только без неё
                    if vac.get("work_format") and "удал" not in vac["work_format"].lower():
                        req += "\nФормат работы: " + vac["work_format"]
                text = req
            if r.hit(text, vac.get("experience")):
                out.append(r.name)
        return out

    def post_reasons(self, text: str) -> list[str]:
        """Пост Telegram проверяется как вакансия основной работы: правила по названию — по первым
        строкам поста (должность часто идёт после «Новая вакансия»), по требованиям — по тексту
        без «будет плюсом», правила для постов — по всему тексту."""
        head = post_title(text)
        req: str | None = None
        out = []
        for r in self.rules:
            if not r.applies_to("main"):
                continue
            if r.where == "title":
                part = head
            elif r.where == "requirements":
                if req is None:
                    req = requirements_text(text)
                part = req
            elif r.where in CONDITIONS:
                continue
            else:
                part = text or ""
            if r.hit(part, None):
                out.append(r.name)
        return out


def post_title(text: str, lines: int = 4) -> str:
    """Первые содержательные строки поста: со словами, а не только хэштегами и эмодзи."""
    out = []
    for line in (text or "").splitlines():
        words = [w for w in re.findall(r"#?\w+", line) if not w.startswith("#")]
        if words and re.search(r"[A-Za-zА-Яа-яЁё]{3}", " ".join(words)):
            out.append(line.strip())
            if len(out) >= lines:
                break
    return "\n".join(out)


# вакансия с откликом или заметкой не отсеивается
PROTECTED = (
    "SELECT 1 FROM tracking t WHERE t.item_type = ? AND t.item_id = ? AND "
    "(t.status = 'отклик' OR COALESCE(t.notes, '') != '' OR COALESCE(t.applied_at, '') != '')"
)


def refresh_exclusions(conn: sqlite3.Connection, rules_config: list[dict[str, Any]]) -> dict[str, int]:
    """Пересчёт отсева у всех вакансий и постов по текущим правилам.

    Вакансии, отсеянные по названию до загрузки описания, помечаются detail_status='skipped'
    (описание им не нужно). Если правило убрали, такая вакансия возвращается в очередь
    на загрузку описания. Возвращает {'vacancies': отсеяно всего, 'posts': ..., 'returned': вернулось}.
    """
    ex = Excluder(rules_config)
    total = returned = 0
    for row in conn.execute(
        "SELECT id, kind, name, experience, description, snippet, excluded, detail_status, "
        "salary_from, salary_to, currency, schedule, work_format, duplicate_of, source_status FROM vacancies"
    ).fetchall():
        vac = dict(row)
        if vac["detail_status"] == "compacted":
            # описания уже нет: правила по тексту дали бы другой результат, оставляем как есть
            total += bool(vac["excluded"])
            continue
        reasons = [] if conn.execute(PROTECTED, ("vacancy", vac["id"])).fetchone() else ex.reasons(vac)
        value = json.dumps(reasons, ensure_ascii=False) if reasons else None
        status = vac["detail_status"]
        if reasons and status == "pending":
            status = "skipped"
        elif not reasons and status == "skipped":
            status = "pending"
        returned += bool(vac["excluded"]) and not reasons
        total += bool(reasons)
        if value != vac["excluded"] or status != vac["detail_status"]:
            conn.execute("UPDATE vacancies SET excluded = ?, detail_status = ? WHERE id = ?", (value, status, vac["id"]))
    posts = 0
    for row in conn.execute("SELECT hash, text, excluded FROM tg_posts").fetchall():
        reasons = [] if conn.execute(PROTECTED, ("post", row["hash"])).fetchone() else ex.post_reasons(row["text"])
        value = json.dumps(reasons, ensure_ascii=False) if reasons else None
        posts += bool(reasons)
        if value != row["excluded"]:
            conn.execute("UPDATE tg_posts SET excluded = ? WHERE hash = ?", (value, row["hash"]))
    conn.commit()
    return {"vacancies": total, "posts": posts, "returned": returned}
