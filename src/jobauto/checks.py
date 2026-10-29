"""Проверка доступа: один запрос к hh, одна страница первого канала."""

from __future__ import annotations

from datetime import datetime

import requests

from .config import Config
from .sources import hh, telegram


def run_checks(cfg: Config) -> list[dict]:
    results = []
    try:
        client = hh.HhClient(cfg.user_agent(), pause=0)
        data = client.get("/vacancies", [("text", "Bubble"), ("per_page", 1), ("area", 16), ("area", 113)])
        results.append({"name": "hh.ru", "ok": True, "message": f"доступ есть, по запросу Bubble найдено {data.get('found') if data else 0}"})
    except (ValueError, hh.HhBlocked, hh.HhError) as e:
        results.append({"name": "hh.ru", "ok": False, "message": str(e)})

    channel = cfg.channels[0] if cfg.channels else "telegram"
    try:
        posts = telegram.fetch_channel(channel, None, datetime.fromisoformat("2000-01-01T00:00:00+00:00"), max_pages=1)
        results.append({"name": "t.me", "ok": True, "message": f"@{channel} читается, постов на первой странице: {len(posts)}"})
    except (telegram.ChannelSkipped, requests.RequestException) as e:
        results.append({"name": "t.me", "ok": False, "message": str(e)})
    return results
