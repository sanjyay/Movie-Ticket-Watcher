#!/usr/bin/env python3
import asyncio
import sys
from pathlib import Path

from sqlalchemy import select

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Watch  # noqa: E402
from app.services.notifications import TelegramProvider  # noqa: E402


async def main() -> int:
    init_db()
    with SessionLocal() as db:
        watch = db.scalar(select(Watch).order_by(Watch.id))
        if not watch:
            print("No watch exists; create one before testing Telegram.", file=sys.stderr)
            return 2
        await TelegramProvider().send_test(watch)
    print("Telegram test delivery succeeded")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
