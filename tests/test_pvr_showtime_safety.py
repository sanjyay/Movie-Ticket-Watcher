import json
from datetime import date, time

import httpx
import pytest
from sqlalchemy import select

from app.config import Settings
from app.models import DetectedShow, NotificationHistory, PlatformCheck
from app.platforms.pvrinox import PvrInoxAdapter, parse_pvr_showtime
from app.services.notifications import (
    TelegramProvider,
    format_pvr_theatre_message,
    record_pvr_theatre_notification,
)
from tests.conftest import FIXTURES

EXPECTED = [
    "09:00", "09:05", "09:10", "09:15", "09:20", "12:40", "12:50", "12:55",
    "16:00", "16:20", "16:30", "16:35", "19:40", "20:00", "20:10", "20:15",
    "23:40", "23:50", "23:55",
]


@pytest.mark.parametrize(
    ("raw", "normalized", "display"),
    [("19:40", "19:40", "7:40 PM"), ("2340", "23:40", "11:40 PM"),
     ("23:50", "23:50", "11:50 PM"), ("11:55 PM", "23:55", "11:55 PM")],
)
def test_authoritative_time_formats(raw, normalized, display) -> None:
    parsed = parse_pvr_showtime(raw)
    assert parsed.verified
    assert parsed.normalized == normalized
    assert parsed.display == display


def test_local_time_is_not_converted_and_explicit_utc_is_once() -> None:
    assert parse_pvr_showtime("19:40").normalized == "19:40"
    converted = parse_pvr_showtime("2026-07-23T14:10:00Z")
    assert converted.normalized == "19:40"
    assert converted.timezone_treatment == "explicit offset converted exactly once to Asia/Kolkata"


def test_unknown_time_fails_closed() -> None:
    parsed = parse_pvr_showtime("not-a-time")
    assert not parsed.verified and not parsed.normalized and not parsed.display


async def _result(watch, monkeypatch, fixture_data=None, preset="ANY"):
    watch.movie_name = "Jana Nayagan"
    watch.city = "Chennai"
    watch.show_date = date(2026, 7, 23)
    watch.language = "Tamil"
    watch.format = "2D"
    watch.time_preset = preset
    watch.start_time, watch.end_time = (
        (time(0), time(23, 59)) if preset == "ANY" else (time(17), time(21, 59))
    )
    watch.pvrinox_mode = "DIRECT"
    watch.pvrinox_direct_url = "https://www.pvrcinemas.com/moviesessions/chennai/jana-nayagan/33296"
    data = fixture_data or json.loads((FIXTURES / "pvr_palazzo_jana_2026-07-23.json").read_text())

    async def api(self, endpoint, payload, city):
        return data

    monkeypatch.setattr(PvrInoxAdapter, "_api_post", api)
    return await PvrInoxAdapter().search(watch)


async def test_captured_palazzo_sessions_match_page_not_phantoms(watch, monkeypatch) -> None:
    result = await _result(watch, monkeypatch)
    assert [show.normalized_time for show in result.shows] == EXPECTED
    assert "19:55" not in EXPECTED and "23:35" not in EXPECTED
    assert len(result.session_diagnostics) == 21
    assert all(show.theatre == "PVR Palazzo-The Nexus Vijaya Mall" for show in result.shows)
    seven_fifty_five = next(d for d in result.session_diagnostics if d["raw_time"] == "07:55 PM")
    assert not seven_fifty_five["bookable"] and not seven_fifty_five["matched_time_preset"]
    assert {"showTime", "showTimeStamp", "stopTimeStamp", "endTime"}.issubset(
        result.session_diagnostics[0]["time_fields"]
    )


async def test_wrong_time_fields_ignored_and_unknown_is_unverified(watch, monkeypatch) -> None:
    data = json.loads((FIXTURES / "pvr_palazzo_jana_2026-07-23.json").read_text())
    item = data["output"]["movieCinemaSessions"][0]["experienceSessions"][0]["shows"][0]
    item.update(showTime="unknown", endTime="07:55 PM", stopTime="11:35 PM")
    result = await _result(watch, monkeypatch, data)
    unknown = next(show for show in result.shows if show.session_id == "1")
    assert not unknown.time_verified and not unknown.display_time

    result = await _result(watch, monkeypatch, data, "EVENING")
    assert all(show.time_verified for show in result.shows)


def _settings() -> Settings:
    return Settings(_env_file=None, telegram_bot_token="123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef", telegram_default_chat_id="12345", container_deployment=False)


async def test_nineteen_sessions_one_alert_repeat_none_second_theatre_one(db, watch) -> None:
    payloads = []
    client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda request: (payloads.append(json.loads(request.content)) or httpx.Response(200, json={"ok": True}))
    ))
    provider = TelegramProvider(_settings(), client)
    watch.movie_name, watch.city, watch.show_date = "Jana Nayagan", "Chennai", date(2026, 7, 23)
    watch.language, watch.format = "Tamil", "2D"
    watch.time_preset, watch.start_time, watch.end_time = "ANY", time(0), time(23, 59)
    db.add(watch)
    db.flush()
    check = PlatformCheck(watch_id=watch.id, platform="PVR INOX", status="AVAILABLE")
    db.add(check)
    db.flush()

    def add_group(theatre: str):
        rows = []
        for index, hhmm in enumerate(EXPECTED, 1):
            value = time.fromisoformat(hhmm)
            row = DetectedShow(
                watch_id=watch.id, fingerprint=f"{theatre}-{index}", platform="PVR INOX",
                movie_title=watch.movie_name, theatre=theatre, show_date=watch.show_date,
                showtime=value, language="Tamil", format="2D", booking_url="https://example.test/pvr",
                city="Chennai", raw_time=hhmm, normalized_time=hhmm,
                display_time=value.strftime("%I:%M %p").lstrip("0"), time_source="showTime",
                time_verified=True, timezone_treatment="local", session_id=str(index),
            )
            db.add(row)
            rows.append(row)
        db.flush()
        return rows

    first = add_group("PVR Palazzo-The Nexus Vijaya Mall")
    assert await record_pvr_theatre_notification(db, watch.id, check.id, [x.id for x in first], provider)
    assert not await record_pvr_theatre_notification(db, watch.id, check.id, [x.id for x in first], provider)
    second = add_group("PVR VR Mall")
    assert await record_pvr_theatre_notification(db, watch.id, check.id, [x.id for x in second], provider)
    assert len(payloads) == 2
    assert "Available shows: 19" in payloads[0]["text"]
    assert "7:40 PM" in payloads[0]["text"] and "7:55 PM" not in payloads[0]["text"]
    assert db.scalars(select(NotificationHistory)).all()
    await client.aclose()


def test_unverified_time_is_omitted_from_telegram(watch) -> None:
    row = DetectedShow(
        watch_id=1, fingerprint="x", platform="PVR INOX", movie_title="Jana Nayagan",
        theatre="PVR Palazzo", show_date=date(2026, 7, 23), showtime=time(0),
        language="Tamil", format="2D", booking_url="https://example.test/pvr", city="Chennai",
        raw_time="unknown", time_source="showTime", time_verified=False,
    )
    watch.movie_name, watch.city = "Jana Nayagan", "Chennai"
    message = format_pvr_theatre_message(watch, [row])
    assert "SHOWTIME_UNVERIFIED" not in message
    assert "Exact showtimes could not be verified." in message
    assert "Showtime:" not in message and "12:00 AM" not in message
