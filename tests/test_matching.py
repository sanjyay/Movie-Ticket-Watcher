from datetime import time

from app.schemas import ShowResult
from app.services.matching import filter_shows, parse_time, titles_match


def test_title_normalization_is_strict() -> None:
    assert titles_match("Spider-Man: Brand New Day", "SPIDER MAN BRAND NEW DAY")
    assert not titles_match("Spider-Man: Brand New Day", "Spider-Man")
    assert not titles_match("Spider-Man: Brand New Day", "Spider-Man: Homecoming")


def test_filters_concrete_show(watch) -> None:
    show = ShowResult(
        "BookMyShow",
        "Spider Man Brand New Day",
        "PVR Forum",
        watch.show_date,
        time(19, 30),
        "English",
        "2D",
        "https://example.test",
        watch.city,
    )
    matches, reasons = filter_shows(watch, [show])
    assert matches == [show]
    assert "matched all filters" in reasons[0]


def test_matching_fixture_show_inside_preset(watch) -> None:
    show = ShowResult(
        "BookMyShow",
        watch.movie_name,
        "PVR Forum",
        watch.show_date,
        time(19, 30),
        watch.language,
        watch.format,
        "https://example.test",
        watch.city,
    )
    matches, _ = filter_shows(watch, [show])
    assert matches == [show]


def test_rejects_fixture_show_outside_preset(watch) -> None:
    show = ShowResult(
        "BookMyShow",
        watch.movie_name,
        "PVR Forum",
        watch.show_date,
        time(22, 30),
        watch.language,
        watch.format,
        "https://example.test",
        watch.city,
    )
    matches, reasons = filter_shows(watch, [show])
    assert not matches
    assert "outside time window" in reasons[0]


def test_time_parser() -> None:
    assert parse_time("7:30 PM") == time(19, 30)


def test_missing_or_wrong_city_never_matches(watch) -> None:
    show = ShowResult(
        "BookMyShow",
        watch.movie_name,
        "PVR Forum",
        watch.show_date,
        time(19, 30),
        watch.language,
        watch.format,
        "https://example.test",
        "Mumbai",
    )
    matches, reasons = filter_shows(watch, [show])
    assert not matches
    assert "city differs" in reasons[0]
