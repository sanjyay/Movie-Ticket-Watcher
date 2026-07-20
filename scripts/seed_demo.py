#!/usr/bin/env python3
import argparse
import sys
from datetime import date, time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.database import SessionLocal, init_db  # noqa: E402
from app.models import Watch  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Create the documented Spider-Man example watch")
    parser.add_argument(
        "--date", required=True, type=date.fromisoformat, help="Real date in YYYY-MM-DD format"
    )
    parser.add_argument("--city", required=True)
    parser.add_argument("--topic", default="")
    args = parser.parse_args()
    if args.date.day != 31:
        parser.error("The example date must be a real date falling on the 31st")
    init_db()
    with SessionLocal() as db:
        db.add(
            Watch(
                movie_name="Spider-Man: Brand New Day",
                city=args.city,
                show_date=args.date,
                language="English",
                format="2D",
                time_preset="EVENING",
                start_time=time(17),
                end_time=time(21, 59),
                bookmyshow_enabled=True,
                pvrinox_enabled=True,
                bookmyshow_mode="AUTOMATIC",
                pvrinox_mode="AUTOMATIC",
                ntfy_topic=args.topic,
            )
        )
        db.commit()
    print("Demo watch created. Automatic discovery is enabled; direct URLs are optional.")


if __name__ == "__main__":
    main()
