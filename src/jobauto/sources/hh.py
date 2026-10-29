"""Клиент API hh.ru и разбор ответов.

Параметры сверены со спекой https://api.hh.ru/openapi/specification/public:
  GET /vacancies — text, area (повторяется), work_format, employment_form,
      date_from/date_to (нельзя вместе с period, округляются до 5 минут),
      order_by, no_magic, per_page (≤100), page.
      Глубина выдачи ≤ 2000 (per_page * (page + 1)).
  GET /vacancies/{id} — key_skills, description (HTML), archived, previous_id.
      404 — вакансия удалена или недоступна.
  GET /employers/{id} — type из справочника employer_type.
"""

from __future__ import annotations

import time
from collections.abc import Callable, Iterator
from datetime import datetime, timedelta
from typing import Any

import requests

from ..html_text import html_to_text, strip_highlight

BASE_URL = "https://api.hh.ru"
PER_PAGE = 100
MAX_DEPTH = 2000
MIN_WINDOW = timedelta(minutes=10)


class HhBlocked(Exception):
    """hh перестал отвечать: капча, 403, бан User-Agent, затяжные 429. Сбор hh надо остановить."""


class HhError(Exception):
    """Разовая ошибка запроса (после повторов)."""


class HhClient:
    def __init__(
        self,
        user_agent: str,
        pause: float = 1.0,
        session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        max_retries: int = 4,
    ):
        self.session = session or requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept": "application/json"})
        self.pause = pause
        self.sleep = sleep
        self.max_retries = max_retries
        self.requests = 0
        self._last = 0.0

    def _wait(self) -> None:
        delta = time.monotonic() - self._last
        if delta < self.pause:
            self.sleep(self.pause - delta)
        self._last = time.monotonic()

    def get(self, path: str, params: Any = None) -> dict | None:
        """JSON ответа; None, если 404."""
        backoff = 5.0
        for attempt in range(self.max_retries + 1):
            self._wait()
            self.requests += 1
            try:
                resp = self.session.get(BASE_URL + path, params=params, timeout=30)
            except requests.RequestException as e:
                if attempt == self.max_retries:
                    raise HhError(f"{path}: сеть недоступна ({e})") from e
                self.sleep(backoff)
                backoff *= 2
                continue

            if resp.status_code == 200:
                return resp.json()
            if resp.status_code == 404:
                return None
            if resp.status_code in (400, 403):
                raise_if_blocked(resp)
                raise HhError(f"{path}: {describe_response(resp)}")
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    if resp.status_code == 429:
                        raise HhBlocked(
                            f"hh.ru ограничил частоту запросов, сбор hh остановлен. {describe_response(resp)}"
                        )
                    raise HhError(f"{path}: {describe_response(resp)}")
                self.sleep(backoff)
                backoff *= 2
                continue
            raise HhError(f"{path}: {describe_response(resp)}")
        raise HhError(f"{path}: запрос не удался")


def describe_response(resp: requests.Response) -> str:
    """Всё, что пригодится для разбора отказа: код, тело, request_id, сервер, адрес."""
    headers = getattr(resp, "headers", None) or {}
    request_id = headers.get("X-Request-ID") or ""
    try:
        request_id = request_id or resp.json().get("request_id", "")
    except (ValueError, AttributeError):
        pass
    parts = [f"HTTP {resp.status_code}", f"ответ: {(resp.text or '').strip()[:500]}"]
    if request_id:
        parts.append(f"request_id: {request_id}")
    if headers.get("Server"):
        parts.append(f"сервер: {headers['Server']}")
    if getattr(resp, "url", None):
        parts.append(f"адрес: {resp.url}")
    return "; ".join(parts)


def raise_if_blocked(resp: requests.Response) -> None:
    try:
        errors = resp.json().get("errors") or []
    except ValueError:
        errors = []
    types = {(e.get("type"), e.get("value")) for e in errors}
    detail = describe_response(resp)
    if any(t == "captcha_required" for t, _ in types):
        raise HhBlocked(f"hh.ru просит пройти капчу, сбор hh остановлен. {detail}")
    if any(t == "bad_user_agent" for t, _ in types):
        raise HhBlocked(f"hh.ru отклонил User-Agent: проверь почту в настройках. {detail}")
    if resp.status_code == 403:
        raise HhBlocked(
            f"hh.ru отказал в доступе (HTTP 403), подробности в отчёте проверки. {detail}"
        )


def format_date(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def search_params(
    text: str, areas: list[int], host: str, employment_forms: list[str], search_field: str | None = None
) -> list[tuple[str, Any]]:
    params: list[tuple[str, Any]] = [
        ("work_format", "REMOTE"),
        ("order_by", "publication_time"),
        ("no_magic", "true"),
        ("per_page", PER_PAGE),
        ("host", host),
    ]
    if text:
        params.append(("text", text))
        if search_field:
            params.append(("search_field", search_field))
    params += [("area", a) for a in areas]
    params += [("employment_form", f) for f in employment_forms]
    return params


def search(
    client: HhClient,
    base_params: list[tuple[str, Any]],
    start: datetime,
    end: datetime,
    progress: Callable[[str], None] = lambda _: None,
    warnings: list[str] | None = None,
) -> Iterator[dict]:
    """Все элементы выдачи за [start, end]. Если найдено больше 2000, окно делится пополам."""
    first = _page(client, base_params, start, end, 0)
    found = first.get("found", 0)
    if found > MAX_DEPTH and end - start > MIN_WINDOW:
        mid = start + (end - start) / 2
        progress(f"  найдено {found} > {MAX_DEPTH}, делю период {start:%d.%m %H:%M}–{end:%d.%m %H:%M}")
        yield from search(client, base_params, start, mid, progress, warnings)
        yield from search(client, base_params, mid, end, progress, warnings)
        return
    if found > MAX_DEPTH and warnings is not None:
        warnings.append(
            f"за {format_date(start)}–{format_date(end)} найдено {found}, взяты первые {MAX_DEPTH}"
        )
    yield from first.get("items", [])
    pages = min(first.get("pages", 1), MAX_DEPTH // PER_PAGE)
    for page in range(1, pages):
        data = _page(client, base_params, start, end, page)
        items = data.get("items", [])
        if not items:
            break
        yield from items


def _page(client: HhClient, base_params, start: datetime, end: datetime, page: int) -> dict:
    params = base_params + [("date_from", format_date(start)), ("date_to", format_date(end)), ("page", page)]
    data = client.get("/vacancies", params)
    if data is None:
        raise HhError("/vacancies вернул 404")
    return data


def _name(obj: Any) -> str | None:
    return obj.get("name") if isinstance(obj, dict) else None


def parse_common(item: dict) -> dict[str, Any]:
    """Поля, которые есть и в выдаче поиска, и в карточке вакансии."""
    salary = item.get("salary_range") or item.get("salary") or {}
    employer = item.get("employer") or {}
    return {
        "id": str(item["id"]),
        "name": item.get("name") or "",
        "employer_id": str(employer["id"]) if employer.get("id") else None,
        "employer_name": employer.get("name"),
        "salary_from": salary.get("from"),
        "salary_to": salary.get("to"),
        "currency": salary.get("currency"),
        "salary_gross": None if salary.get("gross") is None else int(bool(salary.get("gross"))),
        "area": _name(item.get("area")),
        "experience": _name(item.get("experience")),
        "employment_form": _name(item.get("employment_form")) or _name(item.get("employment")),
        "work_format": ", ".join(filter(None, (_name(w) for w in item.get("work_format") or []))) or None,
        "url": item.get("alternate_url"),
        "published_at": item.get("published_at"),
    }


def parse_search_item(item: dict) -> dict[str, Any]:
    vac = parse_common(item)
    snippet = item.get("snippet") or {}
    vac["snippet"] = "\n".join(
        filter(None, [strip_highlight(snippet.get("requirement")), strip_highlight(snippet.get("responsibility"))])
    )
    return vac


def parse_detail(data: dict) -> dict[str, Any]:
    vac = parse_common(data)
    archived_at = data.get("archived_at")
    vac.update(
        {
            "key_skills": [s["name"] for s in data.get("key_skills") or [] if s.get("name")],
            "description": html_to_text(data.get("description")),
            "previous_id": str(data["previous_id"]) if data.get("previous_id") else None,
            "source_status": source_status(data),
            "archived_at": archived_at if isinstance(archived_at, str) else None,
        }
    )
    return vac


def source_status(data: dict | None) -> str:
    """active | archived | deleted по карточке вакансии (None = 404)."""
    if data is None:
        return "deleted"
    if data.get("hidden"):
        return "deleted"
    if data.get("archived"):
        return "archived"
    return "active"
