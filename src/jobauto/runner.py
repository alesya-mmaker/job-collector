"""Один запуск сбора: hh (основные запросы и подработка), площадки Habr Career / Geekjob /
getmatch, Telegram, пометки, дамп.

Отметки прогресса (таблица state):
  hh:<id запроса> — время начала последнего сбора, в котором этот запрос
      прошёл целиком. Следующий сбор берёт вакансии с этого момента
      (минус небольшой запас). Если запрос упал посередине, отметка не
      сдвигается и в следующий раз период повторяется.
  board:<площадка> — то же для Habr Career, Geekjob, getmatch.
  tg:<канал> — id последнего прочитанного поста.
"""

from __future__ import annotations

import json
import sqlite3
import time
import traceback
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

import requests

from . import db
from .config import DUMPS_DIR, Config, Query
from .duplicates import employer_key, find_original
from .dump import write_dump
from .exclude import Excluder, refresh_exclusions
from .maintenance import compact_vacancies
from .marks import Marker
from .patterns import PatternSet
from .sources import boards, hh, hh_web, telegram

Progress = Callable[[str], None]


@dataclass
class RunResult:
    run_id: int
    status: str
    requests: int = 0
    new_main: int = 0
    new_side: int = 0
    new_posts: int = 0
    skipped_channels: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    dump_path: Path | None = None


def build_keyword_sets(keywords: list[str], on_error: Callable[[str], None] | None = None) -> list[PatternSet]:
    sets = []
    for kw in keywords:
        try:
            sets.append(PatternSet.from_strings(kw.removeprefix("re:"), [kw]))
        except Exception as e:  # noqa: BLE001
            if on_error:
                on_error(f"ключевое слово «{kw}» не разобрано: {e}")
    return sets


def found_keywords(text: str, sets: list[PatternSet]) -> list[str]:
    """Найденные в тексте слова — как они написаны в тексте, а не шаблоны («автоматизации», а не «автоматизаци\\w*»)."""
    found: list[str] = []
    for k in sets:
        spans = k.spans(text)
        if spans:
            word = text[spans[0][0]:spans[0][1]].lower()
            if word not in found:
                found.append(word)
    return found


def refresh_post_keywords(conn: sqlite3.Connection, keywords: list[str]) -> int:
    """Перепроверка уже собранных постов по текущим ключевым словам.

    Посты без совпадений получают пустой список слов и скрываются из ленты
    (если у них нет статуса и заметки). Ничего не удаляется:
    если вернуть слово в настройки, пост снова появится. Возвращает число
    постов, которые стали скрытыми.
    """
    sets = build_keyword_sets(keywords)
    newly_hidden = 0
    for row in conn.execute("SELECT hash, text, keywords FROM tg_posts").fetchall():
        kws = json.dumps(found_keywords(row["text"], sets), ensure_ascii=False)
        if kws != row["keywords"]:
            newly_hidden += kws == "[]"
            conn.execute("UPDATE tg_posts SET keywords = ? WHERE hash = ?", (kws, row["hash"]))
    conn.commit()
    return newly_hidden


class HhApiAdapter:
    """hh через api.hh.ru (нужен доступ к API)."""

    def __init__(self, runner: "Runner", client: hh.HhClient):
        self.runner, self.client = runner, client

    @property
    def requests(self) -> int:
        return self.client.requests

    def search(self, query: Query, start: datetime, end: datetime, warnings: list[str]):
        hh_cfg = self.runner.cfg.hh
        params = hh.search_params(query.text, [int(a) for a in hh_cfg.get("areas", [16, 113])],
                                  hh_cfg.get("host", "hh.ru"), query.employment_forms, query.search_field)
        for item in hh.search(self.client, params, start, end, self.runner.progress, warnings):
            yield hh.parse_search_item(item)

    def detail(self, vac_id: str) -> dict | None:
        data = self.client.get(f"/vacancies/{vac_id}")
        if data is None:
            return None
        d = hh.parse_detail(data)
        d["employer_type"] = self.runner._employer_type(self.client, d.get("employer_id"))
        return d


class HhWebAdapter:
    """hh через страницы сайта hh.ru."""

    def __init__(self, runner: "Runner", web: hh_web.HhWebSource):
        self.runner, self.web = runner, web

    @property
    def requests(self) -> int:
        return self.web.requests

    def search(self, query: Query, start: datetime, end: datetime, warnings: list[str]):
        areas = [int(a) for a in self.runner.cfg.hh.get("areas", [16, 113])]
        return self.web.search(query.text, areas, query.employment_forms, query.search_field, since=start,
                               progress=self.runner.progress, warnings=warnings)

    def detail(self, vac_id: str) -> dict | None:
        return self.web.detail(vac_id)


class Runner:
    def __init__(
        self,
        conn: sqlite3.Connection,
        cfg: Config,
        progress: Progress = print,
        hh_client: hh.HhClient | None = None,
        hh_web_source: hh_web.HhWebSource | None = None,
        board_sources: dict[str, boards.Board] | None = None,
        tg_session: requests.Session | None = None,
        sleep: Callable[[float], None] = time.sleep,
        now: Callable[[], datetime] | None = None,
        dumps_dir: Path = DUMPS_DIR,
        log_path: Path | None = None,
        on_hh_blocked: Callable[[], str] | None = None,
    ):
        self.conn = conn
        self.cfg = cfg
        self._progress = progress
        self.log_path = log_path
        self.on_hh_blocked = on_hh_blocked
        self._hh_client = hh_client
        self._hh_web = hh_web_source
        self._boards: dict[str, boards.Board] = dict(board_sources or {})
        self._columns = {r["name"] for r in conn.execute("PRAGMA table_info(vacancies)")}
        self.tg_session = tg_session or requests.Session()
        self.sleep = sleep
        self.now = now or (lambda: datetime.now().astimezone())
        self.dumps_dir = dumps_dir
        self.queries_by_id = {q.id: q for q in cfg.queries}
        self.excluder = Excluder(cfg.exclude)
        self.errors: list[str] = []

    # ------------------------------------------------------------------ запуск

    def run(self) -> RunResult:
        started = self.now()
        self.conn.execute(
            "UPDATE runs SET status = 'failed', finished_at = ? WHERE status = 'running'", (self._ts(),)
        )
        cur = self.conn.execute("INSERT INTO runs(started_at, status) VALUES(?, 'running')", (started.isoformat(),))
        self.conn.commit()
        run_id = cur.lastrowid
        result = RunResult(run_id=run_id, status="running")
        self.progress(f"сбор №{run_id}, начало {started:%d.%m.%Y %H:%M:%S}")
        requests_made = 0
        try:
            # до сбора: старые вакансии, отсеянные по названию, не ждут загрузки описания
            refresh_exclusions(self.conn, self.cfg.exclude)
            requests_made = self._collect_sources(run_id, started)
            result.skipped_channels = self._collect_telegram(run_id, started)
            ex = refresh_exclusions(self.conn, self.cfg.exclude)
            self.progress(f"отсев: скрыто вакансий {ex['vacancies']}, постов {ex['posts']}")
            self._refresh_marks()
            hidden = refresh_post_keywords(self.conn, self.cfg.keywords)
            if hidden:
                self.progress(f"telegram: по текущим ключевым словам скрыто старых постов: {hidden}")
            result.dump_path = write_dump(self.conn, run_id, self.dumps_dir, started.date())
            compacted = compact_vacancies(self.conn, now=started)
            if compacted:
                self.progress(f"очистка: у {compacted} старых вакансий удалены описания (записи остались)")
            result.status = "partial" if self.errors else "ok"
        except Exception as e:  # noqa: BLE001 — любой сбой фиксируем в логе запуска
            self.errors.append(f"сбор прерван: {type(e).__name__}: {e}")
            self.progress(traceback.format_exc())
            result.status = "failed"
            raise
        finally:
            counts = dict(
                self.conn.execute(
                    "SELECT kind, COUNT(*) FROM vacancies WHERE first_run_id = ? GROUP BY kind", (run_id,)
                ).fetchall()
            )
            result.new_main, result.new_side = counts.get("main", 0), counts.get("side", 0)
            result.new_posts = self.conn.execute(
                "SELECT COUNT(*) FROM tg_posts WHERE first_run_id = ?", (run_id,)
            ).fetchone()[0]
            result.requests = requests_made
            result.errors = list(self.errors)
            self.conn.execute(
                "UPDATE runs SET finished_at=?, status=?, requests=?, new_main=?, new_side=?, new_posts=?, "
                "skipped_channels=?, errors=?, dump_path=? WHERE id=?",
                (
                    self._ts(),
                    result.status,
                    result.requests,
                    result.new_main,
                    result.new_side,
                    result.new_posts,
                    json.dumps(result.skipped_channels, ensure_ascii=False),
                    json.dumps(result.errors, ensure_ascii=False),
                    str(result.dump_path) if result.dump_path else None,
                    run_id,
                ),
            )
            self.conn.commit()
        return result

    def progress(self, msg: str) -> None:
        """Сообщение в панель/консоль и, если задан файл, в подробный лог сбора."""
        self._progress(msg)
        if self.log_path:
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(f"{datetime.now():%H:%M:%S} {msg}\n")

    def _ts(self) -> str:
        return self.now().isoformat(timespec="seconds")

    def _error(self, msg: str) -> None:
        self.errors.append(msg)
        self.progress(f"  ОШИБКА: {msg}")

    # ------------------------------------------------------------- источники

    def _hh_source(self) -> HhApiAdapter | HhWebAdapter | None:
        """hh через сайт (по умолчанию) или через API (если явно задан клиент API или mode: api)."""
        if self._hh_web is not None:
            return HhWebAdapter(self, self._hh_web)
        if self._hh_client is not None or self.cfg.hh_mode == "api":
            if self._hh_client is None:
                try:
                    ua = self.cfg.user_agent()
                except ValueError as e:
                    self._error(str(e))
                    return None
                self._hh_client = hh.HhClient(ua, pause=float(self.cfg.hh.get("pause_seconds", 1.0)), sleep=self.sleep)
            return HhApiAdapter(self, self._hh_client)
        web = hh_web.HhWebSource(pause=float(self.cfg.hh.get("web_pause_seconds", 2.0)), sleep=self.sleep)
        return HhWebAdapter(self, web)

    def _collect_sources(self, run_id: int, started: datetime) -> int:
        """hh и площадки, затем догрузка описаний и перепроверка. Возвращает число запросов."""
        details: dict[str, Callable[[str], dict | None]] = {}
        counters: list[Any] = []

        src = self._hh_source() if self.cfg.queries else None
        if src is not None:
            counters.append(src)
            try:
                for query in self.cfg.queries:
                    self._collect_query(src, query, run_id, started)
                details["hh"] = src.detail
            except hh.HhBlocked as e:
                self._hh_blocked(e)

        for name in self.cfg.boards:
            board = self._board(name)
            if board is None:
                continue
            counters.append(board)
            if self._collect_board(board, run_id, started):
                details[name] = board.detail

        self._fetch_details(details)
        if "hh" in details:
            try:
                self._recheck(details["hh"], run_id, started)
            except hh.HhBlocked as e:
                self._hh_blocked(e)
        return sum(c.requests for c in counters)

    def _hh_blocked(self, e: Exception) -> None:
        self._error(str(e))
        if self.on_hh_blocked:
            self.progress(f"  сеть: {self.on_hh_blocked()}")

    def _since(self, key: str, started: datetime, section: dict) -> datetime:
        mark = db.get_state(self.conn, key)
        if mark:
            return datetime.fromisoformat(mark) - timedelta(minutes=int(self.cfg.hh.get("overlap_minutes", 60)))
        return started - timedelta(days=int(section.get("initial_lookback_days", 7)))

    # ---------------------------------------------------------------------- hh

    def _collect_query(self, src: HhApiAdapter | HhWebAdapter, query: Query, run_id: int, started: datetime) -> None:
        start = self._since(f"hh:{query.id}", started, self.cfg.hh)
        self.progress(f"hh: «{query.label}» с {start:%d.%m.%Y %H:%M}")
        warnings: list[str] = []
        found = new = 0
        try:
            for vac in src.search(query, start, started, warnings):
                found += 1
                new += self._upsert_search_item(vac, query, run_id)
        except hh.HhError as e:
            self._error(f"«{query.label}»: {e}; период повторится при следующем сборе")
            return
        for w in warnings:
            self._error(f"«{query.label}»: {w}")
        self.conn.commit()
        db.set_state(self.conn, f"hh:{query.id}", started.isoformat())
        self.progress(f"  найдено {found}, новых {new}")

    # ---------------------------------------------------------------- площадки

    def _board(self, name: str) -> boards.Board | None:
        if name in self._boards:
            return self._boards[name]
        cls = boards.BOARDS.get(name)
        if cls is None:
            self._error(f"неизвестная площадка «{name}» в boards.yaml")
            return None
        board = cls(pause=float(self.cfg.settings.get("boards", {}).get("pause_seconds", 1.5)), sleep=self.sleep)
        self._boards[name] = board
        return board

    def _term_query(self, board: boards.Board, term: dict) -> Query:
        q = Query(id=f"{board.name}:{term['text']}", text=term["text"], direction=term.get("direction"),
                  label=f"{board.label}: {term['text']}")
        self.queries_by_id[q.id] = q
        return q

    def _collect_board(self, board: boards.Board, run_id: int, started: datetime) -> bool:
        """True, если площадка ответила (тогда по ней догружаются описания)."""
        key = f"board:{board.name}"
        since = self._since(key, started, self.cfg.settings.get("boards", {}))
        terms = self.cfg.board_terms
        self._forget_rejected_if_terms_changed(terms)
        self.progress(f"{board.label}: вакансии с {since:%d.%m.%Y %H:%M}")
        matchers = [(self._term_query(board, t), PatternSet.from_strings(t["text"], [t["text"]])) for t in terms]
        new = rejected = checked = 0
        try:
            # кандидаты: у Habr — выдача его поиска по каждому слову (она нечёткая),
            # у Geekjob и getmatch — вся лента
            candidates: dict[str, dict[str, Any]] = {}
            if board.server_search:
                for term in terms:
                    for vac in board.search(term["text"], since):
                        candidates.setdefault(vac["id"], vac)
            else:
                for vac in board.feed(since):
                    candidates.setdefault(vac["id"], vac)

            for vac in candidates.values():
                if self.conn.execute("SELECT 1 FROM vacancies WHERE id = ?", (vac["id"],)).fetchone():
                    continue
                if self.conn.execute("SELECT 1 FROM board_seen WHERE id = ?", (vac["id"],)).fetchone():
                    continue
                hits = self._match_terms(vac, matchers)
                title_excluded = bool(self.excluder.reasons(vac, kind="main", title_only=True))
                if not hits and not vac.get("description") and not title_excluded:
                    # в названии и навыках слов нет — смотрим полное описание
                    checked += 1
                    detail = board.detail(vac["id"])
                    if detail:
                        vac.update({k: v for k, v in detail.items() if v})
                        hits = self._match_terms(vac, matchers)
                if not hits:
                    rejected += 1
                    self.conn.execute(
                        "INSERT OR IGNORE INTO board_seen(id, checked_at) VALUES(?, ?)", (vac["id"], self._ts())
                    )
                    continue
                for q in hits:
                    new += self._upsert_search_item(dict(vac), q, run_id, source=board.name)
        except boards.BoardError as e:
            self.conn.commit()
            self._error(f"{e}; вакансии с этой площадки возьмутся при следующем сборе")
            return False
        except Exception as e:  # noqa: BLE001 — сбой одной площадки не должен обрывать весь сбор
            self.conn.commit()
            self.progress(traceback.format_exc())
            self._error(f"{board.label}: {type(e).__name__}: {e}; вакансии с этой площадки возьмутся при следующем сборе")
            return False
        self.conn.commit()
        db.set_state(self.conn, key, started.isoformat())
        self.progress(
            f"  кандидатов {len(candidates)}, подошло новых {new}, "
            f"не подошло {rejected} (описаний проверено {checked})"
        )
        return True

    def _forget_rejected_if_terms_changed(self, terms: list[dict[str, Any]]) -> None:
        """Отклонённые вакансии площадок помнятся, чтобы не грузить их описания каждый раз. Но после правки
        слов в boards.yaml они могли бы подойти: тогда список отклонённых сбрасывается."""
        signature = json.dumps(sorted(str(t["text"]).lower() for t in terms), ensure_ascii=False)
        known = db.get_state(self.conn, "board_terms")
        if known is not None and known != signature:
            self.conn.execute("DELETE FROM board_seen")
            self.progress("слова для площадок изменились: отклонённые вакансии проверю заново")
        if known != signature:
            db.set_state(self.conn, "board_terms", signature)

    @staticmethod
    def _match_terms(vac: dict[str, Any], matchers: list[tuple[Query, PatternSet]]) -> list[Query]:
        """Слова ищутся сами, а не по выдаче площадки: в названии, навыках и описании."""
        text = "\n".join([vac.get("name") or "", " ".join(vac.get("key_skills") or []), vac.get("description") or ""])
        return [q for q, ps in matchers if ps.search(text)]

    # ------------------------------------------------------- общие операции

    def _classify(self, query_ids: list[str]) -> tuple[str, str | None]:
        """kind и направление по списку запросов, которыми найдена вакансия."""
        main = [self.queries_by_id[q] for q in query_ids if q in self.queries_by_id and not self.queries_by_id[q].is_side]
        if not main and any(q in self.queries_by_id for q in query_ids):
            return "side", None
        directions = {q.direction for q in main if q.direction}
        for d in self.cfg.directions:
            if d in directions:
                return "main", d
        return "main", next(iter(sorted(directions)), None)

    def _upsert_search_item(self, vac: dict[str, Any], query: Query, run_id: int, source: str = "hh") -> int:
        """1, если вакансия новая."""
        row = self.conn.execute("SELECT query_ids, queries FROM vacancies WHERE id = ?", (vac["id"],)).fetchone()
        now = self._ts()
        if row:
            ids = json.loads(row["query_ids"])
            labels = json.loads(row["queries"])
            if query.id in ids:
                return 0
            ids.append(query.id)
            labels.append(query.label)
            kind, direction = self._classify(ids)
            self.conn.execute(
                "UPDATE vacancies SET query_ids=?, queries=?, kind=?, direction=?, updated_at=? WHERE id=?",
                (json.dumps(ids, ensure_ascii=False), json.dumps(labels, ensure_ascii=False), kind, direction, now, vac["id"]),
            )
            return 0
        kind, direction = self._classify([query.id])
        vac = {k: v for k, v in vac.items() if k in self._columns}
        if isinstance(vac.get("key_skills"), list):
            vac["key_skills"] = json.dumps(vac["key_skills"], ensure_ascii=False)
        has_text = bool(vac.get("description"))
        vac.update(
            kind=kind,
            direction=direction,
            source=source,
            employer_key=employer_key(vac.get("employer_name")),
            query_ids=json.dumps([query.id], ensure_ascii=False),
            queries=json.dumps([query.label], ensure_ascii=False),
            first_run_id=run_id,
            added_at=now,
            updated_at=now,
        )
        if has_text:
            vac.update(detail_status="ok", source_checked_at=now)
        reasons = self.excluder.reasons(vac, kind=kind, title_only=not has_text)
        if reasons:
            vac["excluded"] = json.dumps(reasons, ensure_ascii=False)
            if not has_text:
                vac["detail_status"] = "skipped"  # описание отсеянной вакансии не нужно
        cols = ", ".join(vac)
        self.conn.execute(f"INSERT INTO vacancies({cols}) VALUES({', '.join('?' * len(vac))})", list(vac.values()))
        if has_text:
            self._mark_duplicate(vac["id"], float(self.cfg.duplicates.get("title_similarity", 0.85)))
        return 1

    def _apply_detail(self, vac_id: str, detail: dict[str, Any] | None) -> None:
        now = self._ts()
        if detail is None:
            self.conn.execute(
                "UPDATE vacancies SET detail_status='failed', detail_error=?, source_status='deleted', "
                "source_checked_at=?, updated_at=? WHERE id=?",
                ("вакансия удалена до загрузки описания", now, now, vac_id),
            )
            return
        detail = {k: v for k, v in detail.items() if k in self._columns and k != "id"}
        if detail.get("employer_type") is None:
            detail.pop("employer_type", None)  # не затираем тип, известный из поиска
        if detail.get("schedule") is None:
            detail.pop("schedule", None)
        if isinstance(detail.get("key_skills"), list):
            detail["key_skills"] = json.dumps(detail["key_skills"], ensure_ascii=False)
        detail.update(detail_status="ok", detail_error=None, source_checked_at=now, updated_at=now)
        sets = ", ".join(f"{k}=?" for k in detail)
        self.conn.execute(f"UPDATE vacancies SET {sets} WHERE id=?", [*detail.values(), vac_id])

    def _fetch_details(self, details: dict[str, Callable[[str], dict | None]]) -> None:
        """Полные описания для всех вакансий без них, включая недогруженные в прошлые разы."""
        if not details:
            return
        marks = ", ".join("?" * len(details))
        pending = self.conn.execute(
            f"SELECT id, source FROM vacancies WHERE detail_status = 'pending' AND source IN ({marks}) "
            "ORDER BY published_at, id",
            list(details),
        ).fetchall()
        if not pending:
            return
        self.progress(f"загружаю полные описания: {len(pending)}")
        threshold = float(self.cfg.duplicates.get("title_similarity", 0.85))
        failures: dict[str, int] = {}
        for i, row in enumerate(pending, 1):
            if i % 25 == 0:
                self.progress(f"  {i}/{len(pending)}")
            source = row["source"]
            if source not in details:
                continue
            try:
                detail = details[source](row["id"])
            except hh.HhBlocked as e:
                self._hh_blocked(e)
                details.pop(source, None)
                continue
            except (hh.HhError, boards.BoardError) as e:
                self._error(f"вакансия {row['id']}: {e}")
                failures[source] = failures.get(source, 0) + 1
                if failures[source] >= 5:
                    self._error(f"{source}: много ошибок подряд, остальные описания догрузятся при следующем сборе")
                    details.pop(source, None)
                continue
            failures[source] = 0
            self._apply_detail(row["id"], detail)
            self._mark_duplicate(row["id"], threshold)
            self.conn.commit()

    def _mark_duplicate(self, vac_id: str, threshold: float) -> None:
        vac = db.row_to_dict(self.conn.execute("SELECT * FROM vacancies WHERE id = ?", (vac_id,)).fetchone())
        original = find_original(self.conn, vac, threshold)
        if original:
            self.conn.execute("UPDATE vacancies SET duplicate_of = ? WHERE id = ?", (original, vac_id))

    def _employer_type(self, client: hh.HhClient, employer_id: str | None) -> str | None:
        if not employer_id:
            return None
        row = self.conn.execute("SELECT type FROM employers WHERE id = ?", (employer_id,)).fetchone()
        if row:
            return row["type"]
        try:
            data = client.get(f"/employers/{employer_id}")
        except hh.HhError as e:
            self._error(f"работодатель {employer_id}: {e}")
            return None
        etype = data.get("type") if data else None
        self.conn.execute(
            "INSERT OR REPLACE INTO employers(id, name, type, fetched_at) VALUES(?, ?, ?, ?)",
            (employer_id, data.get("name") if data else None, etype, self._ts()),
        )
        return etype

    def _recheck(self, detail: Callable[[str], dict | None], run_id: int, started: datetime) -> None:
        """Перепроверка вакансий hh: не закрыта ли. Закрытые остаются в базе с отметкой."""
        days = float(self.cfg.hh.get("recheck_every_days", 3))
        border = (started - timedelta(days=days)).isoformat()
        rows = self.conn.execute(
            "SELECT id FROM vacancies WHERE source = 'hh' AND source_status = 'active' AND detail_status = 'ok' "
            "AND excluded IS NULL "
            "AND (source_checked_at IS NULL OR source_checked_at < ?) AND first_run_id != ?",
            (border, run_id),
        ).fetchall()
        if not rows:
            return
        self.progress(f"hh: проверяю, не закрыты ли вакансии: {len(rows)}")
        closed = 0
        for row in rows:
            try:
                data = detail(row["id"])
            except hh.HhError as e:
                self._error(f"проверка {row['id']}: {e}")
                continue
            status = "deleted" if data is None else data.get("source_status", "active")
            archived_at = data.get("archived_at") if data else None
            closed += status != "active"
            self.conn.execute(
                "UPDATE vacancies SET source_status=?, archived_at=COALESCE(?, archived_at), "
                "source_checked_at=?, updated_at=? WHERE id=?",
                (status, archived_at if isinstance(archived_at, str) else None, self._ts(), self._ts(), row["id"]),
            )
            self.conn.commit()
        self.progress(f"  закрыто или удалено: {closed}")

    # ---------------------------------------------------------------- telegram

    def _collect_telegram(self, run_id: int, started: datetime) -> list[str]:
        skipped: list[str] = []
        if not self.cfg.channels:
            return skipped
        keyword_sets = build_keyword_sets(self.cfg.keywords, on_error=self._error)
        pause = float(self.cfg.telegram.get("pause_seconds", 1.0))
        since_date = started - timedelta(days=int(self.cfg.telegram.get("initial_lookback_days", 7)))
        for i, channel in enumerate(self.cfg.channels):
            if i:
                self.sleep(pause)
            key = f"tg:{channel.lower()}"
            last = db.get_state(self.conn, key)
            self.progress(f"telegram: @{channel}")
            try:
                posts = telegram.fetch_channel(
                    channel, int(last) if last else None, since_date, self.tg_session, pause, self.sleep
                )
            except telegram.ChannelSkipped as e:
                skipped.append(channel)
                self.progress(f"  пропущен: {e}")
                continue
            except requests.RequestException as e:
                self._error(f"@{channel}: {e}")
                continue
            new = 0
            for p in posts:
                p.keywords = found_keywords(p.text, keyword_sets)
                if not p.keywords or not p.text:
                    continue
                cur = self.conn.execute(
                    "INSERT OR IGNORE INTO tg_posts(hash, channel, post_id, url, date, text, keywords, first_run_id, added_at) "
                    "VALUES(?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (p.hash, channel, p.post_id, p.url, p.date, p.text,
                     json.dumps(p.keywords, ensure_ascii=False), run_id, self._ts()),
                )
                new += cur.rowcount
            self.conn.commit()
            if posts:
                db.set_state(self.conn, key, str(max(p.post_id for p in posts)))
            self.progress(f"  постов {len(posts)}, подходящих новых {new}")
        return skipped

    # ------------------------------------------------------------------ пометки

    def _refresh_marks(self) -> None:
        """Пересчёт пометок у всех вакансий: правки marks.yaml сразу видны в панели."""
        marker = Marker(self.cfg.marks)
        for row in self.conn.execute("SELECT * FROM vacancies").fetchall():
            vac = db.row_to_dict(row)
            marks = json.dumps(marker.marks_for(vac), ensure_ascii=False)
            if marks != row["marks"]:
                self.conn.execute("UPDATE vacancies SET marks = ? WHERE id = ?", (marks, vac["id"]))
        self.conn.commit()
