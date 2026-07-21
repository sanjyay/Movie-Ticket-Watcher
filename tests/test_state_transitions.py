from sqlalchemy import func, select

from app.models import DetectedShow, NotificationHistory, StateTransition
from app.services.watcher import run_watch_check


async def test_simulation_transition_and_dedup(db, watch, monkeypatch) -> None:
    calls = []

    async def fake_notify(session, target_watch, show):
        fingerprint = f"simulation:{target_watch.id}:{show.platform}:{show.fingerprint}"
        if session.scalar(
            select(NotificationHistory.id).where(
                NotificationHistory.fingerprint == fingerprint,
                NotificationHistory.notification_source == "SIMULATION",
            )
        ):
            return False
        calls.append(show.fingerprint)
        session.add(
            NotificationHistory(
                watch_id=target_watch.id,
                fingerprint=fingerprint,
                success=True,
                notification_source="SIMULATION",
            )
        )
        return True

    monkeypatch.setattr("app.services.watcher.record_simulation_notification", fake_notify)
    watch.telegram_chat_id_override = "12345"
    watch.simulation_state = "UNAVAILABLE"
    db.add(watch)
    db.commit()
    await run_watch_check(db, watch)
    watch.simulation_state = "AVAILABLE"
    await run_watch_check(db, watch)
    await run_watch_check(db, watch)
    assert db.scalar(select(func.count()).select_from(DetectedShow)) == 0
    assert len(calls) == 2  # one stable show per enabled platform
    assert db.scalar(select(func.count()).select_from(StateTransition)) == 0


async def test_disabled_telegram_sends_nothing(db, watch, monkeypatch) -> None:
    async def unexpected(*_args, **_kwargs):
        raise AssertionError("notification delivery must not run")

    monkeypatch.setattr("app.services.watcher.record_notification", unexpected)
    watch.notifications_enabled = False
    watch.simulation_state = "AVAILABLE"
    db.add(watch)
    db.commit()
    await run_watch_check(db, watch)
    assert db.scalar(select(func.count()).select_from(NotificationHistory)) == 0
