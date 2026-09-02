from pathlib import Path

from agent.browser.session import BrowserSession

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"


async def test_browser_session_launches_navigates_and_closes():
    async with BrowserSession(headless=True) as session:
        await session.goto(f"file://{FIXTURES_DIR / 'simple_page.html'}")
        assert "Fixture page" in await session.page.content()


async def test_browser_session_screenshot_returns_a_servable_url(tmp_path, monkeypatch):
    import agent.browser.session as session_module

    monkeypatch.setattr(session_module, "SCREENSHOTS_DIR", tmp_path)

    async with BrowserSession(headless=True) as session:
        await session.goto(f"file://{FIXTURES_DIR / 'simple_page.html'}")
        url = await session.screenshot("test-step")

    assert url.startswith("/screenshots/")
    assert url.endswith("_test-step.png")
    saved_files = list(tmp_path.glob("*.png"))
    assert len(saved_files) == 1
