"""Пометки к вакансиям по marks.yaml. Только подсказки, ничего не отбрасывают."""

from __future__ import annotations

from typing import Any

from .patterns import PatternSet

PRIVATE_EMPLOYER_TYPES = {"private_individual", "self_employed"}


class Marker:
    def __init__(self, marks_config: list[dict[str, Any]]):
        self._items: list[tuple[str, str | None, PatternSet | None]] = []
        for m in marks_config:
            source = m.get("source")
            patterns = m.get("patterns") or []
            ps = PatternSet.from_strings(m["name"], patterns) if patterns else None
            self._items.append((m["name"], source, ps))

    @staticmethod
    def _source_hit(source: str, vac: dict[str, Any]) -> bool:
        if source == "no_salary":
            return vac.get("salary_from") is None and vac.get("salary_to") is None
        if source == "private_employer":
            return vac.get("employer_type") in PRIVATE_EMPLOYER_TYPES
        if source == "six_day":
            return "6/1" in (vac.get("schedule") or "")
        return False

    def marks_for(self, vac: dict[str, Any]) -> list[str]:
        """Пометка ставится, если сработал её источник данных ИЛИ одно из слов."""
        text = "\n".join(filter(None, [vac.get("name"), vac.get("description") or vac.get("snippet")]))
        result = []
        for name, source, ps in self._items:
            hit = (source is not None and self._source_hit(source, vac)) or (ps is not None and ps.search(text))
            if hit:
                result.append(name)
        return result
