from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_bind: str = "0.0.0.0"
    app_port: int = 8787
    app_timezone: str = "Asia/Kolkata"
    database_url: str = "sqlite:///./tickets.db"
    data_dir: Path = Path(".")
    config_dir: Path = Path("./config")
    screenshot_dir: Path = Path("./screenshots")
    log_dir: Path = Path("./logs")
    backup_dir: Path = Path("./backups")
    telegram_bot_token: str = ""
    telegram_default_chat_id: str = ""
    telegram_api_base: str = "https://api.telegram.org"
    telegram_allowed_chat_ids: str = ""
    telegram_allowed_user_ids: str = ""
    telegram_conversation_timeout_seconds: int = 1800
    telegram_long_poll_timeout_seconds: int = 25
    telegram_stale_update_age_seconds: int = 300
    secret_key: str = "development-only-change-me"
    default_poll_interval_seconds: int = 300
    min_poll_interval_seconds: int = 120
    playwright_headless: bool = True
    log_level: str = "INFO"
    simulation_enabled: bool = False
    screenshot_retention: int = 50
    backup_retention: int = 10
    log_retention_days: int = 14
    platform_timeout_seconds: int = 30
    blocked_retry_first_seconds: int = 1800
    blocked_retry_second_seconds: int = 3600
    blocked_retry_subsequent_seconds: int = 10800
    blocked_retry_max_seconds: int = 21600
    worker_heartbeat_max_age_seconds: int = 90
    app_version: str = "0.2.0"
    container_deployment: bool = False

    @model_validator(mode="after")
    def validate_runtime(self) -> "Settings":
        try:
            ZoneInfo(self.app_timezone)
        except Exception as exc:
            raise ValueError(f"Invalid APP_TIMEZONE: {self.app_timezone}") from exc
        parsed_telegram = urlparse(self.telegram_api_base)
        if parsed_telegram.scheme != "https" or not parsed_telegram.netloc:
            raise ValueError("TELEGRAM_API_BASE must be an absolute https:// URL")
        for name, raw in (
            ("TELEGRAM_ALLOWED_CHAT_IDS", self.telegram_allowed_chat_ids),
            ("TELEGRAM_ALLOWED_USER_IDS", self.telegram_allowed_user_ids),
        ):
            for value in filter(None, (item.strip() for item in raw.split(","))):
                if not value.lstrip("-").isdigit() or value in {"0", "-0"}:
                    raise ValueError(f"{name} contains an invalid numeric ID")
        if not 5 <= self.telegram_long_poll_timeout_seconds <= 50:
            raise ValueError("TELEGRAM_LONG_POLL_TIMEOUT_SECONDS must be between 5 and 50")
        if self.min_poll_interval_seconds < 30:
            raise ValueError("MIN_POLL_INTERVAL_SECONDS must be at least 30")
        cooldowns = (
            self.blocked_retry_first_seconds,
            self.blocked_retry_second_seconds,
            self.blocked_retry_subsequent_seconds,
            self.blocked_retry_max_seconds,
        )
        if any(value < 60 for value in cooldowns):
            raise ValueError("Blocked-platform retry intervals must be at least 60 seconds")
        if self.blocked_retry_max_seconds < self.blocked_retry_subsequent_seconds:
            raise ValueError(
                "BLOCKED_RETRY_MAX_SECONDS must be at least BLOCKED_RETRY_SUBSEQUENT_SECONDS"
            )
        if self.container_deployment:
            if len(self.secret_key) < 32 or self.secret_key == "development-only-change-me":
                raise ValueError("SECRET_KEY must contain at least 32 characters in Docker")
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
