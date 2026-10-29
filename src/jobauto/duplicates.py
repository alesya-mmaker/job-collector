"""Поиск повторных публикаций: та же вакансия под новым id.

Вакансия считается возможным повтором, если:
  1. hh указал previous_id, и эта вакансия уже есть в базе
     (работодатель перевыложил её из архива), или
  2. в базе есть вакансия той же компании (по employer_id)
     с очень похожим названием.
Повтор не скрывается и не удаляется: в duplicate_of пишется id
самой ранней вакансии цепочки.
"""

from __future__ import annotations

import re
import sqlite3
from difflib import SequenceMatcher

from .patterns import normalize

NOISE_WORDS = {"удаленно", "удаленка", "удаленная", "удаленный", "remote", "вакансия", "работа"}
GRADE_WORDS = {
    "intern", "junior", "middle", "senior", "lead", "head", "principal",
    "стажер", "младший", "старший", "ведущий", "главный", "тимлид",
}


def normalize_title(title: str) -> str:
    t = normalize(title).lower()
    t = re.sub(r"[^\w+#]+", " ", t)
    return " ".join(w for w in t.split() if w not in NOISE_WORDS)


LEGAL_FORMS = r"\b(ооо|оао|зао|пао|ао|ип|тоо|чуп|чтуп|уп|одо|llc|ltd|inc|gmbh|corp|group|груп+а)\b"


def employer_key(name: str | None) -> str | None:
    """Название компании для сравнения между площадками: без кавычек, ОПФ и регистра."""
    if not name:
        return None
    t = normalize(name).lower()
    t = re.sub(LEGAL_FORMS, " ", t)
    t = re.sub(r"[^\w]+", "", t)
    return t or None


def title_similarity(a: str, b: str) -> float:
    na, nb = normalize_title(a), normalize_title(b)
    if not na or not nb:
        return 0.0
    if na == nb:
        return 1.0
    ta, tb = set(na.split()), set(nb.split())
    ga = {t.rstrip("+") for t in ta} & GRADE_WORDS
    gb = {t.rstrip("+") for t in tb} & GRADE_WORDS
    if ga and gb and ga != gb:
        # Junior и Senior одной профессии — разные вакансии
        return 0.0
    small, big = (ta, tb) if len(ta) <= len(tb) else (tb, ta)
    if len(small) >= 2 and small < big and len(big - small) == 1:
        # одно название = другое плюс одно слово ("... (Junior)", "... в стартап")
        return 0.9
    direct = SequenceMatcher(None, na, nb).ratio()
    tokens = SequenceMatcher(None, " ".join(sorted(na.split())), " ".join(sorted(nb.split()))).ratio()
    return max(direct, tokens)


def _root(conn: sqlite3.Connection, vac_id: str) -> str:
    row = conn.execute("SELECT duplicate_of FROM vacancies WHERE id = ?", (vac_id,)).fetchone()
    return row["duplicate_of"] if row and row["duplicate_of"] else vac_id


def find_original(conn: sqlite3.Connection, vac: dict, threshold: float) -> str | None:
    prev = vac.get("previous_id")
    if prev and prev != vac["id"]:
        if conn.execute("SELECT 1 FROM vacancies WHERE id = ?", (prev,)).fetchone():
            return _root(conn, prev)

    employer_id = vac.get("employer_id")
    key = vac.get("employer_key") or employer_key(vac.get("employer_name"))
    if not employer_id and not key:
        return None
    best_id, best_score, best_pub = None, 0.0, ""
    # оригиналом может быть только вакансия, опубликованная не позже этой
    pub_limit = vac.get("published_at") or "9999"
    for row in conn.execute(
        "SELECT id, name, published_at FROM vacancies "
        "WHERE (employer_id = ? OR employer_key = ?) AND id != ? AND COALESCE(published_at, '') <= ?",
        (employer_id or "-", key or "-", vac["id"], pub_limit),
    ):
        score = title_similarity(vac["name"], row["name"])
        if score < threshold:
            continue
        # при равной похожести берём более раннюю
        pub = row["published_at"] or ""
        if score > best_score or (score == best_score and pub < best_pub):
            best_id, best_score, best_pub = row["id"], score, pub
    return _root(conn, best_id) if best_id else None
