from datetime import time

import pytest

from app.models import PlatformState
from app.platforms.bookmyshow import BookMyShowAdapter
from app.platforms.pvrinox import PvrInoxAdapter
from app.platforms.urls import sanitize_url
from app.schemas import ShowResult
from app.services.watcher import run_watch_check


async def test_watch_without_direct_urls_uses_discovery(watch, monkeypatch) -> None:
    async def fake_fetch(url: str, platform: str, watch_id: int) -> tuple[str, str]:
        assert "q=Spider-Man" in url
        return _fixture_html(platform), ""

    monkeypatch.setattr("app.platforms.bookmyshow.fetch_page", fake_fetch)
    result = await BookMyShowAdapter().search(watch)
    assert result.status == PlatformState.AVAILABLE
    assert result.checked_url.startswith("https://in.bookmyshow.com/explore/search")


async def test_direct_url_override_skips_discovery(watch, monkeypatch) -> None:
    seen = []

    async def fake_fetch(url: str, platform: str, watch_id: int) -> tuple[str, str]:
        seen.append(url)
        return _fixture_html(platform), ""

    watch.bookmyshow_mode = "DIRECT"
    watch.bookmyshow_direct_url = "https://in.bookmyshow.com/events/spider-man/ET123"
    monkeypatch.setattr("app.platforms.bookmyshow.fetch_page", fake_fetch)
    result = await BookMyShowAdapter().search(watch)
    assert result.status == PlatformState.AVAILABLE
    assert seen == [watch.bookmyshow_direct_url]
    assert not watch.bookmyshow_discovered_url


@pytest.mark.parametrize("url", ["javascript:alert(1)", "file:///tmp/show.html"])
async def test_invalid_url_scheme_is_configuration_required(watch, url: str) -> None:
    watch.bookmyshow_mode = "DIRECT"
    watch.bookmyshow_direct_url = url
    result = await BookMyShowAdapter().search(watch)
    assert result.status == PlatformState.CONFIGURATION_REQUIRED


async def test_wrong_platform_hostname_is_configuration_required(watch) -> None:
    watch.pvrinox_mode = "DIRECT"
    watch.pvrinox_direct_url = "https://example.com/show"
    result = await PvrInoxAdapter().search(watch)
    assert result.status == PlatformState.CONFIGURATION_REQUIRED


async def test_bookmyshow_failure_does_not_stop_pvr(watch, db, monkeypatch) -> None:
    async def bms_search(self, target_watch):
        return self.name, target_watch

    async def pvr_search(self, target_watch):
        return self.name, target_watch

    from app.schemas import PlatformResult

    async def bms_result(self, target_watch):
        return PlatformResult(self.name, PlatformState.ERROR, error="discovery failed")

    async def pvr_result(self, target_watch):
        show = ShowResult(
            self.name,
            target_watch.movie_name,
            "PVR Forum",
            target_watch.show_date,
            time(19, 30),
            target_watch.language,
            target_watch.format,
            "https://www.pvrcinemas.com/show",
            target_watch.city,
        )
        return PlatformResult(self.name, PlatformState.AVAILABLE, [show])

    monkeypatch.setattr("app.platforms.bookmyshow.BookMyShowAdapter.search", bms_result)
    monkeypatch.setattr("app.platforms.pvrinox.PvrInoxAdapter.search", pvr_result)
    db.add(watch)
    db.commit()
    results = await run_watch_check(db, watch)
    assert [result.platform for result in results] == ["BookMyShow", "PVR INOX"]
    assert watch.last_status == "AVAILABLE"


async def test_missing_required_url_status_is_configuration_required(watch, monkeypatch) -> None:
    from app.platforms.base import ConfigurationRequiredError

    async def raw_search(self, target_watch):
        raise ConfigurationRequiredError("BookMyShow URL is required")

    monkeypatch.setattr("app.platforms.bookmyshow.BookMyShowAdapter.raw_search", raw_search)
    result = await BookMyShowAdapter().search(watch)
    assert result.status == PlatformState.CONFIGURATION_REQUIRED


def _fixture_html(platform: str) -> str:
    return (
        "<div data-show='{\"movie\":\"Spider Man Brand New Day\",\"theatre\":\"PVR Forum\","
        "\"city\":\"Bengaluru\",\"date\":\"2030-05-31\",\"time\":\"7:30 PM\","
        "\"language\":\"English\",\"format\":\"2D\",\"url\":\"/show/1\"}'></div>"
    )


def test_diagnostic_url_sanitization_removes_credentials() -> None:
    cleaned = sanitize_url(
        "https://in.bookmyshow.com/event/ET123?token=secret&date=2030-05-31#fragment"
    )
    assert "secret" not in cleaned
    assert "token" not in cleaned
    assert "date=2030-05-31" in cleaned
    assert "fragment" not in cleaned
