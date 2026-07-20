from datetime import date, time

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, text

from app.database import Base
from app.main import apply_form
from app.models import Watch
from app.time_presets import range_for


@pytest.mark.parametrize(
    ("preset", "expected"),
    [
        ("ANY", (time(0, 0), time(23, 59))),
        ("MORNING", (time(5, 0), time(11, 59))),
        ("AFTERNOON", (time(12, 0), time(16, 59))),
        ("EVENING", (time(17, 0), time(21, 59))),
        ("NIGHT", (time(22, 0), time(23, 59))),
    ],
)
def test_standard_preset_mapping(preset: str, expected: tuple[time, time]) -> None:
    watch = Watch(movie_name="", city="", show_date=date(2030, 5, 31))
    form = _form(time_preset=preset, start_time="01:00", end_time="02:00")
    apply_form(watch, form, 120)
    assert watch.time_preset == preset
    assert (watch.start_time, watch.end_time) == expected


def test_custom_time_range() -> None:
    watch = Watch(movie_name="", city="", show_date=date(2030, 5, 31))
    apply_form(watch, _form(time_preset="CUSTOM", start_time="10:15", end_time="13:45"), 120)
    assert watch.time_preset == "CUSTOM"
    assert watch.start_time == time(10, 15)
    assert watch.end_time == time(13, 45)


def test_custom_overnight_range_is_rejected() -> None:
    watch = Watch(movie_name="", city="", show_date=date(2030, 5, 31))
    with pytest.raises(HTTPException, match="midnight"):
        apply_form(watch, _form(time_preset="CUSTOM", start_time="22:00", end_time="02:00"), 120)


def test_preset_range_is_not_timezone_shifted() -> None:
    assert range_for("NIGHT") == (time(22, 0), time(23, 59))


def test_invalid_existing_preset_range_migrates(tmp_path, monkeypatch) -> None:
    database = tmp_path / "tickets.db"
    engine = create_engine(f"sqlite:///{database}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO watches "
                "(movie_name, city, show_date, language, format, time_preset, start_time, end_time, "
                "preferred_theatres, bookmyshow_enabled, pvrinox_enabled, bookmyshow_url, "
                "pvrinox_url, bookmyshow_discovered_url, pvrinox_discovered_url, "
                "polling_interval_seconds, ntfy_topic, notification_enabled, enabled, "
                "simulation_state, last_status, last_error, matching_show_count, created_at) "
                "VALUES ('Spider-Man: Brand New Day', 'Bengaluru', '2030-05-31', 'English', "
                "'2D', 'EVENING', '22:00:00.000000', '13:59:00.000000', '', 1, 1, '', '', "
                "'', '', 300, '', 1, 1, 'OFF', 'WAITING', '', 0, CURRENT_TIMESTAMP)"
            )
        )

    import app.database as database_module

    monkeypatch.setattr(database_module, "engine", engine)
    monkeypatch.setattr(database_module.get_settings(), "database_url", f"sqlite:///{database}")
    monkeypatch.setattr(database_module.get_settings(), "data_dir", tmp_path)
    database_module.init_db()
    with engine.connect() as connection:
        row = connection.execute(text("SELECT start_time, end_time FROM watches")).one()
    assert row == ("17:00:00.000000", "21:59:00.000000")


def _form(**overrides: str) -> dict[str, str]:
    values = {
        "movie_name": "Spider-Man: Brand New Day",
        "city": "Bengaluru",
        "show_date": "2030-05-31",
        "language": "English",
        "format": "2D",
        "time_preset": "EVENING",
        "start_time": "17:00",
        "end_time": "21:59",
        "preferred_theatres": "",
        "polling_interval_seconds": "300",
        "ntfy_topic": "",
        "simulation_state": "OFF",
        "bookmyshow_url": "",
        "pvrinox_url": "",
        "bookmyshow_enabled": "on",
        "pvrinox_enabled": "on",
        "notification_enabled": "on",
        "enabled": "on",
    }
    values.update(overrides)
    return values
