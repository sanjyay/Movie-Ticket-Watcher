import argparse

from sqlalchemy import select

from app.database import SessionLocal, init_db
from app.models import DetectedShow, NotificationHistory, PlatformCheck, Watch


def pending(clean: bool = False) -> int:
    init_db()
    changed = 0
    with SessionLocal() as db:
        rows = db.scalars(
            select(NotificationHistory).where(
                NotificationHistory.notification_source == "LIVE_AVAILABILITY",
                NotificationHistory.success.is_(False),
                NotificationHistory.delivery_status == "FAILED",
            )
        ).all()
        for row in rows:
            watch = db.get(Watch, row.watch_id)
            show = db.get(DetectedShow, row.detected_show_id) if row.detected_show_id else None
            check = db.get(PlatformCheck, row.platform_check_id) if row.platform_check_id else None
            reasons = []
            if not watch:
                reasons.append("watch missing")
            elif not watch.enabled:
                reasons.append("watch disabled")
            if not show or show.watch_id != row.watch_id:
                reasons.append("detected show missing")
            if not check or check.watch_id != row.watch_id:
                reasons.append("platform check missing")
            orphan = ", ".join(reasons)
            if clean and orphan:
                row.delivery_status = "CANCELLED"
                row.cancellation_reason = orphan
                changed += 1
            else:
                print(
                    f"history_id={row.id} watch_id={row.watch_id} "
                    f"fingerprint={row.fingerprint[:12]} status={row.delivery_status} "
                    f"validation={orphan or 'eligible'}"
                )
        if clean:
            db.commit()
            print(f"Cancelled {changed} orphaned pending delivery record(s).")
        elif not rows:
            print("No pending live availability notifications.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    group = parser.add_subparsers(dest="group", required=True)
    notifications = group.add_parser("notifications")
    notifications.add_argument("action", choices=("pending", "clean-orphans"))
    args = parser.parse_args()
    return pending(clean=args.action == "clean-orphans")


if __name__ == "__main__":
    raise SystemExit(main())
