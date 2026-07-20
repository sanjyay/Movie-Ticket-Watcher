import asyncio
from abc import ABC, abstractmethod
from datetime import datetime, timezone

import httpx
from sqlalchemy.orm import Session

from app.config import get_settings
from app.models import NotificationHistory, Watch
from app.schemas import ShowResult


class NotificationProvider(ABC):
    @abstractmethod
    async def send_show(self, watch: Watch, show: ShowResult) -> None: ...

    @abstractmethod
    async def send_test(self, topic: str) -> None: ...


class NtfyProvider(NotificationProvider):
    def __init__(self) -> None:
        self.settings = get_settings()

    def _auth(self):  # type: ignore[no-untyped-def]
        if self.settings.ntfy_token:
            return None, {"Authorization": f"Bearer {self.settings.ntfy_token}"}
        auth = (
            httpx.BasicAuth(self.settings.ntfy_username, self.settings.ntfy_password)
            if self.settings.ntfy_username
            else None
        )
        return auth, {}

    async def _send(self, topic: str, body: str, title: str, click: str = "") -> None:
        if not topic:
            raise ValueError("ntfy topic is empty")
        auth, headers = self._auth()
        headers.update({"Title": title, "Tags": "ticket"})
        if click:
            headers["Click"] = click
        async with httpx.AsyncClient(timeout=15) as client:
            response = await client.post(
                f"{self.settings.ntfy_server.rstrip('/')}/{topic}",
                content=body,
                headers=headers,
                auth=auth,
            )
            response.raise_for_status()

    async def send_show(self, watch: Watch, show: ShowResult) -> None:
        detected = datetime.now(timezone.utc).isoformat(timespec="seconds")
        body = (
            f"{watch.movie_name}\n{show.date} · {show.showtime.strftime('%H:%M')}\n"
            f"{show.language} · {show.format}\n{show.theatre}\n{show.platform}\n"
            f"Detected: {detected}\n{show.booking_url}"
        )
        await self._send(watch.ntfy_topic, body, "Tickets available", show.booking_url)

    async def send_test(self, topic: str) -> None:
        await self._send(
            topic, "Movie Ticket Watcher notifications are working.", "Test notification"
        )


async def record_notification(
    db: Session, watch: Watch, show: ShowResult, provider: NotificationProvider | None = None
) -> bool:
    provider = provider or NtfyProvider()
    error = ""
    success = False
    attempts = 0
    for attempt in range(1, 4):
        attempts = attempt
        try:
            await provider.send_show(watch, show)
            success = True
            break
        except (httpx.HTTPError, OSError, ValueError) as exc:
            error = str(exc)
            if attempt < 3:
                await asyncio.sleep(2 ** (attempt - 1))
    db.add(
        NotificationHistory(
            watch_id=watch.id,
            fingerprint=show.fingerprint,
            success=success,
            attempts=attempts,
            error=error,
        )
    )
    return success
