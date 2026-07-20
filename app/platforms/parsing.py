import json
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.schemas import ShowResult
from app.services.matching import parse_time

BLOCK_MARKERS = (
    "captcha",
    "verify you are human",
    "access denied",
    "waiting room",
    "unusual traffic",
    "please log in to continue",
    "sorry, you have been blocked",
    "you are unable to access bookmyshow.com",
    "cloudflare ray id",
)
PARSER_VERSION = "semantic-v2"


class BlockedPageError(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        classification: str = "platform_protection",
        ray_id: str = "",
        screenshot_path: str = "",
        final_url: str = "",
        page_title: str = "",
        page_outcome: str = "blocked",
    ) -> None:
        super().__init__(message)
        self.classification = classification
        self.ray_id = ray_id
        self.screenshot_path = screenshot_path
        self.final_url = final_url
        self.page_title = page_title
        self.page_outcome = page_outcome


@dataclass(slots=True)
class ParseOutcome:
    shows: list[ShowResult] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)
    structured_sources: list[str] = field(default_factory=list)
    page_identified: bool = False
    booking_closed: bool = False


def detect_blocked(html: str) -> None:
    lowered = html.casefold()
    marker = next((item for item in BLOCK_MARKERS if item in lowered), None)
    if marker:
        ray_match = re.search(
            r"(?:cloudflare\s+)?ray\s+id\s*[:#]?\s*(?:</?[^>]+>\s*)*([a-z0-9-]{8,})",
            html,
            flags=re.IGNORECASE,
        )
        classification = "cloudflare" if "cloudflare" in lowered or "blocked" in lowered else marker
        ray_id = ray_match.group(1) if ray_match else ""
        suffix = f"; Cloudflare Ray ID {ray_id}" if ray_id else ""
        raise BlockedPageError(
            f"Platform protection detected: {marker}{suffix}",
            classification=classification,
            ray_id=ray_id,
        )


def _show_from_mapping(platform: str, item: dict, base_url: str) -> ShowResult:
    start = item.get("startDate") or item.get("date")
    if "T" in str(start):
        date_part, time_part = str(start).split("T", 1)
        showtime = time_part[:5]
    else:
        date_part = str(start)
        showtime = str(item.get("time") or item.get("showtime"))
    location = item.get("location", {})
    theatre = location.get("name", "") if isinstance(location, dict) else str(location)
    address = location.get("address", {}) if isinstance(location, dict) else {}
    locality = address.get("addressLocality", "") if isinstance(address, dict) else ""
    offers = item.get("offers", {})
    offer_url = offers.get("url", "") if isinstance(offers, dict) else ""
    return ShowResult(
        platform=platform,
        movie_title=str(item.get("name") or item.get("movie") or item.get("movieTitle")),
        theatre=str(item.get("theatre") or theatre),
        date=date.fromisoformat(date_part),
        showtime=parse_time(showtime),
        language=str(item.get("inLanguage") or item.get("language") or ""),
        format=str(item.get("format") or item.get("screeningType") or ""),
        booking_url=urljoin(base_url, str(item.get("url") or offer_url)),
        city=str(item.get("city") or locality),
    )


def parse_show_page(html: str, platform: str, base_url: str = "") -> ParseOutcome:
    """Parse semantic fixtures and JSON-LD while retaining evidence classification."""
    detect_blocked(html)
    soup = BeautifulSoup(html, "html.parser")
    raw: list[dict] = []
    diagnostics: list[str] = []
    sources: list[str] = []
    for node in soup.select("[data-show]"):
        try:
            raw.append(json.loads(node.get("data-show", "{}")))
            if "data-show" not in sources:
                sources.append("data-show")
        except json.JSONDecodeError as exc:
            diagnostics.append(f"malformed data-show: {exc}")
    for node in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(node.string or "null")
            entries = payload if isinstance(payload, list) else payload.get("@graph", [payload])
            raw.extend(
                x
                for x in entries
                if isinstance(x, dict) and x.get("@type") in {"Event", "ScreeningEvent"}
            )
            if any(
                isinstance(x, dict) and x.get("@type") in {"Event", "ScreeningEvent"}
                for x in entries
            ) and "JSON-LD Event" not in sources:
                sources.append("JSON-LD Event")
        except (json.JSONDecodeError, AttributeError) as exc:
            diagnostics.append(f"malformed JSON-LD: {exc}")
    shows: list[ShowResult] = []
    for item in raw:
        try:
            shows.append(_show_from_mapping(platform, item, base_url))
        except (KeyError, TypeError, ValueError) as exc:
            diagnostics.append(f"invalid show record: {exc}")
    if not raw:
        diagnostics.append("no concrete semantic show records found")
    text = soup.get_text(" ", strip=True).casefold()
    closed_markers = (
        "bookings have not opened",
        "booking closed",
        "coming soon",
        "tickets not available",
        "no shows available",
    )
    booking_closed = any(marker in text for marker in closed_markers)
    page_identified = bool(
        raw
        or booking_closed
        or soup.select_one("[data-movie], [data-event], main [class*='movie'], main [class*='event']")
    )
    return ParseOutcome(
        shows=shows,
        diagnostics=diagnostics,
        structured_sources=sources,
        page_identified=page_identified,
        booking_closed=booking_closed,
    )


def parse_shows(html: str, platform: str, base_url: str = "") -> tuple[list[ShowResult], list[str]]:
    """Backward-compatible parser API used by fixture diagnostics."""
    outcome = parse_show_page(html, platform, base_url)
    return outcome.shows, outcome.diagnostics


def read_fixture(url: str) -> str:
    path = Path(url.removeprefix("fixture://"))
    return path.read_text(encoding="utf-8")
