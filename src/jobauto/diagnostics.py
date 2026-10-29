"""Подробная проверка доступа: всё, что влияет на ответы hh.ru.

Отчёт сохраняется в data/logs/check-ГГГГММДД-ЧЧММСС.txt.
"""

from __future__ import annotations

import platform
import socket
import sys
import urllib.request
from datetime import datetime
from pathlib import Path

import requests

from .config import Config
from .sources import boards, hh_web

HH_API = "https://api.hh.ru"
TIMEOUT = 15
SHOWN_HEADERS = ("Server", "Content-Type", "X-Request-ID", "Location", "Retry-After", "Set-Cookie")


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def section(self, title: str) -> None:
        self.lines += ["", f"=== {title} ==="]

    def add(self, text: str = "") -> None:
        self.lines.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip() + "\n"


def external_ip() -> dict:
    """IP и страна, с которых запросы уходят в интернет."""
    resp = requests.get("https://ipinfo.io/json", timeout=TIMEOUT)
    resp.raise_for_status()
    data = resp.json()
    return {k: data.get(k) for k in ("ip", "country", "city", "org")}


def quick_network_summary() -> str:
    """Короткая строка для лога сбора, когда hh отказал."""
    try:
        ip = external_ip()
        return f"внешний адрес: {ip['ip']}, страна: {ip['country']}, сеть: {ip['org']}"
    except Exception as e:  # noqa: BLE001
        return f"внешний адрес определить не удалось: {e}"


def _probe(rep: Report, title: str, url: str, headers: dict, params=None) -> requests.Response | None:
    rep.add(f"--- {title}")
    rep.add(f"GET {url}" + (f"  параметры: {params}" if params else ""))
    rep.add(f"заголовки запроса: {headers}")
    try:
        resp = requests.get(url, headers=headers, params=params, timeout=TIMEOUT, allow_redirects=False)
    except requests.RequestException as e:
        rep.add(f"ОШИБКА СЕТИ: {type(e).__name__}: {e}")
        return None
    rep.add(f"итоговый адрес: {resp.url}")
    rep.add(f"ответ: HTTP {resp.status_code} {resp.reason}  ({resp.elapsed.total_seconds():.2f} с)")
    for h in SHOWN_HEADERS:
        if h in resp.headers:
            rep.add(f"  {h}: {resp.headers[h][:200]}")
    body = resp.text.strip()
    if "json" in resp.headers.get("Content-Type", ""):
        rep.add(f"тело: {body[:800]}")
    elif body:
        rep.add(f"тело (начало): {' '.join(body[:300].split())}")
    return resp


def run_diagnostics(cfg: Config, log_dir: Path | None = None) -> tuple[str, Path | None]:
    rep = Report()
    rep.add(f"Проверка доступа {datetime.now().astimezone().isoformat(timespec='seconds')}")
    rep.add(f"Python {sys.version.split()[0]}, {platform.platform()}")
    verdict: list[str] = []

    rep.section("Прокси")
    sys_proxies = urllib.request.getproxies()
    req_proxies = requests.utils.get_environ_proxies(HH_API)
    rep.add(f"системные прокси: {sys_proxies or 'нет'}")
    rep.add(f"прокси, которые возьмёт requests для api.hh.ru: {req_proxies or 'нет'}")
    if req_proxies:
        verdict.append("запросы идут через прокси — это может менять страну")

    rep.section("Внешний адрес")
    country = None
    try:
        ip = external_ip()
        country = ip["country"]
        rep.add(f"IP: {ip['ip']}  страна: {ip['country']}  город: {ip['city']}  сеть: {ip['org']}")
        if country not in ("BY", "RU"):
            verdict.append(
                f"внешний адрес в стране {country} ({ip['org']}): hh.ru может ограничивать доступ "
                "для части стран"
            )
    except Exception as e:  # noqa: BLE001
        rep.add(f"не удалось определить: {e}")

    rep.section("DNS")
    for host in ("api.hh.ru", "hh.ru", "t.me"):
        try:
            addrs = sorted({a[4][0] for a in socket.getaddrinfo(host, 443, proto=socket.IPPROTO_TCP)})
            rep.add(f"{host}: {', '.join(addrs)}")
        except OSError as e:
            rep.add(f"{host}: не резолвится ({e})")

    rep.section("hh.ru: сайт (основной способ сбора)")
    site = _probe(rep, "главная hh.ru", "https://hh.ru/", {"User-Agent": "Mozilla/5.0"})
    search_params = [("text", "Bubble"), ("area", 16), ("area", 113), ("work_format", "REMOTE"),
                     ("order_by", "publication_time"), ("items_on_page", 20)]
    search = _probe(rep, "страница поиска", "https://hh.ru/search/vacancy", hh_web.HEADERS, search_params)
    site_ok = False
    if search is not None and search.status_code == 200:
        state = hh_web.extract_state(search.text)
        if state and "vacancySearchResult" in state:
            res = state["vacancySearchResult"]
            site_ok = True
            rep.add(f"данные в странице есть: точных совпадений {res.get('totalResults')}, "
                    f"вакансий на странице {len(res.get('vacancies') or [])}")
        else:
            rep.add("в странице НЕТ блока с данными (капча, блокировка или hh поменял вёрстку)")
            verdict.append("сайт hh отвечает, но без данных о вакансиях: капча или изменилась вёрстка")

    rep.section("hh.ru: API (для справки)")
    _probe(rep, "справочники", f"{HH_API}/dictionaries", {"User-Agent": "JobAutomation/1.0"})
    vac = None
    try:
        ua = cfg.user_agent()
    except ValueError:
        ua = None
    if ua:
        vac = _probe(rep, "поиск через API", f"{HH_API}/vacancies", {"User-Agent": ua},
                     [("text", "Bubble"), ("per_page", 1)])

    rep.section("Площадки")
    for label, url, params in (
        ("Habr Career", "https://career.habr.com/vacancies", {"q": "python", "remote": "true", "type": "all"}),
        ("Geekjob", "https://geekjob.ru/json/find/vacancy", {"page": 1}),
        ("getmatch", "https://getmatch.ru/api/offers", {"limit": 1, "offset": 0}),
    ):
        resp = _probe(rep, label, url, boards.HEADERS, params)
        if resp is None or resp.status_code != 200:
            verdict.append(f"{label} не отвечает — вакансии с неё в этот раз не соберутся")

    rep.section("Telegram")
    channel = cfg.channels[0] if cfg.channels else "telegram"
    _probe(rep, f"канал @{channel}", f"https://t.me/s/{channel}", {"User-Agent": "Mozilla/5.0"})

    if site is not None and site.status_code == 451:
        verdict.append("сайт hh.ru ответил 451 (недоступно по юридическим причинам): ограничение доступа по стране")
    if cfg.hh_mode == "api" and vac is not None and vac.status_code == 403:
        verdict.append("в настройках выбран сбор через API, а API отвечает 403: переключи режим hh на «сайт»")
    if site_ok and cfg.hh_mode == "web":
        verdict = ["hh.ru отвечает нормально"] + [v for v in verdict if "не отвечает" in v]
    elif vac is not None and vac.status_code == 200 and cfg.hh_mode == "api":
        verdict = ["hh.ru отвечает нормально"] + [v for v in verdict if "не отвечает" in v]

    rep.section("Вывод")
    rep.lines += [f"- {v}" for v in verdict] or ["- явных проблем не найдено"]

    path = None
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / f"check-{datetime.now():%Y%m%d-%H%M%S}.txt"
        path.write_text(rep.text, encoding="utf-8")
    return rep.text, path
