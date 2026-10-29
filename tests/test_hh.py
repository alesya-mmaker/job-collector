import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest
import requests

from fakes import TZ, FakeHh, item
from jobauto.sources import hh

FIX = Path(__file__).parent / "fixtures"


def test_parse_search_fixture():
    data = json.loads((FIX / "hh_search.json").read_text(encoding="utf-8"))
    v = hh.parse_search_item(data["items"][0])
    assert v["id"] == "8331228"
    assert v["employer_id"] == "1455" and v["employer_name"] == "HeadHunter"
    assert v["salary_from"] == 30000 and v["salary_to"] is None and v["currency"] == "RUR"
    assert v["area"] == "Москва"
    assert v["url"] == "https://hh.ru/vacancy/8331228"
    assert "<" not in v["snippet"] and "секретаря" in v["snippet"]


def test_parse_detail_fixture():
    data = json.loads((FIX / "hh_vacancy.json").read_text(encoding="utf-8"))
    v = hh.parse_detail(data)
    assert v["key_skills"] == ["Прием посетителей", "Первичный документооборот"]
    assert v["description"] == "Работа хороша"
    assert v["source_status"] == "active"
    assert v["previous_id"] is None


def test_source_status():
    assert hh.source_status(None) == "deleted"
    assert hh.source_status({"archived": True}) == "archived"
    assert hh.source_status({"archived": False, "hidden": True}) == "deleted"
    assert hh.source_status({"archived": False}) == "active"


def test_search_params_repeat_area_and_forms():
    p = hh.search_params("Bubble", [16, 113], "hh.ru", ["PART", "PROJECT"])
    assert ("area", 16) in p and ("area", 113) in p
    assert ("employment_form", "PART") in p and ("employment_form", "PROJECT") in p
    assert ("work_format", "REMOTE") in p and ("no_magic", "true") in p
    assert not any(k == "period" for k, _ in p)


def test_search_splits_window_over_2000():
    end = datetime(2026, 10, 19, 12, tzinfo=TZ)
    items = [item(i, f"v{i}", end - timedelta(minutes=i)) for i in range(4500)]
    client = FakeHh(items)
    params = hh.search_params("Bubble", [16], "hh.ru", [])
    got = {it["id"] for it in hh.search(client, params, end - timedelta(days=4), end)}
    assert got == {str(i) for i in range(4500)}
    assert all(int(c[1]["page"][0]) < 20 for c in client.calls)


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = json.dumps(body)

    def json(self):
        return self._body


class _Session:
    def __init__(self, responses):
        self.responses = list(responses)
        self.headers = {}

    def get(self, *a, **kw):
        return self.responses.pop(0)


def _client(*responses):
    return hh.HhClient("t (x@y.z)", pause=0, session=_Session(responses), sleep=lambda s: None)


def test_client_blocked_on_captcha_and_forbidden():
    with pytest.raises(hh.HhBlocked, match="капчу"):
        _client(_Resp(403, {"errors": [{"type": "captcha_required"}]})).get("/vacancies")
    with pytest.raises(hh.HhBlocked, match="403"):
        _client(_Resp(403, {"errors": [{"type": "forbidden"}]})).get("/vacancies")
    with pytest.raises(hh.HhBlocked, match="User-Agent"):
        _client(_Resp(400, {"errors": [{"type": "bad_user_agent", "value": "blacklisted"}]})).get("/vacancies")


def test_client_retries_429_then_ok_and_404_is_none():
    c = _client(_Resp(429, {}), _Resp(200, {"ok": 1}))
    assert c.get("/vacancies") == {"ok": 1}
    assert c.requests == 2
    assert _client(_Resp(404, {})).get("/vacancies/1") is None


def test_client_gives_up_after_repeated_429():
    c = _client(*[_Resp(429, {})] * 5)
    with pytest.raises(hh.HhBlocked, match="429"):
        c.get("/vacancies")


def test_client_network_error_retries():
    class S(_Session):
        def get(self, *a, **kw):
            r = self.responses.pop(0)
            if isinstance(r, Exception):
                raise r
            return r

    c = hh.HhClient("t", pause=0, session=S([requests.ConnectionError("x"), _Resp(200, {"a": 1})]), sleep=lambda s: None)
    assert c.get("/x") == {"a": 1}
