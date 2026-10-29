"""Поддельный hh.ru для тестов: фильтрует по тексту запроса, датам и занятости,
соблюдает пагинацию и ограничение глубины 2000."""

from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from jobauto.sources.hh import HhBlocked, HhError

TZ = timezone(timedelta(hours=3))


def ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S%z")


def item(vid, name, published: datetime, employer_id="e1", employer_name="Ромашка", query="Bubble",
         forms=("FULL",), salary=None, snippet="", **detail):
    return {
        "id": str(vid),
        "name": name,
        "employer": {"id": employer_id, "name": employer_name} if employer_id else {"name": employer_name},
        "published_at": ts(published),
        "alternate_url": f"https://hh.ru/vacancy/{vid}",
        "area": {"name": "Минск"},
        "experience": {"name": "От 1 года до 3 лет"},
        "employment_form": {"id": forms[0], "name": "Полная" if forms[0] == "FULL" else "Частичная"},
        "salary_range": salary,
        "snippet": {"requirement": snippet, "responsibility": None},
        "_query": query,
        "_forms": set(forms),
        "_detail": detail,
    }


class FakeHh:
    def __init__(self, items, employers=None, deleted=(), archived=(), block_after=None):
        self.items = items
        self.employers = employers or {}
        self.deleted = set(deleted)
        self.archived = set(archived)
        self.block_after = block_after
        self.requests = 0
        self.calls: list[tuple[str, dict]] = []

    def get(self, path, params=None):
        self.requests += 1
        p: dict = {}
        for k, v in params or []:
            p.setdefault(k, []).append(v)
        self.calls.append((path, p))
        if self.block_after is not None and self.requests > self.block_after:
            raise HhBlocked("hh.ru просит пройти капчу, сбор hh остановлен")

        if path == "/vacancies":
            text = (p.get("text") or [""])[0]
            forms = set(p.get("employment_form") or [])
            df = datetime.strptime(p["date_from"][0], "%Y-%m-%dT%H:%M:%S%z")
            dt = datetime.strptime(p["date_to"][0], "%Y-%m-%dT%H:%M:%S%z")
            per_page, page = int(p["per_page"][0]), int(p["page"][0])
            if per_page * (page + 1) > 2000:
                raise HhError("depth limit")
            found = [
                it for it in self.items
                if (not text or it["_query"] == text)
                and (not forms or it["_forms"] & forms)
                and df <= datetime.strptime(it["published_at"], "%Y-%m-%dT%H:%M:%S%z") <= dt
            ]
            found.sort(key=lambda it: it["published_at"], reverse=True)
            public = [{k: v for k, v in it.items() if not k.startswith("_")} for it in found]
            return {
                "found": len(found),
                "pages": max(1, math.ceil(len(found) / per_page)),
                "items": public[page * per_page:(page + 1) * per_page],
            }
        if path.startswith("/vacancies/"):
            vid = path.rsplit("/", 1)[1]
            if vid in self.deleted:
                return None
            it = next((i for i in self.items if i["id"] == vid), None)
            if it is None:
                return None
            data = {k: v for k, v in it.items() if not k.startswith("_")}
            data.update({"description": "<p>Описание</p>", "key_skills": [], "archived": vid in self.archived})
            data.update(it["_detail"])
            return data
        if path.startswith("/employers/"):
            eid = path.rsplit("/", 1)[1]
            return {"id": eid, "name": "x", "type": self.employers.get(eid, "company")}
        raise AssertionError(path)
