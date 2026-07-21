import asyncio
import json
import random
from datetime import datetime, timedelta, timezone

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import (
    DetectedShow,
    PlatformCheck,
    PlatformMode,
    PlatformRetryState,
    PlatformState,
    StateTransition,
    Watch,
)
from app.platforms.bookmyshow import BookMyShowAdapter
from app.platforms.pvrinox import PvrInoxAdapter
from app.schemas import PlatformResult, ShowResult
from app.services.notifications import record_notification, record_simulation_notification

PLATFORM_NAMES = ("BookMyShow", "PVR INOX")


def platform_mode(watch: Watch, platform: str) -> str:
    if platform == "BookMyShow":
        mode = watch.bookmyshow_mode
        legacy_enabled = watch.bookmyshow_enabled
    else:
        mode = watch.pvrinox_mode
        legacy_enabled = watch.pvrinox_enabled
    if not mode:
        return PlatformMode.AUTOMATIC if legacy_enabled else PlatformMode.DISABLED
    return str(mode).upper()


def enabled_platforms(watch: Watch) -> list[str]:
    return [name for name in PLATFORM_NAMES if platform_mode(watch, name) != PlatformMode.DISABLED]


def adapter_for(platform: str):  # type: ignore[no-untyped-def]
    if platform == "BookMyShow":
        return BookMyShowAdapter()
    if platform == "PVR INOX":
        return PvrInoxAdapter()
    raise ValueError(f"Unknown platform: {platform}")


def adapters_for(watch: Watch):  # type: ignore[no-untyped-def]
    return [adapter_for(name) for name in enabled_platforms(watch)]


def simulation_result(watch: Watch, platform: str) -> PlatformResult:
    state = watch.simulation_state.upper()
    mode = platform_mode(watch, platform)
    if state == "AVAILABLE":
        show = ShowResult(
            platform,
            watch.movie_name,
            "Simulation Cinema",
            watch.show_date,
            watch.start_time,
            watch.language,
            watch.format,
            "https://example.invalid/simulated-booking",
            watch.city,
        )
        return PlatformResult(
            platform,
            PlatformState.AVAILABLE,
            [show],
            "simulation: matching show",
            configured_mode=mode,
            matching_count=1,
            raw_candidate_count=1,
            phase="simulation",
            parser_version="simulation-v1",
        )
    if state == "BLOCKED":
        return PlatformResult(
            platform,
            PlatformState.BLOCKED,
            error="simulation: platform protection detected",
            configured_mode=mode,
            phase="simulation",
            block_classification="simulation",
            parser_version="simulation-v1",
        )
    if state == "ERROR":
        return PlatformResult(
            platform,
            PlatformState.ERROR,
            error="simulation: browser failure",
            configured_mode=mode,
            phase="simulation",
            parser_version="simulation-v1",
        )
    return PlatformResult(
        platform,
        PlatformState.UNAVAILABLE,
        reason="simulation: booking closed",
        configured_mode=mode,
        phase="simulation",
        parser_version="simulation-v1",
    )


def previous_state(db: Session, watch_id: int, platform: str) -> str:
    row = db.scalar(
        select(PlatformCheck)
        .where(PlatformCheck.watch_id == watch_id, PlatformCheck.platform == platform)
        .order_by(PlatformCheck.checked_at.desc(), PlatformCheck.id.desc())
    )
    return row.status if row else PlatformState.UNAVAILABLE


def retry_state(db: Session, watch: Watch, platform: str) -> PlatformRetryState:
    latest = db.scalar(
        select(PlatformCheck)
        .where(PlatformCheck.watch_id == watch.id, PlatformCheck.platform == platform)
        .order_by(PlatformCheck.id.desc())
    )
    state = db.scalar(
        select(PlatformRetryState).where(
            PlatformRetryState.watch_id == watch.id,
            PlatformRetryState.platform == platform,
        )
    )
    if state is None:
        state = PlatformRetryState(
            watch_id=watch.id,
            platform=platform,
            last_status=latest.status if latest else PlatformState.UNAVAILABLE,
        )
        db.add(state)
        db.flush()
    if (
        latest
        and latest.status == PlatformState.BLOCKED
        and state.last_status == PlatformState.BLOCKED
        and state.consecutive_block_count == 0
    ):
        state.consecutive_block_count = max(latest.failure_count, 1)
        checked_at = _aware_utc(latest.checked_at) or datetime.now(timezone.utc)
        state.blocked_until = checked_at + timedelta(
            seconds=blocked_cooldown_seconds(state.consecutive_block_count)
        )
        state.last_block_reason = latest.error or latest.reason
    return state


def _aware_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return (
        value.replace(tzinfo=timezone.utc)
        if value.tzinfo is None
        else value.astimezone(timezone.utc)
    )


def blocked_cooldown_seconds(count: int) -> int:
    settings = get_settings()
    if count <= 1:
        return settings.blocked_retry_first_seconds
    if count == 2:
        return settings.blocked_retry_second_seconds
    multiplier = 2 ** (count - 3)
    return min(
        settings.blocked_retry_subsequent_seconds * multiplier, settings.blocked_retry_max_seconds
    )


WAITING_STATES = {PlatformState.UNAVAILABLE, PlatformState.PAGE_LOADED_NO_SHOWS}
UNCERTAIN_STATES = {
    PlatformState.DISCOVERY_NO_RESULTS,
    PlatformState.DISCOVERY_FAILED,
    PlatformState.PARSE_UNSUPPORTED,
    PlatformState.ERROR,
}


def aggregate_watch_status(states: list[str], *, watch_enabled: bool = True) -> str:
    if not watch_enabled or not states:
        return "DISABLED"
    normalized = {str(state) for state in states if state != PlatformState.DISABLED}
    if not normalized:
        return "DISABLED"
    if PlatformState.AVAILABLE in normalized:
        return "AVAILABLE"
    if PlatformState.CONFIGURATION_REQUIRED in normalized:
        return "CONFIGURATION_REQUIRED"
    if normalized == {PlatformState.BLOCKED}:
        return "ALL_PLATFORMS_BLOCKED"
    if PlatformState.BLOCKED in normalized:
        return "PARTIAL"
    if normalized.issubset(WAITING_STATES):
        return "WAITING"
    if normalized.issubset(UNCERTAIN_STATES):
        return "CHECK_FAILED"
    if normalized & UNCERTAIN_STATES:
        return "PARTIAL"
    return "WAITING"


def _record_platform_check(
    db: Session, watch: Watch, result: PlatformResult, *, record_transition: bool = True
) -> PlatformCheck:
    old = previous_state(db, watch.id, result.platform)
    if record_transition and old != result.status:
        db.add(
            StateTransition(
                watch_id=watch.id,
                platform=result.platform,
                old_state=old,
                new_state=result.status,
            )
        )
    failure_count = 0
    if result.status in {PlatformState.ERROR, PlatformState.BLOCKED}:
        prior = db.scalar(
            select(PlatformCheck)
            .where(PlatformCheck.watch_id == watch.id, PlatformCheck.platform == result.platform)
            .order_by(PlatformCheck.id.desc())
        )
        failure_count = (prior.failure_count if prior else 0) + 1
    check = PlatformCheck(
        watch_id=watch.id,
        platform=result.platform,
        status=result.status,
        error=result.error,
        reason=result.reason,
        checked_url=result.checked_url,
        phase=result.phase,
        screenshot_path=result.screenshot_path,
        failure_count=failure_count,
        configured_mode=result.configured_mode,
        supplied_url=result.supplied_url,
        discovered_url=result.discovered_url,
        final_url=result.final_url,
        page_outcome=result.page_outcome,
        page_title=result.page_title,
        structured_sources=json.dumps(result.structured_sources),
        raw_candidate_count=result.raw_candidate_count,
        matching_count=result.matching_count,
        block_classification=result.block_classification,
        ray_id=result.ray_id,
        parser_version=result.parser_version,
    )
    db.add(check)
    db.flush()
    return check


async def run_watch_check(
    db: Session,
    watch: Watch,
    *,
    only_platform: str | None = None,
    bypass_cooldown: bool = False,
    test_direct_url: bool = False,
) -> list[PlatformResult]:
    settings = get_settings()
    names = enabled_platforms(watch)
    if only_platform:
        if only_platform not in PLATFORM_NAMES:
            raise ValueError(f"Unknown platform: {only_platform}")
        if only_platform not in names and not test_direct_url:
            return []
        names = [only_platform]
    if not names:
        watch.last_status, watch.last_error = "DISABLED", "No platform enabled"
        db.commit()
        return []

    now = datetime.now(timezone.utc)
    runnable: list[str] = []
    for name in names:
        state = retry_state(db, watch, name)
        blocked_until = _aware_utc(state.blocked_until)
        if (
            state.last_status == PlatformState.BLOCKED
            and blocked_until
            and blocked_until > now
            and not bypass_cooldown
            and watch.simulation_state == "OFF"
        ):
            continue
        runnable.append(name)

    if watch.simulation_state != "OFF":
        results = [simulation_result(watch, name) for name in runnable]
    else:
        adapters = [adapter_for(name) for name in runnable]
        if test_direct_url:
            for adapter in adapters:
                direct = (
                    watch.bookmyshow_direct_url
                    if adapter.name == "BookMyShow"
                    else watch.pvrinox_direct_url
                )
                adapter.forced_direct_url = direct
        results = await asyncio.gather(*(adapter.search(watch) for adapter in adapters))

    total = 0
    errors: list[str] = []
    for result in results:
        state = retry_state(db, watch, result.platform)
        state.last_status = str(result.status)
        if result.status == PlatformState.BLOCKED:
            state.consecutive_block_count += 1
            cooldown = blocked_cooldown_seconds(state.consecutive_block_count)
            state.blocked_until = now + timedelta(seconds=cooldown)
            state.last_block_reason = result.error or result.reason
        else:
            state.consecutive_block_count = 0
            state.blocked_until = None
            state.last_block_reason = ""
        if result.status in {
            PlatformState.ERROR,
            PlatformState.BLOCKED,
            PlatformState.CONFIGURATION_REQUIRED,
            PlatformState.DISCOVERY_FAILED,
            PlatformState.DISCOVERY_NO_RESULTS,
            PlatformState.PARSE_UNSUPPORTED,
        }:
            errors.append(f"{result.platform}: {result.error or result.reason}")
        check = _record_platform_check(
            db, watch, result, record_transition=watch.simulation_state == "OFF"
        )
        total += len(result.shows)
        for show in result.shows:
            if watch.simulation_state != "OFF":
                if watch.enabled and watch.notifications_enabled:
                    await record_simulation_notification(db, watch, show)
                continue
            detected = db.scalar(
                select(DetectedShow).where(
                    DetectedShow.watch_id == watch.id,
                    DetectedShow.fingerprint == show.fingerprint,
                )
            )
            if detected:
                detected.last_seen_at = now
                if watch.notifications_enabled and not detected.notification_sent:
                    detected.notification_sent = await record_notification(
                        db, watch.id, check.id, detected.id
                    )
                continue
            detected = DetectedShow(
                watch_id=watch.id,
                fingerprint=show.fingerprint,
                platform=show.platform,
                movie_title=show.movie_title,
                theatre=show.theatre,
                show_date=show.date,
                showtime=show.showtime,
                language=show.language,
                format=show.format,
                booking_url=show.booking_url,
                city=show.city,
            )
            db.add(detected)
            db.flush()
            if watch.notifications_enabled:
                detected.notification_sent = await record_notification(
                    db, watch.id, check.id, detected.id
                )

    all_names = enabled_platforms(watch)
    states = [retry_state(db, watch, name).last_status for name in all_names]
    watch.last_status = aggregate_watch_status(states, watch_enabled=watch.enabled)
    watch.last_check_at = now if results else watch.last_check_at
    watch.matching_show_count = total if results else watch.matching_show_count
    watch.last_error = "; ".join(errors)

    active_states = [retry_state(db, watch, name) for name in all_names]
    unblocked = [
        state
        for state in active_states
        if state.last_status != PlatformState.BLOCKED
        or not _aware_utc(state.blocked_until)
        or _aware_utc(state.blocked_until) <= now
    ]
    if unblocked:
        interval = max(watch.polling_interval_seconds, settings.min_poll_interval_seconds)
        watch.next_check_at = now + timedelta(seconds=interval + random.randint(0, 20))
    else:
        retry_times = [
            value
            for state in active_states
            if (value := _aware_utc(state.blocked_until)) is not None
        ]
        watch.next_check_at = min(retry_times) if retry_times else now + timedelta(hours=1)
    db.commit()
    return results
