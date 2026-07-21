from dataclasses import dataclass, field
from datetime import date, time
from hashlib import sha256


@dataclass(slots=True)
class ShowResult:
    platform: str
    movie_title: str
    theatre: str
    date: date
    showtime: time
    language: str
    format: str
    booking_url: str
    city: str = ""
    raw_time: str = ""
    normalized_time: str = ""
    display_time: str = ""
    time_source: str = ""
    time_verified: bool = True
    timezone_treatment: str = ""
    session_id: str = ""
    bookable: bool = True
    time_fields: dict[str, object] = field(default_factory=dict)

    @property
    def fingerprint(self) -> str:
        raw = "|".join(
            [
                self.platform,
                self.theatre,
                self.date.isoformat(),
                self.normalized_time or self.showtime.isoformat(),
                self.format,
                self.language,
                self.session_id,
            ]
        ).casefold()
        return sha256(raw.encode()).hexdigest()


@dataclass(slots=True)
class PlatformResult:
    platform: str
    status: str
    shows: list[ShowResult] = field(default_factory=list)
    reason: str = ""
    error: str = ""
    screenshot_path: str = ""
    checked_url: str = ""
    phase: str = ""
    configured_mode: str = ""
    supplied_url: str = ""
    discovered_url: str = ""
    final_url: str = ""
    page_outcome: str = ""
    page_title: str = ""
    structured_sources: list[str] = field(default_factory=list)
    raw_candidate_count: int = 0
    matching_count: int = 0
    block_classification: str = ""
    ray_id: str = ""
    parser_version: str = ""
    session_diagnostics: list[dict[str, object]] = field(default_factory=list)


@dataclass(slots=True)
class RawAdapterData:
    shows: list[ShowResult] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    screenshot_path: str = ""
    empty_status: str = "UNAVAILABLE"
    phase: str = "parsing"
    configured_mode: str = ""
    supplied_url: str = ""
    discovered_url: str = ""
    final_url: str = ""
    page_outcome: str = ""
    page_title: str = ""
    structured_sources: list[str] = field(default_factory=list)
    block_classification: str = ""
    ray_id: str = ""
    parser_version: str = ""
    session_diagnostics: list[dict[str, object]] = field(default_factory=list)
