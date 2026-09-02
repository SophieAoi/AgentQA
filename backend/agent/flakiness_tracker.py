"""
Tracks, per test case, whether the Verifier's majority-vote mechanism
(agent/verifier.py) has ever actually had to break a tie for that case —
i.e. real, observed evidence that this specific case's DOM state pattern
produces inconsistent model judgments, not just "this case failed once"
(which can happen for legitimate reasons — a real site bug, stale test
data — that voting doesn't address and shouldn't be conflated with).

Two separate triggers decide whether verifier.py actually votes on a
given assertion, both defined here:
- is_known_flaky(): a case that's already proven itself inconsistent
  (repeated observed tie-breaks) always votes.
- should_sample_vote(): every OTHER case votes occasionally anyway (see
  _SAMPLE_RATE) — without this, a case could never accumulate its first
  tie-break in the first place, since nothing would ever call it more
  than once to find out. Most runs still take the cheap single-call path;
  only a fraction sample-check for latent flakiness.

Persisted to a small local JSON file rather than the in-memory
StoreProtocol store: that store resets on every backend restart (which
happens often during active development), and flakiness history is only
useful if it survives restarts and accumulates across real working
sessions over time.

Deliberately narrow: this file only answers "has this case's votes ever
disagreed" (bool) and "how many times" (int), not a full pass/fail
history — that's a separate, larger feature (see docs/AgentQA_Problems_
and_Solutions_Plan.docx, Problem 2's proposed results dashboard) this
module isn't trying to be.
"""

import json
import random
from pathlib import Path
from threading import Lock
from typing import Optional

_STATE_FILE = Path(__file__).resolve().parent.parent / "data" / "flakiness_history.json"

# A case needs this many OBSERVED tie-breaks (not just runs) before it's
# treated as "flaky enough to always vote on" — one disagreement could
# itself be a fluke; requiring a couple of real occurrences avoids
# permanently paying the extra-vote cost for a case that only ever
# disagreed with itself once, while still reacting quickly to a case that
# does it repeatedly.
_FLAKY_THRESHOLD = 2

# Fraction of assertion evaluations, for a case NOT already known-flaky,
# that vote anyway purely to sample for latent inconsistency. 1-in-5
# balances "actually discover flakiness within a normal handful of runs"
# against "don't quietly double the cost of most runs" — the whole point
# of gating voting on known-flaky status in the first place.
_SAMPLE_RATE = 0.2

_lock = Lock()


def should_sample_vote() -> bool:
    """Random sample-check, independent of any specific case's history —
    called for a case that isn't already known-flaky, so latent
    inconsistency can still be discovered without voting on every run."""
    return random.random() < _SAMPLE_RATE


def _load() -> dict:
    if not _STATE_FILE.exists():
        return {}
    try:
        return json.loads(_STATE_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        # Corrupt or unreadable state file is not worth crashing a test
        # run over — start fresh rather than block verification entirely.
        return {}


def _save(data: dict) -> None:
    _STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    _STATE_FILE.write_text(json.dumps(data, indent=2, sort_keys=True))


def record_tie_break(test_case_id: Optional[str]) -> None:
    """Called by verifier.py exactly when a tie-break vote actually fired
    for this case — i.e. real, direct evidence of judgment inconsistency,
    not an inference from a failed run."""
    if not test_case_id:
        return
    with _lock:
        data = _load()
        data[test_case_id] = data.get(test_case_id, 0) + 1
        _save(data)


def is_known_flaky(test_case_id: Optional[str]) -> bool:
    """True once a case has hit _FLAKY_THRESHOLD observed tie-breaks —
    the signal agent/executor.py uses to decide whether an assertion step
    for this case should vote at all, or take the single-call fast path."""
    if not test_case_id:
        return False
    with _lock:
        return _load().get(test_case_id, 0) >= _FLAKY_THRESHOLD


def get_tie_break_count(test_case_id: Optional[str]) -> int:
    if not test_case_id:
        return 0
    with _lock:
        return _load().get(test_case_id, 0)


def reset(test_case_id: Optional[str] = None) -> None:
    """Clears tracked history — for a specific case, or everything if
    None. Exposed mainly for tests; also useful if a case's flakiness
    was actually caused by something since fixed (a test-case wording
    bug, a real site bug) and its accumulated tie-break count would
    otherwise keep it voting long after the underlying cause is gone."""
    with _lock:
        if test_case_id is None:
            _save({})
            return
        data = _load()
        data.pop(test_case_id, None)
        _save(data)
