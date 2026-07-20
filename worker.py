import asyncio
import fcntl
import logging
import signal
from pathlib import Path

from app.config import get_settings
from app.database import init_db
from app.services.scheduler import worker_loop


async def main() -> None:
    settings = get_settings()
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = Path(settings.data_dir) / "worker.lock"
    with lock_path.open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another worker already holds the lock") from None
        init_db()
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        await worker_loop(stop)


if __name__ == "__main__":
    asyncio.run(main())
