import json

from app.models import PlatformState
from app.platforms.pvrinox import PvrInoxAdapter
from tests.conftest import FIXTURES


def fixture(name: str) -> dict:
    return json.loads((FIXTURES / name).read_text())


async def test_automatic_mode_uses_official_discovery(watch, monkeypatch) -> None:
    calls = []

    async def api(self, endpoint, payload, city):
        calls.append(endpoint)
        return fixture(
            "pvr_search_results.json" if endpoint == "search" else "pvr_sessions_available.json"
        )

    monkeypatch.setattr(PvrInoxAdapter, "_api_post", api)
    result = await PvrInoxAdapter().search(watch)
    assert calls == ["search", "msessions"]
    assert result.status == PlatformState.AVAILABLE
    assert watch.pvrinox_discovered_url.endswith("/12345")


async def test_direct_url_mode_skips_discovery(watch, monkeypatch) -> None:
    watch.pvrinox_mode = "DIRECT"
    watch.pvrinox_direct_url = (
        "https://www.pvrcinemas.com/moviesessions/bengaluru/spider-man-brand-new-day/12345"
    )
    calls = []

    async def api(self, endpoint, payload, city):
        calls.append(endpoint)
        return fixture("pvr_sessions_available.json")

    monkeypatch.setattr(PvrInoxAdapter, "_api_post", api)
    result = await PvrInoxAdapter().search(watch)
    assert calls == ["msessions"]
    assert result.status == PlatformState.AVAILABLE


async def test_discovery_no_search_results(watch, monkeypatch) -> None:
    async def api(self, endpoint, payload, city):
        return fixture("pvr_search_no_results.json")

    monkeypatch.setattr(PvrInoxAdapter, "_api_post", api)
    result = await PvrInoxAdapter().search(watch)
    assert result.status == PlatformState.DISCOVERY_NO_RESULTS


async def test_correct_movie_page_booking_closed(watch, monkeypatch) -> None:
    async def api(self, endpoint, payload, city):
        return fixture(
            "pvr_search_results.json" if endpoint == "search" else "pvr_sessions_closed.json"
        )

    monkeypatch.setattr(PvrInoxAdapter, "_api_post", api)
    result = await PvrInoxAdapter().search(watch)
    assert result.status == PlatformState.PAGE_LOADED_NO_SHOWS


async def test_correct_movie_page_has_qualifying_show(watch, monkeypatch) -> None:
    async def api(self, endpoint, payload, city):
        return fixture(
            "pvr_search_results.json" if endpoint == "search" else "pvr_sessions_available.json"
        )

    monkeypatch.setattr(PvrInoxAdapter, "_api_post", api)
    result = await PvrInoxAdapter().search(watch)
    assert result.status == PlatformState.AVAILABLE
    assert result.raw_candidate_count == 1
    assert result.matching_count == 1


async def test_unsupported_public_response_is_not_unavailable(watch, monkeypatch) -> None:
    async def api(self, endpoint, payload, city):
        return fixture(
            "pvr_search_results.json" if endpoint == "search" else "pvr_sessions_unsupported.json"
        )

    monkeypatch.setattr(PvrInoxAdapter, "_api_post", api)
    result = await PvrInoxAdapter().search(watch)
    assert result.status == PlatformState.PARSE_UNSUPPORTED
