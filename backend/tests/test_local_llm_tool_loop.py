"""
Regression coverage for agent/local_llm.py::run_tool_loop() calling a real
tool function with model-supplied arguments — the layer above (executor.py,
chat_service.py) always mocks run_tool_loop() itself, so none of that
coverage ever exercised the actual `await tool(**tool_input)` call this
file tests.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent.local_llm import local_tool, run_tool_loop


def _tool_call_response(tool_name: str, arguments: dict):
    """Mimics the shape of ollama.AsyncClient.chat()'s return value when the
    model requests a tool call."""
    function = SimpleNamespace(name=tool_name, arguments=arguments)
    tool_call = SimpleNamespace(function=function)
    message = SimpleNamespace(content=None, tool_calls=[tool_call])
    return SimpleNamespace(message=message, prompt_eval_count=10, eval_count=5)


def _text_response(text: str):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(message=message, prompt_eval_count=10, eval_count=5)


async def test_run_tool_loop_recovers_from_a_malformed_tool_call():
    """
    Regression test: the model can hallucinate an argument that doesn't
    match a tool's real signature (e.g. calling navigate(description=...)
    when it only accepts url) — this previously raised an unhandled
    TypeError that crashed the entire test run instead of just failing
    that one tool call. The loop should feed the error back as a normal
    tool result and let the model try again.
    """

    @local_tool
    async def navigate(url: str) -> str:
        """Navigate the browser to a URL."""
        return f"Navigated to {url}"

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _tool_call_response("navigate", {"description": "the login page"}),
        _text_response("Gave up.\nRESULT: FAILED"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[navigate], max_iterations=3
            )
        ]

    tool_call_turns = [t for t in turns if t.type == "tool_call"]
    assert len(tool_call_turns) == 1
    assert "Tool call failed" in tool_call_turns[0].tool_result
    assert "unexpected keyword argument" in tool_call_turns[0].tool_result

    final_turns = [t for t in turns if t.type == "text"]
    assert final_turns[-1].text == "Gave up.\nRESULT: FAILED"


async def test_run_tool_loop_calls_a_well_formed_tool_normally():
    @local_tool
    async def navigate(url: str) -> str:
        """Navigate the browser to a URL."""
        return f"Navigated to {url}"

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _tool_call_response("navigate", {"url": "https://example.com"}),
        _text_response("Done.\nRESULT: PASS"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[navigate], max_iterations=3
            )
        ]

    tool_call_turns = [t for t in turns if t.type == "tool_call"]
    assert tool_call_turns[0].tool_result == "Navigated to https://example.com"


async def test_run_tool_loop_recovers_a_tool_call_leaked_as_plain_text():
    """
    Regression test: observed live with the 14B model — instead of issuing
    a real structured tool call, it sometimes narrates one as JSON prose in
    msg.content (msg.tool_calls stays empty). Previously this silently
    ended the step as a "final answer" with the described action never
    actually happening (e.g. the model says "I clicked Login" but the real
    click() function is never called, leaving the browser exactly where it
    was). The loop should recover and dispatch the tool call anyway.
    """

    @local_tool
    async def click(selector_hint: str) -> str:
        """Click an element on the page."""
        return f"Clicked {selector_hint}"

    leaked_text = (
        "I will now click the login button to proceed.\n\n"
        '{\n    "name": "click",\n    "arguments": {\n        '
        '"selector_hint": "the Login button"\n    }\n}'
    )

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _text_response(leaked_text),
        _text_response("Clicked the button.\nRESULT: PASS"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[click], max_iterations=3
            )
        ]

    tool_call_turns = [t for t in turns if t.type == "tool_call"]
    assert len(tool_call_turns) == 1
    assert tool_call_turns[0].tool_name == "click"
    assert tool_call_turns[0].tool_result == "Clicked the Login button"

    text_turns = [t for t in turns if t.type == "text"]
    assert text_turns[-1].text == "Clicked the button.\nRESULT: PASS"


async def test_run_tool_loop_recovers_a_leaked_tool_call_with_no_wrapping_braces():
    """
    Regression test for a second observed malformed shape: the model wraps
    the leaked call in a broken chat-template token instead of real JSON
    object braces — e.g. "ICA\\n\"name\": \"click\", \"arguments\": {...}\\n
    ичество" with no outer {...} at all. The original recovery regex
    required a wrapping {...} and missed this; brace-balancing from the
    "arguments": { onward (rather than a fixed-shape regex) recovers it as
    long as the arguments JSON itself is well-formed.
    """

    @local_tool
    async def click(selector_hint: str) -> str:
        """Click an element on the page."""
        return f"Clicked {selector_hint}"

    leaked_text = (
        "คณะกรรมการทำงานของคุณต้องการให้คลิกที่ปุ่ม Login\n\n"
        "ICA    \n"
        '"name": "click", \n'
        '"arguments": {"selector_hint": "the Login button"}\n'
        "ичество\n"
        "คณะกรรมการทำงานได้ทำการคลิกปุ่ม Login แล้ว\n\n"
        "RESULT: PASS"
    )

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _text_response(leaked_text),
        # The leaked text is mostly Thai, so it also triggers the leaked
        # -text-language guard (agent/local_llm.py::_looks_leaked) — a
        # follow-up translation call comes before the tool loop's own
        # next turn.
        _text_response("I will click the Login button.\n\nRESULT: PASS"),
        _text_response("Clicked the button.\nRESULT: PASS"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[click], max_iterations=3
            )
        ]

    tool_call_turns = [t for t in turns if t.type == "tool_call"]
    assert len(tool_call_turns) == 1
    assert tool_call_turns[0].tool_name == "click"
    assert tool_call_turns[0].tool_result == "Clicked the Login button"

    text_turns = [t for t in turns if t.type == "text"]
    # The leaked Thai text was translated before being surfaced.
    assert "ปุ่ม" not in text_turns[0].text
    assert text_turns[0].text == "I will click the Login button.\n\nRESULT: PASS"


async def test_run_tool_loop_recovers_a_bare_tool_name_colon_leaked_call():
    """
    Regression test for a third observed malformed shape: no "name"/
    "arguments" wrapper keys at all, just `<tool_name>: {...}` — e.g.
    `fill: { "selector_hint": "the Email field", "value": "jeki@jeki.co" }`
    — sometimes alongside a fully hallucinated fake <tool_response>/USER/
    ADMIN conversation the model role-plays instead of making a real call.
    Recovery here is anchored on the actual tool names available in this
    loop (passed through to _extract_leaked_tool_call), not an arbitrary
    \\w+, since a bare `word: {` pattern would false-positive on ordinary
    prose far too easily otherwise.
    """

    @local_tool
    async def fill(selector_hint: str, value: str) -> str:
        """Fill a text input."""
        return f"Filled {selector_hint} with {value}"

    leaked_text = (
        "คณะกรรมการทำงานของคุณอาจต้องการให้ฉันกรอกอีเมลที่ไม่ถูกต้องในฟิลด์อีเมล\n\n"
        'fill: { "selector_hint": "the Email field", "value": "jeki@jeki.co" }\n\n'
        "USER <tool_response>\n"
        'Filled the input with selector_hint="the Email field" and value="jeki@jeki.co".\n'
        "</tool_response>\n\n"
        "ADMIN The invalid email 'jeki@jeki.co' has been entered.\n\nRESULT: PASS"
    )

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _text_response(leaked_text),
        # Also mostly Thai — triggers the same translation guard as above.
        _text_response("I will fill the invalid email in the Email field.\n\nRESULT: PASS"),
        _text_response("Entered the email.\nRESULT: PASS"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[fill], max_iterations=3
            )
        ]

    tool_call_turns = [t for t in turns if t.type == "tool_call"]
    assert len(tool_call_turns) == 1
    assert tool_call_turns[0].tool_name == "fill"
    assert tool_call_turns[0].tool_input == {
        "selector_hint": "the Email field",
        "value": "jeki@jeki.co",
    }
    assert tool_call_turns[0].tool_result == "Filled the Email field with jeki@jeki.co"

    text_turns = [t for t in turns if t.type == "text"]
    assert "คณะกรรมการ" not in text_turns[0].text


async def test_run_tool_loop_treats_unmatched_leaked_json_as_plain_text():
    """A text response that merely contains JSON-shaped content but doesn't
    name a real tool shouldn't be treated as a leaked tool call — it should
    just flow through as a normal final answer."""

    @local_tool
    async def click(selector_hint: str) -> str:
        """Click an element on the page."""
        return f"Clicked {selector_hint}"

    text = 'Here is some data: {"name": "not_a_real_tool", "arguments": {"x": 1}}'

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [_text_response(text)]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[click], max_iterations=3
            )
        ]

    assert len(turns) == 1
    assert turns[0].type == "text"
    assert turns[0].text == text


async def test_run_tool_loop_does_not_re_dispatch_an_identical_leaked_call_forever():
    """
    Regression test: observed live — the model can keep re-narrating (and
    re-leaking) the SAME already-completed action turn after turn instead
    of ever producing a clean final answer, e.g. "The Email field was
    filled...\\n\\n{leaked fill call}\\n\\nRESULT: PASS" repeated 6 times
    in a row before max_iterations gave up (each repeat re-ran the fill).
    Once a given (tool_name, arguments) pair has been recovered once this
    step, an identical repeat must fall through to the final-answer
    branch instead of re-dispatching the same action again.
    """

    @local_tool
    async def fill(selector_hint: str, value: str) -> str:
        """Fill a text input."""
        return f"Filled {selector_hint} with {value}"

    leaked_and_repeated_text = (
        'The Email field was filled with the placeholder {{VALID_EMAIL}}.\n\n'
        '{\n  "name": "fill",\n  "arguments": {\n    "selector_hint": "the Email field",\n    '
        '"value": "{{VALID_EMAIL}}"\n  }\n}\nRESULT: PASS'
    )

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _text_response(leaked_and_repeated_text),
        _text_response(leaked_and_repeated_text),  # model repeats itself verbatim
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[fill], max_iterations=5
            )
        ]

    tool_call_turns = [t for t in turns if t.type == "tool_call"]
    # Dispatched exactly once, not once per turn.
    assert len(tool_call_turns) == 1
    assert tool_call_turns[0].tool_name == "fill"

    # The loop ends on the second (repeated) turn via the final-answer
    # path rather than burning through all 5 iterations.
    assert mock_client.chat.call_count == 2
    text_turns = [t for t in turns if t.type == "text"]
    assert text_turns[-1].text == leaked_and_repeated_text


async def test_run_tool_loop_does_not_re_execute_an_identical_real_tool_call():
    """
    Regression test: observed live (AD_LG_04) — the model can issue a
    second, genuine, STRUCTURED tool call (msg.tool_calls populated, not a
    leaked/malformed text response — a different failure mode than the
    leaked-call dedup test above) identical to one that already succeeded
    earlier in the same step, even though the first call's result was a
    clear, unambiguous success message and the system prompt explicitly
    says not to repeat a successful action. Re-running fill()/click()
    against the live page a second time is redundant at best and risks a
    double-submit at worst — the second identical call must be answered
    from the cached result without invoking the real tool function again.
    """
    call_count = {"n": 0}

    @local_tool
    async def fill(selector_hint: str, value: str) -> str:
        """Fill a text input."""
        call_count["n"] += 1
        return f"Filled {selector_hint!r} with {value!r}"

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _tool_call_response("fill", {"selector_hint": "the Email field", "value": "{{VALID_EMAIL}}"}),
        _tool_call_response("fill", {"selector_hint": "the Email field", "value": "{{VALID_EMAIL}}"}),
        _text_response("Done.\nRESULT: PASS"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[fill], max_iterations=5
            )
        ]

    assert call_count["n"] == 1  # the real tool function only actually ran once
    tool_call_turns = [t for t in turns if t.type == "tool_call"]
    assert len(tool_call_turns) == 2  # both turns still surfaced to the caller/trace
    assert tool_call_turns[0].tool_result == tool_call_turns[1].tool_result


async def test_run_tool_loop_dedup_does_not_apply_to_read_only_tools():
    """
    assert_condition/read_page must always execute fresh even with
    identical args — the same description can legitimately need a new
    read if the page changed between calls (e.g. a toast that appeared
    since the first read). Caching those risks a real correctness bug to
    fix a performance one.
    """
    call_count = {"n": 0}

    @local_tool
    async def assert_condition(description: str) -> str:
        """Read the page and judge a condition."""
        call_count["n"] += 1
        return f"read #{call_count['n']}"

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _tool_call_response("assert_condition", {"description": "an error is shown"}),
        _tool_call_response("assert_condition", {"description": "an error is shown"}),
        _text_response("Done.\nRESULT: PASS"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[assert_condition], max_iterations=5
            )
        ]

    assert call_count["n"] == 2  # executed fresh both times, not deduped
    tool_call_turns = [t for t in turns if t.type == "tool_call"]
    assert tool_call_turns[0].tool_result == "read #1"
    assert tool_call_turns[1].tool_result == "read #2"


async def test_run_tool_loop_does_not_dedup_a_call_that_previously_failed():
    """
    A failed call must never be treated as 'already done' — a legitimate
    retry with the same args after a failure should still actually run.
    Real Playwright-backed tools (click/fill in agent/tools/playwright_tools.py)
    signal a resolver failure as a plain return-value string (e.g. "Could
    not find an element matching..."), not a raised exception — matching
    that real shape here, since a raised exception is a different code
    path (only TypeError from a bad argument shape is caught in the loop;
    an arbitrary exception would propagate uncaught).
    """
    call_count = {"n": 0}

    @local_tool
    async def click(selector_hint: str) -> str:
        """Click an element."""
        call_count["n"] += 1
        if call_count["n"] == 1:
            return "Could not find an element matching the hint."
        return f"Clicked {selector_hint!r}"

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _tool_call_response("click", {"selector_hint": "Sign In"}),
        _tool_call_response("click", {"selector_hint": "Sign In"}),
        _text_response("Done.\nRESULT: PASS"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[click], max_iterations=5
            )
        ]

    assert call_count["n"] == 2
    tool_call_turns = [t for t in turns if t.type == "tool_call"]
    assert tool_call_turns[0].tool_result == "Could not find an element matching the hint."
    assert tool_call_turns[1].tool_result == "Clicked 'Sign In'"


async def test_run_tool_loop_does_not_mistake_ordinary_prose_for_a_bare_leaked_call():
    """
    The bare `<tool_name>: {` pattern is the broadest of the three leaked-
    call shapes recognized, so it's the one most likely to false-positive.
    Ordinary text that happens to use a real tool's name as a word,
    followed by a colon, with no actual JSON arguments object right after
    it, must not be mistaken for a leaked call.
    """

    @local_tool
    async def fill(selector_hint: str, value: str) -> str:
        """Fill a text input."""
        return f"Filled {selector_hint} with {value}"

    text = "Next I need to fill: the form still needs a value in the Email field before I can continue."

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [_text_response(text)]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[fill], max_iterations=3
            )
        ]

    assert len(turns) == 1
    assert turns[0].type == "text"
    assert turns[0].text == text
