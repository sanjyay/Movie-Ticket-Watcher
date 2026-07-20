import fcntl
from collections.abc import Generator

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import DeclarativeBase, Session, sessionmaker

from app.config import get_settings


class Base(DeclarativeBase):
    pass


engine = create_engine(
    get_settings().database_url,
    connect_args={"check_same_thread": False}
    if get_settings().database_url.startswith("sqlite")
    else {},
)


@event.listens_for(engine, "connect")
def configure_sqlite(dbapi_connection, _connection_record) -> None:  # type: ignore[no-untyped-def]
    if get_settings().database_url.startswith("sqlite"):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=10000")
        cursor.close()


SessionLocal = sessionmaker(bind=engine, expire_on_commit=False)


def init_db() -> None:
    from app import models  # noqa: F401
    from app.time_presets import TIME_PRESETS

    settings = get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    lock_path = settings.data_dir / "database-init.lock"
    with lock_path.open("w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        Base.metadata.create_all(engine)
        if settings.database_url.startswith("sqlite"):
            inspector = inspect(engine)
            watch_columns = {column["name"] for column in inspector.get_columns("watches")}
            check_columns = {column["name"] for column in inspector.get_columns("platform_checks")}
            with engine.begin() as connection:
                if "time_preset" not in watch_columns:
                    connection.execute(
                        text(
                            "ALTER TABLE watches ADD COLUMN time_preset VARCHAR(20) "
                            "DEFAULT 'CUSTOM'"
                        )
                    )
                if "bookmyshow_discovered_url" not in watch_columns:
                    connection.execute(
                        text(
                            "ALTER TABLE watches ADD COLUMN bookmyshow_discovered_url TEXT "
                            "DEFAULT ''"
                        )
                    )
                if "pvrinox_discovered_url" not in watch_columns:
                    connection.execute(
                        text(
                            "ALTER TABLE watches ADD COLUMN pvrinox_discovered_url TEXT "
                            "DEFAULT ''"
                        )
                    )
                added_bookmyshow_mode = "bookmyshow_mode" not in watch_columns
                added_pvrinox_mode = "pvrinox_mode" not in watch_columns
                if added_bookmyshow_mode:
                    connection.execute(
                        text(
                            "ALTER TABLE watches ADD COLUMN bookmyshow_mode VARCHAR(20) "
                            "DEFAULT 'AUTOMATIC'"
                        )
                    )
                if added_pvrinox_mode:
                    connection.execute(
                        text(
                            "ALTER TABLE watches ADD COLUMN pvrinox_mode VARCHAR(20) "
                            "DEFAULT 'AUTOMATIC'"
                        )
                    )
                if "bookmyshow_direct_url" not in watch_columns:
                    connection.execute(
                        text(
                            "ALTER TABLE watches ADD COLUMN bookmyshow_direct_url TEXT DEFAULT ''"
                        )
                    )
                if "pvrinox_direct_url" not in watch_columns:
                    connection.execute(
                        text("ALTER TABLE watches ADD COLUMN pvrinox_direct_url TEXT DEFAULT ''")
                    )
                if "checked_url" not in check_columns:
                    connection.execute(
                        text(
                            "ALTER TABLE platform_checks ADD COLUMN checked_url TEXT DEFAULT ''"
                        )
                    )
                if "phase" not in check_columns:
                    connection.execute(
                        text(
                            "ALTER TABLE platform_checks ADD COLUMN phase VARCHAR(40) "
                            "DEFAULT ''"
                        )
                    )
                check_migrations = {
                    "configured_mode": "VARCHAR(20) DEFAULT ''",
                    "supplied_url": "TEXT DEFAULT ''",
                    "discovered_url": "TEXT DEFAULT ''",
                    "final_url": "TEXT DEFAULT ''",
                    "page_outcome": "VARCHAR(80) DEFAULT ''",
                    "page_title": "TEXT DEFAULT ''",
                    "structured_sources": "TEXT DEFAULT ''",
                    "raw_candidate_count": "INTEGER DEFAULT 0",
                    "matching_count": "INTEGER DEFAULT 0",
                    "block_classification": "VARCHAR(80) DEFAULT ''",
                    "ray_id": "VARCHAR(120) DEFAULT ''",
                    "parser_version": "VARCHAR(40) DEFAULT ''",
                }
                for column, definition in check_migrations.items():
                    if column not in check_columns:
                        connection.execute(
                            text(f"ALTER TABLE platform_checks ADD COLUMN {column} {definition}")
                        )
                connection.execute(
                    text(
                        "UPDATE watches SET bookmyshow_direct_url = bookmyshow_url "
                        "WHERE COALESCE(bookmyshow_direct_url, '') = '' "
                        "AND COALESCE(bookmyshow_url, '') != ''"
                    )
                )
                connection.execute(
                    text(
                        "UPDATE watches SET pvrinox_direct_url = pvrinox_url "
                        "WHERE COALESCE(pvrinox_direct_url, '') = '' "
                        "AND COALESCE(pvrinox_url, '') != ''"
                    )
                )
                if added_bookmyshow_mode:
                    connection.execute(
                        text(
                            "UPDATE watches SET bookmyshow_mode = CASE "
                            "WHEN bookmyshow_enabled = 0 THEN 'DISABLED' "
                            "WHEN COALESCE(bookmyshow_url, '') != '' THEN 'DIRECT' "
                            "ELSE 'AUTOMATIC' END"
                        )
                    )
                if added_pvrinox_mode:
                    connection.execute(
                        text(
                            "UPDATE watches SET pvrinox_mode = CASE "
                            "WHEN pvrinox_enabled = 0 THEN 'DISABLED' "
                            "WHEN COALESCE(pvrinox_url, '') != '' THEN 'DIRECT' "
                            "ELSE 'AUTOMATIC' END"
                        )
                    )
                for preset, (_label, start, end) in TIME_PRESETS.items():
                    connection.execute(
                        text(
                            "UPDATE watches SET start_time = :start_time, end_time = :end_time "
                            "WHERE time_preset = :preset "
                            "AND (start_time != :start_time OR end_time != :end_time)"
                        ),
                        {
                            "preset": preset,
                            "start_time": start.strftime("%H:%M:%S.000000"),
                            "end_time": end.strftime("%H:%M:%S.000000"),
                        },
                    )


def get_db() -> Generator[Session, None, None]:
    with SessionLocal() as session:
        yield session
