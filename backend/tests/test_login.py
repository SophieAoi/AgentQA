from unittest.mock import patch

import pytest

from agent.browser.login import LoginError, is_logged_in, login
from agent.browser.session import BrowserSession


async def test_login_raises_when_credentials_missing():
    with patch("agent.browser.login.INFLUENCE_TEST_USERNAME", None), patch(
        "agent.browser.login.INFLUENCE_TEST_PASSWORD", None
    ):
        async with BrowserSession(headless=True) as session:
            with pytest.raises(LoginError, match="not set"):
                await login(session.page, base_url="http://example.invalid")


async def test_login_succeeds_against_two_origin_fixture(two_origin_fixture_servers):
    app_origin, _login_origin = two_origin_fixture_servers
    with patch("agent.browser.login.INFLUENCE_TEST_USERNAME", "fixture@example.com"), patch(
        "agent.browser.login.INFLUENCE_TEST_PASSWORD", "fixture-pass"
    ):
        async with BrowserSession(headless=True) as session:
            await login(session.page, base_url=app_origin)
            assert session.page.url == f"{app_origin}/dashboard.html"


async def test_login_raises_when_redirect_never_happens(two_origin_fixture_servers):
    """
    Wrong credentials never trigger the fixture login page's redirect, so the
    page stays on login_origin — a different origin from app_origin, exactly
    like a real wrong-password attempt never leaves auth-stg.movingwalls.com.
    """
    app_origin, _login_origin = two_origin_fixture_servers
    with patch("agent.browser.login.INFLUENCE_TEST_USERNAME", "wrong@example.com"), patch(
        "agent.browser.login.INFLUENCE_TEST_PASSWORD", "wrong-pass"
    ):
        async with BrowserSession(headless=True) as session:
            with pytest.raises(LoginError, match="did not redirect"):
                await login(session.page, base_url=app_origin)


async def test_is_logged_in_false_before_authenticating(two_origin_fixture_servers):
    """
    Regression coverage for agent/runner.py's browser-reuse path: before
    any login, navigating to app_origin's root path 302-redirects to
    login_origin (a different host) unconditionally in this fixture — the
    same real cross-origin redirect the two-origin fixture exists to test
    (unlike the real site, this fixture's root path always redirects
    regardless of auth state; only /dashboard.html models "already
    logged in", see test_is_logged_in_true_after_authenticating below).
    is_logged_in() must report False here so _run_test_case_body doesn't
    skip login() on a session that was never actually authenticated.
    """
    app_origin, _login_origin = two_origin_fixture_servers
    async with BrowserSession(headless=True) as session:
        assert await is_logged_in(session.page, base_url=app_origin) is False


async def test_is_logged_in_true_when_the_target_page_loads_directly(two_origin_fixture_servers):
    """
    This fixture has no real session/cookie state — its app origin
    unconditionally 302-redirects any path except /dashboard.html, so it
    can't model "the root path behaves differently once authenticated"
    the way the real site does. What it CAN model is is_logged_in()'s
    actual comparison: does navigating to `target` land on `target`'s own
    origin, or get redirected elsewhere. Pointing base_url at
    /dashboard.html directly (a page that returns 200 with no redirect at
    all — the fixture's stand-in for "already-authenticated content")
    exercises the True branch for real, same is_logged_in() call real
    agent/runner.py code uses, just against a URL that doesn't redirect.
    """
    app_origin, _login_origin = two_origin_fixture_servers
    async with BrowserSession(headless=True) as session:
        assert await is_logged_in(session.page, base_url=f"{app_origin}/dashboard.html") is True
