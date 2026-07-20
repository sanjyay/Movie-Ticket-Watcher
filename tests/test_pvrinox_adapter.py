import pytest

from app.models import PlatformState
from app.platforms.pvrinox import PvrInoxAdapter
from tests.conftest import FIXTURES


@pytest.mark.parametrize(
    "name",
    [
        "wrong_language.html",
        "wrong_format.html",
        "morning_only.html",
        "wrong_title.html",
    ],
)
async def test_non_matching_fixtures(watch, name: str) -> None:
    watch.pvrinox_url = f"fixture://{FIXTURES / name}"
    result = await PvrInoxAdapter().search(watch)
    assert result.status == PlatformState.UNAVAILABLE
    assert not result.shows


async def test_malformed_fixture_is_parse_unsupported(watch) -> None:
    watch.pvrinox_url = f"fixture://{FIXTURES / 'malformed.html'}"
    result = await PvrInoxAdapter().search(watch)
    assert result.status == PlatformState.PARSE_UNSUPPORTED


async def test_multiple_shows(watch) -> None:
    watch.pvrinox_url = f"fixture://{FIXTURES / 'multiple.html'}"
    result = await PvrInoxAdapter().search(watch)
    assert result.status == PlatformState.AVAILABLE
    assert len(result.shows) == 2
