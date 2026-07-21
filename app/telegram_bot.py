from __future__ import annotations

import asyncio
import fcntl
import hashlib
import json
import logging
import secrets
import signal
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.exc import OperationalError, SQLAlchemyError

from app.config import get_settings
from app.database import SessionLocal, init_db
from app.main import apply_form
from app.models import (
    DetectedShow,
    PlatformCheck,
    PlatformMode,
    RuntimeState,
    TelegramConversationState,
    TelegramWatchCreation,
    Watch,
)
from app.platforms.pvrinox import PvrInoxAdapter
from app.services.notifications import safe_booking_url, sanitize_telegram_error
from app.services.watcher import run_watch_check
from app.time_presets import TIME_PRESETS, label_for

LOG = logging.getLogger("telegram-bot")
settings = get_settings()
LANGUAGES = ("English", "Tamil", "Hindi", "Telugu", "Malayalam", "Kannada")
FORMATS = ("2D", "3D", "IMAX 2D", "IMAX 3D", "4DX", "Other")
CITIES = ("Chennai", "Bengaluru", "Mumbai", "Delhi NCR", "Hyderabad", "Kochi")
COMMANDS = (
    "start",
    "newwatch",
    "help",
    "search",
    "watches",
    "status",
    "check",
    "enable",
    "disable",
    "delete",
    "cancel",
)
CONFIRMATION = "CONFIRMATION"


def nonce_tag(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()[:8]


def allowed_ids(raw: str) -> set[int]:
    return {int(item.strip()) for item in raw.split(",") if item.strip()}


def authorized(chat_id: int, user_id: int) -> bool:
    if chat_id not in allowed_ids(settings.telegram_allowed_chat_ids):
        return False
    users = allowed_ids(settings.telegram_allowed_user_ids)
    return not users or chat_id > 0 or user_id in users


def stale_initial_update(update: dict, initialized: bool, now: datetime | None = None) -> bool:
    if initialized:
        return False
    message = update.get("message") or (update.get("callback_query") or {}).get("message") or {}
    timestamp = int(message.get("date", 0) or 0)
    now = now or datetime.now(timezone.utc)
    return bool(
        timestamp and now.timestamp() - timestamp > settings.telegram_stale_update_age_seconds
    )


def keyboard(rows: list[list[tuple[str, str]]]) -> dict:
    return {
        "inline_keyboard": [
            [
                {
                    "text": text,
                    **({"url": data} if safe_booking_url(data) else {"callback_data": data}),
                }
                for text, data in row
            ]
            for row in rows
        ]
    }


class BotAPI:
    def __init__(self, client: httpx.AsyncClient | None = None, sleep=asyncio.sleep) -> None:  # type: ignore[no-untyped-def]
        self.client = client or httpx.AsyncClient(timeout=40)
        self.owned = client is None
        self.sleep = sleep
        self.base = f"{settings.telegram_api_base.rstrip('/')}/bot{settings.telegram_bot_token}"

    async def call(self, method: str, **payload):  # type: ignore[no-untyped-def]
        if not settings.telegram_bot_token:
            raise RuntimeError("Telegram bot token missing")
        for attempt in range(3):
            try:
                response = await self.client.post(f"{self.base}/{method}", json=payload)
                data = response.json()
                if response.status_code < 400 and data.get("ok"):
                    return data.get("result")
                retry_after = min(float(data.get("parameters", {}).get("retry_after", 0) or 0), 30)
                temporary = response.status_code in {429, 500, 502, 503, 504}
                error = str(data.get("description") or f"Telegram HTTP {response.status_code}")
            except (httpx.NetworkError, httpx.TimeoutException, OSError, ValueError) as exc:
                temporary, retry_after = True, 0
                error = sanitize_telegram_error(exc, settings.telegram_bot_token)
            if not temporary or attempt == 2:
                raise RuntimeError(sanitize_telegram_error(error, settings.telegram_bot_token))
            await self.sleep(retry_after or 2**attempt)
        raise RuntimeError("Telegram API unavailable")

    async def send(self, chat_id: int, text: str, rows=None):  # type: ignore[no-untyped-def]
        payload = {"chat_id": chat_id, "text": text}
        if rows:
            payload["reply_markup"] = keyboard(rows)
        return await self.call("sendMessage", **payload)

    async def edit(self, chat_id: int, message_id: int, text: str, rows=None):  # type: ignore[no-untyped-def]
        payload = {"chat_id": chat_id, "message_id": message_id, "text": text}
        if rows:
            payload["reply_markup"] = keyboard(rows)
        return await self.call("editMessageText", **payload)


def set_runtime(key: str, value: str) -> None:
    with SessionLocal() as db:
        state = db.get(RuntimeState, key) or RuntimeState(key=key)
        state.value = value
        db.add(state)
        db.commit()


def _expired(value: datetime) -> bool:
    aware = value.replace(tzinfo=timezone.utc) if value.tzinfo is None else value
    return aware <= datetime.now(timezone.utc)


def _watch_form(chat: int, payload: dict) -> dict[str, str | PlatformMode]:
    required = {"movie", "city", "date", "language", "format", "time_preset", "platforms"}
    missing = required - payload.keys()
    if missing:
        raise ValueError(f"missing field: {sorted(missing)[0]}")
    return {
        "movie_name": payload["movie"],
        "city": payload["city"],
        "show_date": payload["date"],
        "language": payload["language"],
        "format": payload["format"],
        "time_preset": payload["time_preset"],
        "start_time": "00:00",
        "end_time": "23:59",
        "preferred_theatres": "",
        "polling_interval_seconds": str(settings.default_poll_interval_seconds),
        "telegram_chat_id_override": str(chat),
        "simulation_state": "OFF",
        "bookmyshow_mode": PlatformMode.AUTOMATIC
        if payload["platforms"] in {"bms", "both"}
        else PlatformMode.DISABLED,
        "pvrinox_mode": PlatformMode.AUTOMATIC
        if payload["platforms"] in {"pvr", "both"}
        else PlatformMode.DISABLED,
        "notifications_enabled": "on",
        "enabled": "on",
    }


def get_state(chat_id: int, user_id: int) -> tuple[TelegramConversationState | None, dict]:
    with SessionLocal() as db:
        state = db.scalar(
            select(TelegramConversationState).where(
                TelegramConversationState.chat_id == str(chat_id),
                TelegramConversationState.user_id == str(user_id),
            )
        )
        if not state:
            return None, {}
        expires = (
            state.expires_at.replace(tzinfo=timezone.utc)
            if state.expires_at.tzinfo is None
            else state.expires_at
        )
        if expires <= datetime.now(timezone.utc):
            db.delete(state)
            db.commit()
            return None, {}
        db.expunge(state)
        return state, json.loads(state.payload)


def save_state(
    chat_id: int,
    user_id: int,
    step: str,
    payload: dict,
    nonce: str | None = None,
    *,
    rotate_nonce: bool = False,
) -> str:
    with SessionLocal() as db:
        state = db.scalar(
            select(TelegramConversationState).where(
                TelegramConversationState.chat_id == str(chat_id),
                TelegramConversationState.user_id == str(user_id),
            )
        ) or TelegramConversationState(chat_id=str(chat_id), user_id=str(user_id))
        state.step, state.payload = step, json.dumps(payload, ensure_ascii=False)
        state.nonce = nonce or ("" if rotate_nonce else state.nonce) or secrets.token_urlsafe(6)
        state.expires_at = datetime.now(timezone.utc) + timedelta(
            seconds=settings.telegram_conversation_timeout_seconds
        )
        db.add(state)
        db.commit()
        return state.nonce


def persist_confirmation(chat_id: int, user_id: int, payload: dict) -> str:
    """Persist final state and receipt before its keyboard can be delivered."""
    nonce = secrets.token_urlsafe(6)
    request_id = secrets.token_urlsafe(12)
    expires_at = datetime.now(timezone.utc) + timedelta(
        seconds=settings.telegram_conversation_timeout_seconds
    )
    with SessionLocal() as db:
        previous = db.scalar(
            select(TelegramWatchCreation).where(
                TelegramWatchCreation.chat_id == str(chat_id),
                TelegramWatchCreation.user_id == str(user_id),
                TelegramWatchCreation.status == "PENDING",
            )
        )
        if previous:
            previous.status = "SUPERSEDED"
        state = db.scalar(
            select(TelegramConversationState).where(
                TelegramConversationState.chat_id == str(chat_id),
                TelegramConversationState.user_id == str(user_id),
            )
        ) or TelegramConversationState(chat_id=str(chat_id), user_id=str(user_id))
        state.step = CONFIRMATION
        state.payload = json.dumps(payload, ensure_ascii=False)
        state.nonce = nonce
        state.expires_at = expires_at
        db.add(state)
        db.add(
            TelegramWatchCreation(
                request_id=request_id,
                chat_id=str(chat_id),
                user_id=str(user_id),
                confirmation_nonce=nonce,
                status="PENDING",
                expires_at=expires_at,
            )
        )
        db.commit()
    return nonce


def supersede_confirmation(chat_id: int, user_id: int, nonce: str) -> None:
    with SessionLocal() as db:
        receipt = db.scalar(
            select(TelegramWatchCreation).where(
                TelegramWatchCreation.chat_id == str(chat_id),
                TelegramWatchCreation.user_id == str(user_id),
                TelegramWatchCreation.confirmation_nonce == nonce,
            )
        )
        if receipt and receipt.status == "PENDING":
            receipt.status = "SUPERSEDED"
            db.commit()


def clear_state(chat_id: int, user_id: int) -> None:
    with SessionLocal() as db:
        state = db.scalar(
            select(TelegramConversationState).where(
                TelegramConversationState.chat_id == str(chat_id),
                TelegramConversationState.user_id == str(user_id),
            )
        )
        if state:
            db.delete(state)
            db.commit()


def accessible_watches(chat_id: int) -> list[Watch]:
    if chat_id not in allowed_ids(settings.telegram_allowed_chat_ids):
        return []
    with SessionLocal() as db:
        watches = db.scalars(select(Watch).order_by(Watch.id)).all()
        for watch in watches:
            db.expunge(watch)
        return list(watches)


def watch_by_id(chat_id: int, watch_id: int) -> Watch | None:
    return next((watch for watch in accessible_watches(chat_id) if watch.id == watch_id), None)


def watch_text(watch: Watch) -> str:
    return (
        f"Watch #{watch.id}\n{watch.movie_name}\n{watch.city} · {watch.show_date}\n"
        f"{watch.language} · {watch.format} · {label_for(watch.time_preset)}\n"
        f"State: {watch.last_status}\nTelegram: {'Enabled' if watch.notifications_enabled else 'Disabled'}\n"
        f"Next check: {watch.next_check_at or 'not scheduled'}"
    )


class BotController:
    def __init__(self, api: BotAPI) -> None:
        self.api = api

    async def handle(self, update: dict) -> None:
        callback = update.get("callback_query")
        message = update.get("message") or (callback or {}).get("message") or {}
        chat_id = int((message.get("chat") or {}).get("id", 0))
        user_id = int(((callback or {}).get("from") or message.get("from") or {}).get("id", 0))
        if callback:
            await self.api.call("answerCallbackQuery", callback_query_id=callback.get("id"))
        if not authorized(chat_id, user_id):
            if chat_id:
                await self.api.send(chat_id, "Unauthorized.")
            return
        if callback:
            await self.callback(
                chat_id, user_id, int(message.get("message_id", 0)), str(callback.get("data") or "")
            )
            return
        text = str(message.get("text") or "").strip()
        if text.startswith("/"):
            command, _, argument = text.partition(" ")
            await self.command(chat_id, user_id, command.split("@")[0].lower(), argument.strip())
        else:
            await self.typed(chat_id, user_id, text)

    async def command(self, chat: int, user: int, command: str, argument: str = "") -> None:
        if command == "/start":
            await self.api.send(
                chat,
                "Movie Ticket Watcher\nChoose an action:",
                [
                    [("Create watch", "cmd:newwatch")],
                    [("Search movies", "cmd:search")],
                    [("My watches", "cmd:watches"), ("System status", "cmd:status")],
                    [("Help", "cmd:help")],
                ],
            )
        elif command == "/help":
            await self.api.send(
                chat,
                "/newwatch — create a persistent watch for any movie\n/search Movie — search the PVR INOX catalogue\n/watches — list watches\n/status — safe service status\n/check /enable /disable /delete — manage watches\n/cancel — cancel a wizard\n\nPVR INOX search is supported. BookMyShow monitoring unavailable from this server because platform protection is active.\nWebhook diagnostics: stop the bot, inspect getWebhookInfo, and explicitly call deleteWebhook only if you choose to switch to long polling.",
            )
        elif command == "/newwatch":
            save_state(chat, user, "movie_name", {})
            await self.api.send(
                chat, "Enter the exact movie title you want to monitor, or /cancel."
            )
        elif command == "/search":
            await self.search(chat, user, argument)
        elif command == "/watches":
            await self.list_watches(chat, "details")
        elif command == "/status":
            await self.status(chat)
        elif command in {"/check", "/enable", "/disable", "/delete"}:
            await self.list_watches(chat, command[1:])
        elif command == "/cancel":
            clear_state(chat, user)
            await self.api.send(chat, "Cancelled.")
        else:
            await self.api.send(chat, "Unknown command. Use /help.")

    async def search(self, chat: int, user: int, query: str) -> None:
        if not query:
            save_state(chat, user, "search_query", {})
            await self.api.send(chat, "Enter a movie name, or /cancel.")
            return
        await self.api.send(chat, "Searching PVR INOX…")
        try:
            results = await PvrInoxAdapter().interactive_search(query)
        except Exception as exc:
            await self.api.send(chat, f"PVR INOX search failed: {sanitize_telegram_error(exc)}")
            return
        if not results:
            nonce = save_state(chat, user, "search_no_results", {"query": query})
            await self.api.send(
                chat,
                f"No current PVR catalogue match found for “{query}”.\nYou can still create a persistent watch.",
                [
                    [(f"Create watch for “{query[:30]}”", f"manual:{nonce}")],
                    [("Search again", "cmd:search"), ("Cancel", f"cancel:{nonce}")],
                ],
            )
            return
        nonce = save_state(chat, user, "search_results", {"results": results})
        lines = [
            f"{i + 1}. {r['title']}\nPVR INOX · ID {r['id']}\n{r['languages'] or 'Languages unavailable'} · {r['formats'] or 'Formats unavailable'}"
            for i, r in enumerate(results)
        ]
        await self.api.send(
            chat,
            "\n\n".join(lines),
            [
                [(f"Create watch — {r['title'][:30]}", f"s:{nonce}:{i}")]
                for i, r in enumerate(results)
            ],
        )

    async def typed(self, chat: int, user: int, text: str) -> None:
        state, payload = get_state(chat, user)
        if not state:
            return
        if state.step == "search_query":
            await self.search(chat, user, text)
        elif state.step == "movie_name":
            title = " ".join(text.split()).strip()
            if not title:
                await self.api.send(chat, "Movie title cannot be empty.")
                return
            if payload.get("platforms"):
                payload["movie"] = title
                await self.confirm_prompt(chat, user, payload)
            else:
                await self.city_prompt(chat, user, {"movie": title, "platform": "Manual"})
        elif state.step == "city":
            payload["city"] = " ".join(word.capitalize() for word in text.strip().split())
            await self.date_prompt(chat, user, payload)
        elif state.step == "date_custom":
            try:
                chosen = date.fromisoformat(text)
            except ValueError:
                await self.api.send(chat, "Enter a date as YYYY-MM-DD.")
                return
            if chosen < datetime.now(ZoneInfo(settings.app_timezone)).date():
                await self.api.send(chat, "Past dates are not allowed.")
                return
            payload["date"] = chosen.isoformat()
            await self.language_prompt(chat, user, payload)
        elif state.step == "language_custom":
            language = " ".join(text.split()).strip()
            if not language:
                await self.api.send(chat, "Language cannot be empty.")
                return
            payload["language"] = language
            await self.format_prompt(chat, user, payload)

    async def callback(self, chat: int, user: int, message_id: int, data: str) -> None:
        if data.startswith("cmd:"):
            await self.command(chat, user, "/" + data[4:])
            return
        parts = data.split(":")
        if len(parts) == 3 and parts[:2] == ["watch", "create"]:
            await self.create_watch(chat, user, message_id, parts[2])
            return
        state, payload = get_state(chat, user)
        if parts[0] == "w" and len(parts) == 3:
            await self.watch_action(chat, user, message_id, parts[1], int(parts[2]))
            return
        if not state or len(parts) < 2 or parts[1] != state.nonce:
            await self.api.send(chat, "This action is invalid or expired.")
            return
        try:
            if parts[0] == "s" and state.step == "search_results":
                movie = payload["results"][int(parts[2])]
                payload = {"movie_id": movie["id"], "movie": movie["title"], "platform": "PVR INOX"}
                await self.city_prompt(chat, user, payload)
            elif parts[0] == "manual" and state.step == "search_no_results":
                await self.city_prompt(
                    chat, user, {"movie": payload["query"], "platform": "Manual"}
                )
            elif parts[0] == "c" and state.step == "city":
                payload["city"] = CITIES[int(parts[2])]
                await self.date_prompt(chat, user, payload)
            elif parts[0] == "d" and state.step == "date":
                if parts[2] == "x":
                    save_state(chat, user, "date_custom", payload, state.nonce)
                    await self.api.send(chat, "Enter YYYY-MM-DD.")
                else:
                    payload["date"] = parts[2]
                    await self.language_prompt(chat, user, payload)
            elif parts[0] == "l" and state.step == "language":
                if parts[2] == "x":
                    save_state(chat, user, "language_custom", payload, state.nonce)
                    await self.api.send(chat, "Enter the language.")
                else:
                    payload["language"] = LANGUAGES[int(parts[2])]
                    await self.format_prompt(chat, user, payload)
            elif parts[0] == "f" and state.step == "format":
                payload["format"] = FORMATS[int(parts[2])]
                await self.time_prompt(chat, user, payload)
            elif parts[0] == "t" and state.step == "time":
                payload["time_preset"] = parts[2]
                await self.platform_prompt(chat, user, payload)
            elif parts[0] == "p" and state.step == "platforms":
                payload["platforms"] = parts[2]
                await self.confirm_prompt(chat, user, payload)
            elif parts[0] == "edit" and state.step == CONFIRMATION:
                supersede_confirmation(chat, user, state.nonce)
                target = parts[2]
                if target == "movie":
                    save_state(chat, user, "movie_name", payload, rotate_nonce=True)
                    await self.api.send(chat, "Enter the corrected movie title.")
                elif target == "city":
                    save_state(chat, user, "city", payload, rotate_nonce=True)
                    await self.city_prompt(chat, user, payload)
                elif target == "date":
                    save_state(chat, user, "date", payload, rotate_nonce=True)
                    await self.date_prompt(chat, user, payload)
                elif target == "language":
                    save_state(chat, user, "language", payload, rotate_nonce=True)
                    await self.language_prompt(chat, user, payload)
                elif target == "format":
                    save_state(chat, user, "format", payload, rotate_nonce=True)
                    await self.format_prompt(chat, user, payload)
                elif target == "time":
                    save_state(chat, user, "time", payload, rotate_nonce=True)
                    await self.time_prompt(chat, user, payload)
                elif target == "platforms":
                    save_state(chat, user, "platforms", payload, rotate_nonce=True)
                    await self.platform_prompt(chat, user, payload)
            elif parts[0] == "restart":
                clear_state(chat, user)
                await self.search(chat, user, "")
            elif parts[0] == "cancel":
                clear_state(chat, user)
                await self.api.edit(chat, message_id, "Cancelled.")
            elif (
                parts[0] == "del"
                and state.step == "delete"
                and payload.get("watch_id") == int(parts[2])
            ):
                clear_state(chat, user)
                with SessionLocal() as db:
                    watch = db.get(Watch, int(parts[2]))
                    if watch:
                        db.delete(watch)
                        db.commit()
                await self.api.edit(chat, message_id, "Watch deleted.")
            elif (
                parts[0] == "dis"
                and state.step == "disable"
                and payload.get("watch_id") == int(parts[2])
            ):
                clear_state(chat, user)
                with SessionLocal() as db:
                    watch = db.get(Watch, int(parts[2]))
                    if watch:
                        watch.enabled = False
                        watch.last_status = "DISABLED"
                        db.commit()
                await self.api.edit(chat, message_id, "Watch disabled.")
            else:
                await self.api.send(chat, "This action is invalid or expired.")
        except (IndexError, KeyError, ValueError):
            await self.api.send(chat, "This action is invalid or expired.")

    async def city_prompt(self, chat: int, user: int, payload: dict) -> None:
        nonce = save_state(chat, user, "city", payload)
        await self.api.send(
            chat,
            "Choose a city or type another city:",
            [
                [(name, f"c:{nonce}:{i}") for i, name in enumerate(CITIES[:4])],
                [(name, f"c:{nonce}:{i}") for i, name in enumerate(CITIES[4:], 4)],
            ],
        )

    async def date_prompt(self, chat: int, user: int, payload: dict) -> None:
        nonce = save_state(chat, user, "date", payload)
        today = datetime.now(ZoneInfo(settings.app_timezone)).date()
        rows = [
            [
                (
                    (today + timedelta(days=i)).strftime("%a %d %b"),
                    f"d:{nonce}:{today + timedelta(days=i)}",
                )
            ]
            for i in range(5)
        ]
        rows.append([("Enter another date", f"d:{nonce}:x"), ("Cancel", f"cancel:{nonce}")])
        await self.api.send(chat, "Choose the show date:", rows)

    async def language_prompt(self, chat: int, user: int, payload: dict) -> None:
        nonce = save_state(chat, user, "language", payload)
        await self.api.send(
            chat,
            "Choose language:",
            [
                [(v, f"l:{nonce}:{i}") for i, v in enumerate(LANGUAGES[:3])],
                [(v, f"l:{nonce}:{i}") for i, v in enumerate(LANGUAGES[3:], 3)],
                [("Other", f"l:{nonce}:x")],
            ],
        )

    async def format_prompt(self, chat: int, user: int, payload: dict) -> None:
        nonce = save_state(chat, user, "format", payload)
        await self.api.send(
            chat,
            "Choose format:",
            [
                [(v, f"f:{nonce}:{i}") for i, v in enumerate(FORMATS[:3])],
                [(v, f"f:{nonce}:{i}") for i, v in enumerate(FORMATS[3:], 3)],
            ],
        )

    async def time_prompt(self, chat: int, user: int, payload: dict) -> None:
        nonce = save_state(chat, user, "time", payload)
        await self.api.send(
            chat,
            "Choose time preference:",
            [[(label, f"t:{nonce}:{key}")] for key, (label, _start, _end) in TIME_PRESETS.items()],
        )

    async def platform_prompt(self, chat: int, user: int, payload: dict) -> None:
        nonce = save_state(chat, user, "platforms", payload)
        await self.api.send(
            chat,
            "Choose platforms. PVR INOX only is recommended. BookMyShow may remain blocked by Cloudflare on this server.",
            [
                [("PVR INOX only", f"p:{nonce}:pvr")],
                [("PVR INOX and BookMyShow", f"p:{nonce}:both")],
                [("BookMyShow only", f"p:{nonce}:bms")],
            ],
        )

    async def confirm_prompt(self, chat: int, user: int, payload: dict) -> None:
        nonce = persist_confirmation(chat, user, payload)
        platforms = payload["platforms"]
        display_date = date.fromisoformat(payload["date"]).strftime("%d %B %Y").lstrip("0")
        text = (
            f"Confirm watch\nMovie: {payload['movie']}\nCity: {payload['city']}\n"
            f"Date: {display_date}\nLanguage: {payload['language']}\nFormat: {payload['format']}\n"
            f"Time: {label_for(payload['time_preset'])}\n"
            f"PVR INOX: {'Enabled' if platforms in {'pvr', 'both'} else 'Disabled'}\n"
            f"BookMyShow: {'Enabled' if platforms in {'bms', 'both'} else 'Disabled'}\n"
            f"Telegram notifications: Enabled\nPolling interval: {settings.default_poll_interval_seconds} seconds"
        )
        await self.api.send(
            chat,
            text,
            [
                [("Create watch", f"watch:create:{nonce}")],
                [("Edit movie", f"edit:{nonce}:movie"), ("Edit city", f"edit:{nonce}:city")],
                [("Edit date", f"edit:{nonce}:date"), ("Edit language", f"edit:{nonce}:language")],
                [("Edit format", f"edit:{nonce}:format"), ("Edit time", f"edit:{nonce}:time")],
                [("Edit platforms", f"edit:{nonce}:platforms"), ("Cancel", f"cancel:{nonce}")],
            ],
        )

    async def create_watch(self, chat: int, user: int, message_id: int, nonce: str) -> None:
        tag = nonce_tag(nonce)
        for attempt in range(3):
            try:
                with SessionLocal() as db:
                    receipt = db.scalar(
                        select(TelegramWatchCreation).where(
                            TelegramWatchCreation.chat_id == str(chat),
                            TelegramWatchCreation.user_id == str(user),
                            TelegramWatchCreation.confirmation_nonce == nonce,
                        )
                    )
                    state = db.scalar(
                        select(TelegramConversationState).where(
                            TelegramConversationState.chat_id == str(chat),
                            TelegramConversationState.user_id == str(user),
                        )
                    )
                    LOG.info(
                        "confirmation action=create nonce=%s chat=%s user=%s state_found=%s step=%s",
                        tag,
                        chat,
                        user,
                        bool(state),
                        state.step if state else "missing",
                    )
                    if receipt and receipt.status == "CREATED":
                        await self.api.edit(
                            chat,
                            message_id,
                            f"This watch has already been created.\nWatch ID: {receipt.watch_id}",
                            [[("My watches", "cmd:watches")]],
                        )
                        return
                    if receipt and receipt.status == "SUPERSEDED":
                        await self.api.send(chat, "This confirmation was replaced by a newer one.")
                        return
                    if receipt and _expired(receipt.expires_at):
                        await self.api.send(
                            chat, "This watch setup expired. Use /newwatch to start again."
                        )
                        return
                    if not receipt or not state:
                        await self.api.send(
                            chat,
                            "I could not recover this watch setup. Use /newwatch to start again.",
                        )
                        return
                    if _expired(state.expires_at):
                        await self.api.send(
                            chat, "This watch setup expired. Use /newwatch to start again."
                        )
                        return
                    if state.nonce != nonce:
                        await self.api.send(chat, "This confirmation was replaced by a newer one.")
                        return
                    if state.step != CONFIRMATION:
                        await self.api.send(chat, "This confirmation was replaced by a newer one.")
                        return
                    payload = json.loads(state.payload)
                    form = _watch_form(chat, payload)
                    watch = Watch(movie_name="", city="", show_date=date.today())
                    apply_form(watch, form, settings.min_poll_interval_seconds)
                    watch.next_check_at = datetime.now(timezone.utc)
                    db.add(watch)
                    db.flush()
                    watch_id = watch.id
                    receipt.status = "CREATED"
                    receipt.watch_id = watch_id
                    db.delete(state)
                    db.commit()
                LOG.info(
                    "confirmation committed action=create nonce=%s chat=%s user=%s request=%s watch=%s",
                    tag,
                    chat,
                    user,
                    receipt.request_id,
                    watch_id,
                )
                await self.api.edit(
                    chat,
                    message_id,
                    f"Watch created successfully\nWatch ID: {watch_id}\nNext scheduled check: now\nTelegram notifications enabled",
                    [
                        [
                            ("Run check now", f"w:check:{watch_id}"),
                            ("View watch", f"w:details:{watch_id}"),
                        ],
                        [("My watches", "cmd:watches"), ("Create another watch", "cmd:newwatch")],
                    ],
                )
                return
            except OperationalError as exc:
                if "locked" not in str(exc).lower() or attempt == 2:
                    LOG.warning(
                        "confirmation database failure nonce=%s chat=%s user=%s", tag, chat, user
                    )
                    await self.api.send(
                        chat, "I could not save this watch yet. Please try Create watch again."
                    )
                    return
                await asyncio.sleep(0.1 * (attempt + 1))
            except (KeyError, TypeError, ValueError) as exc:
                LOG.info(
                    "confirmation validation failure nonce=%s chat=%s user=%s", tag, chat, user
                )
                await self.api.send(
                    chat,
                    f"Watch validation failed: {type(exc).__name__}. Please edit the affected field.",
                )
                return
            except HTTPException as exc:
                LOG.info(
                    "confirmation validation failure nonce=%s chat=%s user=%s", tag, chat, user
                )
                await self.api.send(chat, f"Watch validation failed: {exc.detail}")
                return
            except SQLAlchemyError:
                LOG.warning(
                    "confirmation database failure nonce=%s chat=%s user=%s", tag, chat, user
                )
                await self.api.send(
                    chat, "I could not save this watch yet. Please try Create watch again."
                )
                return

    async def list_watches(self, chat: int, action: str) -> None:
        watches = accessible_watches(chat)
        if not watches:
            await self.api.send(chat, "No accessible watches.")
            return
        for watch in watches[:10]:
            label = "Disable" if watch.enabled else "Enable"
            rows = [
                [("Details", f"w:details:{watch.id}"), ("Run check", f"w:check:{watch.id}")],
                [
                    (label, f"w:{'disable' if watch.enabled else 'enable'}:{watch.id}"),
                    ("Delete", f"w:delete:{watch.id}"),
                ],
            ]
            if action != "details":
                rows = [[(action.title(), f"w:{action}:{watch.id}")]]
            await self.api.send(chat, watch_text(watch), rows)

    async def watch_action(
        self, chat: int, user: int, message_id: int, action: str, watch_id: int
    ) -> None:
        watch = watch_by_id(chat, watch_id)
        if not watch:
            await self.api.send(chat, "Watch not found.")
            return
        if action == "details":
            await self.api.edit(chat, message_id, watch_text(watch))
            return
        if action == "delete":
            nonce = save_state(chat, user, "delete", {"watch_id": watch_id})
            await self.api.edit(
                chat,
                message_id,
                f"Delete this watch?\n{watch_text(watch)}",
                [[("Delete", f"del:{nonce}:{watch_id}"), ("Cancel", f"cancel:{nonce}")]],
            )
            return
        if action in {"enable", "disable"}:
            if action == "disable" and watch.last_status == "AVAILABLE":
                nonce = save_state(chat, user, "disable", {"watch_id": watch_id})
                await self.api.edit(
                    chat,
                    message_id,
                    "This watch is currently available. Disable it?",
                    [[("Disable", f"dis:{nonce}:{watch_id}"), ("Cancel", f"cancel:{nonce}")]],
                )
                return
            with SessionLocal() as db:
                target = db.get(Watch, watch_id)
                target.enabled = action == "enable"
                target.last_status = "WAITING" if target.enabled else "DISABLED"
                db.commit()
            await self.api.edit(chat, message_id, f"Watch #{watch_id} {action}d.")
            return
        if action == "check":
            await self.api.edit(chat, message_id, f"Running check for watch #{watch_id}…")
            with SessionLocal() as db:
                target = db.get(Watch, watch_id)
                await run_watch_check(db, target)
                checks = db.scalars(
                    select(PlatformCheck)
                    .where(PlatformCheck.watch_id == watch_id)
                    .order_by(PlatformCheck.id.desc())
                    .limit(2)
                ).all()
                shows = db.scalars(
                    select(DetectedShow)
                    .where(DetectedShow.watch_id == watch_id)
                    .order_by(DetectedShow.last_seen_at.desc())
                    .limit(5)
                ).all()
                text = (
                    f"Check complete\nState: {target.last_status}\nMatches: {target.matching_show_count}\n"
                    + "\n".join(f"{c.platform}: {c.status}" for c in checks)
                )
                if not shows and target.last_status in {
                    "WAITING",
                    "DISCOVERY_NO_RESULTS",
                    "CHECK_FAILED",
                }:
                    text += "\n\nNo matching movie or sessions found yet. I will keep checking automatically."
                rows = [
                    [("Open booking page", url) for url in [safe_booking_url(s.booking_url)] if url]
                    for s in shows
                ]
            await self.api.edit(chat, message_id, text, rows)

    async def status(self, chat: int) -> None:
        def display(value: str | None) -> str:
            if not value:
                return "missing"
            stamp = datetime.fromisoformat(value)
            if stamp.tzinfo is None:
                stamp = stamp.replace(tzinfo=timezone.utc)
            local = stamp.astimezone(ZoneInfo(settings.app_timezone))
            today = datetime.now(ZoneInfo(settings.app_timezone)).date()
            return local.strftime(
                "%-I:%M %p %Z" if local.date() == today else "%d %b %Y, %-I:%M %p %Z"
            )

        with SessionLocal() as db:
            worker = db.get(RuntimeState, "worker_heartbeat")
            bot = db.get(RuntimeState, "telegram_bot_heartbeat")
            cycle = db.get(RuntimeState, "last_successful_cycle")
            recent = db.scalars(
                select(PlatformCheck).order_by(PlatformCheck.id.desc()).limit(20)
            ).all()
        pvr = next((x.status for x in recent if x.platform == "PVR INOX"), "Not checked")
        bms = next((x.status for x in recent if x.platform == "BookMyShow"), "Not checked")
        await self.api.send(
            chat,
            f"System status\nWeb: healthy\nWorker heartbeat: {display(worker.value if worker else None)}\nBot heartbeat: {display(bot.value if bot else None)}\nLast monitoring cycle: {display(cycle.value if cycle else None)}\nTimezone: {settings.app_timezone}\nTelegram: configured\nPVR INOX: {pvr}\nBookMyShow: {bms}",
        )


async def poll(stop: asyncio.Event) -> None:
    api, controller = BotAPI(), None
    controller = BotController(api)
    try:
        webhook = await api.call("getWebhookInfo")
        if webhook.get("url"):
            set_runtime("telegram_bot_status", "webhook_conflict")
            LOG.error("Webhook conflict: remove it explicitly before using long polling")
            while not stop.is_set():
                set_runtime("telegram_bot_heartbeat", datetime.now(timezone.utc).isoformat())
                await asyncio.sleep(10)
            return
        set_runtime("telegram_bot_status", "long_polling")
        try:
            await api.call(
                "setMyCommands",
                commands=[
                    {"command": c, "description": c.replace("_", " ").title()} for c in COMMANDS
                ],
            )
        except Exception as exc:
            LOG.warning(
                "Could not set command menu: %s",
                sanitize_telegram_error(exc, settings.telegram_bot_token),
            )
        failures = 0
        while not stop.is_set():
            set_runtime("telegram_bot_heartbeat", datetime.now(timezone.utc).isoformat())
            with SessionLocal() as db:
                offset = int(
                    (
                        db.get(RuntimeState, "telegram_update_offset") or RuntimeState(value="0")
                    ).value
                    or 0
                )
                initialized = db.get(RuntimeState, "telegram_update_initialized") is not None
            try:
                updates = await api.call(
                    "getUpdates",
                    offset=offset,
                    timeout=settings.telegram_long_poll_timeout_seconds,
                    allowed_updates=["message", "callback_query"],
                )
                failures = 0
                for update in updates:
                    update_id = int(update.get("update_id", -1))
                    if update_id < offset:
                        continue
                    set_runtime("telegram_update_offset", str(update_id + 1))
                    if stale_initial_update(update, initialized):
                        continue
                    await controller.handle(update)
                if not initialized:
                    set_runtime("telegram_update_initialized", "true")
            except Exception as exc:
                failures += 1
                LOG.warning(
                    "Long polling failed: %s",
                    sanitize_telegram_error(exc, settings.telegram_bot_token),
                )
                await asyncio.sleep(min(2**failures, 30))
    finally:
        set_runtime("telegram_bot_status", "stopped")
        if api.owned:
            await api.client.aclose()


async def main() -> None:
    logging.basicConfig(
        level=settings.log_level, format="%(asctime)s %(levelname)s %(name)s %(message)s"
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    with (Path(settings.data_dir) / "telegram-bot.lock").open("w") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another Telegram bot process holds the lock") from None
        init_db()
        from app.cli import pending

        pending(clean=True)
        stop = asyncio.Event()
        loop = asyncio.get_running_loop()
        for signum in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(signum, stop.set)
        if not settings.telegram_bot_token or not settings.telegram_allowed_chat_ids:
            while not stop.is_set():
                set_runtime("telegram_bot_status", "configuration_incomplete")
                set_runtime("telegram_bot_heartbeat", datetime.now(timezone.utc).isoformat())
                try:
                    await asyncio.wait_for(stop.wait(), timeout=10)
                except TimeoutError:
                    pass
            return
        await poll(stop)


if __name__ == "__main__":
    asyncio.run(main())
