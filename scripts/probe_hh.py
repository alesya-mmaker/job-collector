"""Разовая проба страниц сайта hh.ru: что приходит и какие там поля.

Сохраняет всё в data/probe/<время>/:
  summary.txt            — итог: коды ответов, найденные данные, список полей
  <шаг>.html             — страница как есть
  <шаг>.state.json       — встроенный в страницу JSON (блок HH-Lux-InitialState)

Запуск: двойной клик по probe.bat.
"""

from __future__ import annotations

import html
import json
import re
import sys
import time
from datetime import datetime
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "data" / "probe" / datetime.now().strftime("%Y%m%d-%H%M%S")
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
STATE_RE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)
SEARCH = "https://hh.ru/search/vacancy"
BASE = [("area", 16), ("area", 113), ("work_format", "REMOTE"), ("order_by", "publication_time"),
        ("items_on_page", 100)]

lines: list[str] = []


def log(text: str = "") -> None:
    print(text)
    lines.append(text)


def keys_tree(obj, depth: int = 2, indent: str = "  ") -> list[str]:
    """Список полей объекта с типами, на пару уровней вглубь."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            kind = type(v).__name__
            sample = ""
            if isinstance(v, (str, int, float, bool)) or v is None:
                sample = f" = {json.dumps(v, ensure_ascii=False)[:80]}"
            elif isinstance(v, list):
                kind = f"list[{len(v)}]"
            out.append(f"{indent}{k}: {kind}{sample}")
            if depth > 1 and isinstance(v, dict):
                out += keys_tree(v, depth - 1, indent + "  ")
            if depth > 1 and isinstance(v, list) and v and isinstance(v[0], dict):
                out.append(f"{indent}  [0]:")
                out += keys_tree(v[0], depth - 1, indent + "    ")
    return out


def fetch(name: str, url: str, params=None) -> dict | None:
    log(f"\n=== {name}")
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=30)
    except requests.RequestException as e:
        log(f"ОШИБКА СЕТИ: {e}")
        return None
    log(f"GET {r.url}")
    log(f"ответ: HTTP {r.status_code}, {len(r.text)} символов, сервер: {r.headers.get('Server')}")
    (OUT / f"{name}.html").write_text(r.text, encoding="utf-8")
    m = STATE_RE.search(r.text)
    if not m:
        log("блок HH-Lux-InitialState НЕ найден (страница-заглушка, капча или другая вёрстка)")
        title = re.search(r"<title[^>]*>(.*?)</title>", r.text, re.S)
        log(f"заголовок страницы: {title.group(1).strip()[:150] if title else '—'}")
        return None
    try:
        state = json.loads(html.unescape(m.group(1)))
    except ValueError as e:
        log(f"блок найден, но JSON не разобран: {e}")
        return None
    (OUT / f"{name}.state.json").write_text(json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8")
    log(f"блок найден, полей верхнего уровня: {len(state)}")
    return state


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    log(f"Проба hh.ru {datetime.now().astimezone().isoformat(timespec='seconds')}")
    try:
        ip = requests.get("https://ipinfo.io/json", timeout=15).json()
        log(f"внешний адрес: страна {ip.get('country')}, {ip.get('city')}, {ip.get('org')}")
        if ip.get("country") not in ("BY", "RU"):
            log("ВНИМАНИЕ: адрес не белорусский и не российский, hh может ответить 451.")
    except Exception as e:  # noqa: BLE001
        log(f"внешний адрес определить не удалось: {e}")

    first_id = None
    s1 = fetch("1_search_bubble_p0", SEARCH, BASE + [("text", "Bubble"), ("page", 0)])
    if s1:
        res = s1.get("vacancySearchResult") or {}
        vacs = res.get("vacancies") or []
        log(f"vacancySearchResult: {len(vacs)} вакансий на странице")
        log("поля vacancySearchResult:")
        lines.extend(keys_tree({k: v for k, v in res.items() if k != "vacancies"}, depth=1))
        if vacs:
            first_id = vacs[0].get("vacancyId")
            log("поля одной вакансии в поиске:")
            lines.extend(keys_tree(vacs[0], depth=3))
        log("ключи верхнего уровня страницы: " + ", ".join(sorted(s1)))

    time.sleep(2)
    s2 = fetch("2_search_nocode_p1", SEARCH, BASE + [("text", "no-code OR nocode OR low-code"), ("page", 1)])
    if s2:
        vacs = (s2.get("vacancySearchResult") or {}).get("vacancies") or []
        log(f"вторая страница: {len(vacs)} вакансий; первые id: {[v.get('vacancyId') for v in vacs[:3]]}")

    time.sleep(2)
    s3 = fetch(
        "3_search_side",
        SEARCH,
        [("area", 16), ("area", 113), ("work_format", "REMOTE"), ("order_by", "publication_time"),
         ("items_on_page", 20), ("text", "оператор данных OR модератор OR ассистент"),
         ("search_field", "name"), ("employment_form", "PART"), ("employment_form", "PROJECT")],
    )
    if s3:
        vacs = (s3.get("vacancySearchResult") or {}).get("vacancies") or []
        log(f"подработка: {len(vacs)} вакансий; названия: {[v.get('name') for v in vacs[:5]]}")

    if first_id:
        time.sleep(2)
        v = fetch("4_vacancy", f"https://hh.ru/vacancy/{first_id}")
        if v:
            view = v.get("vacancyView") or {}
            log("поля vacancyView:")
            lines.extend(keys_tree(view, depth=2))

    time.sleep(2)
    old = fetch("5_vacancy_old", "https://hh.ru/vacancy/8331228")
    if old:
        view = old.get("vacancyView") or {}
        log("поля vacancyView у старой (скорее всего архивной) вакансии:")
        lines.extend(keys_tree(view, depth=1))

    (OUT / "summary.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nГотово. Папка с результатами: {OUT}")
    return 0


if __name__ == "__main__":
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    raise SystemExit(main())
