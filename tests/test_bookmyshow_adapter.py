from app.models import PlatformState
from app.platforms.bookmyshow import BookMyShowAdapter
from tests.conftest import FIXTURES


async def test_matching_fixture(watch) -> None:
    watch.bookmyshow_url = f"fixture://{FIXTURES / 'booking_open.html'}"
    result = await BookMyShowAdapter().search(watch)
    assert result.status == PlatformState.AVAILABLE
    assert len(result.shows) == 1


async def test_generic_book_button_does_not_match(watch) -> None:
    watch.bookmyshow_url = f"fixture://{FIXTURES / 'booking_closed.html'}"
    result = await BookMyShowAdapter().search(watch)
    assert result.status == PlatformState.PAGE_LOADED_NO_SHOWS


async def test_blocked_fixture(watch) -> None:
    watch.bookmyshow_url = f"fixture://{FIXTURES / 'blocked.html'}"
    result = await BookMyShowAdapter().search(watch)
    assert result.status == PlatformState.BLOCKED


async def test_cloudflare_fixture_extracts_ray_id(watch) -> None:
    watch.bookmyshow_url = f"fixture://{FIXTURES / 'cloudflare_blocked.html'}"
    result = await BookMyShowAdapter().search(watch)
    assert result.status == PlatformState.BLOCKED
    assert result.block_classification == "cloudflare"
    assert result.ray_id == "8abc1234def56789"
