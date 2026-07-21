import re
import unicodedata
from datetime import datetime, time

from app.models import Watch
from app.schemas import ShowResult


def normalized_words(value: str) -> tuple[str, ...]:
    value = unicodedata.normalize("NFKD", value).encode("ascii", "ignore").decode()
    return tuple(re.findall(r"[a-z0-9]+", value.casefold()))


def titles_match(expected: str, actual: str) -> bool:
    """Match punctuation/case variants while rejecting sequels or partial titles."""
    return normalized_words(expected) == normalized_words(actual)


def parse_time(value: str) -> time:
    cleaned = value.strip().upper().replace(".", "")
    for fmt in ("%H:%M", "%I:%M %p", "%I:%M%p"):
        try:
            return datetime.strptime(cleaned, fmt).time()
        except ValueError:
            continue
    raise ValueError(f"Unsupported showtime: {value}")


def time_in_window(value: time, start: time, end: time) -> bool:
    return start <= value <= end if start <= end else value >= start or value <= end


def match_reason(watch: Watch, show: ShowResult) -> tuple[bool, str]:
    checks = [
        (titles_match(watch.movie_name, show.movie_title), "movie title differs"),
        (watch.show_date == show.date, "date differs"),
        (
            bool(show.city) and normalized_words(watch.city) == normalized_words(show.city),
            "city differs or is missing",
        ),
        (normalized_words(watch.language) == normalized_words(show.language), "language differs"),
        (normalized_words(watch.format) == normalized_words(show.format), "format differs"),
        (
            show.time_verified
            and time_in_window(show.showtime, watch.start_time, watch.end_time)
            or (not show.time_verified and str(watch.time_preset).upper() == "ANY" and show.bookable),
            "SHOWTIME_UNVERIFIED"
            if not show.time_verified
            else "outside time window",
        ),
    ]
    wanted = [
        normalized_words(x.strip())
        for x in (watch.preferred_theatres or "").split(",")
        if x.strip()
    ]
    if wanted:
        theatre_words = normalized_words(show.theatre)
        checks.append(
            (any(all(word in theatre_words for word in name) for name in wanted), "theatre differs")
        )
    for passed, reason in checks:
        if not passed:
            return False, reason
    return True, "matched all filters"


def filter_shows(watch: Watch, shows: list[ShowResult]) -> tuple[list[ShowResult], list[str]]:
    matches: list[ShowResult] = []
    reasons: list[str] = []
    for show in shows:
        matched, reason = match_reason(watch, show)
        shown_time = show.display_time if show.time_verified else "SHOWTIME_UNVERIFIED"
        reasons.append(f"{show.movie_title} at {shown_time}: {reason}")
        if matched:
            matches.append(show)
    return matches, reasons
