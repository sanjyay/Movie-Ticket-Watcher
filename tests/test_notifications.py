import json

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import DetectedShow, NotificationHistory, PlatformCheck
from app.schemas import ShowResult
from app.services.notifications import (
    TelegramDeliveryError,
    TelegramProvider,
    effective_chat_id,
    format_show_message,
    record_notification,
    safe_booking_url,
    sanitize_telegram_error,
    validate_chat_id,
)

TOKEN = "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"


def settings(**values) -> Settings:
    values.setdefault("telegram_bot_token", TOKEN)
    values.setdefault("telegram_default_chat_id", "12345")
    values.setdefault("container_deployment", False)
    return Settings(
        _env_file=None,
        **values,
    )


def show(watch, *, url="https://example.test/book", theatre="PVR & Cinéma <One>"):
    return ShowResult(
        "BookMyShow",
        watch.movie_name,
        theatre,
        watch.show_date,
        watch.start_time,
        "தமிழ் & English",
        "2D > IMAX",
        url,
        watch.city,
    )


@pytest.mark.parametrize("value", ["1", "987654321", "-1", "-1001234567890"])
def test_valid_chat_ids(value: str) -> None:
    assert validate_chat_id(value) == value


@pytest.mark.parametrize("value", ["", "0", "-0", "+123", "12.3", "abc", "--123"])
def test_invalid_chat_ids(value: str) -> None:
    with pytest.raises(ValueError, match="invalid"):
        validate_chat_id(value)


def test_watch_override_precedes_default(watch) -> None:
    watch.telegram_chat_id_override = "-10099"
    assert effective_chat_id(watch, settings()) == "-10099"


def test_missing_effective_chat_id(watch) -> None:
    with pytest.raises(ValueError, match="missing"):
        effective_chat_id(watch, settings(telegram_default_chat_id=""))


def test_plain_text_content_preserves_special_and_unicode(watch) -> None:
    watch.movie_name = "காதல் & <Hope> 'quoted' 🎬"
    message = format_show_message(watch, show(watch))
    assert watch.movie_name in message
    assert "PVR & Cinéma <One>" in message
    assert "தமிழ் & English" in message


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.test/a?q=1", True),
        ("http://example.test", True),
        ("javascript:alert(1)", False),
        ("file:///tmp/a", False),
        ("data:text/plain,x", False),
        ("not a url", False),
    ],
)
def test_booking_url_allowlist(url: str, expected: bool) -> None:
    assert bool(safe_booking_url(url)) is expected


async def test_successful_alert_and_inline_button(db, watch) -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True, "result": {}})

    provider = TelegramProvider(
        settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    db.add(watch)
    db.commit()
    result = show(watch)
    result.language = watch.language
    result.format = watch.format
    check = PlatformCheck(watch_id=watch.id, platform=result.platform, status="AVAILABLE")
    detected = DetectedShow(
        watch_id=watch.id,
        fingerprint=result.fingerprint,
        platform=result.platform,
        movie_title=result.movie_title,
        theatre=result.theatre,
        show_date=result.date,
        showtime=result.showtime,
        language=result.language,
        format=result.format,
        booking_url=result.booking_url,
        city=result.city,
    )
    db.add_all([check, detected])
    db.flush()
    assert await record_notification(db, watch.id, check.id, detected.id, provider)
    db.commit()
    assert requests[0]["reply_markup"]["inline_keyboard"][0][0]["url"].startswith("https://")
    history = db.scalar(select(NotificationHistory))
    assert history.success and history.provider == "telegram" and not history.is_test
    await provider.client.aclose()


async def test_live_alert_requires_persisted_active_records(db, watch) -> None:
    db.add(watch)
    db.commit()
    provider = TelegramProvider(
        settings(),
        httpx.AsyncClient(
            transport=httpx.MockTransport(
                lambda _request: (_ for _ in ()).throw(AssertionError("must not send"))
            )
        ),
    )
    assert not await record_notification(db, watch.id, 999, 999, provider)
    watch.enabled = False
    result = show(watch)
    check = PlatformCheck(watch_id=watch.id, platform=result.platform, status="AVAILABLE")
    detected = DetectedShow(
        watch_id=watch.id,
        fingerprint=result.fingerprint,
        platform=result.platform,
        movie_title=result.movie_title,
        theatre=result.theatre,
        show_date=result.date,
        showtime=result.showtime,
        language=result.language,
        format=result.format,
        booking_url=result.booking_url,
        city=result.city,
    )
    db.add_all([check, detected])
    db.flush()
    assert not await record_notification(db, watch.id, check.id, detected.id, provider)
    await provider.client.aclose()


async def test_test_and_simulation_labels(watch) -> None:
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    provider = TelegramProvider(
        settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await provider.send_test(watch)
    await provider.send_simulation(watch, show(watch))
    assert payloads[0]["text"].startswith("TEST — Movie Ticket Watcher")
    assert "No real ticket availability was detected" in payloads[0]["text"]
    assert payloads[1]["text"].startswith("SIMULATION — Ticket availability")
    assert "does not represent a real booking" in payloads[1]["text"]
    await provider.client.aclose()


async def test_invalid_url_has_no_button(watch) -> None:
    payloads = []

    def handler(request: httpx.Request) -> httpx.Response:
        payloads.append(json.loads(request.content))
        return httpx.Response(200, json={"ok": True})

    provider = TelegramProvider(
        settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler))
    )
    await provider.send_show(watch, show(watch, url="javascript:alert(1)"))
    assert "reply_markup" not in payloads[0]
    await provider.client.aclose()


async def test_429_honors_retry_after(watch) -> None:
    calls, sleeps = 0, []

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={
                    "ok": False,
                    "description": "Too Many Requests",
                    "parameters": {"retry_after": 7},
                },
            )
        return httpx.Response(200, json={"ok": True})

    async def fake_sleep(value: float) -> None:
        sleeps.append(value)

    provider = TelegramProvider(
        settings(), httpx.AsyncClient(transport=httpx.MockTransport(handler)), fake_sleep
    )
    assert await provider.send_test(watch) == 2
    assert calls == 2 and sleeps == [7]
    await provider.client.aclose()


async def test_temporary_failure_retries_but_permanent_does_not(watch) -> None:
    temporary_calls = 0

    def temporary(_request: httpx.Request) -> httpx.Response:
        nonlocal temporary_calls
        temporary_calls += 1
        return httpx.Response(503, json={"ok": False, "description": "unavailable"})

    async def no_sleep(_value: float) -> None:
        return None

    provider = TelegramProvider(
        settings(), httpx.AsyncClient(transport=httpx.MockTransport(temporary)), no_sleep
    )
    with pytest.raises(TelegramDeliveryError):
        await provider.send_test(watch)
    assert temporary_calls == 3
    await provider.client.aclose()

    permanent_calls = 0

    def permanent(_request: httpx.Request) -> httpx.Response:
        nonlocal permanent_calls
        permanent_calls += 1
        return httpx.Response(
            403, json={"ok": False, "description": "Forbidden: bot was blocked by the user"}
        )

    provider = TelegramProvider(
        settings(), httpx.AsyncClient(transport=httpx.MockTransport(permanent)), no_sleep
    )
    with pytest.raises(TelegramDeliveryError):
        await provider.send_test(watch)
    assert permanent_calls == 1
    await provider.client.aclose()


def test_token_is_sanitized() -> None:
    error = sanitize_telegram_error(
        f"failed https://api.telegram.org/bot{TOKEN}/sendMessage", TOKEN
    )
    assert TOKEN not in error
    assert "[redacted]" in error
