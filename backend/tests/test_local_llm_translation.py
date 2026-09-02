"""
Regression coverage for agent/local_llm.py's non-English-leak detection
and translation retry — added after live runs kept showing Thai/Chinese
text in step details despite an explicit "always respond in English"
system-prompt instruction in every prompt that uses these. That
instruction alone wasn't reliable enough on the 14B model, so this is a
detect-and-fix-after-the-fact safety net: cosmetic only, never touches
the actual pass/fail logic (RESULT: PASS/FAILED, tool-call arguments).
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from agent.local_llm import _looks_leaked, _translate_to_english, local_tool, run_tool_loop


def _text_response(text: str):
    message = SimpleNamespace(content=text, tool_calls=None)
    return SimpleNamespace(message=message, prompt_eval_count=10, eval_count=5)


def test_looks_leaked_flags_majority_thai_text():
    assert _looks_leaked("คณะกรรมการทำงานได้กรอกอีเมลเป็น jeki@jeki.co") is True


def test_looks_leaked_flags_majority_chinese_text():
    assert _looks_leaked("已成功在Email字段中填写了{{VALID_EMAIL}}") is True


def test_looks_leaked_flags_majority_cyrillic_text():
    """Regression test: the model was observed leaking Cyrillic/Ukrainian
    text too (not just Thai/Chinese), which the original detector — Thai
    + CJK ranges only — completely missed."""
    assert _looks_leaked("Алексаній виконуватиму крок, як вказано") is True


def test_looks_leaked_ignores_plain_english_text():
    assert _looks_leaked("The email was entered successfully.\nRESULT: PASS") is False


def test_looks_leaked_ignores_empty_text():
    assert _looks_leaked("") is False
    assert _looks_leaked(None) is False


def test_looks_leaked_ignores_a_single_stray_non_latin_character():
    """A tiny fraction of non-Latin characters (e.g. one stray token)
    shouldn't trigger a translation retry — only genuinely majority-
    non-English text should."""
    assert _looks_leaked("The café's name in Thai is written as ก somewhere in docs.") is False


async def test_translate_to_english_returns_the_translated_text():
    mock_client = AsyncMock()
    mock_client.chat.return_value = _text_response("The email was entered successfully.")

    with patch("agent.local_llm._client", return_value=mock_client):
        result = await _translate_to_english("已成功输入电子邮件。")

    assert result == "The email was entered successfully."


async def test_translate_to_english_retries_when_first_attempt_still_leaks():
    """
    Regression test: observed live — asking this same model to "translate
    into English" is exactly the kind of instruction it doesn't reliably
    follow (that's the whole reason this function exists), so a single
    blind translation attempt can itself come back still partially in the
    original script. A second, more insistent attempt should be made
    before giving up.
    """
    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        # First attempt: still leaked (translated most of it, echoed a
        # trailing Thai fragment).
        _text_response("The email was entered successfully. คณะกรรมการ"),
        # Second, sharper attempt: fully English.
        _text_response("The email was entered successfully."),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        result = await _translate_to_english("已成功输入电子邮件。คณะกรรมการ")

    assert result == "The email was entered successfully."
    assert mock_client.chat.call_count == 2


async def test_translate_to_english_gives_up_after_two_attempts():
    """If it's STILL leaked after the sharper second attempt, return
    whatever came back rather than retrying indefinitely against an
    already-struggling model."""
    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _text_response("Still มีภาษาไทย here"),
        _text_response("Still มีภาษาไทย here too"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        result = await _translate_to_english("มีภาษาไทย original")

    assert result == "Still มีภาษาไทย here too"
    assert mock_client.chat.call_count == 2


async def test_translate_to_english_falls_back_to_original_on_failure():
    """Best-effort: a translation call failing must never break the
    calling step — the original (still-leaked) text is better than
    raising and failing an otherwise-successful step over a cosmetic
    concern."""
    mock_client = AsyncMock()
    mock_client.chat.side_effect = ConnectionError("local model unreachable")

    with patch("agent.local_llm._client", return_value=mock_client):
        result = await _translate_to_english("已成功输入电子邮件。")

    assert result == "已成功输入电子邮件。"


async def test_translate_to_english_falls_back_when_response_is_empty():
    mock_client = AsyncMock()
    mock_client.chat.return_value = _text_response("")

    with patch("agent.local_llm._client", return_value=mock_client):
        result = await _translate_to_english("已成功输入电子邮件。")

    assert result == "已成功输入电子邮件。"


async def test_run_tool_loop_translates_a_leaked_final_answer():
    """
    Distinct from the leaked-tool-call regression tests: this covers a
    plain final-answer turn (no tool call at all, leaked or otherwise)
    that comes back in non-English — the third of the three yield sites
    in run_tool_loop() that needed the same guard.
    """

    @local_tool
    async def click(selector_hint: str) -> str:
        """Click an element on the page."""
        return f"Clicked {selector_hint}"

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [
        _text_response("已成功点击了登录按钮。\n\nRESULT: PASS"),
        _text_response("The Login button has been clicked.\n\nRESULT: PASS"),
    ]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[click], max_iterations=3
            )
        ]

    assert len(turns) == 1
    assert turns[0].type == "text"
    assert turns[0].text == "The Login button has been clicked.\n\nRESULT: PASS"
    assert "登录" not in turns[0].text


async def test_run_tool_loop_does_not_translate_english_final_answers():
    """The common case (plain English response) must not pay for an extra
    translation call at all."""

    @local_tool
    async def click(selector_hint: str) -> str:
        """Click an element on the page."""
        return f"Clicked {selector_hint}"

    mock_client = AsyncMock()
    mock_client.chat.side_effect = [_text_response("The Login button has been clicked.\nRESULT: PASS")]

    with patch("agent.local_llm._client", return_value=mock_client):
        turns = [
            turn
            async for turn in run_tool_loop(
                system="system prompt", messages=[], tools=[click], max_iterations=3
            )
        ]

    assert mock_client.chat.call_count == 1  # no extra translation call
    assert turns[0].text == "The Login button has been clicked.\nRESULT: PASS"
