"""Командная строка.

  python -m jobauto run     — сбор (hh + Telegram) и дамп
  python -m jobauto check   — подробная проверка доступа, отчёт в data/logs
  python -m jobauto serve   — панель в браузере (по умолчанию)
"""

from __future__ import annotations

import argparse
import sys

from datetime import datetime

from . import db
from .config import BACKUP_DIR, DATA_DIR, DB_PATH, LOG_DIR, ensure_config, load_config
from .maintenance import cleanup_files
from .diagnostics import quick_network_summary, run_diagnostics
from .runner import Runner


def cmd_run() -> int:
    cfg = load_config()
    cleanup_files([LOG_DIR, DATA_DIR / "probe"])
    backup = db.backup(DB_PATH, BACKUP_DIR)
    if backup:
        print(f"резервная копия базы: {backup.name}")
    conn = db.connect(DB_PATH)
    log_path = LOG_DIR / f"run-{datetime.now():%Y%m%d-%H%M%S}.txt"
    result = Runner(conn, cfg, log_path=log_path, on_hh_blocked=quick_network_summary).run()
    print()
    print(f"готово: {result.status}")
    print(f"  запросов к hh: {result.requests}")
    print(f"  новых вакансий: {result.new_main}, подработки: {result.new_side}, постов: {result.new_posts}")
    if result.skipped_channels:
        print(f"  пропущены каналы: {', '.join('@' + c for c in result.skipped_channels)}")
    if result.errors:
        print(f"  ошибок: {len(result.errors)}")
        for e in result.errors:
            print(f"    - {e}")
    print(f"  дамп: {result.dump_path or 'нового нет, файл не создан'}")
    print(f"  подробный лог: {log_path}")
    return 0 if result.status == "ok" else 1


def cmd_check() -> int:
    print("Проверяю доступ, это займёт до минуты...")
    report, path = run_diagnostics(load_config(), LOG_DIR)
    print(report)
    print(f"Отчёт сохранён: {path}")
    return 0 if "hh.ru отвечает нормально" in report else 1


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="jobauto")
    parser.add_argument("command", choices=["run", "check", "serve"], nargs="?", default="serve")
    args = parser.parse_args(argv)
    created = ensure_config()
    if created:
        print(f"созданы настройки по умолчанию в config/: {', '.join(created)}")
    if args.command == "run":
        return cmd_run()
    if args.command == "check":
        return cmd_check()
    from .api import serve

    serve()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
