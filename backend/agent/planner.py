"""
Planner — one structured-output local-model call that turns a test case's
natural-language description into an ordered list of PlannedSteps.

Not a persistent process or a tool-use loop: see docs/BUILD-PLAN.md
§ "Why two agent roles" for why this is a single call, with the Executor
(agent/executor.py) doing all the turn-by-turn work.
"""

from pydantic import BaseModel

from agent.local_llm import LocalLLMError, _looks_leaked, _translate_to_english, structured_chat
from app.models.schemas import PlannedStep

PLANNER_SYSTEM_PROMPT = """You are the planning stage of an automated QA agent for Influence, a \
DOOH/OOH advertising campaign management web application.

Given a test case written in natural language, break it into an ordered list of concrete UI \
steps a browser-automation executor can carry out one at a time.

Rules:
- Each step is a single action (navigate, click, fill, select) or a single assertion — never a \
compound instruction like "log in and check the dashboard".
- Write `intent` as a short, specific instruction referring to a real on-screen element or \
destination, e.g. "Click the Sign In button", not "log in".
- Use step_type "navigate" for URL navigation, "action" for clicks/fills/selects, "assertion" \
for a check against expected behavior, and "observation" only when the executor genuinely needs \
to read the page before it can decide what to do next (don't add gratuitous observation steps).
- Only set `expected_outcome` on "assertion" steps. Leave it empty/null for "navigate", "action", \
and "observation" steps — a non-empty expected_outcome makes that step get verified, so setting \
one on a plain navigation or action step turns ordinary setup into a check that can fail for the \
wrong reason.
- Never assert on an exact URL after navigating to "the login page" or "the app" — this site's \
login flow legitimately redirects through a separate OAuth domain \
(auth-stg.movingwalls.com) before the login form appears, which is expected behavior, not a \
failure. IF the test case's own description explicitly asks you to confirm the login page loaded \
(and only then — do not add this on your own for a case that just goes straight from navigating to \
filling in fields), phrase that assertion around what's visibly on screen (e.g. "the email and \
password fields are visible"), never around the URL matching a specific string.
- Do not invent assertion, wait, or "confirm X is visible/ready" steps that the test case's own \
description doesn't ask for, even if they seem like reasonable intermediate checks — extra steps \
are extra chances to fail on something the test was never actually trying to verify. If the \
description just says to fill a field, plan a fill step, not a "wait for the field to be visible" \
step first. Only plan the assertions/checks the description explicitly describes. This applies \
even when a nearby rule in this prompt gives an example of how to PHRASE a login-page-visibility \
assertion (see the URL rule above) — that example only tells you HOW to word such a step IF the \
description asks for one; it is never itself a reason to add one. Concretely: a description like \
"leave the Email and Password fields blank, then click Sign In" gets a navigate step, a click step, \
and the assertion(s) it explicitly names — NOT an extra "the email and password fields are visible" \
step first, since the description never asked to confirm that.
- Never plan a step for "leave field X blank," "don't fill in Y," or similar — there is no tool \
call for doing nothing, so a planned step like that has no real action for the executor to take and \
will incorrectly get marked as a failure (no tool call was made). A field that's simply never \
mentioned in a fill step is already blank; that's the correct way to represent "leave it blank" — \
omit the step entirely rather than planning one for it. Concretely: "Leave the Campaign Name field \
blank, then click Create Campaign" gets ONE step — "Click Create Campaign" — never a preceding \
"Leave the Campaign Name field blank" step. This applies no matter how the description phrases the \
instruction to skip a field (leave blank, don't enter, omit, skip) — none of these ever become their \
own planned step.
- If the test case gives you a literal URL to navigate to, copy it into the navigate step's \
`intent` character-for-character — never paraphrase, "clean up", or substitute a different-looking \
URL (e.g. a generic *.example.com placeholder) even if it looks more like a typical URL. The URL \
in the test case is the real target system; treat it as an opaque string to copy, not something to \
normalize.
- For assertion steps, describe exactly what should be true in `expected_outcome`.
- Assume the executor is already logged in unless the test case explicitly says otherwise — do \
not add a login step unless the description asks for one.
- Always write in English, regardless of the language of the test case description or any \
on-screen text it references.
"""


class PlannedStepList(BaseModel):
    steps: list[PlannedStep]


class PlannerError(RuntimeError):
    """Raised when the planner can't produce a usable plan."""


# Process-wide (not per-run, unlike agent/tools/selector_resolver.py's
# SelectorCache, which is deliberately instantiated fresh per run_executor()
# call) cache of (description, page_context) -> the plan the Planner
# produced for it last time. A test case's YAML description never changes
# between separate runs of the same case, so re-paying a full LLM call
# (5-10+s) to regenerate an identical plan on every run is pure waste —
# this makes every run after the first of a given, unchanged case skip the
# Planner call entirely. Keyed on the exact combined input (not just
# test_case_id) since page_context can legitimately vary the correct plan;
# runner.py's real call site never passes page_context, so in practice this
# always hits on repeat runs of the same case.
_plan_cache: dict[tuple[str, str], list[PlannedStep]] = {}


async def plan_test_case(description: str, page_context: str = "") -> list[PlannedStep]:
    cache_key = (description, page_context)
    cached = _plan_cache.get(cache_key)
    if cached is not None:
        # Return copies, not the cached PlannedStep objects themselves —
        # callers (e.g. executor.py) may mutate a step's fields in place
        # (translation, hallucinated-URL fixups), which must never leak
        # back into the cached plan and corrupt it for the next run.
        return [step.model_copy() for step in cached]

    user_message = f"Test case:\n{description}"
    if page_context:
        user_message += f"\n\nPage context:\n{page_context}"

    try:
        parsed = await structured_chat(
            system=PLANNER_SYSTEM_PROMPT,
            user_message=user_message,
            output_model=PlannedStepList,
        )
    except LocalLLMError as exc:
        raise PlannerError(f"Planner call failed: {exc}") from exc

    steps = parsed.steps
    if not steps:
        raise PlannerError("Planner returned an empty plan.")

    # `intent` is displayed as the step description in the UI, and
    # `expected_outcome` becomes part of the Verifier's user_message — the
    # JSON schema constrains structure, not language, so either can still
    # leak non-English text despite the prompt's explicit instruction.
    for step in steps:
        if _looks_leaked(step.intent):
            step.intent = await _translate_to_english(step.intent)
        if step.expected_outcome and _looks_leaked(step.expected_outcome):
            step.expected_outcome = await _translate_to_english(step.expected_outcome)

    # Cache defensive copies, not `steps` itself — this same list is about
    # to be returned to and potentially mutated in place by this call's own
    # caller (e.g. runner.py's _fix_hallucinated_urls), which must not
    # silently corrupt what every future run of this case reads back out.
    _plan_cache[cache_key] = [step.model_copy() for step in steps]
    return steps
