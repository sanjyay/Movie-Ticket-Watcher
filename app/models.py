from __future__ import annotations

from datetime import date, datetime, time, timezone
from enum import StrEnum

from sqlalchemy import (
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    Time,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class PlatformState(StrEnum):
    UNAVAILABLE = "UNAVAILABLE"
    AVAILABLE = "AVAILABLE"
    CONFIGURATION_REQUIRED = "CONFIGURATION_REQUIRED"
    DISCOVERY_FAILED = "DISCOVERY_FAILED"
    DISCOVERY_NO_RESULTS = "DISCOVERY_NO_RESULTS"
    PAGE_LOADED_NO_SHOWS = "PAGE_LOADED_NO_SHOWS"
    PARSE_UNSUPPORTED = "PARSE_UNSUPPORTED"
    BLOCKED = "BLOCKED"
    ERROR = "ERROR"
    DISABLED = "DISABLED"


class PlatformMode(StrEnum):
    AUTOMATIC = "AUTOMATIC"
    DIRECT = "DIRECT"
    DISABLED = "DISABLED"


class Watch(Base):
    __tablename__ = "watches"
    id: Mapped[int] = mapped_column(primary_key=True)
    movie_name: Mapped[str] = mapped_column(String(250))
    city: Mapped[str] = mapped_column(String(100))
    show_date: Mapped[date] = mapped_column(Date)
    language: Mapped[str] = mapped_column(String(50), default="English")
    format: Mapped[str] = mapped_column(String(50), default="2D")
    time_preset: Mapped[str] = mapped_column(String(20), default="EVENING")
    start_time: Mapped[time] = mapped_column(Time, default=time(17, 0))
    end_time: Mapped[time] = mapped_column(Time, default=time(21, 59))
    preferred_theatres: Mapped[str] = mapped_column(Text, default="")
    bookmyshow_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    pvrinox_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    bookmyshow_mode: Mapped[str] = mapped_column(
        String(20), default=PlatformMode.AUTOMATIC, server_default="AUTOMATIC"
    )
    pvrinox_mode: Mapped[str] = mapped_column(
        String(20), default=PlatformMode.AUTOMATIC, server_default="AUTOMATIC"
    )
    bookmyshow_url: Mapped[str] = mapped_column(Text, default="")
    pvrinox_url: Mapped[str] = mapped_column(Text, default="")
    bookmyshow_direct_url: Mapped[str] = mapped_column(Text, default="", server_default="")
    pvrinox_direct_url: Mapped[str] = mapped_column(Text, default="", server_default="")
    bookmyshow_discovered_url: Mapped[str] = mapped_column(Text, default="")
    pvrinox_discovered_url: Mapped[str] = mapped_column(Text, default="")
    polling_interval_seconds: Mapped[int] = mapped_column(Integer, default=300)
    telegram_chat_id_override: Mapped[str] = mapped_column(String(32), default="")
    notifications_enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    # Legacy SQLite compatibility only. Old databases made these columns NOT NULL;
    # map them privately so new inserts use inert values without restoring ntfy behavior.
    _legacy_ntfy_topic: Mapped[str] = mapped_column(
        "ntfy_topic", Text, default="", server_default=""
    )
    _legacy_notification_enabled: Mapped[bool] = mapped_column(
        "notification_enabled", Boolean, default=True, server_default="1"
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    simulation_state: Mapped[str] = mapped_column(String(20), default="OFF")
    last_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(40), default="WAITING")
    last_error: Mapped[str] = mapped_column(Text, default="")
    matching_show_count: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    checks: Mapped[list[PlatformCheck]] = relationship(cascade="all, delete-orphan")
    shows: Mapped[list[DetectedShow]] = relationship(cascade="all, delete-orphan")


class PlatformCheck(Base):
    __tablename__ = "platform_checks"
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    checked_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    error: Mapped[str] = mapped_column(Text, default="")
    reason: Mapped[str] = mapped_column(Text, default="")
    checked_url: Mapped[str] = mapped_column(Text, default="")
    phase: Mapped[str] = mapped_column(String(40), default="")
    screenshot_path: Mapped[str] = mapped_column(Text, default="")
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    configured_mode: Mapped[str] = mapped_column(String(20), default="")
    supplied_url: Mapped[str] = mapped_column(Text, default="")
    discovered_url: Mapped[str] = mapped_column(Text, default="")
    final_url: Mapped[str] = mapped_column(Text, default="")
    page_outcome: Mapped[str] = mapped_column(String(80), default="")
    page_title: Mapped[str] = mapped_column(Text, default="")
    structured_sources: Mapped[str] = mapped_column(Text, default="")
    raw_candidate_count: Mapped[int] = mapped_column(Integer, default=0)
    matching_count: Mapped[int] = mapped_column(Integer, default=0)
    block_classification: Mapped[str] = mapped_column(String(80), default="")
    ray_id: Mapped[str] = mapped_column(String(120), default="")
    parser_version: Mapped[str] = mapped_column(String(40), default="")


class PlatformRetryState(Base):
    """Persisted, per-watch platform state used for cooldown and aggregation."""

    __tablename__ = "platform_retry_state"
    __table_args__ = (UniqueConstraint("watch_id", "platform"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    consecutive_block_count: Mapped[int] = mapped_column(Integer, default=0)
    blocked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_block_reason: Mapped[str] = mapped_column(Text, default="")
    last_block_notification_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_status: Mapped[str] = mapped_column(String(40), default=PlatformState.UNAVAILABLE)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class DetectedShow(Base):
    __tablename__ = "detected_shows"
    __table_args__ = (UniqueConstraint("watch_id", "fingerprint"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    platform: Mapped[str] = mapped_column(String(30))
    movie_title: Mapped[str] = mapped_column(String(250))
    theatre: Mapped[str] = mapped_column(String(250))
    show_date: Mapped[date] = mapped_column(Date)
    showtime: Mapped[time] = mapped_column(Time)
    language: Mapped[str] = mapped_column(String(50))
    format: Mapped[str] = mapped_column(String(50))
    booking_url: Mapped[str] = mapped_column(Text)
    city: Mapped[str] = mapped_column(String(100), default="")
    first_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    last_seen_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    notification_sent: Mapped[bool] = mapped_column(Boolean, default=False)


class StateTransition(Base):
    __tablename__ = "state_transitions"
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    platform: Mapped[str] = mapped_column(String(30))
    old_state: Mapped[str] = mapped_column(String(30))
    new_state: Mapped[str] = mapped_column(String(30))
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class NotificationHistory(Base):
    __tablename__ = "notification_history"
    id: Mapped[int] = mapped_column(primary_key=True)
    watch_id: Mapped[int] = mapped_column(ForeignKey("watches.id", ondelete="CASCADE"), index=True)
    fingerprint: Mapped[str] = mapped_column(String(64), default="")
    provider: Mapped[str] = mapped_column(String(30), default="telegram")
    is_test: Mapped[bool] = mapped_column(Boolean, default=False)
    notification_source: Mapped[str] = mapped_column(String(30), default="LIVE_AVAILABILITY")
    delivery_status: Mapped[str] = mapped_column(String(20), default="FAILED")
    cancellation_reason: Mapped[str] = mapped_column(Text, default="")
    platform_check_id: Mapped[int | None] = mapped_column(Integer)
    detected_show_id: Mapped[int | None] = mapped_column(Integer)
    success: Mapped[bool] = mapped_column(Boolean)
    attempts: Mapped[int] = mapped_column(Integer, default=1)
    error: Mapped[str] = mapped_column(Text, default="")
    sent_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class RuntimeState(Base):
    """Small persisted key/value state shared by the web and worker containers."""

    __tablename__ = "runtime_state"
    key: Mapped[str] = mapped_column(String(80), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TelegramConversationState(Base):
    __tablename__ = "telegram_conversation_state"
    __table_args__ = (UniqueConstraint("chat_id", "user_id"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    chat_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32))
    step: Mapped[str] = mapped_column(String(30), default="")
    payload: Mapped[str] = mapped_column(Text, default="{}")
    nonce: Mapped[str] = mapped_column(String(24), default="")
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow, onupdate=utcnow
    )


class TelegramWatchCreation(Base):
    """Persisted idempotency receipt for a Telegram confirmation button."""

    __tablename__ = "telegram_watch_creations"
    __table_args__ = (UniqueConstraint("chat_id", "user_id", "confirmation_nonce"),)
    id: Mapped[int] = mapped_column(primary_key=True)
    request_id: Mapped[str] = mapped_column(String(24), unique=True, index=True)
    chat_id: Mapped[str] = mapped_column(String(32), index=True)
    user_id: Mapped[str] = mapped_column(String(32))
    confirmation_nonce: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(20), default="PENDING")
    watch_id: Mapped[int | None] = mapped_column(Integer)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
