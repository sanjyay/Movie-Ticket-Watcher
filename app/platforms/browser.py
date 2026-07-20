from dataclasses import dataclass
from datetime import datetime, timezone

from playwright.async_api import TimeoutError as PlaywrightTimeoutError
from playwright.async_api import async_playwright

from app.config import get_settings
from app.platforms.parsing import BlockedPageError, detect_blocked


@dataclass(slots=True)
class PageSnapshot:
    html: str
    screenshot_path: str = ""
    final_url: str = ""
    title: str = ""
    outcome: str = "loaded"
    http_status: int | None = None


def prune_screenshots() -> None:
    settings = get_settings()
    files = sorted(
        settings.screenshot_dir.glob("*.png"), key=lambda p: p.stat().st_mtime, reverse=True
    )
    for path in files[settings.screenshot_retention :]:
        path.unlink(missing_ok=True)


async def fetch_page(url: str, platform: str, watch_id: int) -> PageSnapshot:
    settings = get_settings()
    if not url.startswith(("https://", "http://")):
        raise ValueError("URL must use http or https")
    settings.screenshot_dir.mkdir(parents=True, exist_ok=True)
    screenshot = ""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=settings.playwright_headless)
        page = await browser.new_page(locale="en-IN")
        try:
            response = await page.goto(
                url, wait_until="domcontentloaded", timeout=settings.platform_timeout_seconds * 1000
            )
            await page.locator("body").wait_for(state="attached", timeout=5000)
            html = await page.content()
            try:
                detect_blocked(html)
            except BlockedPageError as exc:
                stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
                path = settings.screenshot_dir / f"{watch_id}-{platform}-blocked-{stamp}.png"
                await page.screenshot(path=str(path), full_page=True)
                prune_screenshots()
                raise BlockedPageError(
                    str(exc),
                    classification=exc.classification,
                    ray_id=exc.ray_id,
                    screenshot_path=str(path),
                    final_url=page.url,
                    page_title=await page.title(),
                    page_outcome="blocked",
                ) from None
            status = response.status if response else None
            return PageSnapshot(
                html=html,
                screenshot_path=screenshot,
                final_url=page.url,
                title=await page.title(),
                outcome=f"HTTP {status}" if status else "loaded",
                http_status=status,
            )
        except PlaywrightTimeoutError as exc:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            path = settings.screenshot_dir / f"{watch_id}-{platform}-{stamp}.png"
            await page.screenshot(path=str(path), full_page=True)
            screenshot = str(path)
            prune_screenshots()
            raise TimeoutError(f"Platform timed out; screenshot: {path}") from exc
        finally:
            await browser.close()
