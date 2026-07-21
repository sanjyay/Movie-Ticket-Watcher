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


def legacy_pvr_times(invalidate: bool = False, confirmed: bool = False) -> int:
    init_db()
    with SessionLocal() as db:
        rows = db.scalars(
            select(DetectedShow).where(
                DetectedShow.platform == "PVR INOX",
                DetectedShow.time_source == "",
                DetectedShow.legacy_time_invalidated.is_(False),
            )
        ).all()
        for row in rows:
            print(
                f"show_id={row.id} watch_id={row.watch_id} theatre={row.theatre!r} "
                f"date={row.show_date} legacy_time={row.showtime.strftime('%H:%M')} "
                "status=LEGACY_TIME_UNVERIFIED"
            )
        if not invalidate:
            print(f"Found {len(rows)} legacy PVR show record(s); no records changed.")
            return 0
        if not confirmed:
            print("Refusing to change records without --confirm-invalidate-legacy-times.")
            return 2
        for row in rows:
            row.time_verified = False
            row.normalized_time = ""
            row.display_time = ""
            row.time_source = "legacy-pvr-public-json-v1"
            row.timezone_treatment = "SHOWTIME_UNVERIFIED"
            row.legacy_time_invalidated = True
            # Retain session/history rows, but never enqueue this legacy row again.
            row.notification_sent = True
        db.commit()
        print(f"Invalidated {len(rows)} legacy PVR time(s); history was preserved.")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(prog="python -m app.cli")
    group = parser.add_subparsers(dest="group", required=True)
    notifications = group.add_parser("notifications")
    notifications.add_argument("action", choices=("pending", "clean-orphans"))
    shows = group.add_parser("shows")
    shows.add_argument("action", choices=("audit-times", "invalidate-legacy-times"))
    shows.add_argument("--confirm-invalidate-legacy-times", action="store_true")
    args = parser.parse_args()
    if args.group == "shows":
        return legacy_pvr_times(
            invalidate=args.action == "invalidate-legacy-times",
            confirmed=args.confirm_invalidate_legacy_times,
        )
    return pending(clean=args.action == "clean-orphans")


if __name__ == "__main__":
    raise SystemExit(main())
