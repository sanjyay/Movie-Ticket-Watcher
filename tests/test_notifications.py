from app.models import NotificationHistory
from app.schemas import ShowResult
from app.services.notifications import NotificationProvider, record_notification


class FakeProvider(NotificationProvider):
    async def send_show(self, watch, show) -> None:  # type: ignore[no-untyped-def]
        return None

    async def send_test(self, topic: str) -> None:
        return None


async def test_mock_notification_is_recorded(db, watch) -> None:
    db.add(watch)
    db.commit()
    show = ShowResult(
        "BookMyShow",
        watch.movie_name,
        "PVR",
        watch.show_date,
        watch.start_time,
        watch.language,
        watch.format,
        "https://example.test",
        watch.city,
    )
    assert await record_notification(db, watch, show, FakeProvider())
    db.commit()
    assert db.query(NotificationHistory).one().success
