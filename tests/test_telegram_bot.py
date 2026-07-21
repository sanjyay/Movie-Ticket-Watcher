from datetime import date, datetime, timedelta, timezone

import httpx
import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.models import RuntimeState, TelegramConversationState, TelegramWatchCreation, Watch
from app.platforms.pvrinox import PvrInoxAdapter
from app.telegram_bot import (
    CONFIRMATION,
    BotAPI,
    BotController,
    accessible_watches,
    authorized,
    clear_state,
    get_state,
    save_state,
    set_runtime,
    settings,
    stale_initial_update,
)


class FakeAPI:
    def __init__(self) -> None:
        self.calls = []

    async def call(self, method, **payload):  # type: ignore[no-untyped-def]
        self.calls.append((method, payload))
        return True

    async def send(self, chat_id, text, rows=None):  # type: ignore[no-untyped-def]
        self.calls.append(("send", {"chat_id": chat_id, "text": text, "rows": rows}))
        return {"message_id": 1}

    async def edit(self, chat_id, message_id, text, rows=None):  # type: ignore[no-untyped-def]
        self.calls.append(("edit", {"chat_id": chat_id, "text": text, "rows": rows}))


@pytest.fixture
def bot_store(tmp_path, monkeypatch):  # type: ignore[no-untyped-def]
    engine = create_engine(f"sqlite:///{tmp_path / 'bot.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    monkeypatch.setattr("app.telegram_bot.SessionLocal", factory)
    monkeypatch.setattr(settings, "telegram_allowed_chat_ids", "123,-10099")
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", "")
    monkeypatch.setattr(settings, "telegram_default_chat_id", "123")
    monkeypatch.setattr(
        settings, "telegram_bot_token", "123456789:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdef"
    )
    yield factory


def update(chat=123, user=123, text="/start"):  # type: ignore[no-untyped-def]
    return {
        "update_id": 1,
        "message": {"message_id": 1, "chat": {"id": chat}, "from": {"id": user}, "text": text},
    }


async def test_authorized_start(bot_store) -> None:
    api = FakeAPI()
    await BotController(api).handle(update())
    assert "Choose an action" in api.calls[-1][1]["text"]


async def test_unauthorized_start_is_generic(bot_store) -> None:
    api = FakeAPI()
    await BotController(api).handle(update(chat=999, user=999))
    assert api.calls[-1][1]["text"] == "Unauthorized."


def test_negative_group_authorization(bot_store) -> None:
    assert authorized(-10099, 42)
    assert not authorized(-10098, 42)


async def test_search_and_no_results(bot_store, monkeypatch) -> None:
    async def results(_self, query):  # type: ignore[no-untyped-def]
        return [
            {
                "id": "7",
                "title": "The Odyssey",
                "languages": "English",
                "formats": "2D",
                "platform": "PVR INOX",
            }
        ]

    monkeypatch.setattr(PvrInoxAdapter, "interactive_search", results)
    api = FakeAPI()
    await BotController(api).handle(update(text="/search The Odyssey"))
    assert "The Odyssey" in api.calls[-1][1]["text"]

    async def empty(_self, query):  # type: ignore[no-untyped-def]
        return []

    monkeypatch.setattr(PvrInoxAdapter, "interactive_search", empty)
    await BotController(api).handle(update(text="/search Missing"))
    assert "No current PVR catalogue match" in api.calls[-1][1]["text"]
    assert "Create watch" in str(api.calls[-1][1]["rows"])


async def test_newwatch_accepts_manual_unlisted_title(bot_store) -> None:
    api = FakeAPI()
    controller = BotController(api)
    await controller.handle(update(text="/newwatch"))
    assert get_state(123, 123)[0].step == "movie_name"
    await controller.handle(update(text="A Movie That Is Not Listed"))
    state, payload = get_state(123, 123)
    assert state.step == "city"
    assert payload["movie"] == "A Movie That Is Not Listed"


async def test_search_failure_is_sanitized(bot_store, monkeypatch) -> None:
    async def failure(_self, query):  # type: ignore[no-untyped-def]
        raise OSError("temporary")

    monkeypatch.setattr(PvrInoxAdapter, "interactive_search", failure)
    api = FakeAPI()
    await BotController(api).handle(update(text="/search Odyssey"))
    assert "search failed" in api.calls[-1][1]["text"]


async def test_pvr_interactive_search_uses_existing_source(monkeypatch) -> None:
    async def response(_self, endpoint, payload, city):  # type: ignore[no-untyped-def]
        assert endpoint == "search" and city == "Chennai"
        return {
            "result": "success",
            "output": {
                "ns": [{"id": 8, "n": "The Odyssey", "otherlanguages": "English", "fmts": ["2D"]}],
                "cs": [],
            },
        }

    monkeypatch.setattr(PvrInoxAdapter, "_api_post", response)
    result = await PvrInoxAdapter().interactive_search("Odyssey")
    assert result[0]["id"] == "8"


def test_state_isolation_cancel_and_timeout(bot_store) -> None:
    save_state(123, 1, "city", {"movie": "A"})
    save_state(123, 2, "city", {"movie": "B"})
    clear_state(123, 1)
    assert get_state(123, 1)[0] is None
    assert get_state(123, 2)[1]["movie"] == "B"
    with bot_store() as db:
        state = db.scalar(
            select(TelegramConversationState).where(TelegramConversationState.user_id == "2")
        )
        state.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    assert get_state(123, 2)[0] is None


async def test_confirmed_watch_created_once_and_visible(bot_store) -> None:
    payload = {
        "movie_id": "7",
        "movie": "The Odyssey",
        "platform": "PVR INOX",
        "city": "Chennai",
        "date": (date.today() + timedelta(days=2)).isoformat(),
        "language": "English",
        "format": "2D",
        "time_preset": "EVENING",
        "platforms": "pvr",
    }
    api = FakeAPI()
    controller = BotController(api)
    await controller.confirm_prompt(123, 123, payload)
    state, stored = get_state(123, 123)
    assert state.step == CONFIRMATION and stored == payload
    create_data = api.calls[-1][1]["rows"][0][0][1]
    assert create_data == f"watch:create:{state.nonce}"
    assert len(create_data.encode()) <= 64
    await controller.create_watch(123, 123, 5, state.nonce)
    assert get_state(123, 123)[0] is None
    await controller.create_watch(123, 123, 5, state.nonce)
    watches = accessible_watches(123)
    assert len(watches) == 1
    assert watches[0].pvrinox_mode == "AUTOMATIC"
    assert watches[0].bookmyshow_mode == "DISABLED"
    assert any("already been created" in call[1].get("text", "") for call in api.calls)
    with bot_store() as db:
        receipt = db.scalar(select(TelegramWatchCreation))
        assert receipt.status == "CREATED" and receipt.watch_id == watches[0].id


async def test_superseded_and_expired_confirmation_messages(bot_store) -> None:
    payload = {
        "movie": "Movie",
        "city": "Chennai",
        "date": (date.today() + timedelta(days=2)).isoformat(),
        "language": "Tamil",
        "format": "2D",
        "time_preset": "ANY",
        "platforms": "pvr",
    }
    api = FakeAPI()
    controller = BotController(api)
    await controller.confirm_prompt(123, 123, payload)
    old = get_state(123, 123)[0].nonce
    await controller.confirm_prompt(123, 123, payload)
    await controller.create_watch(123, 123, 5, old)
    assert "replaced by a newer" in api.calls[-1][1]["text"]
    current = get_state(123, 123)[0].nonce
    with bot_store() as db:
        receipt = db.scalar(
            select(TelegramWatchCreation).where(TelegramWatchCreation.confirmation_nonce == current)
        )
        receipt.expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)
        db.commit()
    await controller.create_watch(123, 123, 5, current)
    assert "setup expired" in api.calls[-1][1]["text"]


async def test_creation_failure_retains_confirmation_for_retry(bot_store, monkeypatch) -> None:
    payload = {
        "movie": "Retry Movie",
        "city": "Chennai",
        "date": (date.today() + timedelta(days=2)).isoformat(),
        "language": "Tamil",
        "format": "2D",
        "time_preset": "ANY",
        "platforms": "pvr",
    }
    api = FakeAPI()
    controller = BotController(api)
    await controller.confirm_prompt(123, 123, payload)
    nonce = get_state(123, 123)[0].nonce

    def fail_validation(*_args, **_kwargs):  # type: ignore[no-untyped-def]
        raise HTTPException(400, "City is invalid")

    monkeypatch.setattr("app.telegram_bot.apply_form", fail_validation)
    await controller.create_watch(123, 123, 5, nonce)
    state, _ = get_state(123, 123)
    assert state is not None and state.step == CONFIRMATION and state.nonce == nonce
    with bot_store() as db:
        receipt = db.scalar(select(TelegramWatchCreation))
        assert receipt.status == "PENDING" and receipt.watch_id is None


async def test_web_created_watch_visible_and_enable_disable_delete(bot_store) -> None:
    with bot_store() as db:
        watch = Watch(
            movie_name="Web movie",
            city="Chennai",
            show_date=date.today(),
            telegram_chat_id_override="123",
        )
        db.add(watch)
        db.commit()
        watch_id = watch.id
    assert accessible_watches(123)[0].movie_name == "Web movie"
    api = FakeAPI()
    controller = BotController(api)
    await controller.watch_action(123, 123, 1, "disable", watch_id)
    assert not accessible_watches(123)[0].enabled
    await controller.watch_action(123, 123, 1, "enable", watch_id)
    assert accessible_watches(123)[0].enabled
    await controller.watch_action(123, 123, 1, "delete", watch_id)
    state, payload = get_state(123, 123)
    assert state.step == "delete" and payload["watch_id"] == watch_id


def test_offset_and_heartbeat_persist(bot_store) -> None:
    set_runtime("telegram_update_offset", "44")
    set_runtime("telegram_bot_heartbeat", datetime.now(timezone.utc).isoformat())
    with bot_store() as db:
        assert db.get(RuntimeState, "telegram_update_offset").value == "44"
        assert db.get(RuntimeState, "telegram_bot_heartbeat") is not None


def test_stale_initial_update_is_not_processed(bot_store) -> None:
    now = datetime.now(timezone.utc)
    old = update(text="/newwatch")
    old["message"]["date"] = int((now - timedelta(hours=1)).timestamp())
    assert stale_initial_update(old, False, now)
    assert not stale_initial_update(old, True, now)


async def test_status_formats_local_times(bot_store) -> None:
    stamp = datetime.now(timezone.utc).isoformat()
    set_runtime("worker_heartbeat", stamp)
    set_runtime("telegram_bot_heartbeat", stamp)
    set_runtime("last_successful_cycle", stamp)
    api = FakeAPI()
    await BotController(api).status(123)
    text = api.calls[-1][1]["text"]
    assert "+00:00" not in text and stamp not in text
    assert "IST" in text


async def test_bot_api_429_retry_after(bot_store) -> None:
    calls, sleeps = 0, []

    def handler(_request):  # type: ignore[no-untyped-def]
        nonlocal calls
        calls += 1
        if calls == 1:
            return httpx.Response(
                429,
                json={"ok": False, "description": "rate limited", "parameters": {"retry_after": 4}},
            )
        return httpx.Response(200, json={"ok": True, "result": []})

    async def sleep(value):  # type: ignore[no-untyped-def]
        sleeps.append(value)

    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    api = BotAPI(client, sleep)
    assert await api.call("getUpdates", offset=1) == []
    assert calls == 2 and sleeps == [4]
    await client.aclose()


async def test_invalid_callback_is_rejected(bot_store) -> None:
    api = FakeAPI()
    callback = {
        "update_id": 2,
        "callback_query": {
            "id": "q",
            "from": {"id": 123},
            "data": "s:bad:0",
            "message": {"message_id": 5, "chat": {"id": 123}},
        },
    }
    await BotController(api).handle(callback)
    assert any("invalid or expired" in call[1].get("text", "") for call in api.calls)
