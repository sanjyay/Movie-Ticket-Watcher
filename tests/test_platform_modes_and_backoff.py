import json
from datetime import datetime, timezone

from sqlalchemy import func, select

from app.models import NotificationHistory, PlatformRetryState, PlatformState
from app.schemas import PlatformResult, ShowResult
from app.services.watcher import aggregate_watch_status, run_watch_check
from tests.conftest import FIXTURES


def _result(platform: str, status: str, watch, *, available: bool = False) -> PlatformResult:
    shows = []
    if available:
        shows = [
            ShowResult(
                platform,
                watch.movie_name,
                "PVR Forum",
                watch.show_date,
                watch.start_time,
                watch.language,
                watch.format,
                "https://example.invalid/show",
                watch.city,
            )
        ]
    return PlatformResult(
        platform,
        status,
        shows,
        error="blocked" if status == PlatformState.BLOCKED else "",
    )


def test_aggregate_blocked_and_unavailable_is_partial() -> None:
    assert aggregate_watch_status([PlatformState.BLOCKED, PlatformState.UNAVAILABLE]) == "PARTIAL"


def test_aggregate_blocked_and_available_is_available() -> None:
    assert aggregate_watch_status([PlatformState.BLOCKED, PlatformState.AVAILABLE]) == "AVAILABLE"


def test_aggregate_both_blocked() -> None:
    assert (
        aggregate_watch_status([PlatformState.BLOCKED, PlatformState.BLOCKED])
        == "ALL_PLATFORMS_BLOCKED"
    )


async def test_block_cooldown_persists_and_other_platform_continues(db, watch, monkeypatch) -> None:
    calls = {"BookMyShow": 0, "PVR INOX": 0}

    async def blocked(self, target):
        calls[self.name] += 1
        return _result(self.name, PlatformState.BLOCKED, target)

    async def unavailable(self, target):
        calls[self.name] += 1
        return _result(self.name, PlatformState.UNAVAILABLE, target)

    monkeypatch.setattr("app.platforms.bookmyshow.BookMyShowAdapter.search", blocked)
    monkeypatch.setattr("app.platforms.pvrinox.PvrInoxAdapter.search", unavailable)
    db.add(watch)
    db.commit()
    await run_watch_check(db, watch)
    state = db.scalar(select(PlatformRetryState).where(PlatformRetryState.platform == "BookMyShow"))
    assert state is not None
    assert state.consecutive_block_count == 1
    blocked_until = state.blocked_until
    if blocked_until.tzinfo is None:
        blocked_until = blocked_until.replace(tzinfo=timezone.utc)
    assert blocked_until > datetime.now(timezone.utc)
    assert watch.last_status == "PARTIAL"

    await run_watch_check(db, watch)
    assert calls == {"BookMyShow": 1, "PVR INOX": 2}


async def test_manual_retry_bypasses_cooldown_once_without_block_notification(
    db, watch, monkeypatch
) -> None:
    calls = 0

    async def blocked(self, target):
        nonlocal calls
        calls += 1
        return _result(self.name, PlatformState.BLOCKED, target)

    async def unavailable(self, target):
        return _result(self.name, PlatformState.UNAVAILABLE, target)

    monkeypatch.setattr("app.platforms.bookmyshow.BookMyShowAdapter.search", blocked)
    monkeypatch.setattr("app.platforms.pvrinox.PvrInoxAdapter.search", unavailable)
    db.add(watch)
    db.commit()
    await run_watch_check(db, watch)
    await run_watch_check(db, watch)
    assert calls == 1
    await run_watch_check(db, watch, only_platform="BookMyShow", bypass_cooldown=True)
    assert calls == 2
    state = db.scalar(select(PlatformRetryState).where(PlatformRetryState.platform == "BookMyShow"))
    assert state.consecutive_block_count == 2
    assert db.scalar(select(func.count()).select_from(NotificationHistory)) == 0


async def test_disabled_mode_does_not_invoke_adapter(db, watch, monkeypatch) -> None:
    watch.bookmyshow_mode = "DISABLED"
    watch.bookmyshow_enabled = False
    invoked = False

    async def unexpected(self, target):
        nonlocal invoked
        invoked = True
        return _result(self.name, PlatformState.ERROR, target)

    async def unavailable(self, target):
        return _result(self.name, PlatformState.UNAVAILABLE, target)

    monkeypatch.setattr("app.platforms.bookmyshow.BookMyShowAdapter.search", unexpected)
    monkeypatch.setattr("app.platforms.pvrinox.PvrInoxAdapter.search", unavailable)
    db.add(watch)
    db.commit()
    await run_watch_check(db, watch)
    assert not invoked


def load_json(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())
