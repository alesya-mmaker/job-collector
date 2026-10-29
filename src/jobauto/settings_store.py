"""Чтение и запись конфигов из панели: проверка значений, сохранение
с сохранением поясняющего комментария в начале файла."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import yaml

from .config import CONFIG_DIR
from .patterns import validate
from .skills import LEVELS

FILES = {
    "settings": "settings.yaml",
    "queries": "queries.yaml",
    "channels": "channels.yaml",
    "marks": "marks.yaml",
    "skills": "skills.yaml",
    "boards": "boards.yaml",
    "exclude": "exclude.yaml",
}
API_SOURCES = {"no_salary", "private_employer", "six_day"}
BOARD_NAMES = ("habr", "geekjob", "getmatch")


class ValidationError(Exception):
    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


def _dir(config_dir: Path | None) -> Path:
    return config_dir or CONFIG_DIR


def read(name: str, config_dir: Path | None = None) -> dict[str, Any]:
    path = _dir(config_dir) / FILES[name]
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def _header(path: Path) -> str:
    if not path.exists():
        return ""
    lines = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.startswith("#") or not line.strip():
            lines.append(line)
        else:
            break
    return "\n".join(lines).rstrip() + "\n\n" if any(l.startswith("#") for l in lines) else ""


def write(name: str, data: dict[str, Any], config_dir: Path | None = None) -> dict[str, Any]:
    data = normalize(name, data)
    path = _dir(config_dir) / FILES[name]
    body = yaml.safe_dump(data, allow_unicode=True, sort_keys=False, width=1000)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(_header(path) + body, encoding="utf-8")
    tmp.replace(path)
    return data


def channel_name(raw: str) -> str:
    raw = raw.strip()
    m = re.search(r"t\.me/(?:s/)?([A-Za-z0-9_]+)", raw)
    return (m.group(1) if m else raw).lstrip("@").strip("/")


def _patterns(items: Any, where: str, errors: list[str]) -> list[str]:
    result = []
    for p in items or []:
        p = str(p).strip()
        if not p:
            continue
        err = validate(p)
        if err:
            errors.append(f"{where}: «{p}» — ошибка в регулярке: {err}")
        result.append(p)
    return result


def normalize(name: str, data: dict[str, Any]) -> dict[str, Any]:
    """Чистит значения из формы и проверяет их. Бросает ValidationError."""
    errors: list[str] = []
    if name == "channels":
        seen, channels = set(), []
        for c in data.get("channels") or []:
            n = channel_name(str(c.get("name") or ""))
            if not n:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_]{3,}", n):
                errors.append(f"канал «{n}»: имя может содержать только латиницу, цифры и _")
            if n.lower() in seen:
                continue
            seen.add(n.lower())
            channels.append({"name": n, "enabled": bool(c.get("enabled", True))})
        data = {"channels": channels, "keywords": _patterns(data.get("keywords"), "ключевое слово", errors)}

    elif name == "queries":
        directions = [str(d).strip() for d in data.get("directions") or [] if str(d).strip()]
        ids, queries = set(), []
        for q in data.get("queries") or []:
            qid = str(q.get("id") or "").strip()
            text = str(q.get("text") or "").strip()
            if not qid and not text:
                continue
            if not re.fullmatch(r"[A-Za-z0-9_\-]+", qid):
                errors.append(f"запрос «{text}»: id может содержать только латиницу, цифры, _ и -")
            if qid in ids or qid == "side":
                errors.append(f"id «{qid}» повторяется или зарезервирован")
            ids.add(qid)
            if not text:
                errors.append(f"запрос «{qid}»: пустой текст")
            direction = str(q.get("direction") or "").strip()
            if direction and direction not in directions:
                errors.append(f"запрос «{qid}»: направления «{direction}» нет в списке направлений")
            queries.append({"id": qid, "text": text, "direction": direction, "enabled": bool(q.get("enabled", True))})
        side = data.get("side_jobs") or {}
        forms = [f for f in side.get("employment_forms") or [] if f in ("FULL", "PART", "PROJECT", "FLY_IN_FLY_OUT")]
        data = {
            "directions": directions,
            "queries": queries,
            "side_jobs": {
                "enabled": bool(side.get("enabled", True)),
                "text": str(side.get("text") or "").strip(),
                "search_field": side.get("search_field") or "",
                "employment_forms": forms or ["PART", "PROJECT"],
            },
        }

    elif name == "marks":
        marks, names = [], set()
        for m in data.get("marks") or []:
            mname = str(m.get("name") or "").strip()
            if not mname:
                continue
            if mname in names:
                errors.append(f"пометка «{mname}» повторяется")
            names.add(mname)
            item: dict[str, Any] = {"name": mname, "enabled": bool(m.get("enabled", True))}
            if m.get("source"):
                if m["source"] not in API_SOURCES:
                    errors.append(f"пометка «{mname}»: неизвестный источник {m['source']}")
                item["source"] = m["source"]
            patterns = _patterns(m.get("patterns"), f"пометка «{mname}»", errors)
            if patterns or not m.get("source"):
                item["patterns"] = patterns
            marks.append(item)
        data = {"marks": marks}

    elif name == "exclude":
        rules, names = [], set()
        for r in data.get("rules") or []:
            rname = str(r.get("name") or "").strip()
            if not rname:
                continue
            if rname in names:
                errors.append(f"правило «{rname}» повторяется")
            names.add(rname)
            where = r.get("where") if r.get("where") in ("requirements", "post", "salary", "duplicate", "closed") else "title"
            patterns = _patterns(r.get("patterns"), f"правило «{rname}»", errors)
            item: dict[str, Any] = {
                "name": rname,
                "enabled": bool(r.get("enabled", True)),
                "where": where,
                "scope": r.get("scope") if r.get("scope") in ("main", "side", "all") else "all",
            }
            if where == "salary":
                try:
                    item["salary_below"] = int(r.get("salary_below") or 0)
                except (TypeError, ValueError):
                    item["salary_below"] = 0
                if item["salary_below"] <= 0:
                    errors.append(f"правило «{rname}»: укажи порог зарплаты в рублях")
            elif where in ("duplicate", "closed"):
                pass
            else:
                if not patterns:
                    errors.append(f"правило «{rname}»: нет ни одного слова")
                item["patterns"] = patterns
            unless = _patterns(r.get("unless"), f"правило «{rname}», исключения", errors)
            if unless:
                item["unless"] = unless
            if r.get("unless_no_experience"):
                item["unless_no_experience"] = True
            rules.append(item)
        data = {"rules": rules}

    elif name == "boards":
        src = data.get("boards") or {}
        out_boards = {b: {"enabled": bool((src.get(b) or {}).get("enabled", True))} for b in BOARD_NAMES}
        terms, seen = [], set()
        for t in data.get("terms") or []:
            text = str(t.get("text") or "").strip()
            if not text or text.lower() in seen:
                continue
            seen.add(text.lower())
            _patterns([text], f"слово «{text}»", errors)
            terms.append({"text": text, "direction": str(t.get("direction") or "").strip()})
        data = {"boards": out_boards, "terms": terms}

    elif name == "skills":
        skills = []
        for s in data.get("skills") or []:
            sname = str(s.get("name") or "").strip()
            if not sname:
                continue
            level = s.get("level") if s.get("level") in LEVELS else "частично"
            skills.append({"name": sname, "level": level,
                           "variants": _patterns(s.get("variants"), f"навык «{sname}»", errors)})
        data = {"skills": skills}

    elif name == "settings":
        hh = data.get("hh") or {}
        tg = data.get("telegram") or {}
        bd = data.get("boards") or {}
        dup = data.get("duplicates") or {}
        email = str(hh.get("contact_email") or "").strip()
        if email and not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
            errors.append("почта для hh.ru выглядит неправильно")

        def num(v, lo, hi, label, cast=float):
            try:
                x = cast(v)
            except (TypeError, ValueError):
                errors.append(f"{label}: нужно число")
                return lo
            if not lo <= x <= hi:
                errors.append(f"{label}: от {lo} до {hi}")
            return x

        areas = []
        for a in hh.get("areas") or []:
            try:
                areas.append(int(a))
            except (TypeError, ValueError):
                errors.append(f"регион «{a}»: нужен числовой id")
        if not areas:
            errors.append("нужен хотя бы один регион")
        data = {
            "hh": {
                "contact_email": email,
                "app_name": str(hh.get("app_name") or "JobAutomation/1.0"),
                "mode": "api" if hh.get("mode") == "api" else "web",
                "web_pause_seconds": num(hh.get("web_pause_seconds", 2.0), 0.5, 30, "пауза сайта hh"),
                "areas": areas,
                "host": str(hh.get("host") or "hh.ru"),
                "pause_seconds": num(hh.get("pause_seconds", 1.0), 0.2, 30, "пауза hh"),
                "initial_lookback_days": num(hh.get("initial_lookback_days", 7), 1, 30, "дней при первом запуске", int),
                "overlap_minutes": num(hh.get("overlap_minutes", 60), 0, 1440, "запас, минут", int),
                "recheck_every_days": num(hh.get("recheck_every_days", 3), 1, 60, "перепроверка, дней", int),
            },
            "telegram": {
                "pause_seconds": num(tg.get("pause_seconds", 1.0), 0, 30, "пауза Telegram"),
                "initial_lookback_days": num(tg.get("initial_lookback_days", 7), 1, 365, "дней Telegram", int),
            },
            "boards": {
                "pause_seconds": num(bd.get("pause_seconds", 1.5), 0.2, 30, "пауза площадок"),
                "initial_lookback_days": num(bd.get("initial_lookback_days", 7), 1, 60, "дней площадок", int),
            },
            "duplicates": {
                "title_similarity": num(dup.get("title_similarity", 0.85), 0.5, 1.0, "похожесть названий"),
            },
        }
    else:
        raise KeyError(name)

    if errors:
        raise ValidationError(errors)
    return data
