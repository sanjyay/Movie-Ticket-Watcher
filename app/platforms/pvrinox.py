"""PVR INOX adapter.

The production site is a JavaScript SPA. Its normal public page currently loads the
unauthenticated ``content/search`` and ``content/msessions`` JSON endpoints used here.
Those response shapes are unofficial and fragile; fixture tests protect our parser from
silently converting a changed shape into a false "unavailable" result.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from app.models import PlatformMode, PlatformState, Watch
from app.platforms.base import ConfigurationRequiredError, TicketPlatformAdapter
from app.platforms.browser import PageSnapshot, fetch_page
from app.platforms.parsing import (
    PARSER_VERSION,
    BlockedPageError,
    parse_show_page,
    read_fixture,
)
from app.platforms.urls import PVRINOX_URLS, sanitize_url
from app.schemas import RawAdapterData, ShowResult
from app.services.matching import normalized_words, titles_match

PVR_API_BASE = "https://api3.pvrcinemas.com/api/v1/booking/content"
PVR_PARSER_VERSION = "pvr-public-json-v2-verified-showtime"
SHOWTIME_UNVERIFIED = "SHOWTIME_UNVERIFIED"
PVR_TIMEZONE = ZoneInfo("Asia/Kolkata")


@dataclass(frozen=True, slots=True)
class ParsedPvrTime:
    raw: str
    value: time
    normalized: str
    display: str
    source: str
    verified: bool
    timezone_treatment: str


def parse_pvr_showtime(raw: object, source: str = "showTime") -> ParsedPvrTime:
    """Parse only PVR's page-visible showTime field; never infer from adjacent fields."""
    value = str(raw or "").strip()
    parsed: time | None = None
    treatment = "Asia/Kolkata local wall-clock; no timezone conversion"
    if re.fullmatch(r"[0-2]?\d:[0-5]\d", value):
        parsed = datetime.strptime(value, "%H:%M").time()
    elif re.fullmatch(r"\d{3,4}", value):
        compact = value.zfill(4)
        try:
            parsed = time(int(compact[:2]), int(compact[2:]))
        except ValueError:
            parsed = None
    else:
        for fmt in ("%I:%M %p", "%I:%M%p"):
            try:
                parsed = datetime.strptime(value.upper().replace(".", ""), fmt).time()
                break
            except ValueError:
                pass
    if parsed is None and re.search(r"(?:Z|[+-]\d\d:\d\d)$", value):
        try:
            instant = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if instant.tzinfo is not None:
                parsed = instant.astimezone(PVR_TIMEZONE).time().replace(tzinfo=None)
                treatment = "explicit offset converted exactly once to Asia/Kolkata"
        except ValueError:
            pass
    if parsed is None:
        return ParsedPvrTime(value, time(0), "", "", source, False, "not converted")
    return ParsedPvrTime(
        value,
        parsed,
        parsed.strftime("%H:%M"),
        parsed.strftime("%I:%M %p").lstrip("0"),
        source,
        True,
        treatment,
    )


class PvrInoxAdapter(TicketPlatformAdapter):
    name = "PVR INOX"

    async def interactive_search(
        self, query: str, city: str = "Chennai", limit: int = 10
    ) -> list[dict[str, Any]]:
        """Search the same public source used by automatic discovery."""
        data = await self._api_post(
            "search", {"city": city, "lat": "0.000", "lng": "0.000", "type": "HOME"}, city
        )
        output = data.get("output")
        if data.get("result") != "success" or not isinstance(output, dict):
            raise ValueError("PVR INOX search response is unsupported")
        pools = [
            item
            for key in ("ns", "cs")
            for item in (output.get(key) if isinstance(output.get(key), list) else [])
            if isinstance(item, dict)
        ]
        wanted = normalized_words(query)
        matches = [
            item
            for item in pools
            if all(
                word in normalized_words(str(item.get("n") or item.get("filmCommonName") or ""))
                for word in wanted
            )
        ]
        return [
            {
                "id": str(item.get("id") or ""),
                "title": str(item.get("n") or item.get("filmCommonName") or "Unknown movie"),
                "languages": str(item.get("otherlanguages") or ""),
                "formats": ", ".join(str(value) for value in (item.get("fmts") or []) if value),
                "platform": self.name,
            }
            for item in matches[:limit]
            if item.get("id")
        ]

    def _mode_and_direct_url(self, watch: Watch) -> tuple[str, str]:
        forced = getattr(self, "forced_direct_url", None)
        if forced is not None:
            return PlatformMode.DIRECT, str(forced).strip()
        mode = (watch.pvrinox_mode or PlatformMode.AUTOMATIC).upper()
        direct = (watch.pvrinox_direct_url or "").strip()
        legacy = (watch.pvrinox_url or "").strip()
        if not direct and legacy and (not watch.pvrinox_mode or legacy.startswith("fixture://")):
            direct = legacy
            mode = PlatformMode.DIRECT
        return mode, direct

    @staticmethod
    def _headers(city: str) -> dict[str, str]:
        return {
            "Content-Type": "application/json",
            # The public SPA sends this empty bearer header before login. It carries no credential.
            "Authorization": "Bearer",
            "chain": "PVR",
            "city": city,
            "appVersion": "1.0",
            "platform": "WEBSITE",
            "country": "INDIA",
        }

    async def _api_post(self, endpoint: str, payload: dict[str, Any], city: str) -> dict[str, Any]:
        timeout = httpx.Timeout(30)
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True) as client:
            response = await client.post(
                f"{PVR_API_BASE}/{endpoint}", json=payload, headers=self._headers(city)
            )
            if response.status_code in {403, 429}:
                raise BlockedPageError(
                    f"PVR INOX public page-data request returned HTTP {response.status_code}",
                    classification=f"http_{response.status_code}",
                    final_url=str(response.url),
                    page_outcome=f"HTTP {response.status_code}",
                )
            response.raise_for_status()
            data = response.json()
        if not isinstance(data, dict):
            raise ValueError("PVR INOX public JSON response was not an object")
        return data

    @staticmethod
    def _movie_id_from_url(url: str) -> str:
        patterns = (
            r"/moviesessions/(?:[^/?#]+/)?[^/?#]+/(\d+)(?:[/?#]|$)",
            r"/comingsoon/(?:[^/?#]+/)?(\d+)(?:[/?#]|$)",
            r"/m\.movie-details/(\d+)(?:[/?#]|$)",
        )
        return next(
            (match.group(1) for pattern in patterns if (match := re.search(pattern, url))), ""
        )

    @staticmethod
    def _slug(value: str) -> str:
        return "-".join(normalized_words(value))

    @staticmethod
    def _candidate_language_format(candidate: dict[str, Any], watch: Watch) -> bool:
        films = candidate.get("films") if isinstance(candidate.get("films"), list) else []
        languages = {
            *normalized_words(str(candidate.get("otherlanguages") or "")),
            *(
                word
                for film in films
                if isinstance(film, dict)
                for word in normalized_words(str(film.get("language") or ""))
            ),
        }
        if not set(normalized_words(watch.language)).issubset(languages):
            return False
        wanted_format = " ".join(normalized_words(watch.format))
        if wanted_format == "2d":
            return True
        formats = {
            " ".join(normalized_words(str(value)))
            for value in [candidate.get("format"), *(candidate.get("fmts") or [])]
            if value
        }
        formats.update(
            " ".join(normalized_words(str(film.get("format") or "")))
            for film in films
            if isinstance(film, dict)
        )
        return wanted_format in formats

    async def _discover(self, watch: Watch) -> tuple[str, str, list[str], str]:
        payload = {
            "city": watch.city,
            "lat": "0.000",
            "lng": "0.000",
            "type": "HOME",
        }
        data = await self._api_post("search", payload, watch.city)
        output = data.get("output")
        if data.get("result") != "success" or not isinstance(output, dict):
            return (
                "",
                "",
                [f"official search returned {data.get('result')}: {data.get('msg', '')}"],
                "",
            )
        pools = []
        for key in ("ns", "cs"):
            values = output.get(key)
            if isinstance(values, list):
                pools.extend(value for value in values if isinstance(value, dict))
        title_matches = [
            item
            for item in pools
            if titles_match(
                watch.movie_name, str(item.get("n") or item.get("filmCommonName") or "")
            )
        ]
        candidates = [
            item for item in title_matches if self._candidate_language_format(item, watch)
        ]
        if not candidates:
            details = (
                f"official search returned {len(pools)} movies, {len(title_matches)} exact title "
                "matches, and no candidate matching language/format"
            )
            return "", "", [details], "public JSON: content/search"
        candidate = sorted(
            candidates,
            key=lambda item: (
                str(item.get("movieType")) != "NOWSHOWING",
                -int(item.get("showCount") or 0),
            ),
        )[0]
        movie_id = str(candidate.get("id") or "")
        movie_name = str(candidate.get("n") or watch.movie_name)
        canonical = (
            f"https://www.pvrcinemas.com/moviesessions/{self._slug(watch.city)}/"
            f"{self._slug(movie_name)}/{movie_id}"
        )
        return (
            movie_id,
            canonical,
            [f"official search selected exact movie ID {movie_id}"],
            "public JSON: content/search",
        )

    @staticmethod
    def _coerce_snapshot(value) -> PageSnapshot:  # type: ignore[no-untyped-def]
        if isinstance(value, PageSnapshot):
            return value
        html, screenshot = value
        return PageSnapshot(html=html, screenshot_path=screenshot)

    async def _fallback_page(
        self, watch: Watch, url: str, mode: str, supplied: str, discovered: str
    ) -> RawAdapterData:
        snapshot = self._coerce_snapshot(await fetch_page(url, "pvrinox", watch.id))
        parsed = parse_show_page(snapshot.html, self.name, snapshot.final_url or url)
        empty_status = (
            PlatformState.PAGE_LOADED_NO_SHOWS
            if parsed.booking_closed and parsed.page_identified
            else PlatformState.PARSE_UNSUPPORTED
        )
        return RawAdapterData(
            shows=parsed.shows,
            diagnostics=parsed.diagnostics,
            screenshot_path=snapshot.screenshot_path,
            empty_status=empty_status,
            phase="parsing",
            configured_mode=mode,
            supplied_url=supplied,
            discovered_url=discovered,
            final_url=sanitize_url(snapshot.final_url or url),
            page_outcome=snapshot.outcome,
            page_title=snapshot.title,
            structured_sources=parsed.structured_sources,
            parser_version=f"{PVR_PARSER_VERSION}+{PARSER_VERSION}",
        )

    async def raw_search(self, watch: Watch) -> RawAdapterData:
        mode, direct = self._mode_and_direct_url(watch)
        if mode == PlatformMode.DISABLED:
            raise ConfigurationRequiredError("PVR INOX is disabled for this watch")
        supplied = ""
        discovered = ""
        notes: list[str] = []
        sources: list[str] = []

        if mode == PlatformMode.DIRECT:
            if not direct:
                raise ConfigurationRequiredError(
                    "PVR INOX direct URL is required in Direct URL mode"
                )
            if direct.startswith("fixture://"):
                self._checked_url = direct
                self._diagnostic_context = {
                    "configured_mode": mode,
                    "supplied_url": direct,
                    "parser_version": PVR_PARSER_VERSION,
                }
                parsed = parse_show_page(read_fixture(direct), self.name, direct)
                empty_status = (
                    PlatformState.PAGE_LOADED_NO_SHOWS
                    if parsed.booking_closed
                    else PlatformState.PARSE_UNSUPPORTED
                    if not parsed.shows
                    else PlatformState.UNAVAILABLE
                )
                return RawAdapterData(
                    shows=parsed.shows,
                    diagnostics=parsed.diagnostics,
                    empty_status=empty_status,
                    phase="parsing",
                    configured_mode=mode,
                    supplied_url=direct,
                    final_url=direct,
                    page_outcome="fixture",
                    structured_sources=parsed.structured_sources,
                    parser_version=PVR_PARSER_VERSION,
                )
            try:
                url = PVRINOX_URLS.validate(direct)
            except ValueError as exc:
                raise ConfigurationRequiredError(str(exc)) from exc
            supplied = sanitize_url(url)
            movie_id = self._movie_id_from_url(url)
            notes.append("direct URL mode; generic discovery skipped")
        else:
            cached = (watch.pvrinox_discovered_url or "").strip()
            movie_id = ""
            url = ""
            if cached:
                try:
                    url = PVRINOX_URLS.validate(cached)
                    movie_id = self._movie_id_from_url(url)
                except ValueError:
                    watch.pvrinox_discovered_url = ""
                if movie_id:
                    notes.append("using cached canonical movie URL")
                    discovered = sanitize_url(url)
                else:
                    watch.pvrinox_discovered_url = ""
            if not movie_id:
                movie_id, url, discovery_notes, source = await self._discover(watch)
                notes.extend(discovery_notes)
                if source:
                    sources.append(source)
                if not movie_id:
                    self._checked_url = "https://api3.pvrcinemas.com/api/v1/booking/content/search"
                    self._diagnostic_context = {
                        "configured_mode": mode,
                        "supplied_url": "",
                        "discovered_url": "",
                        "parser_version": PVR_PARSER_VERSION,
                    }
                    return RawAdapterData(
                        diagnostics=notes,
                        empty_status=PlatformState.DISCOVERY_NO_RESULTS,
                        phase="discovery",
                        configured_mode=mode,
                        final_url=self._checked_url,
                        page_outcome="official search completed",
                        structured_sources=sources,
                        parser_version=PVR_PARSER_VERSION,
                    )
                watch.pvrinox_discovered_url = url
                discovered = sanitize_url(url)

        self._checked_url = sanitize_url(url)
        self._diagnostic_context = {
            "configured_mode": mode,
            "supplied_url": supplied,
            "discovered_url": discovered,
            "parser_version": PVR_PARSER_VERSION,
        }
        if not movie_id:
            notes.append("direct page does not expose a supported movie ID; trying page structure")
            return await self._fallback_page(watch, url, mode, supplied, discovered)

        payload = {
            "city": watch.city,
            "mid": movie_id,
            "experience": "ALL",
            "specialTag": "ALL",
            "lat": "0.00",
            "lng": "0.00",
            "lang": watch.language,
            "format": watch.format,
            "dated": watch.show_date.isoformat(),
            "time": "00:00-23:59",
            "cinetype": "ALL",
            "hc": "ALL",
            "adFree": False,
            "bbt": False,
        }
        data = await self._api_post("msessions", payload, watch.city)
        sources.append("public JSON: content/msessions")
        output = data.get("output")
        if data.get("result") != "success" or not isinstance(output, dict):
            no_record = "no record" in str(data.get("msg") or "").casefold()
            return RawAdapterData(
                diagnostics=[*notes, f"movie sessions response: {data.get('msg', '')}"],
                empty_status=(
                    PlatformState.PAGE_LOADED_NO_SHOWS
                    if no_record and mode == PlatformMode.AUTOMATIC
                    else PlatformState.PARSE_UNSUPPORTED
                ),
                phase="matching" if no_record else "parsing",
                configured_mode=mode,
                supplied_url=supplied,
                discovered_url=discovered,
                final_url=sanitize_url(url),
                page_outcome="official movie-session request completed",
                structured_sources=sources,
                parser_version=PVR_PARSER_VERSION,
            )

        movie = output.get("movie")
        if not isinstance(movie, dict):
            return RawAdapterData(
                diagnostics=[*notes, "movie-session response omitted the expected movie object"],
                empty_status=PlatformState.PARSE_UNSUPPORTED,
                phase="parsing",
                configured_mode=mode,
                supplied_url=supplied,
                discovered_url=discovered,
                final_url=sanitize_url(url),
                page_outcome="official movie-session request completed",
                structured_sources=sources,
                parser_version=PVR_PARSER_VERSION,
            )
        if not titles_match(
            watch.movie_name, str(movie.get("n") or movie.get("filmCommonName") or "")
        ):
            return RawAdapterData(
                diagnostics=[
                    *notes,
                    "movie-session response did not identify the configured movie",
                ],
                empty_status=PlatformState.DISCOVERY_NO_RESULTS,
                phase="discovery",
                configured_mode=mode,
                supplied_url=supplied,
                discovered_url=discovered,
                final_url=sanitize_url(url),
                page_outcome="official movie-session request completed",
                structured_sources=sources,
                parser_version=PVR_PARSER_VERSION,
            )

        shows: list[ShowResult] = []
        session_diagnostics: list[dict[str, object]] = []
        session_nodes = 0
        cinemas = output.get("movieCinemaSessions")
        if not isinstance(cinemas, list):
            cinemas = []
        for cinema_entry in cinemas:
            if not isinstance(cinema_entry, dict):
                continue
            cinema = (
                cinema_entry.get("cinema") if isinstance(cinema_entry.get("cinema"), dict) else {}
            )
            theatre = str(cinema.get("name") or "")
            city = str(cinema.get("cityName") or watch.city)
            experiences = cinema_entry.get("experienceSessions")
            if not isinstance(experiences, list):
                continue
            for experience in experiences:
                if not isinstance(experience, dict):
                    continue
                raw_shows = experience.get("shows")
                if not isinstance(raw_shows, list):
                    continue
                for item in raw_shows:
                    if not isinstance(item, dict):
                        continue
                    session_nodes += 1
                    # Captured 2026-07-23 msessions data proves status=0 is the set rendered
                    # by the public PVR page. status=1 records included the non-rendered
                    # 07:55 PM and 11:35 PM sessions, so accepting 1/2 is unsafe.
                    bookable = item.get("status") == 0
                    time_fields = {
                        str(key): value
                        for key, value in item.items()
                        if "time" in str(key).casefold()
                    }
                    parsed_time = parse_pvr_showtime(item.get("showTime"), "showTime")
                    explicit_format = str(item.get("filmFormat") or "").strip()
                    # movieFormat/screenType carry experiences such as ATMOS/Premium in
                    # this schema. The request itself was constrained by `format`.
                    raw_format = (
                        explicit_format
                        if " ".join(normalized_words(explicit_format)) in {"2d", "3d", "4dx"}
                        else watch.format
                    )
                    session_id = str(item.get("sessionId") or item.get("id") or "")
                    diagnostic = {
                        "time_fields": time_fields,
                        "selected_field": "showTime",
                        "raw_time": parsed_time.raw,
                        "normalized_time": parsed_time.normalized or SHOWTIME_UNVERIFIED,
                        "display_time": parsed_time.display or SHOWTIME_UNVERIFIED,
                        "timezone_treatment": parsed_time.timezone_treatment,
                        "verification_status": (
                            "VERIFIED_PAGE_VISIBLE_FIELD"
                            if parsed_time.verified
                            else SHOWTIME_UNVERIFIED
                        ),
                        "session_id": session_id,
                        "theatre": theatre,
                        "date": str(item.get("showDate") or item.get("showDateStr") or ""),
                        "language": str(item.get("language") or ""),
                        "format": raw_format,
                        "raw_status": item.get("status"),
                        "bookable": bookable,
                        "matched_time_preset": False,
                    }
                    session_diagnostics.append(diagnostic)
                    if not bookable:
                        continue
                    try:
                        show_date = watch.show_date.fromisoformat(
                            str(item.get("showDate") or item.get("showDateStr"))
                        )
                    except (TypeError, ValueError):
                        continue
                    shows.append(
                        ShowResult(
                            platform=self.name,
                            movie_title=str(movie.get("n") or watch.movie_name),
                            theatre=theatre,
                            date=show_date,
                            showtime=parsed_time.value,
                            language=str(item.get("language") or ""),
                            format=raw_format,
                            booking_url=sanitize_url(url),
                            city=city,
                            raw_time=parsed_time.raw,
                            normalized_time=parsed_time.normalized,
                            display_time=parsed_time.display,
                            time_source=parsed_time.source,
                            time_verified=parsed_time.verified,
                            timezone_treatment=parsed_time.timezone_treatment,
                            session_id=session_id,
                            bookable=True,
                            time_fields=time_fields,
                        )
                    )
        shows.sort(key=lambda show: (normalized_words(show.theatre), show.showtime, show.session_id))
        notes.append(
            f"identified exact movie ID {movie_id}; {session_nodes} session records, "
            f"{len(shows)} bookable candidates"
        )
        return RawAdapterData(
            shows=shows,
            diagnostics=notes,
            empty_status=PlatformState.PAGE_LOADED_NO_SHOWS,
            phase="matching",
            configured_mode=mode,
            supplied_url=supplied,
            discovered_url=discovered,
            final_url=sanitize_url(url),
            page_outcome="official movie-session JSON loaded",
            page_title=f"{movie.get('n', watch.movie_name)} sessions in {watch.city}",
            structured_sources=sources,
            parser_version=PVR_PARSER_VERSION,
            session_diagnostics=session_diagnostics,
        )
