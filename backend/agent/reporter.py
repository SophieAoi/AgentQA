"""
Reporter — deterministic Jinja2 templating, not an LLM call (see
docs/BUILD-PLAN.md § "Why two agent roles": Reporter was never a fifth
agent — a test run's pass/fail facts are already fully known from
TestRunDetail/ExecutionStep, so generating the report is formatting, not
reasoning).

PDF export uses Playwright's own print-to-PDF rather than adding weasyprint:
Playwright/Chromium is already a hard dependency for the whole agent, and
weasyprint pulls in Cairo/Pango system libraries that are a common source of
install friction — reusing the existing browser avoids a second heavyweight
rendering stack for one feature.
"""

from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from playwright.async_api import async_playwright

from app.config import BACKEND_BASE_URL
from app.models.schemas import ExecutionStep, TestRunDetail

TEMPLATES_DIR = Path(__file__).resolve().parent / "templates"

_env = Environment(
    loader=FileSystemLoader(str(TEMPLATES_DIR)),
    autoescape=select_autoescape(["html", "jinja2"]),
)


def render_html(run: TestRunDetail, execution_steps: list[ExecutionStep]) -> str:
    template = _env.get_template("report.html.jinja2")
    return template.render(
        run=run,
        execution_steps=execution_steps,
        backend_base_url=BACKEND_BASE_URL,
    )


async def render_pdf(html: str) -> bytes:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await browser.new_page()
            await page.set_content(html, wait_until="networkidle")
            return await page.pdf(format="A4", print_background=True)
        finally:
            await browser.close()
