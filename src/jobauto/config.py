"""Пути проекта и загрузка YAML-конфигов."""

from __future__ import annotations

import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[2]
CONFIG_DIR = ROOT / "config"
EXAMPLE_DIR = ROOT / "config.example"
DATA_DIR = ROOT / "data"
BACKUP_DIR = DATA_DIR / "backups"
LOG_DIR = DATA_DIR / "logs"
DUMPS_DIR = ROOT / "dumps"
DB_PATH = DATA_DIR / "jobs.db"

SIDE_QUERY_ID = "side"
SIDE_QUERY_LABEL = "подработка"


def ensure_config(config_dir: Path = CONFIG_DIR, example_dir: Path = EXAMPLE_DIR) -> list[str]:
    """Личные настройки (config/) не хранятся в репозитории: недостающие файлы берутся из config.example/.
    Возвращает имена созданных файлов."""
    created = []
    config_dir.mkdir(parents=True, exist_ok=True)
    for src in sorted(example_dir.glob("*.yaml")):
        dst = config_dir / src.name
        if not dst.exists():
            shutil.copyfile(src, dst)
            created.append(src.name)
    return created


def load_yaml(name: str, config_dir: Path = CONFIG_DIR) -> dict[str, Any]:
    path = config_dir / name
    if not path.exists():
        return {}
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def save_yaml(name: str, data: dict[str, Any], config_dir: Path = CONFIG_DIR) -> None:
    path = config_dir / name
    tmp = path.with_suffix(".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        yaml.safe_dump(data, f, allow_unicode=True, sort_keys=False)
    tmp.replace(path)


@dataclass
class Query:
    id: str
    text: str
    direction: str | None
    label: str
    employment_forms: list[str] = field(default_factory=list)
    search_field: str | None = None

    @property
    def is_side(self) -> bool:
        return self.id == SIDE_QUERY_ID


@dataclass
class Config:
    settings: dict[str, Any]
    queries: list[Query]
    directions: list[str]
    channels: list[str]
    keywords: list[str]
    marks: list[dict[str, Any]]
    skills: list[dict[str, Any]]
    boards: list[str] = field(default_factory=list)
    board_terms: list[dict[str, Any]] = field(default_factory=list)
    exclude: list[dict[str, Any]] = field(default_factory=list)

    @property
    def hh(self) -> dict[str, Any]:
        return self.settings.get("hh", {})

    @property
    def telegram(self) -> dict[str, Any]:
        return self.settings.get("telegram", {})

    @property
    def duplicates(self) -> dict[str, Any]:
        return self.settings.get("duplicates", {})

    @property
    def hh_mode(self) -> str:
        """web — страницы сайта hh.ru (по умолчанию), api — api.hh.ru (нужен доступ)."""
        return "api" if self.hh.get("mode") == "api" else "web"

    def user_agent(self) -> str:
        email = (self.hh.get("contact_email") or "").strip()
        if not email:
            raise ValueError(
                "Не заполнена почта для hh.ru: config/settings.yaml → hh.contact_email"
            )
        return f"{self.hh.get('app_name', 'JobAutomation/1.0')} ({email})"


def load_config(config_dir: Path = CONFIG_DIR) -> Config:
    settings = load_yaml("settings.yaml", config_dir)
    q = load_yaml("queries.yaml", config_dir)
    ch = load_yaml("channels.yaml", config_dir)

    queries = [
        Query(id=str(x["id"]), text=x.get("text", ""), direction=x.get("direction"), label=x.get("text", ""))
        for x in q.get("queries", [])
        if x.get("enabled", True)
    ]
    side = q.get("side_jobs") or {}
    if side.get("enabled", False):
        queries.append(
            Query(
                id=SIDE_QUERY_ID,
                text=side.get("text", "") or "",
                direction=None,
                label=SIDE_QUERY_LABEL,
                employment_forms=list(side.get("employment_forms") or ["PART", "PROJECT"]),
                search_field=side.get("search_field") or None,
            )
        )

    b = load_yaml("boards.yaml", config_dir)
    boards = [name for name, opts in (b.get("boards") or {}).items() if (opts or {}).get("enabled", True)]
    terms = [t for t in b.get("terms") or [] if str(t.get("text") or "").strip()]

    return Config(
        settings=settings,
        queries=queries,
        directions=list(q.get("directions") or []),
        channels=[
            str(c["name"]).lstrip("@")
            for c in ch.get("channels") or []
            if c.get("enabled", True) and c.get("name")
        ],
        keywords=list(ch.get("keywords") or []),
        marks=[m for m in load_yaml("marks.yaml", config_dir).get("marks") or [] if m.get("enabled", True)],
        skills=list(load_yaml("skills.yaml", config_dir).get("skills") or []),
        boards=boards,
        board_terms=terms,
        exclude=[r for r in load_yaml("exclude.yaml", config_dir).get("rules") or [] if r.get("enabled", True)],
    )
