"""
Executor — a ReAct-style tool-use loop that walks a Planner-produced list of
PlannedSteps, driving the real Playwright tool set (agent/tools/playwright_tools.py).

See docs/BUILD-PLAN.md § "Why two agent roles": there is no separate Observer
agent because every tool result already returns to the model's own context as
the next turn's input — that tool-result turn IS the observation step.

Verification (phase 4) is now a formal step: for an assertion step (or any
step with an expected_outcome), agent/verifier.py::evaluate() judges the
DOM state captured by the tool loop against the expected outcome, producing
a status/confidence/explanation instead of the executor's own RESULT:
PASS/FAILED text parsing. That marker convention stays as the fallback when
there's nothing to verify (a plain action step) or when there's no DOM
state to judge against.

Reliability (also phase 4): a tool-loop exception is retried with backoff
before being treated as an infra error (status ERROR, distinct from a
verifier-judged FAILED — see docs/phase-04-verifier-and-reliability.md).

Phase 8: the tool-use loop runs against a local model via
agent/local_llm.py::run_tool_loop() instead of Anthropic's tool_runner. This
loop owns its own `messages` list directly, so the old cross-step
continuity workaround (reaching into the Anthropic SDK's private
`runner._params["messages"]`) is gone — `messages` just carries forward
naturally between run_tool_loop() calls.
"""

import asyncio
import uuid
from datetime import datetime
from typing import Callable, Optional

from agent.local_llm import (
    _DEDUPABLE_TOOLS,
    _KNOWN_TOOL_FAILURE_PREFIX,
    LocalLLMError,
    run_tool_loop,
)
from agent.tools.playwright_tools import ToolContext, build_tools
from agent.tools.redaction import redact_tool_input
from agent.tools.selector_resolver import SelectorCache
from agent.verifier import VerifierError, evaluate as verify
from app.config import INFLUENCE_BASE_URL, INFLUENCE_TEST_CAMPAIGN_ID

_KNOWN_DEAL_CLAUSE = (
    ""
    if not INFLUENCE_TEST_CAMPAIGN_ID
    else (
        "\nWhen a step needs \"an existing deal/campaign\" and doesn't name a specific one, use this "
        "known-good one directly rather than searching the list or creating a new one: navigate to "
        f"{INFLUENCE_BASE_URL}/deals/{INFLUENCE_TEST_CAMPAIGN_ID}/line-items. This is a real, stable "
        "QA fixture deal that always exists — prefer it over picking an arbitrary row from the "
        "Campaigns list or creating a throwaway campaign, since a step that says \"an existing deal\" "
        "only cares that some valid deal is being acted on, not which specific one. Only create a new "
        "deal or navigate the list manually when a step's own wording specifically requires that "
        "(e.g. testing campaign creation itself, or explicitly asking for a *different* deal).\n"
    )
)
from app.models.schemas import AgentTrace, PlannedStep, StepType, TestStepResult
from app.services.event_bus import EventBus
from app.services.store import StoreProtocol

EXECUTOR_SYSTEM_PROMPT = f"""You are the execution stage of an automated QA agent driving a real \
web browser via tools. You will be given one step at a time from a test plan.

The application under test is Influence, a DOOH/OOH advertising campaign management platform, at \
{INFLUENCE_BASE_URL}. If a navigate step's intent doesn't spell out a specific URL (e.g. "Navigate \
to the dashboard" or "Open an existing deal"), that means "go to the app" — use this exact base URL, \
never invent, guess, or construct a different hostname/path yourself. This is the only real, \
correct target; a step needing a specific page within the app should be reached by navigating here \
first and then using the app's own on-screen navigation (clicking a link/tab), not by guessing what \
that page's URL path might be.

Confirmed live: this app calls the same thing "Campaign" in the UI (nav link label, page heading, \
button text) but "deal" in the URL and backend concept (e.g. the Campaigns page itself lives at \
/deals). A step that talks about "a deal," "an existing deal," "deal activation," etc. means the \
same list you'd reach by clicking "Campaigns" in the main navigation — there is no separate "Deals" \
nav item to look for. When a step's own wording only says "deal" with no navigation instruction, \
click "Campaigns" first before looking for whatever the step describes next.
{_KNOWN_DEAL_CLAUSE}

Always respond in English, regardless of the language of any on-screen text, page content, or \
tool output you observe.

Use the available tools to carry out the step. For an assertion step, call assert_condition to \
read the current page state, then judge whether the expected outcome holds based on what it \
returns — the tool does not decide this for you.

This site's login flow legitimately redirects through a separate OAuth domain \
(auth-stg.movingwalls.com) before the login form appears — landing there after navigating to the \
main app URL is expected, correct behavior, not a failure or a wrong destination. When a plain \
navigate step (one with no expected_outcome to check) redirects there, treat it as \
RESULT: PASS — do not report it as unexpected, incorrect, or a sign something went wrong. Only an \
assertion step whose expected_outcome specifically concerns the URL should ever judge the redirect \
itself; a plain navigate step's job is just to get to the app, and the OAuth hop is simply part of \
how that happens here.

CRITICAL: you have NOT done anything until you have actually called a tool. Writing a sentence \
describing an action ("Entered the email...", "Clicked the button...", "Filled the field with...") \
is not the same as performing it, and never counts as having performed it — only a real tool call \
does. Never respond with a description of an action you have not yet actually taken via a tool \
call in this same step. If the step calls for filling a field or clicking something, your very \
next output must be a tool call, not a sentence about one.

Example — given the step "Enter the invalid email 'jeki@jeki.co'":
  WRONG (do not do this): responding immediately with "Entered an invalid email address as \
instructed.\\n\\nRESULT: PASS" and no tool call. Nothing happened on the page — this is a \
description of an intention, not an action, and it must never be treated as one.
  RIGHT: your first output for that step is a tool call — fill(selector_hint="the Email field", \
value="jeki@jeki.co") — and only after that tool call returns a result do you write a summary \
ending in RESULT: PASS or RESULT: FAILED.

If a step's instructions mention the valid test account's email or password, use the literal \
placeholder tokens {{VALID_EMAIL}} and {{VALID_PASSWORD}} as the value argument to fill() — do not \
guess, invent, or ask for real credentials. These tokens are resolved to the real values internally; \
never write out an actual email or password yourself.

You are operating autonomously with no user available to respond — never ask a question, request \
confirmation, or pause for input of any kind, EVEN IF a step's target isn't immediately obvious. If \
instructed to use {{VALID_EMAIL}} or {{VALID_PASSWORD}}, immediately call fill() with that exact \
literal token as the value — do not ask whether to use it, do not ask for a real value instead, just \
call the tool. If a step names an on-screen destination that isn't visible yet (e.g. "Navigate to \
Creatives" when you're on the dashboard), call read_page to see what's actually on screen and look \
for a matching link/tab/button yourself — do not respond with a question asking what to do or where \
something is; read_page is how you find out, not asking.

If a tool call fails, you may retry with a different selector_hint once or twice, but do not \
loop indefinitely — after a couple of failed attempts, stop and report the failure.

Once a tool call succeeds, the action it performed is done — do not call it AGAIN with the SAME \
arguments just to "confirm" it. This does NOT mean stop after one tool call: if the step describes \
more than one action (e.g. "fill the Email field with X and the Password field with Y", or "fill \
in the Name, then select the Category, then click Save"), each distinct action needs its own tool \
call with its own arguments — keep calling tools until every action the step describes has actually \
been performed, then immediately move on to reporting the result. A step is not complete just \
because the first tool call in it succeeded.

Once the step is complete (whether it succeeded or not), respond with a short summary of what \
happened, with no further tool calls, ending your response with exactly one line in this exact \
format:
RESULT: PASS
or
RESULT: FAILED
Use PASS if the step's expected outcome (when it has one) was achieved, or the action completed \
normally when there is no expected outcome to check. Use FAILED if the expected outcome was not \
met, or the step could not be completed."""

MAX_ITERATIONS_PER_STEP = 6
MAX_STEP_RETRIES = 2
RETRY_BACKOFF_BASE_SECONDS = 1.0


class ExecutorError(RuntimeError):
    """Raised when the executor can't run at all (e.g. the local model is unreachable)."""


def _new_id() -> str:
    return uuid.uuid4().hex[:12]


def _parse_verdict(final_text: str) -> bool:
    """RESULT: PASS / RESULT: FAILED, defaulting to failed if the model
    didn't follow the format — a missing verdict is not a silent pass."""
    lowered = final_text.lower()
    if "result: pass" in lowered:
        return True
    return False


async def run_executor(
    page,
    store: StoreProtocol,
    event_bus: EventBus,
    run_id: str,
    test_case_id: str,
    planned_steps: list[PlannedStep],
    log,
    on_step: Optional[Callable[[TestStepResult], None]] = None,
) -> list[TestStepResult]:
    ctx = ToolContext(
        page=page,
        store=store,
        run_id=run_id,
        test_case_id=test_case_id,
        selector_cache=SelectorCache(),
        log=log,
        event_bus=event_bus,
    )
    tools = build_tools(ctx)

    def trace(role: str, message_type: str, content: str, tokens_in=None, tokens_out=None) -> None:
        store.add_agent_trace(
            AgentTrace(
                id=_new_id(),
                run_id=run_id,
                role=role,
                message_type=message_type,
                content=content[:2000],
                token_usage_input=tokens_in,
                token_usage_output=tokens_out,
            )
        )
        event_bus.publish(
            run_id, "logs", "trace", {"role": role, "message_type": message_type, "content": content[:2000]}
        )

    messages: list[dict] = []
    results: list[TestStepResult] = []

    for planned in planned_steps:
        log(f"Step {planned.step_index}: {planned.intent}")
        trace(
            "planner",
            "reasoning",
            f"Step {planned.step_index}: {planned.intent}"
            + (f" (expect: {planned.expected_outcome})" if planned.expected_outcome else ""),
        )

        step_instruction = f"Now do step {planned.step_index}: {planned.intent}"
        if planned.expected_outcome:
            step_instruction += f"\nExpected outcome: {planned.expected_outcome}"
        messages.append({"role": "user", "content": step_instruction})

        # ctx.post_action_snapshot is meant to carry only from "the action
        # this step's own verification, if any, is checking" — i.e. survive
        # exactly one step boundary (an action step, then the assertion
        # step right after it). Captured here, BEFORE this step's own tool
        # loop runs, so it holds whatever the *previous* step's click left
        # behind; cleared immediately after so a click that happens later,
        # inside *this* step's own loop, can't be mistaken for evidence
        # belonging to a different, later assertion step.
        carried_post_action_snapshot = ctx.post_action_snapshot
        ctx.post_action_snapshot = None

        last_text = ""
        tool_loop_error: Optional[Exception] = None
        attempt = 0
        # Whether any real tool call actually executed this step — distinct
        # from the model's own narrated "RESULT: PASS" text. Observed live:
        # the model can leak a tool call as plain text (in a format that
        # doesn't match the local recovery parser in local_llm.py) instead
        # of issuing a real one, then confidently narrate "I clicked the
        # button... RESULT: PASS" even though nothing happened — the
        # browser silently never moves, which cascades into "about:blank"
        # on the next step. A plain action/navigate step making zero real
        # tool calls is never legitimately "done"; used below to override
        # the model's own self-reported verdict for exactly that case.
        made_real_tool_call = False

        is_first_step = planned.step_index == planned_steps[0].step_index
        # Computed here (not after the loop, where it lived before) so the
        # early-exit short-circuit below can use it — see its comment.
        needs_verification = planned.step_type == StepType.assertion or bool(planned.expected_outcome)

        while attempt <= MAX_STEP_RETRIES:
            attempt += 1
            tool_loop_error = None
            made_real_tool_call = False
            try:
                async for turn in run_tool_loop(
                    system=EXECUTOR_SYSTEM_PROMPT,
                    messages=messages,
                    tools=tools,
                    max_iterations=MAX_ITERATIONS_PER_STEP,
                ):
                    if turn.type == "tool_call":
                        made_real_tool_call = True
                        safe_input = redact_tool_input(turn.tool_name, dict(turn.tool_input or {}))
                        trace("executor", "tool_call", f"{turn.tool_name}({safe_input})")
                        if (
                            not needs_verification
                            and turn.tool_name in _DEDUPABLE_TOOLS
                            and not str(turn.tool_result).startswith(_KNOWN_TOOL_FAILURE_PREFIX)
                        ):
                            # A plain action step (nothing to verify) whose
                            # tool call just succeeded doesn't need the
                            # model to spend a second real LLM call
                            # restating "RESULT: PASS" — that verdict is
                            # already fully determined by the tool's own
                            # return value (see the same
                            # _KNOWN_TOOL_FAILURE_PREFIX check the final
                            # `else` branch below uses for a step where
                            # this short-circuit DIDN'T fire, e.g. a
                            # verification step). Breaking out of this
                            # async generator here stops run_tool_loop
                            # before it ever makes that second network
                            # call — halving the LLM calls per plain
                            # action step. summary_text is deliberately
                            # left empty; the `else` branch's `has_result_
                            # marker` check treats "no marker at all" as
                            # passed, so an empty summary here still
                            # correctly resolves to RESULT: PASS below.
                            last_text = ""
                            break
                    elif turn.type == "text" and turn.text:
                        last_text = turn.text
                        trace("executor", "reasoning", turn.text, turn.tokens_in, turn.tokens_out)

                if (
                    not made_real_tool_call
                    and is_first_step
                    and planned.step_type in (StepType.action, StepType.navigate)
                    and attempt == 1
                ):
                    # A bad first roll (model narrates instead of calling a
                    # tool) currently kills the whole case with zero recovery
                    # attempt, unlike mid-run steps which at least inherit
                    # prior successful turns as context to anchor on.
                    # Observed live (AD_LG_03): same case/model/prompt, run
                    # at a different moment, produced a garbled refusal on
                    # step 1 instead of a real navigate() call, then passed
                    # cleanly on every later step of other cases in the same
                    # run — genuine per-call non-determinism, not a
                    # systemic bug. One retry gives the model a second roll
                    # before the case is written off.
                    log(f"Step {planned.step_index}: no real tool call on first attempt — retrying once...")
                    continue

                break  # tool loop completed without raising — no retry needed

            except LocalLLMError as exc:
                tool_loop_error = exc
                if attempt <= MAX_STEP_RETRIES:
                    backoff = RETRY_BACKOFF_BASE_SECONDS * (2 ** (attempt - 1))
                    log(
                        f"Step {planned.step_index} raised {exc} (attempt {attempt}/"
                        f"{MAX_STEP_RETRIES + 1}) — retrying in {backoff:.0f}s..."
                    )
                    await asyncio.sleep(backoff)

        if tool_loop_error is not None:
            # Exhausted retries — this is a tooling/infra problem (local
            # model unreachable, timeout), not a judged-false assertion, so
            # it's an ERROR, not a FAILED. See
            # docs/phase-04-verifier-and-reliability.md.
            log(f"✗ Step {planned.step_index} errored after {attempt} attempt(s): {tool_loop_error}")
            step_result = TestStepResult(
                step_description=planned.intent,
                status="ERROR",
                detail=f"Executor error after {attempt} attempt(s): {tool_loop_error}",
            )
            results.append(step_result)
            if on_step:
                on_step(step_result)
            results += _skip_remaining(planned_steps, planned.step_index)
            return results

        summary_text = last_text
        trace("executor", "final_answer", summary_text)

        if needs_verification and not ctx.last_snapshot:
            # The model answered without ever calling assert_condition/
            # read_page — a known reliability gap with the local model (see
            # docs/phase-08-local-llm-migration.md), which otherwise leaves
            # the Verifier nothing to judge and silently defaults to FAILED
            # with no explanation. Force one real DOM read here so every
            # assertion step always gets judged against actual page state,
            # never the model's own unverified say-so.
            log(f"Step {planned.step_index}: model didn't read the page before answering — forcing a read now.")
            assert_condition_tool = next((t for t in tools if t.name == "assert_condition"), None)
            if assert_condition_tool:
                await assert_condition_tool(description=planned.expected_outcome or planned.intent)

        if needs_verification and ctx.last_snapshot:
            try:
                verdict = await verify(
                    planned.expected_outcome or planned.intent,
                    ctx.last_snapshot,
                    post_action_dom_state=carried_post_action_snapshot,
                    test_case_id=test_case_id,
                )
                passed = verdict.status == "passed"
                detail = verdict.explanation
                confidence = verdict.confidence
                trace(
                    "verifier",
                    "reasoning",
                    f"status={verdict.status} confidence={verdict.confidence:.2f}: {verdict.explanation}",
                )
                if ctx.steps:
                    ctx.steps[-1].confidence = confidence
                log(
                    f"{'✓' if passed else '✗'} Step {planned.step_index} "
                    f"(verifier, confidence={confidence:.2f}): {detail[:200]}"
                )
            except VerifierError as exc:
                log(f"✗ Step {planned.step_index} verification errored: {exc}")
                step_result = TestStepResult(
                    step_description=planned.intent,
                    status="ERROR",
                    detail=f"Verifier error: {exc}",
                )
                results.append(step_result)
                if on_step:
                    on_step(step_result)
                results += _skip_remaining(planned_steps, planned.step_index)
                return results
        elif needs_verification:
            # No DOM state was captured to verify against (e.g. the model
            # never called assert_condition/read_page) — fall back to the
            # executor's own RESULT: PASS/FAILED marker rather than guessing.
            passed = _parse_verdict(summary_text)
            detail = summary_text.strip() or None
            log(f"{'✓' if passed else '✗'} Step {planned.step_index}: {(detail or '')[:200]}")
        elif planned.step_type in (StepType.action, StepType.navigate) and not made_real_tool_call:
            # The model's text claims the action happened (often ending
            # "RESULT: PASS"), but no real tool call was ever executed —
            # a leaked/malformed tool call that the recovery parser
            # couldn't salvage. An action/navigate step making zero real
            # tool calls never actually did anything, regardless of what
            # the model narrated; trusting that text here is exactly what
            # let a no-op step cascade into "about:blank" on the next one.
            passed = False
            detail = (
                "The model described this action in text but never issued a real tool call, "
                f"so nothing actually happened on the page. Raw response: {summary_text.strip()[:300]}"
            )
            log(f"✗ Step {planned.step_index}: no real tool call was made — {detail[:200]}")
        else:
            # A real tool call happened, but a real tool call "happening"
            # isn't the same as it succeeding — click()/fill() can, and
            # do, return a failure string (e.g. "Could not find an
            # element matching...") without raising an exception, and the
            # model can correctly recognize that and end with
            # RESULT: FAILED. Trusting the tool call's mere occurrence
            # here (the previous behavior) meant a step whose own
            # narration said "the button could not be found... RESULT:
            # FAILED" still got recorded as OK and rolled the whole run
            # up to passed. Deferring to the model's own marker — same
            # parser the needs_verification branch already uses — closes
            # that gap; a step with no RESULT: marker at all still
            # defaults to passed, matching the old behavior for the
            # common case where the model just doesn't bother stating one.
            has_result_marker = "result: pass" in summary_text.lower() or "result: failed" in summary_text.lower()
            passed = _parse_verdict(summary_text) if has_result_marker else True
            detail = summary_text.strip() or None
            log(f"{'✓' if passed else '✗'} Step {planned.step_index}: {(detail or '')[:200]}")

        step_result = TestStepResult(
            step_description=planned.intent,
            status="OK" if passed else "FAILED",
            detail=detail,
        )
        results.append(step_result)
        if on_step:
            on_step(step_result)

        if not passed:
            results += _skip_remaining(planned_steps, planned.step_index)
            break

    return results


def _skip_remaining(planned_steps: list[PlannedStep], up_to_step_index: int) -> list[TestStepResult]:
    return [
        TestStepResult(
            step_description=step.intent,
            status="FAILED",
            detail="Skipped — a prior step failed or errored.",
        )
        for step in planned_steps
        if step.step_index > up_to_step_index
    ]
