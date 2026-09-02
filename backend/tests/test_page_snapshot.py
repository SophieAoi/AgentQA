"""
Regression coverage for agent/tools/playwright_tools.py::_page_snapshot()
including the page <title> — observed live: an assertion like "verify the
page title is present" could never pass, since the accessibility-tree
snapshot aria_snapshot() returns doesn't include <title> at all, and
nothing else was adding it to what the Verifier gets to judge against
(same class of gap the existing "Current URL: ..." prefix already solved
for URL-based assertions).
"""

from pathlib import Path

from agent.browser.session import BrowserSession
from agent.tools.playwright_tools import _page_snapshot, _split_merged_required_text

FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
INTERACTIVE_PAGE = f"file://{FIXTURES_DIR / 'interactive_page.html'}"


async def test_page_snapshot_includes_the_real_page_title():
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)
        snapshot = await _page_snapshot(session.page)

    assert "Page title: Interactive Fixture" in snapshot


async def test_page_snapshot_still_includes_the_current_url():
    """Guards the pre-existing URL prefix isn't accidentally dropped or
    reordered by the title addition."""
    async with BrowserSession(headless=True) as session:
        await session.goto(INTERACTIVE_PAGE)
        snapshot = await _page_snapshot(session.page)

    assert "Current URL:" in snapshot
    assert "interactive_page.html" in snapshot


# _split_merged_required_text() — see docs/phase-14-blank-field-steps-and-
# merged-text-bias.md and the function's own docstring for the full story:
# Playwright's aria_snapshot() concatenates adjacent unlabeled text nodes
# onto one "text:" line with no separator, which repeatedly caused the
# Verifier to misjudge a genuinely-present validation message as absent
# because it appeared "merged" with the next field's label. Three rounds
# of verifier-prompt wording didn't fix it; a first code-level fix was
# shipped and reverted in the same phase after it also split a
# legitimate, unmerged label. These tests pin the exact real-world cases
# (both the ones that must split and the ones that must NOT) so neither
# regression can recur silently.


def test_splits_a_validation_message_merged_with_the_next_labels_asterisk():
    """The exact bug this function exists to fix — confirmed live on the
    real login form's blank-field validation state."""
    line = "- text: Email / Username is required Password *"
    result = _split_merged_required_text(line)
    assert result == "- text: Email / Username is required\n- text: Password *"


def test_splits_two_required_messages_merged_without_a_trailing_asterisk():
    """Same merge shape, confirmed live on the Line Item form's Flight
    Dates fields — no asterisk this time, so the split must key off the
    second phrase also containing 'is required', not just off '*'."""
    line = "- text: Start date is required End date is required"
    result = _split_merged_required_text(line)
    assert result == "- text: Start date is required\n- text: End date is required"


def test_does_not_split_a_single_unmerged_label_ending_in_asterisk():
    """Regression guard for the exact bug that got the first attempt at
    this fix reverted: a label ending in ' *' that ISN'T actually two
    merged phrases must be left completely alone."""
    line = "- text: Email / Username *"
    assert _split_merged_required_text(line) == line


def test_does_not_split_a_label_merged_with_its_own_helper_text():
    """A label followed by its own descriptive helper text (not a second
    field's label) must not be split — there's only one real thing here,
    the label plus its own explanation, not two unrelated elements."""
    line = (
        "- text: Line Item Name A descriptive name for this line item. "
        "It appears on the campaign page, Deal Desk, and reports, so make "
        "it easy to recognize (e.g. 'KL Malls - Video - March')."
    )
    assert _split_merged_required_text(line) == line


def test_does_not_split_ordinary_prose_containing_the_word_required():
    """'required' appearing in an ordinary sentence, with nothing
    label-shaped immediately after it, must not trigger a split."""
    line = "- text: This field is required for verification purposes only."
    assert _split_merged_required_text(line) == line


def test_does_not_touch_non_text_lines():
    """Only 'text:' lines are ever candidates — a heading, paragraph, or
    other node type is left completely untouched even if its content
    happens to look similar."""
    line = '- heading "Flight Dates *" [level=3]'
    assert _split_merged_required_text(line) == line


def test_splits_only_the_affected_line_in_a_multi_line_snapshot():
    snapshot = (
        '- heading "Welcome Back" [level=2]\n'
        "- paragraph: Access your global OOH campaign management platform\n"
        "- text: Email / Username *\n"
        "- text: Email / Username is required Password *\n"
        '- button "Sign In"'
    )
    result = _split_merged_required_text(snapshot)
    lines = result.split("\n")
    assert lines[0] == '- heading "Welcome Back" [level=2]'
    assert lines[1] == "- paragraph: Access your global OOH campaign management platform"
    assert lines[2] == "- text: Email / Username *"
    assert lines[3] == "- text: Email / Username is required"
    assert lines[4] == "- text: Password *"
    assert lines[5] == '- button "Sign In"'
