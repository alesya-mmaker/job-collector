"""Подсветка моих навыков в тексте вакансии. Ничего не считает."""

from __future__ import annotations

from typing import Any

from .patterns import PatternSet

LEVELS = ("знаю", "частично")


class SkillHighlighter:
    def __init__(self, skills: list[dict[str, Any]]):
        self._sets: list[tuple[str, str, PatternSet]] = []
        for s in skills:
            name = str(s.get("name") or "").strip()
            if not name:
                continue
            level = s.get("level") if s.get("level") in LEVELS else "частично"
            variants = [name, *(s.get("variants") or [])]
            self._sets.append((name, level, PatternSet.from_strings(name, variants)))

    def segments(self, text: str) -> list[dict[str, str]]:
        """Текст, разбитый на куски: [{"t": ..., "level": "знаю"|"частично", "skill": ...}]."""
        if not text:
            return []
        spans: list[tuple[int, int, str, str]] = []
        for name, level, ps in self._sets:
            spans += [(a, b, level, name) for a, b in ps.spans(text)]
        # длинные совпадения важнее; пересекающиеся короткие отбрасываем
        spans.sort(key=lambda s: (s[0], -(s[1] - s[0])))
        result, pos = [], 0
        for a, b, level, name in spans:
            if a < pos:
                continue
            if a > pos:
                result.append({"t": text[pos:a]})
            result.append({"t": text[a:b], "level": level, "skill": name})
            pos = b
        if pos < len(text):
            result.append({"t": text[pos:]})
        return result

    def found(self, text: str) -> list[dict[str, str]]:
        """Какие мои навыки встречаются в тексте."""
        return [{"skill": name, "level": level} for name, level, ps in self._sets if ps.search(text)]
