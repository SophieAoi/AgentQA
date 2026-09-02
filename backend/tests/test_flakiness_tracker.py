"""
Regression coverage for agent/flakiness_tracker.py — the persisted,
per-test-case tie-break history that gates whether agent/verifier.py's
majority-vote mechanism actually fires for a given case (see
docs/AgentQA_Problems_and_Solutions_Plan.docx, Problem 1 follow-up).
"""

from unittest.mock import patch

import pytest

import agent.flakiness_tracker as flakiness_tracker


@pytest.fixture
def isolated_state_file(tmp_path):
    """Points the module at a throwaway file for the duration of each
    test, so tests never read or write the real, persisted
    backend/data/flakiness_history.json."""
    state_file = tmp_path / "flakiness_history.json"
    with patch.object(flakiness_tracker, "_STATE_FILE", state_file):
        yield state_file


def test_new_case_is_not_flaky(isolated_state_file):
    assert flakiness_tracker.is_known_flaky("TC-NEW") is False
    assert flakiness_tracker.get_tie_break_count("TC-NEW") == 0


def test_a_single_tie_break_does_not_yet_mark_a_case_flaky(isolated_state_file):
    """One disagreement could itself be a fluke — the threshold requires
    a couple of real occurrences before a case is treated as reliably
    inconsistent, not just unlucky once."""
    flakiness_tracker.record_tie_break("TC-001")

    assert flakiness_tracker.get_tie_break_count("TC-001") == 1
    assert flakiness_tracker.is_known_flaky("TC-001") is False


def test_repeated_tie_breaks_mark_a_case_flaky(isolated_state_file):
    flakiness_tracker.record_tie_break("TC-001")
    flakiness_tracker.record_tie_break("TC-001")

    assert flakiness_tracker.get_tie_break_count("TC-001") == 2
    assert flakiness_tracker.is_known_flaky("TC-001") is True


def test_tie_break_counts_are_isolated_per_case(isolated_state_file):
    flakiness_tracker.record_tie_break("TC-001")
    flakiness_tracker.record_tie_break("TC-001")

    assert flakiness_tracker.is_known_flaky("TC-001") is True
    assert flakiness_tracker.is_known_flaky("TC-002") is False
    assert flakiness_tracker.get_tie_break_count("TC-002") == 0


def test_history_persists_across_separate_calls_via_the_state_file(isolated_state_file):
    """The whole point of file-backed (not in-memory-store-backed)
    persistence: history must survive what would be a backend restart in
    production — simulated here by never holding state in a Python
    object between calls, only reading/writing the file each time."""
    flakiness_tracker.record_tie_break("TC-001")
    assert isolated_state_file.exists()

    # A fresh read (no shared in-process state) still sees it.
    assert flakiness_tracker.get_tie_break_count("TC-001") == 1


def test_reset_clears_a_specific_case(isolated_state_file):
    flakiness_tracker.record_tie_break("TC-001")
    flakiness_tracker.record_tie_break("TC-001")
    flakiness_tracker.record_tie_break("TC-002")

    flakiness_tracker.reset("TC-001")

    assert flakiness_tracker.get_tie_break_count("TC-001") == 0
    assert flakiness_tracker.get_tie_break_count("TC-002") == 1


def test_reset_with_no_argument_clears_everything(isolated_state_file):
    flakiness_tracker.record_tie_break("TC-001")
    flakiness_tracker.record_tie_break("TC-002")

    flakiness_tracker.reset()

    assert flakiness_tracker.get_tie_break_count("TC-001") == 0
    assert flakiness_tracker.get_tie_break_count("TC-002") == 0


def test_none_test_case_id_is_handled_safely_for_reads_and_records(isolated_state_file):
    """A call site that hasn't been updated to pass test_case_id (or
    genuinely has none) must degrade safely, not raise. Note: reset(None)
    is documented to clear EVERYTHING (its "no argument" default), so it's
    covered separately in test_reset_with_no_argument_clears_everything —
    this test only covers the read/record paths degrading safely."""
    assert flakiness_tracker.is_known_flaky(None) is False
    assert flakiness_tracker.get_tie_break_count(None) == 0
    flakiness_tracker.record_tie_break(None)  # must not raise, must not create an entry

    assert flakiness_tracker._load() == {}


def test_should_sample_vote_respects_the_configured_rate(isolated_state_file):
    with patch("agent.flakiness_tracker._SAMPLE_RATE", 1.0):
        assert flakiness_tracker.should_sample_vote() is True
    with patch("agent.flakiness_tracker._SAMPLE_RATE", 0.0):
        assert flakiness_tracker.should_sample_vote() is False


def test_corrupt_state_file_does_not_crash_verification(isolated_state_file):
    """A hand-edited or partially-written state file should degrade to
    'no history' rather than take down every subsequent verification call."""
    isolated_state_file.write_text("{not valid json")

    assert flakiness_tracker.is_known_flaky("TC-001") is False
    assert flakiness_tracker.get_tie_break_count("TC-001") == 0
    # And it should still be writable afterward, not permanently wedged.
    flakiness_tracker.record_tie_break("TC-001")
    assert flakiness_tracker.get_tie_break_count("TC-001") == 1
