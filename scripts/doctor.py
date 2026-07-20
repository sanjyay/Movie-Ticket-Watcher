#!/usr/bin/env python3
import asyncio
import importlib
import os
import socket
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import text

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from app.config import get_settings  # noqa: E402
from app.database import SessionLocal, init_db  # noqa: E402
from app.models import RuntimeState  # noqa: E402
from app.platforms.parsing import parse_shows  # noqa: E402


def report(name: str, ok: bool, detail: str, essential: bool = True) -> bool:
    print(f"[{'PASS' if ok else 'FAIL' if essential else 'WARN'}] {name}: {detail}")
    return ok or not essential


async def browser_check(screenshot_dir: Path) -> tuple[bool, str]:
    target = screenshot_dir / "doctor-browser.png"
    fixture = ROOT / "tests/fixtures/booking_open.html"
    try:
        from playwright.async_api import async_playwright

        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(headless=True)
            page = await browser.new_page()
            await page.goto(fixture.as_uri())
            await page.screenshot(path=target)
            ok = target.stat().st_size > 0
            await browser.close()
        target.unlink(missing_ok=True)
        return ok, "Chromium loaded a local fixture and wrote/removed a screenshot"
    except Exception as exc:
        target.unlink(missing_ok=True)
        return False, str(exc)


async def main() -> int:
    settings = get_settings()
    results = [report("Python", sys.version_info >= (3, 10), sys.version.split()[0])]
    required = ["fastapi", "sqlalchemy", "playwright", "httpx", "bs4"]
    missing = [name for name in required if importlib.util.find_spec(name) is None]
    results.append(report("Packages", not missing, "all present" if not missing else str(missing)))
    try:
        init_db()
        with SessionLocal() as db:
            db.execute(text("CREATE TABLE IF NOT EXISTS doctor_probe (value INTEGER)"))
            db.execute(text("INSERT INTO doctor_probe VALUES (1)"))
            db.execute(text("DELETE FROM doctor_probe"))
            db.commit()
        results.append(report("SQLite read/write", True, settings.database_url))
    except Exception as exc:
        results.append(report("SQLite read/write", False, str(exc)))
    for name, directory in {
        "data": settings.data_dir,
        "config": settings.config_dir,
        "screenshots": settings.screenshot_dir,
        "logs": settings.log_dir,
        "backups": settings.backup_dir,
    }.items():
        try:
            directory.mkdir(parents=True, exist_ok=True)
            with tempfile.NamedTemporaryFile(dir=directory):
                pass
            results.append(report(f"Writable {name}", True, str(directory)))
        except OSError as exc:
            results.append(report(f"Writable {name}", False, str(exc)))
    fixture = ROOT / "tests/fixtures/booking_open.html"
    shows, _ = parse_shows(fixture.read_text(), "doctor")
    results.append(report("Fixture parser", len(shows) == 1, f"{len(shows)} show(s)"))
    browser_ok, detail = await browser_check(settings.screenshot_dir)
    results.append(report("Playwright Chromium", browser_ok, detail))
    try:
        socket.getaddrinfo("ntfy.sh", 443)
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(settings.ntfy_server)
        results.append(report("DNS and outbound HTTPS", response.status_code < 500, f"HTTP {response.status_code}"))
    except Exception as exc:
        results.append(report("DNS and outbound HTTPS", False, str(exc)))
    ntfy = urlparse(settings.ntfy_server)
    results.append(report("ntfy configuration", ntfy.scheme == "https" and bool(ntfy.netloc), settings.ntfy_server))
    try:
        response = httpx.get(f"http://127.0.0.1:{settings.app_port}/health", timeout=5)
        results.append(report("Web health", response.status_code == 200, f"HTTP {response.status_code}"))
    except Exception as exc:
        results.append(report("Web health", False, str(exc), essential=False))
    try:
        with SessionLocal() as db:
            state = db.get(RuntimeState, "worker_heartbeat")
        stamp = datetime.fromisoformat(state.value) if state else None
        if stamp and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else float("inf")
        results.append(report("Worker heartbeat", age <= settings.worker_heartbeat_max_age_seconds, f"age={age:.0f}s", essential=False))
    except Exception as exc:
        results.append(report("Worker heartbeat", False, str(exc), essential=False))
    tz_ok = datetime.now(ZoneInfo(settings.app_timezone)).tzname() is not None and os.environ.get("TZ", settings.app_timezone) == settings.app_timezone
    results.append(report("Timezone", tz_ok, f"Python={settings.app_timezone} TZ={os.environ.get('TZ', 'unset')}"))
    results.append(report("Runtime identity", os.getuid() != 0, f"uid={os.getuid()} gid={os.getgid()} PUID={os.environ.get('PUID')} PGID={os.environ.get('PGID')}"))
    return 0 if all(results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
