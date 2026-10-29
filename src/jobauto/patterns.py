"""Сопоставление текста со списками слов из конфигов.

Формат шаблона одинаковый для пометок, ключевых слов Telegram и навыков:
  - обычная фраза ищется целым словом, без учёта регистра, ё = е;
    пробел и дефис внутри фразы взаимозаменяемы и необязательны
    («AI-разработчик» = «AI разработчик» = «AIразработчик»);
  - звёздочка в конце слова — любое окончание: «менеджер*» найдёт «менеджера»,
    «менеджеров», но не «Нейроменеджер» (слово должно начинаться так);
  - "re:<regex>" — регулярное выражение, тоже без учёта регистра.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

REGEX_PREFIX = "re:"


def normalize(text: str) -> str:
    """Нормализация без изменения длины строки, чтобы позиции совпадали с исходником."""
    return text.replace("ё", "е").replace("Ё", "Е")


def compile_pattern(pattern: str) -> re.Pattern[str]:
    """Компилирует шаблон из конфига. Бросает re.error на невалидной регулярке."""
    pattern = pattern.strip()
    if pattern.startswith(REGEX_PREFIX):
        body = normalize(pattern[len(REGEX_PREFIX):])
    else:
        # части фразы через пробел или дефис: «no-code» найдёт и «no code», и «nocode»
        words = [w for w in re.split(r"[\s\-‐‑–]+", normalize(pattern)) if w]
        parts = [re.escape(w[:-1]) + r"\w*" if w.endswith("*") and len(w) > 1 else re.escape(w) for w in words]
        body = r"(?<!\w)" + r"[\s\-‐‑–]*".join(parts) + r"(?!\w)"
    return re.compile(body, re.IGNORECASE)


@dataclass
class PatternSet:
    """Именованный набор шаблонов: срабатывает, если совпал хотя бы один."""

    name: str
    patterns: list[re.Pattern[str]]

    @classmethod
    def from_strings(cls, name: str, patterns: list[str]) -> "PatternSet":
        return cls(name, [compile_pattern(p) for p in patterns if p and p.strip()])

    def search(self, text: str) -> bool:
        norm = normalize(text)
        return any(p.search(norm) for p in self.patterns)

    def spans(self, text: str) -> list[tuple[int, int]]:
        norm = normalize(text)
        return [m.span() for p in self.patterns for m in p.finditer(norm) if m.end() > m.start()]


def validate(pattern: str) -> str | None:
    """Текст ошибки для невалидного шаблона или None. Для проверки в настройках."""
    try:
        compile_pattern(pattern)
    except re.error as e:
        return str(e)
    return None
