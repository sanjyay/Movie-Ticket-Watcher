import asyncio
import logging
import re
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.config import Settings, get_settings
from app.models import DetectedShow, NotificationHistory, PlatformCheck, PlatformState, Watch
from app.schemas import ShowResult
from app.services.matching import match_reason
from app.time_presets import label_for

# Bot API URLs contain the secret token in their path. HTTPX request logs must stay disabled.
logging.getLogger("httpx").setLevel(logging.WARNING)

CHAT_ID_PATTERN = re.compile(r"^-?[1-9][0-9]*$")
TOKEN_PATTERN = re.compile(r"\b[0-9]{6,}:[A-Za-z0-9_-]{20,}\b")
TEMPORARY_STATUS_CODES = {429, 500, 502, 503, 504}
MAX_ATTEMPTS = 3
MAX_RETRY_AFTER_SECONDS = 30
MAX_DELIVERY_CYCLES = 3


def validate_chat_id(value: str) -> str:
    chat_id = value.strip()
    if not CHAT_ID_PATTERN.fullmatch(chat_id):
        raise ValueError("Telegram chat ID is invalid")
    return chat_id


def effective_chat_id(watch: Watch, settings: Settings | None = None) -> str:
    settings = settings or get_settings()
    value = (
        watch.telegram_chat_id_override or ""
    ).strip() or settings.telegram_default_chat_id.strip()
    if not value:
        raise ValueError("Telegram chat ID missing")
    return validate_chat_id(value)


def telegram_configured(settings: Settings | None = None) -> bool:
    settings = settings or get_settings()
    if not settings.telegram_bot_token:
        return False
    try:
        validate_chat_id(settings.telegram_default_chat_id)
    except ValueError:
        return False
    return True


def safe_booking_url(value: str) -> str | None:
    try:
        parsed = urlparse(value.strip())
    except ValueError:
        return None
    return value.strip() if parsed.scheme in {"http", "https"} and parsed.netloc else None


def sanitize_telegram_error(error: object, token: str = "") -> str:
    message = str(error) or error.__class__.__name__
    if token:
        message = message.replace(token, "[redacted]")
    message = TOKEN_PATTERN.sub("[redacted]", message)
    message = re.sub(r"/bot[^/\s]+/", "/bot[redacted]/", message)
    return message[:500]


def format_show_message(watch: Watch, show: ShowResult, detected_at: datetime | None = None) -> str:
    settings = get_settings()
    detected_at = detected_at or datetime.now(timezone.utc)
    local = detected_at.astimezone(ZoneInfo(settings.app_timezone))
    show_date = show.date.strftime("%d %B %Y").lstrip("0")
    showtime = show.showtime.strftime("%I:%M %p").lstrip("0")
    detected = local.strftime("%I:%M %p %Z").lstrip("0")
    booking_url = safe_booking_url(show.booking_url)
    lines = [
        "🎟 Ticket booking available",
        "",
        f"Movie: {watch.movie_name}",
        f"Platform: {show.platform}",
        f"City: {watch.city}",
        f"Date: {show_date}",
        f"Showtime: {showtime}",
        f"Theatre: {show.theatre}",
        f"Language: {show.language}",
        f"Format: {show.format}",
        f"Preference: {label_for(watch.time_preset)}",
        f"Detected: {detected}",
        f"Booking: {booking_url or 'Unavailable'}",
    ]
    return "\n".join(lines)


class TelegramDeliveryError(Exception):
    def __init__(self, message: str, *, temporary: bool = False, retry_after: float = 0) -> None:
        super().__init__(message)
        self.temporary = temporary
        self.retry_after = retry_after


class TelegramProvider:
    def __init__(
        self,
        settings: Settings | None = None,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ) -> None:
        self.settings = settings or get_settings()
        self.client = client
        self.sleep = sleep

    async def _request(self, chat_id: str, text: str, booking_url: str | None = None) -> int:
        token = self.settings.telegram_bot_token
        if not token:
            raise TelegramDeliveryError("Telegram bot token missing")
        payload: dict[str, object] = {"chat_id": validate_chat_id(chat_id), "text": text}
        if booking_url:
            payload["reply_markup"] = {
                "inline_keyboard": [[{"text": "Open booking page", "url": booking_url}]]
            }
        endpoint = f"{self.settings.telegram_api_base.rstrip('/')}/bot{token}/sendMessage"
        owned_client = self.client is None
        client = self.client or httpx.AsyncClient(timeout=15)
        try:
            for attempt in range(1, MAX_ATTEMPTS + 1):
                try:
                    response = await client.post(endpoint, json=payload)
                except (httpx.TimeoutException, httpx.NetworkError, OSError) as exc:
                    failure = TelegramDeliveryError(
                        sanitize_telegram_error(exc, token), temporary=True
                    )
                else:
                    data: dict = {}
                    try:
                        data = response.json()
                    except ValueError:
                        pass
                    if response.status_code < 400 and data.get("ok", True):
                        return attempt
                    description = sanitize_telegram_error(
                        data.get("description") or f"Telegram HTTP {response.status_code}", token
                    )
                    retry_after = min(
                        float(data.get("parameters", {}).get("retry_after", 0) or 0),
                        MAX_RETRY_AFTER_SECONDS,
                    )
                    failure = TelegramDeliveryError(
                        description,
                        temporary=response.status_code in TEMPORARY_STATUS_CODES,
                        retry_after=retry_after,
                    )
                if not failure.temporary or attempt == MAX_ATTEMPTS:
                    raise failure
                await self.sleep(failure.retry_after or 2 ** (attempt - 1))
        finally:
            if owned_client:
                await client.aclose()
        raise TelegramDeliveryError("Telegram delivery failed")

    async def send_show(self, watch: Watch, show: ShowResult) -> int:
        return await self._request(
            effective_chat_id(watch, self.settings),
            format_show_message(watch, show),
            safe_booking_url(show.booking_url),
        )

    async def send_test(self, watch: Watch) -> int:
        return await self._request(
            effective_chat_id(watch, self.settings),
            "TEST — Movie Ticket Watcher\n\nThis is a test. No real ticket availability was detected.\n\nTelegram notifications are configured correctly.",
        )

    async def send_simulation(self, watch: Watch, show: ShowResult) -> int:
        text = format_show_message(watch, show).replace(
            "🎟 Ticket booking available", "SIMULATION — Ticket availability", 1
        )
        text += "\n\nThis is simulated data and does not represent a real booking."
        return await self._request(effective_chat_id(watch, self.settings), text)


async def record_notification(
    db: Session,
    watch_id: int,
    platform_check_id: int,
    detected_show_id: int,
    provider: TelegramProvider | None = None,
) -> bool:
    """Deliver only a fully persisted, revalidated live availability event."""
    provider = provider or TelegramProvider()
    watch = db.get(Watch, watch_id)
    check = db.get(PlatformCheck, platform_check_id)
    detected = db.get(DetectedShow, detected_show_id)
    reason = ""
    if not watch:
        reason = "watch no longer exists"
    elif not watch.enabled:
        reason = "watch is disabled"
    elif not watch.notifications_enabled:
        reason = "watch notifications are disabled"
    elif watch.simulation_state != "OFF":
        reason = "watch is in simulation mode"
    elif not check or check.watch_id != watch.id or check.status != PlatformState.AVAILABLE:
        reason = "matching live platform check is missing"
    elif not detected or detected.watch_id != watch.id or detected.platform != check.platform:
        reason = "matching detected show is missing"
    if reason:
        logging.getLogger(__name__).warning("Cancelled Telegram delivery: %s", reason)
        if watch:
            db.add(
                NotificationHistory(
                    watch_id=watch.id,
                    fingerprint=detected.fingerprint if detected else "",
                    provider="telegram",
                    success=False,
                    error="",
                    notification_source="LIVE_AVAILABILITY",
                    delivery_status="CANCELLED",
                    cancellation_reason=reason,
                    platform_check_id=check.id if check else platform_check_id,
                    detected_show_id=detected.id if detected else detected_show_id,
                )
            )
        return False
    show = ShowResult(
        detected.platform,
        detected.movie_title,
        detected.theatre,
        detected.show_date,
        detected.showtime,
        detected.language,
        detected.format,
        detected.booking_url,
        detected.city,
    )
    matched, match_failure = match_reason(watch, show)
    if not matched or detected.fingerprint != show.fingerprint:
        logging.getLogger(__name__).warning(
            "Cancelled Telegram delivery: persisted show validation failed (%s)", match_failure
        )
        return False
    last_delivery = db.scalar(
        select(NotificationHistory)
        .where(
            NotificationHistory.watch_id == watch.id,
            NotificationHistory.fingerprint == detected.fingerprint,
            NotificationHistory.provider == "telegram",
            NotificationHistory.notification_source == "LIVE_AVAILABILITY",
        )
        .order_by(NotificationHistory.id.desc())
    )
    if (
        last_delivery
        and not last_delivery.success
        and last_delivery.error.startswith("permanent: ")
    ):
        return False
    prior_cycles = (
        db.scalar(
            select(func.count())
            .select_from(NotificationHistory)
            .where(
                NotificationHistory.watch_id == watch.id,
                NotificationHistory.fingerprint == detected.fingerprint,
                NotificationHistory.provider == "telegram",
                NotificationHistory.notification_source == "LIVE_AVAILABILITY",
            )
        )
        or 0
    )
    if prior_cycles >= MAX_DELIVERY_CYCLES:
        return False
    success, attempts, error = False, 1, ""
    try:
        attempts = await provider.send_show(watch, show)
        success = True
    except Exception as exc:
        error = sanitize_telegram_error(exc, provider.settings.telegram_bot_token)
        if isinstance(exc, TelegramDeliveryError):
            attempts = MAX_ATTEMPTS if exc.temporary else 1
            if not exc.temporary and error not in {
                "Telegram bot token missing",
                "Telegram chat ID missing",
                "Telegram chat ID is invalid",
            }:
                error = f"permanent: {error}"
    db.add(
        NotificationHistory(
            watch_id=watch.id,
            fingerprint=detected.fingerprint,
            provider="telegram",
            success=success,
            attempts=attempts,
            error=error,
            is_test=False,
            notification_source="LIVE_AVAILABILITY",
            delivery_status="SENT" if success else "FAILED",
            platform_check_id=check.id,
            detected_show_id=detected.id,
        )
    )
    return success


async def record_simulation_notification(
    db: Session, watch: Watch, show: ShowResult, provider: TelegramProvider | None = None
) -> bool:
    provider = provider or TelegramProvider()
    fingerprint = f"simulation:{watch.id}:{show.platform}:{show.fingerprint}"
    if db.scalar(
        select(NotificationHistory.id).where(
            NotificationHistory.watch_id == watch.id,
            NotificationHistory.fingerprint == fingerprint,
            NotificationHistory.notification_source == "SIMULATION",
            NotificationHistory.success.is_(True),
        )
    ):
        return False
    success, error, attempts = False, "", 1
    try:
        attempts = await provider.send_simulation(watch, show)
        success = True
    except Exception as exc:
        error = sanitize_telegram_error(exc, provider.settings.telegram_bot_token)
    db.add(
        NotificationHistory(
            watch_id=watch.id,
            fingerprint=fingerprint,
            provider="telegram",
            success=success,
            attempts=attempts,
            error=error,
            notification_source="SIMULATION",
            delivery_status="SENT" if success else "FAILED",
        )
    )
    return success
