from datetime import date, datetime, time
from zoneinfo import ZoneInfo

from fastapi import Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from itsdangerous import BadSignature, URLSafeSerializer
from sqlalchemy import select, text
from sqlalchemy.orm import Session
from starlette.middleware.sessions import SessionMiddleware

from app.config import get_settings
from app.database import get_db, init_db
from app.models import (
    DetectedShow,
    NotificationHistory,
    PlatformCheck,
    PlatformMode,
    PlatformRetryState,
    RuntimeState,
    Watch,
)
from app.platforms.urls import BOOKMYSHOW_URLS, PVRINOX_URLS, sanitize_url
from app.services.notifications import (
    TelegramProvider,
    effective_chat_id,
    sanitize_telegram_error,
    telegram_configured,
    validate_chat_id,
)
from app.services.watcher import (
    aggregate_watch_status,
    enabled_platforms,
    retry_state,
    run_watch_check,
)
from app.time_presets import TIME_PRESETS, canonical_preset, is_standard, label_for, range_for

settings = get_settings()
app = FastAPI(title="Movie Ticket Watcher")
app.add_middleware(
    SessionMiddleware, secret_key=settings.secret_key, same_site="lax", https_only=False
)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
serializer = URLSafeSerializer(settings.secret_key, salt="csrf")


def local_datetime(value: datetime | None) -> str:
    if not value:
        return "Never"
    if value.tzinfo is None:
        value = value.replace(tzinfo=ZoneInfo("UTC"))
    return value.astimezone(ZoneInfo(settings.app_timezone)).strftime("%Y-%m-%d %H:%M:%S %Z")


templates.env.filters["localdt"] = local_datetime
templates.env.filters["basename"] = lambda value: __import__("pathlib").Path(value).name
templates.env.filters["preset_label"] = label_for


def hhmm(value: time) -> str:
    return value.strftime("%H:%M")


templates.env.filters["hhmm"] = hhmm
templates.env.filters["safeurl"] = sanitize_url

STATUS_LABELS = {
    "AVAILABLE": "Matching show available",
    "UNAVAILABLE": "No qualifying show",
    "PAGE_LOADED_NO_SHOWS": "Booking not open / no shows",
    "DISCOVERY_NO_RESULTS": "Discovery found no movie",
    "PARSE_UNSUPPORTED": "Page structure unsupported",
    "BLOCKED": "Blocked",
    "CONFIGURATION_REQUIRED": "Configuration required",
    "DISCOVERY_FAILED": "Discovery failed",
    "ERROR": "Check failed",
    "DISABLED": "Disabled",
}
templates.env.filters["status_label"] = lambda value: STATUS_LABELS.get(
    str(value), str(value).replace("_", " ").title()
)


@app.on_event("startup")
def startup() -> None:
    init_db()


def csrf_for(request: Request) -> str:
    nonce = request.session.setdefault("csrf_nonce", __import__("secrets").token_urlsafe(24))
    return serializer.dumps(nonce)


def verify_csrf(request: Request, csrf_token: str) -> None:
    try:
        valid = serializer.loads(csrf_token) == request.session.get("csrf_nonce")
    except BadSignature:
        valid = False
    if not valid:
        raise HTTPException(403, "Invalid CSRF token")


def context(request: Request, **values):  # type: ignore[no-untyped-def]
    return {
        "request": request,
        "csrf_token": csrf_for(request),
        "settings": settings,
        "time_presets": TIME_PRESETS,
        **values,
    }


@app.get("/login", response_class=HTMLResponse)
def login_page() -> RedirectResponse:
    return RedirectResponse("/", 303)


@app.post("/logout")
def logout(request: Request, csrf_token: str = Form()):  # type: ignore[no-untyped-def]
    verify_csrf(request, csrf_token)
    request.session.clear()
    return RedirectResponse("/", 303)


@app.get("/", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    watches = db.scalars(select(Watch).order_by(Watch.created_at.desc())).all()
    platform_statuses: dict[int, dict[str, PlatformCheck | None]] = {}
    retry_states: dict[int, dict[str, PlatformRetryState]] = {}
    telegram_ready: dict[int, bool] = {}
    for watch in watches:
        platform_statuses[watch.id] = {}
        retry_states[watch.id] = {}
        try:
            effective_chat_id(watch, settings)
            telegram_ready[watch.id] = bool(settings.telegram_bot_token)
        except ValueError:
            telegram_ready[watch.id] = False
        for platform in enabled_platforms(watch):
            platform_statuses[watch.id][platform] = db.scalar(
                select(PlatformCheck)
                .where(PlatformCheck.watch_id == watch.id, PlatformCheck.platform == platform)
                .order_by(PlatformCheck.id.desc())
            )
            retry_states[watch.id][platform] = retry_state(db, watch, platform)
        watch.last_status = aggregate_watch_status(
            [state.last_status for state in retry_states[watch.id].values()],
            watch_enabled=watch.enabled,
        )
    db.commit()
    return templates.TemplateResponse(
        "dashboard.html",
        context(
            request,
            watches=watches,
            platform_statuses=platform_statuses,
            retry_states=retry_states,
            telegram_ready=telegram_ready,
            system_status=system_status(db),
        ),
    )


def _directory_size(path) -> int:  # type: ignore[no-untyped-def]
    try:
        return sum(item.stat().st_size for item in path.rglob("*") if item.is_file())
    except OSError:
        return 0


def system_status(db: Session) -> dict:
    heartbeat = db.get(RuntimeState, "worker_heartbeat")
    last_cycle = db.get(RuntimeState, "last_successful_cycle")
    bot_heartbeat = db.get(RuntimeState, "telegram_bot_heartbeat")
    bot_runtime = db.get(RuntimeState, "telegram_bot_status")
    database_path = settings.database_url.removeprefix("sqlite:///")
    database_file = __import__("pathlib").Path(database_path)
    recent = db.scalars(select(PlatformCheck).order_by(PlatformCheck.id.desc()).limit(50)).all()
    adapters = {
        name: next((check.status for check in recent if check.platform == name), "Not checked")
        for name in ("BookMyShow", "PVR INOX")
    }
    last_delivery = db.scalar(
        select(NotificationHistory)
        .where(NotificationHistory.provider == "telegram", NotificationHistory.is_test.is_(False))
        .order_by(NotificationHistory.id.desc())
    )
    return {
        "version": settings.app_version,
        "worker_heartbeat": datetime.fromisoformat(heartbeat.value) if heartbeat else None,
        "last_cycle": datetime.fromisoformat(last_cycle.value) if last_cycle else None,
        "telegram_bot_heartbeat": datetime.fromisoformat(bot_heartbeat.value)
        if bot_heartbeat
        else None,
        "telegram_bot_status": bot_runtime.value if bot_runtime else "configuration_incomplete",
        "database_path": database_path,
        "database_size": database_file.stat().st_size if database_file.exists() else 0,
        "screenshot_size": _directory_size(settings.screenshot_dir),
        "timezone": settings.app_timezone,
        "container": settings.container_deployment,
        "adapters": adapters,
        "telegram_configured": telegram_configured(settings),
        "last_telegram_delivery": last_delivery,
    }


@app.get("/watches/new", response_class=HTMLResponse)
def new_watch(request: Request):
    return templates.TemplateResponse(
        "watch_form.html", context(request, watch=None, today=date.today())
    )


def apply_form(watch: Watch, form, minimum: int) -> None:  # type: ignore[no-untyped-def]
    watch.movie_name = form["movie_name"].strip()
    watch.city = form["city"].strip()
    watch.show_date = date.fromisoformat(form["show_date"])
    watch.language = form["language"].strip()
    watch.format = form["format"].strip()
    watch.time_preset = canonical_preset(form.get("time_preset"))
    if is_standard(watch.time_preset):
        watch.start_time, watch.end_time = range_for(watch.time_preset)
    else:
        watch.start_time = time.fromisoformat(form["start_time"])
        watch.end_time = time.fromisoformat(form["end_time"])
        if watch.end_time < watch.start_time:
            raise HTTPException(422, "Custom time ranges cannot cross midnight")
    watch.preferred_theatres = form.get("preferred_theatres", "").strip()
    bookmyshow_mode = form.get("bookmyshow_mode")
    pvrinox_mode = form.get("pvrinox_mode")
    if bookmyshow_mode is None:
        bookmyshow_mode = "AUTOMATIC" if "bookmyshow_enabled" in form else "DISABLED"
    if pvrinox_mode is None:
        pvrinox_mode = "AUTOMATIC" if "pvrinox_enabled" in form else "DISABLED"
    valid_modes = {mode.value for mode in PlatformMode}
    watch.bookmyshow_mode = str(bookmyshow_mode).upper()
    watch.pvrinox_mode = str(pvrinox_mode).upper()
    if watch.bookmyshow_mode not in valid_modes or watch.pvrinox_mode not in valid_modes:
        raise HTTPException(422, "Invalid platform mode")
    watch.bookmyshow_enabled = watch.bookmyshow_mode != PlatformMode.DISABLED
    watch.pvrinox_enabled = watch.pvrinox_mode != PlatformMode.DISABLED
    watch.bookmyshow_direct_url = form.get(
        "bookmyshow_direct_url", form.get("bookmyshow_url", "")
    ).strip()
    watch.pvrinox_direct_url = form.get("pvrinox_direct_url", form.get("pvrinox_url", "")).strip()
    # Retain the legacy columns for backward-compatible exports and older tooling.
    watch.bookmyshow_url = watch.bookmyshow_direct_url
    watch.pvrinox_url = watch.pvrinox_direct_url
    if watch.bookmyshow_mode == PlatformMode.DIRECT and not watch.bookmyshow_direct_url:
        raise HTTPException(422, "BookMyShow direct URL is required in Direct URL mode")
    if watch.pvrinox_mode == PlatformMode.DIRECT and not watch.pvrinox_direct_url:
        raise HTTPException(422, "PVR INOX direct URL is required in Direct URL mode")
    if watch.bookmyshow_direct_url and not watch.bookmyshow_direct_url.startswith("fixture://"):
        try:
            BOOKMYSHOW_URLS.validate(watch.bookmyshow_direct_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    if watch.pvrinox_direct_url and not watch.pvrinox_direct_url.startswith("fixture://"):
        try:
            PVRINOX_URLS.validate(watch.pvrinox_direct_url)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    watch.bookmyshow_discovered_url = ""
    watch.pvrinox_discovered_url = ""
    watch.polling_interval_seconds = max(int(form["polling_interval_seconds"]), minimum)
    chat_override = form.get("telegram_chat_id_override", "").strip()
    if chat_override:
        validate_chat_id(chat_override)
    watch.telegram_chat_id_override = chat_override
    watch.notifications_enabled = "notifications_enabled" in form
    if watch.notifications_enabled:
        try:
            effective_chat_id(watch, settings)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
    watch.enabled = "enabled" in form
    watch.simulation_state = form.get("simulation_state", "OFF")


@app.post("/watches")
async def create_watch(request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)):
    verify_csrf(request, csrf_token)
    form = await request.form()
    watch = Watch(movie_name="", city="", show_date=date.today())
    apply_form(watch, form, settings.min_poll_interval_seconds)
    db.add(watch)
    db.commit()
    return RedirectResponse("/", 303)


@app.get("/watches/{watch_id}/edit", response_class=HTMLResponse)
def edit_watch(watch_id: int, request: Request, db: Session = Depends(get_db)):
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    return templates.TemplateResponse(
        "watch_form.html", context(request, watch=watch, today=date.today())
    )


@app.post("/watches/{watch_id}")
async def update_watch(
    watch_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)
):
    verify_csrf(request, csrf_token)
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    apply_form(watch, await request.form(), settings.min_poll_interval_seconds)
    watch.next_check_at = None
    db.commit()
    return RedirectResponse("/", 303)


@app.post("/watches/{watch_id}/toggle")
def toggle(
    watch_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)
):
    verify_csrf(request, csrf_token)
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    watch.enabled = not watch.enabled
    watch.last_status = "WAITING" if watch.enabled else "DISABLED"
    db.commit()
    return RedirectResponse("/", 303)


@app.post("/watches/{watch_id}/delete")
def delete(
    watch_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)
):
    verify_csrf(request, csrf_token)
    watch = db.get(Watch, watch_id)
    if watch:
        db.delete(watch)
        db.commit()
    return RedirectResponse("/", 303)


@app.post("/watches/{watch_id}/run")
async def run_now(
    watch_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)
):
    verify_csrf(request, csrf_token)
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    await run_watch_check(db, watch)
    return RedirectResponse(f"/watches/{watch_id}/result", 303)


@app.get("/watches/{watch_id}/result", response_class=HTMLResponse)
def result(watch_id: int, request: Request, db: Session = Depends(get_db)):
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    shows = db.scalars(
        select(DetectedShow)
        .where(DetectedShow.watch_id == watch_id)
        .order_by(DetectedShow.last_seen_at.desc())
    ).all()
    checks = db.scalars(
        select(PlatformCheck)
        .where(PlatformCheck.watch_id == watch_id)
        .order_by(PlatformCheck.id.desc())
        .limit(20)
    ).all()
    latest_checks = {
        platform: next((check for check in checks if check.platform == platform), None)
        for platform in enabled_platforms(watch)
    }
    retry_states = {
        platform: retry_state(db, watch, platform) for platform in enabled_platforms(watch)
    }
    db.commit()
    return templates.TemplateResponse(
        "result.html",
        context(
            request,
            watch=watch,
            shows=shows,
            checks=checks,
            latest_checks=latest_checks,
            retry_states=retry_states,
        ),
    )


PLATFORM_SLUGS = {"bookmyshow": "BookMyShow", "pvrinox": "PVR INOX"}


def platform_name(slug: str) -> str:
    if slug not in PLATFORM_SLUGS:
        raise HTTPException(404, "Unknown platform")
    return PLATFORM_SLUGS[slug]


@app.post("/watches/{watch_id}/platform/{slug}/retry")
async def retry_platform(
    watch_id: int,
    slug: str,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    await run_watch_check(db, watch, only_platform=platform_name(slug), bypass_cooldown=True)
    return RedirectResponse(f"/watches/{watch_id}/result", 303)


@app.post("/watches/{watch_id}/platform/{slug}/test-direct")
async def test_platform_direct_url(
    watch_id: int,
    slug: str,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    direct = (
        watch.bookmyshow_direct_url
        if slug == "bookmyshow"
        else watch.pvrinox_direct_url
        if slug == "pvrinox"
        else ""
    )
    if not direct:
        raise HTTPException(422, "Save a direct URL before testing it")
    await run_watch_check(
        db,
        watch,
        only_platform=platform_name(slug),
        bypass_cooldown=True,
        test_direct_url=True,
    )
    return RedirectResponse(f"/watches/{watch_id}/result", 303)


@app.post("/watches/{watch_id}/platform/{slug}/disable")
def disable_platform(
    watch_id: int,
    slug: str,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    name = platform_name(slug)
    if name == "BookMyShow":
        watch.bookmyshow_mode = PlatformMode.DISABLED
        watch.bookmyshow_enabled = False
    else:
        watch.pvrinox_mode = PlatformMode.DISABLED
        watch.pvrinox_enabled = False
    states = [retry_state(db, watch, item).last_status for item in enabled_platforms(watch)]
    watch.last_status = aggregate_watch_status(states, watch_enabled=watch.enabled)
    watch.next_check_at = None
    db.commit()
    return RedirectResponse(f"/watches/{watch_id}/result", 303)


@app.post("/watches/{watch_id}/platform/{slug}/clear-cache")
def clear_platform_cache(
    watch_id: int,
    slug: str,
    request: Request,
    csrf_token: str = Form(),
    db: Session = Depends(get_db),
):
    verify_csrf(request, csrf_token)
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    if platform_name(slug) == "BookMyShow":
        watch.bookmyshow_discovered_url = ""
    else:
        watch.pvrinox_discovered_url = ""
    watch.next_check_at = None
    db.commit()
    return RedirectResponse(f"/watches/{watch_id}/result", 303)


@app.get("/watches/{watch_id}/platform/{slug}/diagnostic.json")
def download_platform_diagnostic(
    watch_id: int, slug: str, db: Session = Depends(get_db)
) -> JSONResponse:
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    name = platform_name(slug)
    check = db.scalar(
        select(PlatformCheck)
        .where(PlatformCheck.watch_id == watch_id, PlatformCheck.platform == name)
        .order_by(PlatformCheck.id.desc())
    )
    state = retry_state(db, watch, name)
    report = {
        "watch_id": watch.id,
        "platform": name,
        "movie": watch.movie_name,
        "city": watch.city,
        "show_date": watch.show_date.isoformat(),
        "mode": platform_mode_value(watch, name),
        "supplied_direct_url": sanitize_url(
            watch.bookmyshow_direct_url if name == "BookMyShow" else watch.pvrinox_direct_url
        ),
        "discovered_canonical_url": sanitize_url(
            watch.bookmyshow_discovered_url
            if name == "BookMyShow"
            else watch.pvrinox_discovered_url
        ),
        "last_check": None,
        "retry": {
            "consecutive_block_count": state.consecutive_block_count,
            "blocked_until": state.blocked_until.isoformat() if state.blocked_until else None,
            "last_block_reason": state.last_block_reason,
        },
        "sanitization": "Cookies, request headers, credentials, tokens, and page content are excluded.",
    }
    if check:
        report["last_check"] = {
            "status": check.status,
            "checked_at": check.checked_at.isoformat(),
            "phase": check.phase,
            "checked_url": sanitize_url(check.checked_url),
            "final_url": sanitize_url(check.final_url),
            "page_outcome": check.page_outcome,
            "page_title": check.page_title,
            "structured_sources": check.structured_sources,
            "raw_candidate_count": check.raw_candidate_count,
            "matching_count": check.matching_count,
            "block_classification": check.block_classification,
            "cloudflare_ray_id": check.ray_id,
            "reason": check.reason,
            "error": check.error,
            "screenshot_filename": __import__("pathlib").Path(check.screenshot_path).name,
            "parser_version": check.parser_version,
        }
    response = JSONResponse(report)
    response.headers["Content-Disposition"] = (
        f'attachment; filename="watch-{watch_id}-{slug}-diagnostic.json"'
    )
    db.commit()
    return response


def platform_mode_value(watch: Watch, name: str) -> str:
    return str(watch.bookmyshow_mode if name == "BookMyShow" else watch.pvrinox_mode)


@app.post("/watches/{watch_id}/test-notification")
async def test_notification(
    watch_id: int, request: Request, csrf_token: str = Form(), db: Session = Depends(get_db)
):
    verify_csrf(request, csrf_token)
    watch = db.get(Watch, watch_id)
    if not watch:
        raise HTTPException(404)
    success, error = True, ""
    try:
        attempts = await TelegramProvider().send_test(watch)
    except Exception as exc:
        success = False
        attempts = 1
        error = sanitize_telegram_error(exc, settings.telegram_bot_token)
    db.add(
        NotificationHistory(
            watch_id=watch.id,
            provider="telegram",
            success=success,
            attempts=attempts,
            error=error,
            is_test=True,
            notification_source="TEST",
            delivery_status="SENT" if success else "FAILED",
        )
    )
    db.commit()
    if not success:
        raise HTTPException(502, f"Telegram delivery failed: {error}")
    return RedirectResponse("/", 303)


@app.get("/health")
def health(db: Session = Depends(get_db)) -> dict:
    db.execute(text("SELECT 1"))
    directories = [
        settings.data_dir,
        settings.config_dir,
        settings.screenshot_dir,
        settings.log_dir,
    ]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)
        probe = directory / ".health-write-test"
        try:
            probe.write_text("ok")
            probe.unlink()
        except OSError as exc:
            raise HTTPException(503, f"Runtime directory is not writable: {directory}") from exc
    bot_heartbeat = db.get(RuntimeState, "telegram_bot_heartbeat")
    bot_runtime = db.get(RuntimeState, "telegram_bot_status")
    bot_status = bot_runtime.value if bot_runtime else "configuration_incomplete"
    if bot_status == "long_polling" and bot_heartbeat:
        stamp = datetime.fromisoformat(bot_heartbeat.value)
        if stamp.tzinfo is None:
            stamp = stamp.replace(tzinfo=ZoneInfo("UTC"))
        if (
            datetime.now(ZoneInfo("UTC")) - stamp
        ).total_seconds() > settings.worker_heartbeat_max_age_seconds:
            bot_status = "stale"
        else:
            bot_status = "healthy"
    return {
        "status": "ok",
        "database": "ok",
        "directories": "writable",
        "timezone": settings.app_timezone,
        "telegram_configured": telegram_configured(settings),
        "telegram_bot": bot_status,
    }


@app.get("/diagnostics/{filename}")
def diagnostic_screenshot(filename: str, request: Request) -> FileResponse:
    safe_name = __import__("pathlib").Path(filename).name
    path = (settings.screenshot_dir / safe_name).resolve()
    if path.parent != settings.screenshot_dir.resolve() or not path.is_file():
        raise HTTPException(404)
    return FileResponse(path, media_type="image/png")
