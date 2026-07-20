from urllib.parse import urljoin

from bs4 import BeautifulSoup

from app.models import PlatformMode, PlatformState, Watch
from app.platforms.base import ConfigurationRequiredError, TicketPlatformAdapter
from app.platforms.browser import PageSnapshot, fetch_page
from app.platforms.parsing import PARSER_VERSION, parse_show_page, read_fixture
from app.platforms.urls import BOOKMYSHOW_URLS, sanitize_url
from app.schemas import PlatformResult, RawAdapterData
from app.services.matching import normalized_words


class BookMyShowAdapter(TicketPlatformAdapter):
    name = "BookMyShow"

    def _mode_and_direct_url(self, watch: Watch) -> tuple[str, str]:
        forced = getattr(self, "forced_direct_url", None)
        if forced is not None:
            return PlatformMode.DIRECT, str(forced).strip()
        mode = (watch.bookmyshow_mode or PlatformMode.AUTOMATIC).upper()
        direct = (watch.bookmyshow_direct_url or "").strip()
        legacy = (watch.bookmyshow_url or "").strip()
        if not direct and legacy and (not watch.bookmyshow_mode or legacy.startswith("fixture://")):
            direct = legacy
            mode = PlatformMode.DIRECT
        return mode, direct

    def resolve_url(self, watch: Watch) -> tuple[str, str, str, str]:
        mode, direct = self._mode_and_direct_url(watch)
        if mode == PlatformMode.DISABLED:
            raise ConfigurationRequiredError("BookMyShow is disabled for this watch")
        if mode == PlatformMode.DIRECT:
            if not direct:
                raise ConfigurationRequiredError(
                    "BookMyShow direct URL is required in Direct URL mode"
                )
            if direct.startswith("fixture://"):
                return direct, "direct fixture override", direct, ""
            try:
                validated = BOOKMYSHOW_URLS.validate(direct)
            except ValueError as exc:
                raise ConfigurationRequiredError(str(exc)) from exc
            return validated, "direct URL", sanitize_url(validated), ""

        cached = (watch.bookmyshow_discovered_url or "").strip()
        if cached:
            try:
                validated = BOOKMYSHOW_URLS.validate(cached)
                if "/explore/search" not in validated:
                    return validated, "cached canonical URL", "", sanitize_url(validated)
                watch.bookmyshow_discovered_url = ""
            except ValueError:
                watch.bookmyshow_discovered_url = ""
        discovered = BOOKMYSHOW_URLS.discovery_url(watch)
        return discovered, "automatic discovery", "", sanitize_url(discovered)

    @staticmethod
    def _coerce_snapshot(value) -> PageSnapshot:  # type: ignore[no-untyped-def]
        if isinstance(value, PageSnapshot):
            return value
        html, screenshot = value
        return PageSnapshot(html=html, screenshot_path=screenshot)

    @staticmethod
    def _canonical_candidate(html: str, watch: Watch, base_url: str) -> str:
        expected = normalized_words(watch.movie_name)
        soup = BeautifulSoup(html, "html.parser")
        for anchor in soup.select("a[href]"):
            href = str(anchor.get("href") or "")
            label = anchor.get_text(" ", strip=True)
            combined = normalized_words(f"{label} {href.replace('-', ' ')}")
            if expected and all(word in combined for word in expected):
                candidate = urljoin(base_url, href)
                try:
                    return BOOKMYSHOW_URLS.validate(candidate)
                except ValueError:
                    continue
        return ""

    async def raw_search(self, watch: Watch) -> RawAdapterData:
        url, source, supplied, discovered = self.resolve_url(watch)
        mode, _direct = self._mode_and_direct_url(watch)
        self._checked_url = sanitize_url(url)
        self._diagnostic_context = {
            "configured_mode": mode,
            "supplied_url": supplied,
            "discovered_url": discovered,
            "parser_version": PARSER_VERSION,
        }
        if url.startswith("fixture://"):
            snapshot = PageSnapshot(html=read_fixture(url), outcome="fixture")
        else:
            snapshot = self._coerce_snapshot(await fetch_page(url, "bookmyshow", watch.id))

        canonical = ""
        if mode == PlatformMode.AUTOMATIC and not watch.bookmyshow_discovered_url:
            canonical = self._canonical_candidate(snapshot.html, watch, snapshot.final_url or url)
            if canonical and canonical != url:
                watch.bookmyshow_discovered_url = canonical
                discovered = sanitize_url(canonical)
                self._checked_url = discovered
                snapshot = self._coerce_snapshot(
                    await fetch_page(canonical, "bookmyshow", watch.id)
                )

        parsed = parse_show_page(snapshot.html, self.name, snapshot.final_url or url)
        notes = [f"{source}; checked {self._checked_url}", *parsed.diagnostics]
        if canonical:
            notes.insert(1, f"canonical movie/event URL discovered: {discovered}")
        if parsed.booking_closed:
            empty_status = PlatformState.PAGE_LOADED_NO_SHOWS
            phase = "matching"
        elif not parsed.shows and mode == PlatformMode.AUTOMATIC and not canonical:
            empty_status = PlatformState.DISCOVERY_NO_RESULTS
            phase = "discovery"
        elif not parsed.shows:
            empty_status = PlatformState.PARSE_UNSUPPORTED
            phase = "parsing"
        else:
            empty_status = PlatformState.UNAVAILABLE
            phase = "matching"
        return RawAdapterData(
            shows=parsed.shows,
            diagnostics=notes,
            screenshot_path=snapshot.screenshot_path,
            empty_status=empty_status,
            phase=phase,
            configured_mode=mode,
            supplied_url=supplied,
            discovered_url=discovered,
            final_url=sanitize_url(snapshot.final_url or url),
            page_outcome=snapshot.outcome,
            page_title=snapshot.title,
            structured_sources=parsed.structured_sources,
            parser_version=PARSER_VERSION,
        )

    async def search(self, watch: Watch) -> PlatformResult:
        result = await super().search(watch)
        if result.status == PlatformState.BLOCKED:
            if result.configured_mode == PlatformMode.DIRECT:
                result.error = (
                    f"{result.error}. The supplied direct URL was also blocked during "
                    f"{result.phase}; this server/IP cannot currently monitor BookMyShow "
                    "reliably. No platform protection was bypassed."
                )
            else:
                result.error = (
                    f"{result.error}. BookMyShow blocked this server/IP during {result.phase}. "
                    "A manually copied movie/event URL can be tested in Direct URL mode, but it "
                    "may be blocked too and does not bypass platform protection."
                )
        return result
