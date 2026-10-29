"""hh.ru через страницы сайта, как в браузере.

Анонимный API hh (api.hh.ru/vacancies) с апреля 2026 отвечает 403 forbidden.
Страницы сайта по-прежнему открыты без токена,
а результаты встроены в них JSON-ом (блок HH-Lux-InitialState), поэтому
разбирать вёрстку не нужно.

Поиск:   https://hh.ru/search/vacancy?text=…&area=16&area=113&work_format=REMOTE
         &order_by=publication_time&items_on_page=100&page=N
         → state.vacancySearchResult: vacancies, totalResults, paging.next.disabled
Карточка: https://hh.ru/vacancy/<id> → state.vacancyView.vacancyFull.vacancy
         (description, keySkills, status.archived, workScheduleByDays, company.category)
         Удалённая вакансия: HTTP 403/404 и state.errorPage.isError.

Особенность: если точных совпадений мало, hh добивает страницу «похожими»
вакансиями. Берём только первые totalResults позиций выдачи.
"""

from __future__ import annotations

import html as html_lib
import json
import re
import time
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import Any

import requests

from ..html_text import html_to_text
from .hh import HhBlocked, HhError

SITE = "https://hh.ru"
ITEMS_ON_PAGE = 100
MAX_DEPTH = 2000
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
STATE_RE = re.compile(r'<template[^>]*id="HH-Lux-InitialState"[^>]*>(.*?)</template>', re.S)

EXPERIENCE = {
    "noExperience": "Нет опыта",
    "between1And3": "От 1 года до 3 лет",
    "between3And6": "От 3 до 6 лет",
    "moreThan6": "Более 6 лет",
}
EMPLOYMENT_FORM = {"FULL": "Полная", "PART": "Частичная", "PROJECT": "Проект или разовое задание",
                   "FLY_IN_FLY_OUT": "Вахта", "SIDE_JOB": "Подработка"}
WORK_FORMAT = {"REMOTE": "Удалённо", "ON_SITE": "На месте работодателя", "HYBRID": "Гибрид",
               "FIELD_WORK": "Разъездной"}
SCHEDULE = {"FIVE_ON_TWO_OFF": "5/2", "SIX_ON_ONE_OFF": "6/1", "TWO_ON_TWO_OFF": "2/2",
            "THREE_ON_THREE_OFF": "3/3", "FOUR_ON_THREE_OFF": "4/3", "FOUR_ON_FOUR_OFF": "4/4",
            "FOUR_ON_TWO_OFF": "4/2", "ONE_ON_TWO_OFF": "1/2", "ONE_ON_THREE_OFF": "1/3",
            "TWO_ON_ONE_OFF": "2/1", "THREE_ON_TWO_OFF": "3/2", "WEEKEND": "по выходным",
            "FLEXIBLE": "свободный", "OTHER": "другой"}
# company.category на сайте → employer_type как в справочнике API
EMPLOYER_CATEGORY = {"COMPANY": "company", "AGENCY": "agency", "PROJECT_DIRECTOR": "project_director",
                     "PRIVATE_RECRUITER": "private_recruiter", "SELF_EMPLOYED": "self_employed",
                     "PRIVATE_PERSON": "private_individual", "PRIVATE_INDIVIDUAL": "private_individual"}


def extract_state(page: str) -> dict | None:
    m = STATE_RE.search(page)
    if not m:
        return None
    try:
        return json.loads(html_lib.unescape(m.group(1)))
    except ValueError:
        return None


def _older(published: str, since: datetime) -> bool:
    """Дата в неожиданном формате не должна ронять весь сбор: такую вакансию просто берём."""
    try:
        return datetime.fromisoformat(published) < since
    except ValueError:
        return False


def _elements(items: list | None, key: str) -> list[str]:
    """[{"workFormatsElement": ["REMOTE"]}] → ["REMOTE"]; на странице вакансии — просто ["REMOTE"]."""
    out: list[str] = []
    for x in items or []:
        if isinstance(x, dict):
            out += [str(e) for e in x.get(key) or []]
        elif x:
            out.append(str(x))
    return out


def _employer_type(company: dict) -> str | None:
    cat = company.get("@category") or company.get("category")
    return EMPLOYER_CATEGORY.get(cat, cat.lower() if isinstance(cat, str) else None)


def parse_search_item(v: dict) -> dict[str, Any]:
    comp = v.get("compensation") or {}
    company = v.get("company") or {}
    schedule = _elements(v.get("workScheduleByDays"), "workScheduleByDaysElement")
    form = v.get("employmentForm") or (v.get("employment") or {}).get("@type")
    return {
        "id": str(v["vacancyId"]),
        "name": v.get("name") or "",
        "employer_id": str(company["id"]) if company.get("id") else None,
        "employer_name": company.get("visibleName") or company.get("name"),
        "employer_type": _employer_type(company),
        "salary_from": comp.get("from"),
        "salary_to": comp.get("to"),
        "currency": comp.get("currencyCode"),
        "salary_gross": None if comp.get("gross") is None else int(bool(comp.get("gross"))),
        "area": (v.get("area") or {}).get("name"),
        "experience": EXPERIENCE.get(v.get("workExperience"), v.get("workExperience")),
        "employment_form": EMPLOYMENT_FORM.get(form, form),
        "work_format": ", ".join(WORK_FORMAT.get(f, f) for f in _elements(v.get("workFormats"), "workFormatsElement"))
        or None,
        "schedule": ", ".join(SCHEDULE.get(s, s) for s in schedule) or None,
        "url": ((v.get("links") or {}).get("desktop")) or f"{SITE}/vacancy/{v['vacancyId']}",
        "published_at": (v.get("publicationTime") or {}).get("$"),
        "snippet": "",
    }


def parse_vacancy_page(state: dict) -> dict[str, Any]:
    """Поля со страницы вакансии. Бросает KeyError, если это не страница вакансии."""
    v = state["vacancyView"]["vacancyFull"]["vacancy"]
    status = v.get("status") or {}
    company = v.get("company") or {}
    schedule = _elements(v.get("workScheduleByDays"), "workScheduleByDaysElement")
    archived = bool(status.get("archived")) or bool(status.get("disabled"))
    return {
        "description": html_to_text(v.get("description")),
        "key_skills": [str(s) for s in v.get("keySkills") or [] if s],
        "source_status": "archived" if archived else "active",
        "employer_type": _employer_type(company),
        "schedule": ", ".join(SCHEDULE.get(s, s) for s in schedule) or None,
        "previous_id": None,
        "archived_at": None,
    }


class HhWebSource:
    """Сайт hh.ru. Интерфейс тот же, что у источника через API (см. runner)."""

    name = "hh"

    def __init__(
        self,
        pause: float = 2.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 3,
    ):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self.pause = pause
        self.sleep = sleep
        self.max_retries = max_retries
        self.requests = 0
        self._last = 0.0

    def _get(self, url: str, params: Any = None) -> requests.Response:
        backoff = 5.0
        for attempt in range(self.max_retries + 1):
            delta = time.monotonic() - self._last
            if delta < self.pause:
                self.sleep(self.pause - delta)
            self._last = time.monotonic()
            self.requests += 1
            try:
                resp = self.session.get(url, params=params, timeout=30)
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise HhError(f"{url}: сеть недоступна ({e})") from e
                self.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code == 451:
                raise HhBlocked(
                    "hh.ru недоступен с этого адреса (HTTP 451): ограничение доступа по стране"
                )
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise HhBlocked(f"hh.ru не отвечает или ограничил частоту запросов (HTTP {resp.status_code})")
                self.sleep(backoff)
                backoff *= 2
                continue
            return resp
        raise HhError(f"{url}: запрос не удался")

    def search(
        self,
        text: str,
        areas: list[int],
        employment_forms: list[str],
        search_field: str | None,
        since: datetime,
        progress: Callable[[str], None] = lambda _: None,
        warnings: list[str] | None = None,
    ) -> Iterator[dict[str, Any]]:
        """Вакансии новее since, от свежих к старым. Только точные совпадения, без «похожих»."""
        params: list[tuple[str, Any]] = [("work_format", "REMOTE"), ("order_by", "publication_time"),
                                         ("items_on_page", ITEMS_ON_PAGE)]
        if text:
            params.append(("text", text))
            if search_field:
                params.append(("search_field", search_field))
        params += [("area", a) for a in areas] + [("employment_form", f) for f in employment_forms]

        for page in range(MAX_DEPTH // ITEMS_ON_PAGE):
            resp = self._get(f"{SITE}/search/vacancy", params + [("page", page)])
            state = extract_state(resp.text) if resp.status_code == 200 else None
            if state is None:
                raise HhBlocked(
                    f"hh.ru вернул страницу поиска без данных (HTTP {resp.status_code}): "
                    "капча, блокировка или изменилась вёрстка сайта"
                )
            result = state.get("vacancySearchResult") or {}
            total = int(result.get("totalResults") or 0)
            vacancies = result.get("vacancies") or []
            reached_old = False
            for pos, v in enumerate(vacancies):
                if page * ITEMS_ON_PAGE + pos >= total:
                    # дальше hh подмешивает «похожие» вакансии
                    return
                vac = parse_search_item(v)
                pub = vac["published_at"]
                if pub and _older(pub, since):
                    reached_old = True
                    break
                yield vac
            nxt = (result.get("paging") or {}).get("next") or {}
            if reached_old or not vacancies or nxt.get("disabled", True):
                return
        if warnings is not None:
            warnings.append(f"достигнут предел hh в {MAX_DEPTH} вакансий на запрос; часть старых могла не попасть")

    def detail(self, vac_id: str) -> dict[str, Any] | None:
        """Данные со страницы вакансии; None — вакансия удалена или недоступна."""
        resp = self._get(f"{SITE}/vacancy/{vac_id}")
        state = extract_state(resp.text)
        if state is not None and (state.get("errorPage") or {}).get("isError"):
            return None  # «Вам недоступна эта вакансия» — удалена или скрыта
        if resp.status_code in (404, 410):
            return None
        if resp.status_code == 403:
            raise HhBlocked(f"hh.ru отказал в доступе к странице вакансии {vac_id} (HTTP 403 без страницы hh)")
        if resp.status_code != 200 or state is None:
            raise HhError(f"вакансия {vac_id}: HTTP {resp.status_code}, страница без данных")
        try:
            return parse_vacancy_page(state)
        except KeyError as e:
            raise HhError(f"вакансия {vac_id}: на странице нет данных вакансии ({e})") from e
