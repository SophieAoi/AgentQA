from datetime import datetime

from agent.reporter import render_html
from app.models.schemas import (
    ExecutionStep,
    SelectorStrategy,
    StepType,
    TestRunDetail,
    TestRunStatus,
    TestStepResult,
)


def _run(status: TestRunStatus, steps: list[TestStepResult], **kwargs) -> TestRunDetail:
    return TestRunDetail(
        run_id="run123",
        status=status,
        started_at=datetime(2026, 1, 1, 12, 0, 0),
        finished_at=datetime(2026, 1, 1, 12, 5, 0),
        passed_count=kwargs.get("passed_count", 0),
        failed_count=kwargs.get("failed_count", 0),
        total_count=kwargs.get("total_count", 1),
        steps=steps,
        logs=[],
    )


def test_render_html_for_all_pass_run():
    run = _run(
        TestRunStatus.passed,
        [TestStepResult(step_description="Log in", status="OK")],
        passed_count=1,
        total_count=1,
    )

    html = render_html(run, [])

    assert "run123" in html
    assert "passed" in html
    assert "Log in" in html
    assert "No execution trace recorded" in html


def test_render_html_for_mixed_failure_run_includes_detail_and_screenshot():
    run = _run(
        TestRunStatus.failed,
        [
            TestStepResult(step_description="Log in", status="OK"),
            TestStepResult(
                step_description="Assert dashboard shown",
                status="FAILED",
                detail="Dashboard heading not found.",
                screenshot_url="/screenshots/abc123_failure.png",
            ),
        ],
        passed_count=1,
        failed_count=1,
        total_count=2,
    )

    execution_steps = [
        ExecutionStep(
            id="es1",
            run_id="run123",
            test_case_id="TC-001",
            step_index=1,
            step_type=StepType.assertion,
            intent="Assert dashboard shown",
            tool_name="assert_condition",
            selector_used=None,
            selector_strategy=None,
            actual_result=None,
            status=TestRunStatus.error,
            confidence=0.87,
            started_at=datetime(2026, 1, 1, 12, 1, 0),
            finished_at=datetime(2026, 1, 1, 12, 1, 2),
            error_detail="Dashboard heading not found.",
        )
    ]

    html = render_html(run, execution_steps)

    assert "failed" in html
    assert "Dashboard heading not found." in html
    assert "/screenshots/abc123_failure.png" in html
    assert "87%" in html
    assert "assert_condition" in html


def test_render_html_escapes_untrusted_step_content():
    run = _run(
        TestRunStatus.failed,
        [
            TestStepResult(
                step_description="<script>alert(1)</script>",
                status="FAILED",
                detail="<img src=x onerror=alert(2)>",
            )
        ],
        failed_count=1,
        total_count=1,
    )

    html = render_html(run, [])

    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;" in html


def test_render_html_handles_run_with_no_steps():
    run = _run(TestRunStatus.queued, [], total_count=0)

    html = render_html(run, [])

    assert "No steps recorded" in html
    assert "No execution trace recorded" in html
