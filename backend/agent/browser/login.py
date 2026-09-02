"""
Login flow for the Influence staging site. The site's login is an OAuth
redirect to auth-stg.movingwalls.com — SiteViewer.jsx's comment already
documents why that flow can't be embedded in an iframe (third-party cookie
blocking). This module drives the real redirect flow with Playwright instead.

Selectors below were captured directly against the real staging login page
(placeholder text and button role/name), not guessed.
"""

from typing import Optional
from urllib.parse import urlparse

from playwright.async_api import Page

from app.config import INFLUENCE_BASE_URL, INFLUENCE_TEST_PASSWORD, INFLUENCE_TEST_USERNAME

EMAIL_PLACEHOLDER = "Ex : you@company.com"
PASSWORD_PLACEHOLDER = "••••••••••"


class LoginError(RuntimeError):
    """Raised when credentials are missing or the login flow doesn't complete."""


async def is_logged_in(page: Page, base_url: Optional[str] = None) -> bool:
    """
    Confirmed live: navigating to the app while unauthenticated redirects
    to auth-stg.movingwalls.com/oauth/login; navigating while already
    authenticated stays on the main app host. Used by
    agent/runner.py's browser-reuse path (running a batch of cases
    back-to-back in one shared session) to skip re-running login() — and
    the fill/click flow it does — on a session that's already
    authenticated from an earlier case in the same run, which would
    otherwise fail outright (no email/password fields exist on the
    already-logged-in dashboard for login()'s fill() calls to find).

    Compares scheme+host+port (not just hostname) — caught live by this
    module's own test suite: the two-origin test fixture (real regression
    coverage for the cross-domain OAuth hop) uses 127.0.0.1 for BOTH
    origins, distinguished only by port, so a hostname-only comparison
    couldn't tell them apart and reported "logged in" even immediately
    after a redirect to the login origin. The real site's two hosts
    (influence-stg vs auth-stg.movingwalls.com) happen to differ by
    hostname too, but comparing the full origin is correct in both cases
    and isn't reliant on that always being true.
    """
    target = base_url or INFLUENCE_BASE_URL
    await page.goto(target, wait_until="networkidle", timeout=20000)
    landed = urlparse(page.url)
    expected = urlparse(target)
    return (landed.scheme, landed.hostname, landed.port) == (expected.scheme, expected.hostname, expected.port)


async def login(page: Page, base_url: Optional[str] = None) -> None:
    """
    base_url defaults to the real staging site (INFLUENCE_BASE_URL); tests
    override it to point at a local fixture server instead.
    """
    target = base_url or INFLUENCE_BASE_URL

    if not INFLUENCE_TEST_USERNAME or not INFLUENCE_TEST_PASSWORD:
        raise LoginError(
            "INFLUENCE_TEST_USERNAME / INFLUENCE_TEST_PASSWORD are not set — "
            "add them to backend/.env."
        )

    await page.goto(target, wait_until="networkidle", timeout=20000)
    await page.get_by_placeholder(EMAIL_PLACEHOLDER).fill(INFLUENCE_TEST_USERNAME)
    await page.get_by_placeholder(PASSWORD_PLACEHOLDER).fill(INFLUENCE_TEST_PASSWORD)
    await page.get_by_role("button", name="Sign In").click()

    try:
        await page.wait_for_url(f"{target}/**", timeout=15000)
    except Exception as exc:
        raise LoginError(
            f"Login did not redirect back to {target} — check credentials "
            f"and that the staging site is reachable."
        ) from exc
