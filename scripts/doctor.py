#!/usr/bin/env python3
import argparse
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
from app.services.notifications import (  # noqa: E402
    TelegramProvider,
    sanitize_telegram_error,
    validate_chat_id,
)


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


async def main(test_telegram: bool = False) -> int:
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
    telegram = urlparse(settings.telegram_api_base)
    results.append(
        report(
            "Telegram API base",
            telegram.scheme == "https" and bool(telegram.hostname),
            "valid HTTPS URL" if telegram.scheme == "https" and telegram.hostname else "invalid",
        )
    )
    results.append(
        report(
            "Telegram bot token",
            bool(settings.telegram_bot_token),
            "present" if settings.telegram_bot_token else "missing",
            essential=False,
        )
    )
    chat_ok = False
    if settings.telegram_default_chat_id:
        try:
            validate_chat_id(settings.telegram_default_chat_id)
            chat_ok = True
        except ValueError:
            pass
    results.append(
        report(
            "Telegram default chat ID",
            chat_ok,
            "valid" if chat_ok else "missing or invalid",
            essential=False,
        )
    )
    allowed_ok = True
    allowed_count = 0
    try:
        values = [
            item.strip() for item in settings.telegram_allowed_chat_ids.split(",") if item.strip()
        ]
        for value in values:
            validate_chat_id(value)
        allowed_count = len(values)
        allowed_ok = allowed_count > 0
    except ValueError:
        allowed_ok = False
    results.append(
        report(
            "Telegram allowed chat IDs",
            allowed_ok,
            f"{allowed_count} valid ID(s)" if allowed_ok else "missing or invalid",
            essential=False,
        )
    )
    try:
        socket.getaddrinfo(telegram.hostname or "", 443)
        async with httpx.AsyncClient(timeout=8) as client:
            response = await client.get(settings.telegram_api_base)
        results.append(
            report(
                "Telegram API DNS and HTTPS",
                response.status_code < 500,
                f"HTTP {response.status_code}",
            )
        )
    except Exception as exc:
        detail = sanitize_telegram_error(exc, settings.telegram_bot_token)
        results.append(report("Telegram API DNS and HTTPS", False, detail))
    if settings.telegram_bot_token:
        try:
            endpoint = f"{settings.telegram_api_base.rstrip('/')}/bot{settings.telegram_bot_token}/getWebhookInfo"
            async with httpx.AsyncClient(timeout=8) as client:
                response = await client.post(endpoint)
            info = response.json().get("result", {})
            conflict = bool(info.get("url"))
            results.append(
                report(
                    "Telegram long polling webhook",
                    not conflict,
                    "webhook conflict" if conflict else "no webhook configured",
                    essential=False,
                )
            )
        except Exception as exc:
            results.append(
                report(
                    "Telegram long polling webhook",
                    False,
                    sanitize_telegram_error(exc, settings.telegram_bot_token),
                    essential=False,
                )
            )
    if test_telegram:
        try:
            from types import SimpleNamespace

            await TelegramProvider().send_test(SimpleNamespace(telegram_chat_id_override=""))
            results.append(report("Telegram test delivery", True, "succeeded"))
        except Exception as exc:
            detail = sanitize_telegram_error(exc, settings.telegram_bot_token)
            results.append(report("Telegram test delivery", False, detail))
    try:
        response = httpx.get(f"http://127.0.0.1:{settings.app_port}/health", timeout=5)
        results.append(
            report("Web health", response.status_code == 200, f"HTTP {response.status_code}")
        )
    except Exception as exc:
        results.append(report("Web health", False, str(exc), essential=False))
    try:
        with SessionLocal() as db:
            state = db.get(RuntimeState, "worker_heartbeat")
        stamp = datetime.fromisoformat(state.value) if state else None
        if stamp and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else float("inf")
        results.append(
            report(
                "Worker heartbeat",
                age <= settings.worker_heartbeat_max_age_seconds,
                f"age={age:.0f}s",
                essential=False,
            )
        )
    except Exception as exc:
        results.append(report("Worker heartbeat", False, str(exc), essential=False))
    try:
        with SessionLocal() as db:
            bot_state = db.get(RuntimeState, "telegram_bot_heartbeat")
            offset = db.get(RuntimeState, "telegram_update_offset")
            db.execute(text("SELECT value FROM runtime_state WHERE key = 'telegram_update_offset'"))
        stamp = datetime.fromisoformat(bot_state.value) if bot_state else None
        if stamp and stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=timezone.utc)
        age = (datetime.now(timezone.utc) - stamp).total_seconds() if stamp else float("inf")
        results.append(
            report(
                "Telegram bot heartbeat",
                age <= settings.worker_heartbeat_max_age_seconds,
                f"age={age:.0f}s",
                essential=False,
            )
        )
        results.append(
            report(
                "Telegram update offset",
                True,
                offset.value if offset else "not initialized",
                essential=False,
            )
        )
    except Exception as exc:
        results.append(
            report(
                "Telegram bot state",
                False,
                sanitize_telegram_error(exc, settings.telegram_bot_token),
                essential=False,
            )
        )
    tz_ok = (
        datetime.now(ZoneInfo(settings.app_timezone)).tzname() is not None
        and os.environ.get("TZ", settings.app_timezone) == settings.app_timezone
    )
    results.append(
        report(
            "Timezone", tz_ok, f"Python={settings.app_timezone} TZ={os.environ.get('TZ', 'unset')}"
        )
    )
    results.append(
        report(
            "Runtime identity",
            os.getuid() != 0,
            f"uid={os.getuid()} gid={os.getgid()} PUID={os.environ.get('PUID')} PGID={os.environ.get('PGID')}",
        )
    )
    return 0 if all(results) else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--test-telegram",
        action="store_true",
        help="send one clearly labelled Telegram test message",
    )
    args = parser.parse_args()
    raise SystemExit(asyncio.run(main(args.test_telegram)))
