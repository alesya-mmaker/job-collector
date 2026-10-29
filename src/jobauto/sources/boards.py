"""Русскоязычные IT-площадки: Habr Career, Geekjob, getmatch.

Все три читаются без входа и ключей. Как устроены (проверено 21.10.2026):
  Habr Career — страница поиска career.habr.com/vacancies?q=…&remote=true&sort=date,
      данные в JSON-блоке <script data-ssr-state="true">: vacancies.list, vacancies.meta.
      Ищет на своей стороне. Описание — на странице вакансии, блок ld+json.
  Geekjob — geekjob.ru/json/find/vacancy?page=N. Параметр q игнорирует и отдаёт ленту
      последних вакансий, поэтому слова ищем сами, по названию.
  getmatch — getmatch.ru/api/offers?limit&offset. Тоже отдаёт все вакансии целиком,
      вместе с описанием; слова ищем сами по названию, описанию и навыкам.

Идентификаторы в базе — с префиксом площадки: "habr:123", "geekjob:abc", "getmatch:45".
"""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable
from datetime import date, datetime
from typing import Any

import requests

from ..html_text import html_to_text

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
}
RU_MONTHS = {"января": 1, "февраля": 2, "марта": 3, "апреля": 4, "мая": 5, "июня": 6, "июля": 7,
             "августа": 8, "сентября": 9, "октября": 10, "ноября": 11, "декабря": 12}
EMPLOYMENT = {"full_time": "Полная", "part_time": "Частичная", "project": "Проект или разовое задание"}


class BoardError(Exception):
    pass


class Board:
    """Общее: пауза между запросами, повторы, счётчик."""

    name = ""
    label = ""
    server_search = False  # True — площадка ищет по словам сама

    def __init__(self, pause: float = 1.5, session: requests.Session | None = None,
                 sleep: Callable[[float], None] = time.sleep):
        self.session = session or requests.Session()
        self.session.headers.update(HEADERS)
        self.pause = pause
        self.sleep = sleep
        self.requests = 0
        self._last = 0.0

    def _get(self, url: str, **kwargs) -> requests.Response:
        backoff = 3.0
        for attempt in range(3):
            delta = time.monotonic() - self._last
            if delta < self.pause:
                self.sleep(self.pause - delta)
            self._last = time.monotonic()
            self.requests += 1
            try:
                resp = self.session.get(url, timeout=30, **kwargs)
            except requests.RequestException as e:
                if attempt == 2:
                    raise BoardError(f"{self.label}: сеть недоступна ({type(e).__name__})") from e
                self.sleep(backoff)
                backoff *= 2
                continue
            if resp.status_code in (429, 500, 502, 503, 504) and attempt < 2:
                self.sleep(backoff)
                backoff *= 2
                continue
            return resp
        raise BoardError(f"{self.label}: запрос не удался")

    def vac_id(self, native: Any) -> str:
        return f"{self.name}:{native}"


def older_than(published: str | None, since: datetime) -> bool:
    """Опубликована раньше since. Дата без часового пояса (так отдаёт getmatch) считается местной."""
    if not published:
        return False
    try:
        dt = datetime.fromisoformat(published.replace("Z", "+00:00"))
    except ValueError:
        return False
    if dt.tzinfo is None:
        dt = dt.astimezone()
    if since.tzinfo is None:
        since = since.astimezone()
    return dt < since


def ru_date(text: str, today: date | None = None) -> str | None:
    """'21 октября' → '2026-10-21' (текущий или прошлый год)."""
    m = re.search(r"(\d{1,2})\s+([а-яё]+)", (text or "").lower())
    if not m or m.group(2) not in RU_MONTHS:
        return None
    today = today or date.today()
    d = date(today.year, RU_MONTHS[m.group(2)], int(m.group(1)))
    if d > today:
        d = d.replace(year=today.year - 1)
    return datetime(d.year, d.month, d.day).astimezone().isoformat()


def parse_salary_text(text: str) -> tuple[int | None, int | None, str | None]:
    """'от 250 000 до 270 000 ₽' → (250000, 270000, 'RUR')."""
    text = (text or "").strip()
    if not text:
        return None, None, None
    nums = [int(re.sub(r"\D", "", n)) for n in re.findall(r"\d[\d \xa0 ]*\d|\d", text)]
    cur = "USD" if "$" in text else "EUR" if "€" in text else "RUR"
    low = text.lower()
    if low.startswith("от") and "до" in low and len(nums) >= 2:
        return nums[0], nums[1], cur
    if low.startswith("от") and nums:
        return nums[0], None, cur
    if low.startswith("до") and nums:
        return None, nums[0], cur
    if len(nums) >= 2:
        return nums[0], nums[1], cur
    if nums:
        return nums[0], nums[0], cur
    return None, None, None


# ---------------------------------------------------------------- Habr Career

HABR = "https://career.habr.com"
SSR_RE = re.compile(r'<script[^>]*data-ssr-state="true"[^>]*>(.*?)</script>', re.S)
LD_RE = re.compile(r'<script type="application/ld\+json">(.*?)</script>', re.S)


class HabrBoard(Board):
    name = "habr"
    label = "Habr Career"
    server_search = True
    max_pages = 2

    def search(self, term: str, since: datetime) -> list[dict[str, Any]]:
        out = []
        for page in range(1, self.max_pages + 1):
            resp = self._get(f"{HABR}/vacancies",
                             params={"q": term, "remote": "true", "type": "all", "sort": "date", "page": page})
            if resp.status_code != 200:
                raise BoardError(f"{self.label}: HTTP {resp.status_code}")
            m = SSR_RE.search(resp.text)
            if not m:
                raise BoardError(f"{self.label}: на странице нет блока данных — изменилась вёрстка")
            data = json.loads(m.group(1)).get("vacancies") or {}
            items = data.get("list") or []
            reached_old = False
            for it in items:
                vac = self.parse_item(it)
                if older_than(vac["published_at"], since):
                    reached_old = True
                    continue
                out.append(vac)
            meta = data.get("meta") or {}
            if reached_old or not items or page >= int(meta.get("totalPages") or 0):
                break
        return out

    def parse_item(self, it: dict) -> dict[str, Any]:
        sal = it.get("salary") or {}
        company = it.get("company") or {}
        cur = sal.get("currency")
        return {
            "id": self.vac_id(it["id"]),
            "name": it.get("title") or "",
            "employer_id": None,
            "employer_name": company.get("title"),
            "salary_from": sal.get("from"),
            "salary_to": sal.get("to"),
            "currency": {"rur": "RUR", "rub": "RUR"}.get(str(cur).lower(), cur.upper() if cur else None),
            "area": "Удалённо" if it.get("remoteWork") else None,
            "experience": (it.get("salaryQualification") or {}).get("title"),
            "employment_form": EMPLOYMENT.get(it.get("employment"), it.get("employment")),
            "work_format": "Удалённо" if it.get("remoteWork") else None,
            "url": HABR + (it.get("href") or f"/vacancies/{it['id']}"),
            "published_at": (it.get("publishedDate") or {}).get("date"),
            "key_skills": [s.get("title") for s in it.get("skills") or [] if s.get("title")],
            "snippet": "",
            "source_status": "archived" if it.get("archived") else "active",
        }

    def detail(self, vac_id: str) -> dict[str, Any] | None:
        native = vac_id.split(":", 1)[1]
        resp = self._get(f"{HABR}/vacancies/{native}")
        if resp.status_code in (404, 410):
            return None
        if resp.status_code != 200:
            raise BoardError(f"{self.label}: вакансия {native}, HTTP {resp.status_code}")
        for blob in LD_RE.findall(resp.text):
            try:
                data = json.loads(blob)
            except ValueError:
                continue
            if isinstance(data, dict) and data.get("description"):
                return {"description": html_to_text(data["description"]), "source_status": "active"}
        return {"description": "", "source_status": "active"}


# --------------------------------------------------------------------- Geekjob

GEEKJOB = "https://geekjob.ru"


class GeekjobBoard(Board):
    name = "geekjob"
    label = "Geekjob"
    max_pages = 3

    def feed(self, since: datetime) -> list[dict[str, Any]]:
        out = []
        for page in range(1, self.max_pages + 1):
            resp = self._get(f"{GEEKJOB}/json/find/vacancy", params={"page": page})
            if resp.status_code != 200:
                raise BoardError(f"{self.label}: HTTP {resp.status_code}")
            data = resp.json()
            for v in data.get("data") or []:
                fmt = v.get("jobFormat") or {}
                if not fmt.get("remote"):
                    continue
                vac = self.parse_item(v)
                if older_than(vac["published_at"], since.replace(hour=0, minute=0, second=0, microsecond=0)):
                    continue
                out.append(vac)
            if page >= int(data.get("pagecount") or 1):
                break
        return out

    def parse_item(self, v: dict) -> dict[str, Any]:
        lo, hi, cur = parse_salary_text(v.get("salary") if v.get("salary") not in (None, "None") else "")
        fmt = v.get("jobFormat") or {}
        place = ", ".join(x for x in (v.get("city"), v.get("country")) if x and str(x).lower() not in ("none", "null"))
        return {
            "id": self.vac_id(v["id"]),
            "name": v.get("position") or "",
            "employer_id": None,
            "employer_name": (v.get("company") or {}).get("name"),
            "salary_from": lo,
            "salary_to": hi,
            "currency": cur,
            "area": place or "Удалённо",
            "employment_form": "Частичная" if fmt.get("parttime") else None,
            "work_format": "Удалённо",
            "url": f"{GEEKJOB}/vacancy/{v['id']}",
            "published_at": ru_date((v.get("log") or {}).get("modify", "")),
            "snippet": "",
        }

    def detail(self, vac_id: str) -> dict[str, Any] | None:
        native = vac_id.split(":", 1)[1]
        resp = self._get(f"{GEEKJOB}/vacancy/{native}")
        if resp.status_code in (404, 410):
            return None
        if resp.status_code != 200:
            raise BoardError(f"{self.label}: вакансия {native}, HTTP {resp.status_code}")
        text = html_to_text(re.sub(r"(?s)<!--.*?-->|<script.*?</script>|<style.*?</style>", " ", resp.text))
        m = re.search(r"(?s)(О вакансии|Описание|Обязанности|Задачи|О проекте|Чем предстоит заниматься).*", text)
        text = m.group(0) if m else text
        cut = re.search(r"Похожие вакансии|Другие вакансии|Вакансии компании", text)
        return {"description": (text[:cut.start()] if cut else text).strip()[:20000], "source_status": "active"}


# -------------------------------------------------------------------- getmatch

GETMATCH = "https://getmatch.ru"


class GetmatchBoard(Board):
    name = "getmatch"
    label = "getmatch"
    max_offers = 3000

    def feed(self, since: datetime) -> list[dict[str, Any]]:
        out, offset, total = [], 0, None
        while (total is None or offset < total) and offset < self.max_offers:
            resp = self._get(f"{GETMATCH}/api/offers", params={"limit": 100, "offset": offset})
            if resp.status_code != 200:
                raise BoardError(f"{self.label}: HTTP {resp.status_code}")
            data = resp.json()
            total = int((data.get("meta") or {}).get("total") or 0)
            for o in data.get("offers") or []:
                if not o.get("is_active", True):
                    continue
                remote = [i for i in o.get("location_items") or [] if i.get("format") == "remote" and not i.get("exclude")]
                if not remote:
                    continue
                vac = self.parse_item(o, remote)
                if older_than(vac["published_at"], since):
                    continue
                out.append(vac)
            offset += 100
        return out

    def parse_item(self, o: dict, remote: list[dict]) -> dict[str, Any]:
        hidden = o.get("salary_hidden")
        cur = o.get("salary_currency")
        body = o.get("description_html") or o.get("description") or o.get("offer_description") or ""
        return {
            "id": self.vac_id(o["id"]),
            "name": o.get("position") or "",
            "employer_id": None,
            "employer_name": (o.get("company") or {}).get("name"),
            "salary_from": None if hidden else o.get("salary_display_from"),
            "salary_to": None if hidden else o.get("salary_display_to"),
            "currency": None if hidden else ({"RUB": "RUR"}.get(cur, cur) if cur else "RUR"),
            "area": ", ".join(i.get("label", "") for i in remote if i.get("label")) or "Удалённо",
            "experience": o.get("seniority"),
            "work_format": "Удалённо",
            "url": GETMATCH + (o.get("url") or f"/vacancies/{o['id']}"),
            "published_at": o.get("published_at"),
            "key_skills": [s.get("name", "") if isinstance(s, dict) else str(s) for s in o.get("skills_objects") or []],
            "description": html_to_text(body),
            "snippet": "",
        }

    def detail(self, vac_id: str) -> dict[str, Any] | None:
        native = vac_id.split(":", 1)[1]
        resp = self._get(f"{GETMATCH}/api/offers/{native}")
        if resp.status_code in (404, 410):
            return None
        if resp.status_code != 200:
            raise BoardError(f"{self.label}: вакансия {native}, HTTP {resp.status_code}")
        o = resp.json()
        parts = [html_to_text(o.get("offer_description") or ""), html_to_text(o.get("description") or "")]
        status = "active" if o.get("is_active", True) else "archived"
        return {"description": "\n\n".join(p for p in parts if p), "source_status": status}


BOARDS = {b.name: b for b in (HabrBoard, GeekjobBoard, GetmatchBoard)}
