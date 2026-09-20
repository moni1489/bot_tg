from __future__ import annotations

import asyncio
import concurrent.futures
import logging
import random
from typing import Callable
from urllib.parse import quote_plus

from funko_deal_bot.config import playwright_proxy, proxy_enabled_flag

log = logging.getLogger(__name__)

PlaywrightFetch = Callable[[str], tuple[str | None, str | None, int]]

# Search/item goto: wait_until=commit. networkidle hangs on eBay. Keep this short;
# Playwright is last-resort and skipped by default at home.
DEFAULT_PW_TIMEOUT_MS = 45_000
WAIT_UNTIL = "commit"

_INSTALL_HINT = (
    "Playwright не установлен: uv sync --extra browser && uv run python -m playwright install chromium"
)


def chromium_launch_kwargs(proxy_url: str = "") -> dict:
    launch_kwargs: dict = {"headless": True}
    spec = playwright_proxy(proxy_url)
    if spec:
        launch_kwargs["proxy"] = spec
    return launch_kwargs


def with_stpos(url: str, zip_code: str) -> str:
    zip_code = (zip_code or "").strip()
    if not zip_code or "_stpos=" in (url or ""):
        return url
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}_stpos={quote_plus(zip_code)}"


ZIP_INPUT_SELECTORS = (
    'input[name="zipCode"]',
    "#zipCode",
    "#fshippingDest",
    'input[aria-label*="ZIP" i]',
    'input[aria-label*="postal" i]',
    'input[placeholder*="ZIP" i]',
    'input[placeholder*="zip" i]',
    'input[data-testid*="zip" i]',
    'input[autocomplete="postal-code"]',
)
# eBay item calculator: ZIP field then Update (not a hardcoded USPS dollar amount).
ZIP_UPDATE_SELECTORS = (
    'button:has-text("Update")',
    'input[type="submit"][value="Update"]',
    'button:has-text("Get Rates")',
    'button:has-text("Get rates")',
    'button:has-text("Apply")',
    'button:has-text("Submit")',
    'button:has-text("Go")',
)


async def fill_zip_and_update(page, zip_code: str) -> bool:
    """Fill destination ZIP and click Update. True if the ZIP field was filled."""
    filled = False
    for sel in ZIP_INPUT_SELECTORS:
        loc = page.locator(sel)
        try:
            if await loc.count() == 0:
                continue
            box = loc.first
            await box.wait_for(state="visible", timeout=2_000)
            await box.fill(zip_code, timeout=2_000)
            filled = True
            break
        except Exception:  # noqa: BLE001
            continue
    if not filled:
        return False
    for btn_sel in ZIP_UPDATE_SELECTORS:
        btn = page.locator(btn_sel)
        try:
            if await btn.count() == 0:
                continue
            target = btn.first
            if hasattr(target, "is_visible") and not await target.is_visible():
                continue
            await target.click(timeout=2_000)
            await page.wait_for_timeout(1_200)
            return True
        except Exception:  # noqa: BLE001
            continue
    try:
        box = page.locator(ZIP_INPUT_SELECTORS[0]).first
        await box.press("Enter")
        await page.wait_for_timeout(1_200)
    except Exception:  # noqa: BLE001
        pass
    return filled


async def async_fetch_search_html(
    url: str,
    *,
    proxy_url: str = "",
    timeout_ms: int = DEFAULT_PW_TIMEOUT_MS,
    rng: random.Random | None = None,
    zip_code: str = "",
    wait_until: str = WAIT_UNTIL,
) -> tuple[str | None, str | None, int]:
    """Open one search or item URL in stock Playwright Chromium (async_api).

    Official Playwright API only: headless Chromium, one page, optional single
    proxy the operator set. No stealth plugins, no anti-detect patches,
    no UA farm. Optional zip_code adds `_stpos` and fills ZIP + Update on the lot.
    Used only when RSS/httpx returned no listings. Default wait is ``commit``
    with a 45s navigation timeout. TimedOut is logged; the scan continues.
    """
    try:
        from playwright.async_api import async_playwright
    except ImportError:
        return None, _INSTALL_HINT, 0

    delay_ms = int((rng or random.Random()).uniform(300, 800))
    launch_kwargs = chromium_launch_kwargs(proxy_url)
    proxy_flag = proxy_enabled_flag(proxy_url)
    target = with_stpos(url, zip_code)
    log.info(
        "Playwright Chromium proxy=%s wait_until=%s timeout_ms=%s",
        proxy_flag,
        wait_until,
        timeout_ms,
    )
    browser = None

    try:
        from playwright.async_api import TimeoutError as PlaywrightTimeout
    except ImportError:
        PlaywrightTimeout = TimeoutError  # type: ignore[misc,assignment]

    try:
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(**launch_kwargs)
            page = await browser.new_page()
            page.set_default_timeout(timeout_ms)
            page.set_default_navigation_timeout(timeout_ms)
            try:
                response = await page.goto(target, wait_until=wait_until, timeout=timeout_ms)
            except PlaywrightTimeout as exc:
                log.warning("Playwright goto timed out (proxy=%s): %s", proxy_flag, exc)
                html = None
                try:
                    html = await page.content()
                except Exception:  # noqa: BLE001
                    html = None
                await browser.close()
                if html and "/itm/" in html:
                    return html, None, 200
                return html, f"Playwright: Page.goto timed out after {timeout_ms}ms", 0
            if zip_code:
                try:
                    await page.wait_for_load_state(
                        "domcontentloaded", timeout=min(15_000, timeout_ms)
                    )
                except Exception:  # noqa: BLE001
                    pass
                await fill_zip_and_update(page, zip_code)
            else:
                await page.wait_for_timeout(delay_ms)
            status = response.status if response is not None else 0
            html = await page.content()
            await browser.close()
    except Exception as exc:  # noqa: BLE001
        log.warning("Playwright page fetch failed (proxy=%s): %s", proxy_flag, exc)
        if browser is not None:
            try:
                await browser.close()
            except Exception:  # noqa: BLE001
                pass
        return None, f"Playwright: {exc}", 0
    if status == 403:
        log.warning("Playwright HTTP 403, proxy=%s", proxy_flag)
    return html, None, status


def _hard_cap_seconds(timeout_ms: int) -> float:
    return max(20.0, (timeout_ms / 1000.0) + 20.0)


def _run_async_fetch(
    url: str,
    *,
    proxy_url: str = "",
    timeout_ms: int = DEFAULT_PW_TIMEOUT_MS,
    rng: random.Random | None = None,
    zip_code: str = "",
    wait_until: str = WAIT_UNTIL,
) -> tuple[str | None, str | None, int]:
    """Run async_api in a private event loop (worker thread or no-loop caller)."""

    async def _bounded():
        return await asyncio.wait_for(
            async_fetch_search_html(
                url,
                proxy_url=proxy_url,
                timeout_ms=timeout_ms,
                rng=rng,
                zip_code=zip_code,
                wait_until=wait_until,
            ),
            timeout=_hard_cap_seconds(timeout_ms),
        )

    try:
        return asyncio.run(_bounded())
    except TimeoutError:
        log.warning("Playwright hard cap exceeded for %s", url.split("?")[0])
        return None, f"Playwright: timed out after {_hard_cap_seconds(timeout_ms):.0f}s", 0


def fetch_search_html(
    url: str,
    *,
    proxy_url: str = "",
    timeout_ms: int = DEFAULT_PW_TIMEOUT_MS,
    rng: random.Random | None = None,
    zip_code: str = "",
    wait_until: str = WAIT_UNTIL,
) -> tuple[str | None, str | None, int]:
    """Sync wrapper that never calls Playwright sync_api on a running asyncio loop.

    Telegram job_queue already has a loop. Playwright sync_api raises
    ``Sync API inside asyncio loop`` there. Always use async_api, either on a
    fresh loop or in a worker thread (same idea as asyncio.to_thread).
    A hung page.goto must not block the whole scan: worker result is capped.
    """
    kwargs = {
        "proxy_url": proxy_url,
        "timeout_ms": timeout_ms,
        "rng": rng,
        "zip_code": zip_code,
        "wait_until": wait_until,
    }
    hard_cap = _hard_cap_seconds(timeout_ms)
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return _run_async_fetch(url, **kwargs)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        future = pool.submit(_run_async_fetch, url, **kwargs)
        try:
            return future.result(timeout=hard_cap)
        except concurrent.futures.TimeoutError:
            log.warning("Playwright worker timed out for %s", url.split("?")[0])
            return None, f"Playwright: timed out after {hard_cap:.0f}s", 0
