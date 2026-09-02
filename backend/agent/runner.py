"""
Bridge between the API and the real agent code. Phase 3
(docs/phase-03-planner-executor-agent-loop.md) replaced Phase 2's hardcoded
per-test-case Playwright scripts with a real Planner + Executor loop: the
Planner turns a test case's natural-language description into an ordered
plan, the Executor drives real Playwright tools to carry it out.

Login stays a hardcoded, separately-verified precondition step (Phase 2's
already-proven real login flow) rather than something the agent plans for
itself — it's infrastructure the test case depends on, not the behavior
under test, and OAuth redirect timing is exactly the kind of brittle
mechanic that shouldn't be re-litigated by an LLM on every run.
"""

import asyncio
import re
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional
from urllib.parse import urlparse

import yaml

from app.config import INFLUENCE_BASE_URL, TEST_CASE_TIMEOUT_SECONDS
from app.models.schemas import PlannedStep, StepType, TestStepResult
from app.services.event_bus import EventBus
from app.services.store import StoreProtocol
from agent.browser.login import LoginError, is_logged_in, login
from agent.browser.session import BrowserSession
from agent.executor import ExecutorError, run_executor
from agent.planner import PlannerError, plan_test_case
from agent.tools.playwright_tools import publish_screenshot_frame

TEST_CASES_DIR = Path(__file__).resolve().parent / "test_cases"

_URL_RE = re.compile(r"https?://[^\s\"']+")
_INFLUENCE_HOST = urlparse(INFLUENCE_BASE_URL).hostname or ""


def _fix_hallucinated_urls(planned_steps: list[PlannedStep], log: Callable[[str], None]) -> None:
    """
    Safety net alongside the Planner prompt's explicit "copy URLs
    verbatim" instruction: even with that instruction, a smaller model can
    still occasionally paraphrase a real URL into a generic-looking one
    (observed live: influence-stg.movingwalls.com -> a plausible
    *.example.com placeholder), which then fails DNS resolution and takes
    the rest of the test case down with it. For a "navigate"-type step
    whose intent contains a URL that doesn't match the configured
    Influence host, swap in the real INFLUENCE_BASE_URL — this can only
    correct toward the one real, known-good target, never invent a wrong
    one, so it's safe to apply unconditionally rather than just flagging it.
    """
    if not _INFLUENCE_HOST:
        return
    for step in planned_steps:
        if step.step_type != StepType.navigate:
            continue
        match = _URL_RE.search(step.intent)
        if not match:
            continue
        found_host = urlparse(match.group(0)).hostname or ""
        if found_host and found_host != _INFLUENCE_HOST:
            original = step.intent
            step.intent = step.intent.replace(match.group(0), INFLUENCE_BASE_URL)
            log(
                f"Planner produced a navigate step pointing at {found_host!r} instead of the "
                f"configured site — corrected {original!r} to {step.intent!r}."
            )


def load_test_case(test_case_id: str) -> dict:
    path = TEST_CASES_DIR / f"{test_case_id}.yaml"
    if not path.exists():
        raise ValueError(f"Unknown test case: {test_case_id}")
    return yaml.safe_load(path.read_text())


def list_test_cases() -> list[dict]:
    return [yaml.safe_load(path.read_text()) for path in sorted(TEST_CASES_DIR.glob("*.yaml"))]


class TestCaseValidationError(ValueError):
    """Raised for a test case id/save request that can't be turned into a
    safe filename or a well-formed YAML file — kept distinct from the
    plain ValueError load_test_case raises for "file doesn't exist" so
    callers (the router) can map the two to different HTTP statuses."""


# Anchored so it validates the WHOLE id, not just a prefix — matches the
# same charset every real test case id in this repo already uses
# (letters, digits, underscore, hyphen). Deliberately excludes "." and "/"
# so a crafted id can never escape TEST_CASES_DIR or target a file with a
# different extension (e.g. "../../app/main" or "foo.yaml.bak").
_VALID_TEST_CASE_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")


class _FoldedStr(str):
    """Marks a string for YAML's folded block-scalar style (`>`) on dump —
    matches every hand-written test case file's own description
    formatting, rather than PyYAML's default single-line-with-wrapping
    style, which would make a saved file look unlike every other one in
    this directory."""


def _folded_str_representer(dumper: yaml.Dumper, data: str):
    return dumper.represent_scalar("tag:yaml.org,2002:str", data, style=">")


yaml.add_representer(_FoldedStr, _folded_str_representer)


def _test_case_path(test_case_id: str) -> Path:
    """Validates test_case_id is a safe filename component before ever
    building a path from it — the one thing every write function below
    must do first, since an unchecked id here is a path-traversal
    vulnerability (the id ultimately comes from an HTTP request body)."""
    if not test_case_id or not _VALID_TEST_CASE_ID_RE.match(test_case_id):
        raise TestCaseValidationError(
            f"Invalid test case id {test_case_id!r} — only letters, digits, "
            "underscores, and hyphens are allowed."
        )
    return TEST_CASES_DIR / f"{test_case_id}.yaml"


def save_test_case(
    test_case_id: str,
    title: str,
    description: str,
    suite: Optional[str] = None,
    preconditions: Optional[list[str]] = None,
    essential: bool = False,
    *,
    overwrite: bool,
) -> dict:
    """
    Writes one agent/test_cases/{id}.yaml — used for both create (
    overwrite=False, fails if the id already exists) and edit (
    overwrite=True, fails if it does NOT exist yet, since an edit whose
    id was silently typo'd into a new file would leave the original
    orphaned). Editing a case's id itself isn't supported here — that's
    a rename (delete old file + create new one under a different name),
    which the router exposes as a distinct, explicit operation instead
    of overloading this one.
    """
    path = _test_case_path(test_case_id)
    exists = path.exists()
    if overwrite and not exists:
        raise TestCaseValidationError(f"Unknown test case: {test_case_id}")
    if not overwrite and exists:
        raise TestCaseValidationError(f"Test case {test_case_id!r} already exists.")

    if not title or not title.strip():
        raise TestCaseValidationError("title is required.")
    if not description or not description.strip():
        raise TestCaseValidationError("description is required.")

    data = {
        "id": test_case_id,
        "title": title.strip(),
        "description": _FoldedStr(description if description.endswith("\n") else description + "\n"),
    }
    # preconditions/suite/essential are optional in the schema and, for
    # suite/essential, actively skipped when unset/false in every
    # existing file that doesn't need them — matching that keeps a
    # freshly-saved file indistinguishable from a hand-written one.
    data["preconditions"] = list(preconditions) if preconditions else []
    if suite:
        data["suite"] = suite
    if essential:
        data["essential"] = True

    content = yaml.dump(data, sort_keys=False, allow_unicode=True, width=79, default_flow_style=False)
    path.write_text(content)
    return yaml.safe_load(content)


def delete_test_case(test_case_id: str) -> None:
    path = _test_case_path(test_case_id)
    if not path.exists():
        raise TestCaseValidationError(f"Unknown test case: {test_case_id}")
    path.unlink()


def test_case_requires_fresh_session(test_case_id: str) -> bool:
    """
    True for a case that must run in a genuinely fresh, unauthenticated
    browser (the login suite: AD_LG_*, Ads_Login_* — cases WITHOUT
    "requires login" in their preconditions, since those specifically
    exercise the logged-out flow). Reusing a session already authenticated
    by an earlier case in the same run would silently break these — they'd
    find themselves already logged in and never see the login form they're
    meant to test. Exposed as its own function (not inlined in the suite
    loop) so _run_test_suite_body's reuse-vs-fresh decision reads as the
    same rule this file's own login-precondition check already uses, not a
    second, potentially-diverging one.
    """
    try:
        test_case = load_test_case(test_case_id)
    except ValueError:
        return True  # unknown case — safest to not hand it a possibly-authenticated session
    return "requires login" not in test_case.get("preconditions", [])


async def run_test_case(
    store: StoreProtocol,
    event_bus: EventBus,
    run_id: str,
    test_case_id: str,
    on_step: Optional[Callable[[TestStepResult], None]] = None,
    session: Optional[BrowserSession] = None,
) -> list[TestStepResult]:
    """
    Runs one test case against the real staging site: fresh login if
    required, Planner produces a step plan from the test case's
    description, Executor carries it out. Returns one TestStepResult per
    meaningful step (login + each planned step), each optionally carrying a
    screenshot via the underlying ExecutionStep records.

    `session`: an already-open BrowserSession to reuse (passed by
    _run_test_suite_body when running a batch of cases back-to-back that
    all need to already be logged in — skips paying a fresh
    launch+navigate cost, measured at ~1.6-2.3s, on every case). When
    omitted, a fresh session is opened and closed here, same as before —
    the caller is responsible for only passing a shared session into a
    case where test_case_requires_fresh_session() is False.

    Wrapped in a wall-clock timeout (TEST_CASE_TIMEOUT_SECONDS) so a runaway
    agent loop can't hang a run forever — extends F-005's "don't crash
    silently" to "don't hang indefinitely" (docs/phase-04-verifier-and-reliability.md).
    """
    try:
        return await asyncio.wait_for(
            _run_test_case_body(store, event_bus, run_id, test_case_id, on_step, session),
            timeout=TEST_CASE_TIMEOUT_SECONDS,
        )
    except asyncio.TimeoutError:
        detail = f"Test case exceeded the {TEST_CASE_TIMEOUT_SECONDS}s wall-clock timeout."
        store.add_log(run_id, f"✗ {detail}")
        event_bus.publish(run_id, "logs", "log", {"message": f"✗ {detail}"})
        step = TestStepResult(step_description=f"Run {test_case_id}", status="ERROR", detail=detail)
        if on_step:
            on_step(step)
        return [step]


async def _run_test_case_body(
    store: StoreProtocol,
    event_bus: EventBus,
    run_id: str,
    test_case_id: str,
    on_step: Optional[Callable[[TestStepResult], None]],
    shared_session: Optional[BrowserSession] = None,
) -> list[TestStepResult]:
    def log(message: str) -> None:
        store.add_log(run_id, message)
        event_bus.publish(run_id, "logs", "log", {"message": message})

    def emit(step: TestStepResult) -> None:
        if on_step:
            on_step(step)

    try:
        test_case = load_test_case(test_case_id)
    except ValueError as exc:
        log(f"✗ {exc}")
        step = TestStepResult(step_description=f"Load {test_case_id}", status="FAILED", detail=str(exc))
        emit(step)
        return [step]

    steps: list[TestStepResult] = []

    async def _run_with_session(session: BrowserSession) -> list[TestStepResult]:
        nonlocal steps
        if shared_session is None:
            log("Launching browser...")
        # A viewer previously saw the stale "No test run in progress yet"
        # placeholder for the entire login+Planner-thinking window (often
        # 15-25+s) since the first frame only arrived after the Executor's
        # first tool call. Publishing here means the Live Stream panel
        # shows *something* real almost immediately after launch.
        await publish_screenshot_frame(session.page, event_bus, run_id)

        if "requires login" in test_case.get("preconditions", []):
            # When reusing a shared session across a batch (shared_session
            # is not None), an earlier case in the same run may have
            # already authenticated it — re-running login()'s fill/click
            # flow on an already-logged-in session would fail outright
            # (no email/password fields exist on the dashboard). A fresh
            # session (shared_session is None) is never already logged in,
            # so skip the extra navigate-and-check there.
            already_logged_in = shared_session is not None and await is_logged_in(session.page)
            if already_logged_in:
                log("Already logged in (reused session) — skipping login.")
                step = TestStepResult(step_description="Log in to Influence staging", status="OK")
                steps.append(step)
                emit(step)
            else:
                try:
                    log("Logging in...")
                    await login(session.page)
                except LoginError as exc:
                    screenshot_url = await session.screenshot(f"{test_case_id}-login-failed")
                    log(f"✗ Login failed: {exc}")
                    step = TestStepResult(
                        step_description="Log in to Influence staging",
                        status="FAILED",
                        detail=str(exc),
                        screenshot_url=screenshot_url,
                    )
                    steps.append(step)
                    emit(step)
                    return steps

                log("Login succeeded.")
                step = TestStepResult(step_description="Log in to Influence staging", status="OK")
                steps.append(step)
                emit(step)
                await publish_screenshot_frame(session.page, event_bus, run_id)

        try:
            log("Planning test steps...")
            # {{INFLUENCE_BASE_URL}} lets a test case that doesn't use the
            # "requires login" precondition (e.g. testing the login page
            # itself) still tell the Planner where "the login page" actually
            # is — without it, the Planner has no way to know the real site
            # URL and will hallucinate a plausible-looking placeholder one.
            description = test_case["description"].replace("{{INFLUENCE_BASE_URL}}", INFLUENCE_BASE_URL)
            planned_steps = await plan_test_case(description)
            _fix_hallucinated_urls(planned_steps, log)
            log(f"Plan has {len(planned_steps)} step(s).")
        except PlannerError as exc:
            log(f"✗ Planning failed: {exc}")
            step = TestStepResult(step_description="Plan test steps", status="ERROR", detail=str(exc))
            steps.append(step)
            emit(step)
            return steps

        try:
            executor_steps = await run_executor(
                session.page, store, event_bus, run_id, test_case_id, planned_steps, log, on_step=emit
            )
            steps += executor_steps
        except ExecutorError as exc:
            log(f"✗ Execution failed: {exc}")
            step = TestStepResult(step_description="Execute planned steps", status="ERROR", detail=str(exc))
            steps.append(step)
            emit(step)

        return steps

    if shared_session is not None:
        return await _run_with_session(shared_session)

    async with BrowserSession() as session:
        return await _run_with_session(session)
