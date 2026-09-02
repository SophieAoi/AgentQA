"""
Playwright browser/page lifecycle for a single test-case run: launch,
navigate, screenshot-on-error, close. Async API, matching the FastAPI
event loop run_test_suite() executes inside.
"""

import uuid
from pathlib import Path
from typing import Optional

from playwright.async_api import Browser, BrowserContext, Page, Playwright, async_playwright

from app.config import PLAYWRIGHT_HEADLESS

SCREENSHOTS_DIR = Path(__file__).resolve().parent.parent.parent / "screenshots"


class BrowserSession:
    """
    One Playwright browser instance for the lifetime of a single test-case
    run. Use as an async context manager:

        async with BrowserSession() as session:
            await session.goto("https://example.com")
            url = await session.screenshot("some-step")
    """

    def __init__(self, headless: Optional[bool] = None) -> None:
        self._headless = PLAYWRIGHT_HEADLESS if headless is None else headless
        self._playwright: Optional[Playwright] = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self.page: Optional[Page] = None

    async def __aenter__(self) -> "BrowserSession":
        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(headless=self._headless)
        self._context = await self._browser.new_context(viewport={"width": 1280, "height": 1000})
        self.page = await self._context.new_page()
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        await self.close()

    async def goto(self, url: str, timeout: int = 20000) -> None:
        await self.page.goto(url, wait_until="networkidle", timeout=timeout)

    async def screenshot(self, name: str) -> str:
        """Save a full-page screenshot, returning a URL path the backend serves."""
        SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
        filename = f"{uuid.uuid4().hex[:12]}_{name}.png"
        await self.page.screenshot(path=str(SCREENSHOTS_DIR / filename), full_page=True)
        return f"/screenshots/{filename}"

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
