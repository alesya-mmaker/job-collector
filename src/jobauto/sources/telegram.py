"""Публичные телеграм-каналы через веб-превью https://t.me/s/<канал>.

Если у канала превью отключено (или канала нет), t.me отвечает
редиректом на https://t.me/<канал> — такой канал пропускается.
Листание назад: ?before=<id самого старого поста на странице>.
"""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime

import requests
from bs4 import BeautifulSoup

from ..html_text import html_to_text

BASE_URL = "https://t.me/s/"
MAX_PAGES = 50


class ChannelSkipped(Exception):
    """Превью канала недоступно."""


@dataclass
class Post:
    channel: str
    post_id: int
    url: str
    date: str | None
    text: str
    keywords: list[str] = field(default_factory=list)

    @property
    def hash(self) -> str:
        return post_hash(self.channel, self.text)


def post_hash(channel: str, text: str) -> str:
    norm = re.sub(r"\s+", " ", text.replace("ё", "е").lower()).strip()
    return hashlib.sha1(f"{channel.lower()}\n{norm}".encode("utf-8")).hexdigest()


def parse_page(html: str, channel: str) -> tuple[list[Post], int | None]:
    """Посты страницы и id для следующей (более старой) страницы."""
    soup = BeautifulSoup(html, "html.parser")
    posts = []
    for msg in soup.select(".tgme_widget_message[data-post]"):
        data_post = msg["data-post"]
        try:
            post_id = int(data_post.rsplit("/", 1)[1])
        except (IndexError, ValueError):
            continue
        text_el = msg.select_one(".tgme_widget_message_text")
        text = html_to_text(text_el.decode_contents()) if text_el else ""
        time_el = msg.select_one(".tgme_widget_message_date time[datetime]")
        link_el = msg.select_one("a.tgme_widget_message_date")
        posts.append(
            Post(
                channel=channel,
                post_id=post_id,
                url=link_el["href"] if link_el and link_el.get("href") else f"https://t.me/{data_post}",
                date=time_el["datetime"] if time_el else None,
                text=text,
            )
        )
    more = soup.select_one(".tme_messages_more[data-before]")
    before = int(more["data-before"]) if more and more["data-before"].isdigit() else None
    return posts, before


def fetch_channel(
    channel: str,
    since_id: int | None,
    since_date: datetime,
    session: requests.Session | None = None,
    pause: float = 1.0,
    sleep: Callable[[float], None] = time.sleep,
    max_pages: int = MAX_PAGES,
) -> list[Post]:
    """Посты новее since_id (если он известен) и не старше since_date."""
    session = session or requests.Session()
    result: dict[int, Post] = {}
    before: int | None = None
    for page in range(max_pages):
        if page:
            sleep(pause)
        url = BASE_URL + channel + (f"?before={before}" if before else "")
        resp = session.get(url, timeout=30, allow_redirects=False, headers={"User-Agent": "Mozilla/5.0"})
        if resp.status_code in (301, 302, 303, 307, 308):
            raise ChannelSkipped(f"@{channel}: веб-превью отключено или канал не найден")
        if resp.status_code != 200:
            raise ChannelSkipped(f"@{channel}: t.me ответил HTTP {resp.status_code}")
        posts, next_before = parse_page(resp.text, channel)
        if page == 0 and not posts and "tgme_channel_info" not in resp.text:
            raise ChannelSkipped(f"@{channel}: веб-превью недоступно")

        reached_old = False
        for p in posts:
            too_old = p.date is not None and datetime.fromisoformat(p.date) < since_date
            if (since_id is not None and p.post_id <= since_id) or too_old:
                reached_old = True
                continue
            result[p.post_id] = p
        if reached_old or not next_before or not posts:
            break
        before = next_before
    return sorted(result.values(), key=lambda p: p.post_id)
