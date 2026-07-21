#!/usr/bin/env python3
import argparse
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
from sqlalchemy import text

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.config import get_settings  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import RuntimeState  # noqa: E402


def heartbeat_healthy(key: str) -> bool:
    settings = get_settings()
    with SessionLocal() as db:
        db.execute(text("SELECT 1"))
        state = db.get(RuntimeState, key)
        if not state:
            return False
        heartbeat = datetime.fromisoformat(state.value)
        if heartbeat.tzinfo is None:
            heartbeat = heartbeat.replace(tzinfo=timezone.utc)
        return (
            datetime.now(timezone.utc) - heartbeat
        ).total_seconds() <= settings.worker_heartbeat_max_age_seconds


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--web", action="store_true")
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--telegram-bot", action="store_true")
    args = parser.parse_args()
    try:
        if args.worker:
            return 0 if heartbeat_healthy("worker_heartbeat") else 1
        if args.telegram_bot:
            return 0 if heartbeat_healthy("telegram_bot_heartbeat") else 1
        port = os.environ.get("APP_PORT", "8787")
        response = httpx.get(f"http://127.0.0.1:{port}/health", timeout=5)
        return 0 if response.status_code == 200 and response.json().get("status") == "ok" else 1
    except Exception as exc:
        print(f"healthcheck failed: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
