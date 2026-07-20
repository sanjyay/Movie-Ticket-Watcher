#!/usr/bin/env python3
import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings


def sqlite_path() -> Path:
    url = get_settings().database_url
    if not url.startswith("sqlite:///"):
        raise SystemExit("Backup supports SQLite only")
    return Path(url.removeprefix("sqlite:///"))


def valid_database(path: Path) -> bool:
    try:
        with sqlite3.connect(f"file:{path}?mode=ro", uri=True) as db:
            return db.execute("PRAGMA integrity_check").fetchone() == ("ok",)
    except sqlite3.Error:
        return False


def backup(destination: Path | None = None) -> Path:
    settings = get_settings()
    source = sqlite_path()
    settings.backup_dir.mkdir(parents=True, exist_ok=True)
    destination = destination or settings.backup_dir / f"tickets-{datetime.now(timezone.utc):%Y%m%dT%H%M%SZ}.db"
    with sqlite3.connect(source) as src, sqlite3.connect(destination) as dst:
        src.backup(dst)
    backups = sorted(settings.backup_dir.glob("tickets-*.db"), reverse=True)
    for expired in backups[settings.backup_retention :]:
        expired.unlink()
    print(destination)
    return destination


def restore(source: Path) -> None:
    source = source.resolve()
    if not source.is_file() or not valid_database(source):
        raise SystemExit(f"Not a valid SQLite backup: {source}")
    current = sqlite_path()
    if current.exists():
        backup()
    temporary = current.with_suffix(".restore.tmp")
    with sqlite3.connect(source) as src, sqlite3.connect(temporary) as dst:
        src.backup(dst)
    temporary.replace(current)
    print(current)


parser = argparse.ArgumentParser()
sub = parser.add_subparsers(dest="command", required=True)
sub.add_parser("backup")
restore_parser = sub.add_parser("restore")
restore_parser.add_argument("path", type=Path)
args = parser.parse_args()
backup() if args.command == "backup" else restore(args.path)
