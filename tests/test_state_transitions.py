from sqlalchemy import func, select

from app.models import DetectedShow, NotificationHistory, StateTransition
from app.services.watcher import run_watch_check


async def test_simulation_transition_and_dedup(db, watch, monkeypatch) -> None:
    calls = []

    async def fake_notify(session, target_watch, show):
        calls.append(show.fingerprint)
        session.add(
            NotificationHistory(
                watch_id=target_watch.id, fingerprint=show.fingerprint, success=True
            )
        )
        return True

    monkeypatch.setattr("app.services.watcher.record_notification", fake_notify)
    watch.ntfy_topic = "test"
    watch.simulation_state = "UNAVAILABLE"
    db.add(watch)
    db.commit()
    await run_watch_check(db, watch)
    watch.simulation_state = "AVAILABLE"
    await run_watch_check(db, watch)
    await run_watch_check(db, watch)
    assert db.scalar(select(func.count()).select_from(DetectedShow)) == 2
    assert len(calls) == 2  # one stable show per enabled platform
    assert db.scalar(select(func.count()).select_from(StateTransition)) >= 2
