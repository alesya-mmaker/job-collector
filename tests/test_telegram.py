from datetime import datetime
from pathlib import Path

import pytest

from jobauto.sources import telegram

FIX = Path(__file__).parent / "fixtures"
OLD = datetime.fromisoformat("2000-01-01T00:00:00+00:00")


def test_parse_page_fixture():
    html = (FIX / "tg_telegram.html").read_text(encoding="utf-8")
    posts, before = telegram.parse_page(html, "telegram")
    assert len(posts) == 20
    assert posts[0].url.startswith("https://t.me/telegram/")
    assert all(p.date and p.date.startswith("20") for p in posts)
    assert before == min(p.post_id for p in posts)
    assert any(len(p.text) > 50 for p in posts)


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


class _Session:
    def __init__(self, pages):
        self.pages, self.urls = list(pages), []

    def get(self, url, **kw):
        self.urls.append(url)
        return self.pages.pop(0)


def test_disabled_preview_is_skipped():
    with pytest.raises(telegram.ChannelSkipped, match="превью"):
        telegram.fetch_channel("x", None, OLD, _Session([_Resp(302)]), pause=0, sleep=lambda s: None)


def test_stops_at_last_seen_post():
    html = (FIX / "tg_telegram.html").read_text(encoding="utf-8")
    ids = sorted(p.post_id for p in telegram.parse_page(html, "telegram")[0])
    since = ids[10]
    s = _Session([_Resp(200, html)])
    posts = telegram.fetch_channel("telegram", since, OLD, s, pause=0, sleep=lambda s: None)
    assert [p.post_id for p in posts] == [i for i in ids if i > since]
    assert len(s.urls) == 1


def test_hash_ignores_case_and_spaces():
    assert telegram.post_hash("Ch", "Ищем  n8n\nразработчика") == telegram.post_hash("ch", "ищем n8n разработчика")
    assert telegram.post_hash("a", "текст") != telegram.post_hash("b", "текст")
