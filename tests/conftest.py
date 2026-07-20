from datetime import date, time
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.database import Base
from app.models import Watch

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def db() -> Session:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        yield session


@pytest.fixture
def watch() -> Watch:
    return Watch(
        movie_name="Spider-Man: Brand New Day",
        city="Bengaluru",
        show_date=date(2030, 5, 31),
        language="English",
        format="2D",
        time_preset="EVENING",
        start_time=time(17),
        end_time=time(21, 59),
        bookmyshow_enabled=True,
        pvrinox_enabled=True,
        bookmyshow_mode="AUTOMATIC",
        pvrinox_mode="AUTOMATIC",
        polling_interval_seconds=300,
        notification_enabled=True,
        enabled=True,
    )
