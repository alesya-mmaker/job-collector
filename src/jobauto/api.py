"""Локальный сервер панели: http://127.0.0.1:8000"""

from __future__ import annotations

import io
import re
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any

from fastapi import Body, FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import db, settings_store
from .config import BACKUP_DIR, DATA_DIR, DB_PATH, DUMPS_DIR, LOG_DIR, load_config
from .diagnostics import quick_network_summary, run_diagnostics
from .runner import Runner, refresh_post_keywords
from .excerpt import excerpt
from .exclude import NO_EXPERIENCE, refresh_exclusions
from .maintenance import STALE_AFTER_DAYS, archive_stale, cleanup_files
from .skills import SkillHighlighter

STATIC_DIR = Path(__file__).parent / "static"
STATUSES = ["новая", "отклик", "не подходит"]
REJECT_REASONS = ["стек", "опыт", "зарплата", "компания", "формат", "неинтересно", "устарела"]
DEFAULT_STATUS = STATUSES[0]
# пост виден, если в нём есть текущие ключевые слова или у него уже есть статус или заметка
# отсеянные посты скрыты всегда (пост со статусом или заметкой не отсеивается)
POST_VISIBLE = (
    f"(p.excluded IS NULL AND (p.keywords != '[]' OR COALESCE(t.status, '{DEFAULT_STATUS}') != '{DEFAULT_STATUS}' "
    "OR COALESCE(t.notes, '') != ''))"
)

# Вкладки ленты. Они не пересекаются: каждая вакансия или пост ровно в одной.
#   new      — не открыта и без отметок (по умолчанию)
#   seen     — открыта (подробнее, ссылка), но решения нет
#   applied  — отклик отправлен
#   rejected — не подходит
VIEWS = {
    "new": f"COALESCE(t.status, '{DEFAULT_STATUS}') = '{DEFAULT_STATUS}' AND COALESCE(t.seen, 0) = 0",
    "seen": f"COALESCE(t.status, '{DEFAULT_STATUS}') = '{DEFAULT_STATUS}' AND COALESCE(t.seen, 0) = 1",
    "applied": "t.status = 'отклик'",
    "rejected": "t.status = 'не подходит'",
}
VIEW_OF = (
    "CASE WHEN t.status = 'отклик' THEN 'applied' WHEN t.status = 'не подходит' THEN 'rejected' "
    "WHEN COALESCE(t.seen, 0) = 1 THEN 'seen' ELSE 'new' END"
)

app = FastAPI(title="Вакансии")


LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "testserver"}


def _hostname(value: str) -> str:
    """'localhost:8000' → 'localhost'; '[::1]:8000' → '[::1]'."""
    value = value.strip().lower()
    if value.startswith("["):
        return value.split("]", 1)[0] + "]"
    return value.split(":", 1)[0]


@app.middleware("http")
async def local_only(request, call_next):
    """Панель отвечает только на локальные адреса, а изменения принимает только со своей страницы:
    чужой сайт в соседней вкладке не сможет ни запустить сбор, ни переписать настройки."""
    host = _hostname(request.headers.get("host", ""))
    origin = request.headers.get("origin")
    bad_origin = origin is not None and request.method not in ("GET", "HEAD", "OPTIONS") and (
        _hostname(origin.split("://", 1)[-1]) not in LOCAL_HOSTS
    )
    if host not in LOCAL_HOSTS or bad_origin:
        return JSONResponse({"detail": "запрос не с локальной страницы панели"}, status_code=403)
    return await call_next(request)


@app.middleware("http")
async def no_cache(request, call_next):
    """Браузер не кэширует файлы панели: после обновления кода сразу видна новая версия."""
    response = await call_next(request)
    response.headers["Cache-Control"] = "no-cache"
    return response


@contextmanager
def conn() -> Iterator[sqlite3.Connection]:
    c = db.connect(DB_PATH)
    try:
        yield c
    finally:
        c.close()


# ------------------------------------------------------------------- запуск


class RunManager:
    """Один сбор за раз, в фоновом потоке. Панель опрашивает статус."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.log: list[str] = []
        self.result: dict | None = None
        self.started_at: str | None = None

    @property
    def running(self) -> bool:
        return self.thread is not None and self.thread.is_alive()

    def start(self) -> bool:
        with self._lock:
            if self.running:
                return False
            self.log, self.result = [], None
            self.started_at = datetime.now().astimezone().isoformat(timespec="seconds")
            self.thread = threading.Thread(target=self._work, daemon=True)
            self.thread.start()
            return True

    def _work(self) -> None:
        try:
            cfg = load_config()
            cleanup_files([LOG_DIR, DATA_DIR / "probe"])
            b = db.backup(DB_PATH, DB_PATH.parent / BACKUP_DIR.name)
            if b:
                self.log.append(f"резервная копия базы: {b.name}")
            log_path = LOG_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}.txt"
            with conn() as c:
                r = Runner(
                    c, cfg, progress=self.log.append, dumps_dir=DUMPS_DIR,
                    log_path=log_path, on_hh_blocked=quick_network_summary,
                ).run()
            self.log.append(f"подробный лог: {log_path}")
            self.result = {
                "status": r.status, "requests": r.requests, "new_main": r.new_main, "new_side": r.new_side,
                "new_posts": r.new_posts, "skipped_channels": r.skipped_channels, "errors": r.errors,
                "dump": r.dump_path.name if r.dump_path else None,
            }
        except Exception as e:  # noqa: BLE001
            self.log.append(f"сбор прерван: {type(e).__name__}: {e}")
            self.result = {"status": "failed", "errors": [str(e)]}


runs = RunManager()


@app.post("/api/run")
def start_run() -> dict:
    if not runs.start():
        raise HTTPException(409, "сбор уже идёт")
    return {"started": True}


@app.get("/api/run")
def run_status(since: int = 0) -> dict:
    return {
        "running": runs.running,
        "started_at": runs.started_at,
        "log": runs.log[since:],
        "log_len": len(runs.log),
        "result": runs.result,
    }


@app.post("/api/check")
def check() -> dict:
    report, path = run_diagnostics(load_config(), LOG_DIR)
    return {"results": run_checks_from_report(report), "report": report, "path": str(path) if path else None}


def run_checks_from_report(report: str) -> list[dict]:
    ok = "hh.ru отвечает нормально" in report
    return [{"name": "hh.ru", "ok": ok, "message": "доступ есть" if ok else "доступа нет, причины — в отчёте ниже"}]


# --------------------------------------------------------------- вакансии

VAC_LIST_COLS = (
    "v.id, v.kind, v.name, v.employer_name, v.employer_type, v.salary_from, v.salary_to, v.currency, "
    "v.area, v.experience, v.employment_form, v.url, v.published_at, v.direction, v.marks, "
    "v.source_status, v.duplicate_of, v.detail_status, v.added_at, v.first_run_id, v.source, v.schedule, "
    "v.work_format, v.key_skills, v.description, v.snippet, v.excluded"
)


def _card(row: sqlite3.Row, hl: SkillHighlighter) -> dict:
    """Строка списка: без полного описания, но с выжимкой задач и совпадениями с моими навыками."""
    v = db.row_to_dict(row)
    text = v.pop("description", None) or v.get("snippet") or ""
    v["excerpt"] = excerpt(text)
    v["my_skills"] = hl.found("\n".join([v["name"], " ".join(v.get("key_skills") or []), text]))
    v.pop("snippet", None)
    return v


def _limit(n: int) -> int:
    return max(1, min(n, 1000))


TRACK_COLS = (
    f"COALESCE(t.status, '{DEFAULT_STATUS}') AS status, t.applied_at, t.notes, t.reject_reason, "
    "COALESCE(t.seen, 0) AS seen"
)


def _vacancy_filters(
    kind: str, direction: str | None, status: str | None, mark: list[str], nomark: list[str],
    only_new: bool, hide_dups: bool, hide_closed: bool, q: str | None, source: str | None = None,
    excluded: bool = False, hide_rejected: bool = False, view: str | None = None,
    has_salary: bool = False, no_experience: bool = False,
) -> tuple[str, list[Any]]:
    # отсеянные видны только отдельным фильтром
    where, args = ["v.kind = ?", "v.excluded IS NOT NULL" if excluded else "v.excluded IS NULL"], [kind]
    if source:
        where.append("v.source = ?")
        args.append(source)
    if direction:
        where.append("v.direction = ?")
        args.append(direction)
    if status:
        where.append(f"COALESCE(t.status, '{DEFAULT_STATUS}') = ?")
        args.append(status)
    elif hide_rejected:
        where.append("COALESCE(t.status, '') != 'не подходит'")
    if view in VIEWS:
        where.append(VIEWS[view])
    for m in mark:
        if m == "__none__":
            where.append("v.marks = '[]'")
        else:
            where.append("EXISTS (SELECT 1 FROM json_each(v.marks) WHERE value = ?)")
            args.append(m)
    for m in nomark:
        where.append("NOT EXISTS (SELECT 1 FROM json_each(v.marks) WHERE value = ?)")
        args.append(m)
    if only_new:
        where.append("COALESCE(t.seen, 0) = 0")
    if hide_dups:
        where.append("v.duplicate_of IS NULL")
    if hide_closed:
        where.append("v.source_status = 'active'")
    if has_salary:
        where.append("(v.salary_from IS NOT NULL OR v.salary_to IS NOT NULL)")
    if no_experience:
        # LOWER() в SQLite понижает регистр только у латиницы, поэтому перечисляем варианты написания
        variants = sorted({x for e in NO_EXPERIENCE for x in (e, e.capitalize())})
        where.append(f"COALESCE(v.experience, '') IN ({', '.join('?' * len(variants))})")
        args += variants
    if q:
        where.append("(v.name LIKE ? OR v.employer_name LIKE ? OR v.description LIKE ? OR v.snippet LIKE ?)")
        args += [f"%{q}%"] * 4
    return " AND ".join(where), args


@app.get("/api/vacancies")
def list_vacancies(
    kind: str = "main",
    direction: str | None = None,
    status: str | None = None,
    mark: list[str] = Query(default=[]),
    nomark: list[str] = Query(default=[]),
    only_new: bool = False,
    hide_dups: bool = False,
    hide_closed: bool = False,
    q: str | None = None,
    source: str | None = None,
    excluded: bool = False,
    hide_rejected: bool = False,
    view: str | None = None,
    has_salary: bool = False,
    no_experience: bool = False,
    limit: int = 200,
    offset: int = 0,
) -> dict:
    where, args = _vacancy_filters(
        kind, direction, status, mark, nomark, only_new, hide_dups, hide_closed, q, source, excluded, hide_rejected,
        view, has_salary, no_experience,
    )
    base = f"FROM vacancies v LEFT JOIN tracking t ON t.item_type = 'vacancy' AND t.item_id = v.id WHERE {where}"
    with conn() as c:
        total = c.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
        rows = c.execute(
            f"SELECT {VAC_LIST_COLS}, {TRACK_COLS} {base} ORDER BY v.published_at DESC, v.id DESC LIMIT ? OFFSET ?",
            [*args, _limit(limit), max(offset, 0)],
        ).fetchall()
        # счётчики для вкладок статусов: те же фильтры, кроме самого статуса
        w2, a2 = _vacancy_filters(
            kind, direction, None, mark, nomark, only_new, hide_dups, hide_closed, q, source, excluded,
            has_salary=has_salary, no_experience=no_experience,
        )
        counts = dict(c.execute(
            f"SELECT COALESCE(t.status, '{DEFAULT_STATUS}'), COUNT(*) FROM vacancies v LEFT JOIN tracking t "
            f"ON t.item_type = 'vacancy' AND t.item_id = v.id WHERE {w2} GROUP BY 1", a2,
        ).fetchall())
        # счётчики вкладок: те же фильтры, кроме самой вкладки
        view_counts = dict(c.execute(
            f"SELECT {VIEW_OF}, COUNT(*) FROM vacancies v LEFT JOIN tracking t "
            f"ON t.item_type = 'vacancy' AND t.item_id = v.id WHERE {w2} GROUP BY 1", a2,
        ).fetchall())
        stale_count = archive_stale(c, kind, apply=False)
        excluded_count = c.execute(
            "SELECT COUNT(*) FROM vacancies WHERE kind = ? AND excluded IS NOT NULL", (kind,)
        ).fetchone()[0]
    hl = SkillHighlighter(load_config().skills)
    return {
        "total": total, "items": [_card(r, hl) for r in rows], "status_counts": counts,
        "view_counts": view_counts, "excluded_count": excluded_count, "stale_count": stale_count,
        "stale_days": STALE_AFTER_DAYS,
    }


@app.get("/api/vacancies/{vac_id}")
def get_vacancy(vac_id: str) -> dict:
    with conn() as c:
        row = c.execute(
            f"SELECT v.*, {TRACK_COLS} FROM vacancies v "
            "LEFT JOIN tracking t ON t.item_type = 'vacancy' AND t.item_id = v.id WHERE v.id = ?",
            (vac_id,),
        ).fetchone()
        if not row:
            raise HTTPException(404, "нет такой вакансии")
        v = db.row_to_dict(row)
        related = []
        if v["duplicate_of"]:
            orig = c.execute("SELECT id, name, url FROM vacancies WHERE id = ?", (v["duplicate_of"],)).fetchone()
            if orig:
                v["original"] = dict(orig)
        for r in c.execute(
            "SELECT id, name, url, published_at FROM vacancies WHERE duplicate_of = ? ORDER BY published_at",
            (v["duplicate_of"] or v["id"],),
        ):
            if r["id"] != v["id"]:
                related.append(dict(r))
        v["repeats"] = related
    hl = SkillHighlighter(load_config().skills)
    text = v.get("description") or v.get("snippet") or ""
    v["segments"] = hl.segments(text)
    v["my_skills"] = hl.found("\n".join([v["name"], text, " ".join(v.get("key_skills") or [])]))
    return v


@app.patch("/api/tracking/{item_type}/{item_id}")
def update_tracking(item_type: str, item_id: str, data: dict = Body(...)) -> dict:
    if item_type not in ("vacancy", "post"):
        raise HTTPException(400, "item_type: vacancy | post")
    if "status" in data and data["status"] not in STATUSES:
        raise HTTPException(400, f"статус: {', '.join(STATUSES)}")
    if data.get("applied_at") and not (
        isinstance(data["applied_at"], str) and re.fullmatch(r"\d{4}-\d{2}-\d{2}", data["applied_at"])
    ):
        raise HTTPException(400, "дата отклика: ГГГГ-ММ-ДД")
    if data.get("notes") is not None and not isinstance(data.get("notes", ""), str):
        raise HTTPException(400, "заметка должна быть текстом")
    if data.get("reject_reason") not in (None, *REJECT_REASONS):
        raise HTTPException(400, f"причина: {', '.join(REJECT_REASONS)}")
    if "status" in data and data["status"] != "не подходит":
        data = {**data, "reject_reason": None}  # причина есть только у «не подходит»
    fields = {k: data[k] for k in ("status", "applied_at", "notes", "seen", "reject_reason") if k in data}
    if "seen" in fields:
        fields["seen"] = int(bool(fields["seen"]))
    now = db.now_iso()
    with conn() as c:
        table, key = ("vacancies", "id") if item_type == "vacancy" else ("tg_posts", "hash")
        if not c.execute(f"SELECT 1 FROM {table} WHERE {key} = ?", (item_id,)).fetchone():
            raise HTTPException(404, "нет такой записи")
        c.execute(
            "INSERT INTO tracking(item_type, item_id, updated_at) VALUES(?, ?, ?) ON CONFLICT DO NOTHING",
            (item_type, item_id, now),
        )
        if fields:
            sets = ", ".join(f"{k} = ?" for k in fields)
            c.execute(
                f"UPDATE tracking SET {sets}, updated_at = ? WHERE item_type = ? AND item_id = ?",
                [*fields.values(), now, item_type, item_id],
            )
        c.commit()
        row = c.execute("SELECT * FROM tracking WHERE item_type = ? AND item_id = ?", (item_type, item_id)).fetchone()
    return dict(row)


@app.post("/api/tracking/vacancy/archive-stale")
def archive_stale_vacancies(kind: str = "main") -> dict:
    """Убирает из ленты всё без решения старше STALE_AFTER_DAYS дней: в «Не подходит» с причиной «устарела»."""
    if kind not in ("main", "side"):
        raise HTTPException(400, "kind: main | side")
    with conn() as c:
        return {"archived": archive_stale(c, kind)}


@app.post("/api/tracking/{item_type}/seen-all")
def mark_all_seen(item_type: str, kind: str | None = None) -> dict:
    now = db.now_iso()
    with conn() as c:
        if item_type == "vacancy":
            ids = [r[0] for r in c.execute("SELECT id FROM vacancies WHERE kind = ?", (kind or "main",))]
        else:
            ids = [r[0] for r in c.execute("SELECT hash FROM tg_posts")]
        for i in ids:
            c.execute(
                "INSERT INTO tracking(item_type, item_id, seen, updated_at) VALUES(?, ?, 1, ?) "
                "ON CONFLICT(item_type, item_id) DO UPDATE SET seen = 1",
                (item_type, i, now),
            )
        c.commit()
    return {"updated": len(ids)}


# ------------------------------------------------------------------- посты


@app.get("/api/posts")
def list_posts(
    channel: str | None = None, status: str | None = None, only_new: bool = False,
    q: str | None = None, hide_rejected: bool = False, view: str | None = None, limit: int = 200, offset: int = 0,
) -> dict:
    where, args = [POST_VISIBLE], []
    if channel:
        where.append("p.channel = ?")
        args.append(channel)
    if only_new:
        where.append("COALESCE(t.seen, 0) = 0")
    if q:
        where.append("p.text LIKE ?")
        args.append(f"%{q}%")
    join = "FROM tg_posts p LEFT JOIN tracking t ON t.item_type = 'post' AND t.item_id = p.hash"
    count_where, count_args = " AND ".join(where), list(args)
    if status:
        where.append(f"COALESCE(t.status, '{DEFAULT_STATUS}') = ?")
        args.append(status)
    elif hide_rejected:
        where.append("COALESCE(t.status, '') != 'не подходит'")
    if view in VIEWS:
        where.append(VIEWS[view])
    base = f"{join} WHERE {' AND '.join(where)}"
    with conn() as c:
        total = c.execute(f"SELECT COUNT(*) {base}", args).fetchone()[0]
        rows = c.execute(
            f"SELECT p.*, {TRACK_COLS} {base} ORDER BY p.date DESC LIMIT ? OFFSET ?", [*args, _limit(limit), max(offset, 0)]
        ).fetchall()
        counts = dict(c.execute(
            f"SELECT COALESCE(t.status, '{DEFAULT_STATUS}'), COUNT(*) {join} WHERE {count_where} GROUP BY 1", count_args,
        ).fetchall())
        view_counts = dict(c.execute(
            f"SELECT {VIEW_OF}, COUNT(*) {join} WHERE {count_where} GROUP BY 1", count_args,
        ).fetchall())
    return {"total": total, "items": [db.row_to_dict(r) for r in rows], "status_counts": counts,
            "view_counts": view_counts}


# -------------------------------------------------------------------- логи


@app.get("/api/runs")
def list_runs(limit: int = 50) -> list[dict]:
    with conn() as c:
        return [db.row_to_dict(r) for r in c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT ?", (limit,))]


@app.get("/api/meta")
def meta() -> dict:
    cfg = load_config()
    with conn() as c:
        counts = {}
        for kind in ("main", "side"):
            counts[kind] = c.execute(
                "SELECT COUNT(*) FROM vacancies v LEFT JOIN tracking t ON t.item_type='vacancy' AND t.item_id=v.id "
                f"WHERE v.kind = ? AND v.excluded IS NULL AND {VIEWS['new']}", (kind,),
            ).fetchone()[0]
        counts["posts"] = c.execute(
            "SELECT COUNT(*) FROM tg_posts p LEFT JOIN tracking t ON t.item_type='post' AND t.item_id=p.hash "
            f"WHERE {VIEWS['new']} AND {POST_VISIBLE}"
        ).fetchone()[0]
        last = db.row_to_dict(c.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone())
        channels = [r[0] for r in c.execute("SELECT DISTINCT channel FROM tg_posts ORDER BY channel")]
        sources = [r[0] for r in c.execute("SELECT DISTINCT source FROM vacancies ORDER BY source")]
        stored = dict(zip(("total", "excluded", "compacted"), c.execute(
            "SELECT COUNT(*), COALESCE(SUM(excluded IS NOT NULL), 0), COALESCE(SUM(detail_status = 'compacted'), 0) "
            "FROM vacancies").fetchone()))
    return {
        "statuses": STATUSES,
        "directions": cfg.directions,
        "marks": [m["name"] for m in cfg.marks],
        "channels": channels,
        "sources": sources,
        "unseen": counts,
        "last_run": last,
        "stored": stored,
        "running": runs.running,
        "email_set": bool((cfg.hh.get("contact_email") or "").strip()),
        "version": panel_version().strftime("%Y%m%d%H%M%S"),
    }


# --------------------------------------------------------------- настройки


@app.get("/api/config/{name}")
def get_config(name: str) -> dict:
    if name not in settings_store.FILES:
        raise HTTPException(404, "нет такого раздела")
    return settings_store.read(name)


@app.put("/api/config/{name}")
def put_config(name: str, data: dict = Body(...)) -> dict:
    if name not in settings_store.FILES:
        raise HTTPException(404, "нет такого раздела")
    if runs.running:
        raise HTTPException(409, "идёт сбор, сохрани настройки после него")
    try:
        saved = settings_store.write(name, data)
    except settings_store.ValidationError as e:
        raise HTTPException(422, e.errors) from e
    if name == "channels":
        with conn() as c:
            refresh_post_keywords(c, saved.get("keywords") or [])
    if name == "exclude":
        with conn() as c:
            stats = refresh_exclusions(c, [r for r in saved.get("rules") or [] if r.get("enabled", True)])
        return {**saved, "_stats": stats}
    return saved


# ---------------------------------------------------------- дампы, экспорт


@app.get("/api/dumps")
def list_dumps() -> list[str]:
    if not DUMPS_DIR.exists():
        return []
    return sorted((p.name for p in DUMPS_DIR.glob("*.md")), reverse=True)


@app.get("/api/dumps/{name}")
def get_dump(name: str) -> FileResponse:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}\.md", name) or not (DUMPS_DIR / name).exists():
        raise HTTPException(404, "нет такого дампа")
    return FileResponse(DUMPS_DIR / name, media_type="text/markdown; charset=utf-8", filename=name)


@app.get("/api/export.xlsx")
def export_xlsx() -> StreamingResponse:
    from openpyxl import Workbook
    from openpyxl.cell.cell import ILLEGAL_CHARACTERS_RE
    from openpyxl.styles import Alignment, Font

    def append(ws, row: list) -> None:
        """Текст с площадок бывает с управляющими символами (Excel их не принимает) и с «=» в начале
        (стал бы формулой), поэтому чистим и пишем строго как текст."""
        row = [ILLEGAL_CHARACTERS_RE.sub("", x) if isinstance(x, str) else x for x in row]
        ws.append(row)
        for cell in ws[ws.max_row]:
            if isinstance(cell.value, str) and cell.value.startswith("="):
                cell.data_type = "s"

    status_map = {"active": "активна", "archived": "в архиве", "deleted": "удалена"}
    vac_headers = [
        "id", "источник", "дата публикации", "название", "компания", "тип работодателя", "зарплата от", "зарплата до",
        "валюта", "регион", "опыт", "ссылка", "направление", "найдено по запросу", "пометки",
        "возможный повтор", "отсеяно", "дата добавления", "статус в источнике", "статус", "дата отклика", "заметки",
        "полное описание",
    ]
    wb = Workbook()
    with conn() as c:
        for title, kind in (("Вакансии", "main"), ("Подработка", "side")):
            ws = wb.active if kind == "main" else wb.create_sheet(title)
            ws.title = title
            headers = vac_headers.copy()
            if kind == "side":
                headers.insert(headers.index("пометки"), "тип занятости")
            ws.append(headers)
            for r in c.execute(
                f"SELECT v.*, {TRACK_COLS} FROM vacancies v LEFT JOIN tracking t "
                "ON t.item_type='vacancy' AND t.item_id=v.id WHERE v.kind = ? ORDER BY v.published_at DESC",
                (kind,),
            ):
                v = db.row_to_dict(r)
                row = [
                    v["id"], v["source"], v["published_at"], v["name"], v["employer_name"], v["employer_type"], v["salary_from"],
                    v["salary_to"], v["currency"], v["area"], v["experience"], v["url"], v["direction"],
                    ", ".join(v["queries"]), ", ".join(v["marks"]), v["duplicate_of"] or "", ", ".join(v["excluded"]),
                    v["added_at"],
                    status_map.get(v["source_status"], v["source_status"]), v["status"], v["applied_at"],
                    v["notes"], (v["description"] or "")[:32000],
                ]
                if kind == "side":
                    row.insert(headers.index("тип занятости"), v["employment_form"])
                append(ws, row)
            for cell in ws[1]:
                cell.font = Font(bold=True)
            ws.freeze_panes = "A2"
        ws = wb.create_sheet("Телеграм")
        ws.append(["канал", "дата", "ссылка", "текст", "сработавшие слова", "статус", "заметки"])
        for r in c.execute(
            f"SELECT p.*, {TRACK_COLS} FROM tg_posts p LEFT JOIN tracking t "
            "ON t.item_type='post' AND t.item_id=p.hash ORDER BY p.date DESC"
        ):
            p = db.row_to_dict(r)
            append(ws, [p["channel"], p["date"], p["url"], p["text"][:32000], ", ".join(p["keywords"]), p["status"], p["notes"]])
        for cell in ws[1]:
            cell.font = Font(bold=True)
        for row in ws.iter_rows(min_row=2, min_col=4, max_col=4):
            row[0].alignment = Alignment(wrap_text=False)
    buf = io.BytesIO()
    wb.save(buf)
    buf.seek(0)
    name = f"vacancies-{datetime.now():%Y-%m-%d}.xlsx"
    return StreamingResponse(
        buf,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


def panel_version() -> datetime:
    """Время последнего изменения файлов панели и кода сервера."""
    files = [*STATIC_DIR.glob("*"), *Path(__file__).parent.glob("*.py")]
    return datetime.fromtimestamp(max(f.stat().st_mtime for f in files if f.is_file()))


@app.get("/", include_in_schema=False)
@app.get("/index.html", include_in_schema=False)
def index() -> HTMLResponse:
    """Страница панели. Ссылки на app.js и style.css с номером версии: после обновления
    браузер обязательно возьмёт новые файлы, а не старые из кэша."""
    v = panel_version()
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")
    html = html.replace("{{VERSION}}", v.strftime("%Y%m%d%H%M%S")).replace("{{VERSION_LABEL}}", v.strftime("%d.%m %H:%M"))
    return HTMLResponse(html)


app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")


def serve(port: int = 8000, open_browser: bool = True) -> None:
    import webbrowser

    import uvicorn

    import socket

    url = f"http://127.0.0.1:{port}"
    with socket.socket() as s:
        if s.connect_ex(("127.0.0.1", port)) == 0:
            # старый сервер со старым кодом продолжал бы работать, а новый не запустился бы
            print(f"Порт {port} занят: панель уже запущена в другом окне start.bat (возможно, со старой версией).")
            print("Закрой то окно и запусти start.bat заново.")
            return
    print(f"Панель: {url}  (версия {panel_version():%d.%m %H:%M}; закрой это окно, чтобы остановить)")
    if open_browser:
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
