import asyncio
import logging
from datetime import datetime, timezone

from sqlalchemy import or_, select

from app.database import SessionLocal
from app.models import RuntimeState, Watch
from app.services.watcher import run_watch_check

logger = logging.getLogger(__name__)


async def worker_loop(stop: asyncio.Event) -> None:
    while not stop.is_set():
        with SessionLocal() as db:
            heartbeat = db.get(RuntimeState, "worker_heartbeat")
            if heartbeat is None:
                heartbeat = RuntimeState(key="worker_heartbeat")
                db.add(heartbeat)
            heartbeat.value = datetime.now(timezone.utc).isoformat()
            db.commit()
            due = db.scalars(
                select(Watch).where(
                    Watch.enabled.is_(True),
                    or_(
                        Watch.next_check_at.is_(None),
                        Watch.next_check_at <= datetime.now(timezone.utc),
                    ),
                )
            ).all()
            for watch in due:
                try:
                    await run_watch_check(db, watch)
                    logger.info(
                        "watch_checked id=%s status=%s matches=%s",
                        watch.id,
                        watch.last_status,
                        watch.matching_show_count,
                    )
                except Exception:
                    db.rollback()
                    logger.exception("watch_check_crashed id=%s", watch.id)
            successful = db.get(RuntimeState, "last_successful_cycle")
            if successful is None:
                successful = RuntimeState(key="last_successful_cycle")
                db.add(successful)
            successful.value = datetime.now(timezone.utc).isoformat()
            db.commit()
        try:
            await asyncio.wait_for(stop.wait(), timeout=15)
        except TimeoutError:
            pass
