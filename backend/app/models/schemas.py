"""
Request/response schemas shared across routers.
"""

from datetime import datetime
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field


class User(BaseModel):
    """Response-safe user representation — never carries the password hash."""

    id: str
    username: str
    created_at: datetime = Field(default_factory=datetime.utcnow)


class LoginRequest(BaseModel):
    username: str
    password: str


class RegisterRequest(BaseModel):
    username: str
    password: str


class MessageRole(str, Enum):
    user = "user"
    agent = "agent"


class ChatMessage(BaseModel):
    role: MessageRole
    content: str
    timestamp: datetime = Field(default_factory=datetime.utcnow)


class ChatRequest(BaseModel):
    message: str


class ChatResponse(BaseModel):
    reply: str


class TestRunStatus(str, Enum):
    queued = "queued"
    running = "running"
    passed = "passed"
    failed = "failed"
    error = "error"


class TestRunRequest(BaseModel):
    test_case_ids: list[str]  # which test cases to run, e.g. ["TC-001", "TC-002"]


class TestRunSummary(BaseModel):
    run_id: str
    status: TestRunStatus
    started_at: datetime
    finished_at: Optional[datetime] = None
    passed_count: int = 0
    failed_count: int = 0
    total_count: int = 0
    # Which case is running right now and its position in the queue (1-based),
    # so a multi-case run can show "Running 3 of 15: AD_LG_03" instead of only
    # an aggregate pass/fail count the user has to infer progress from.
    current_test_case_id: Optional[str] = None
    current_test_case_index: Optional[int] = None


class TestStepResult(BaseModel):
    step_description: str
    status: str  # "OK" | "FAILED"
    detail: Optional[str] = None
    screenshot_url: Optional[str] = None


class TestCaseRunResult(BaseModel):
    """One row per test case in a multi-case run — lets the frontend show a
    running per-case scoreboard instead of only a final aggregate count."""

    test_case_id: str
    status: str  # "running" | "passed" | "failed" | "error"


class TestRunDetail(TestRunSummary):
    steps: list[TestStepResult] = []
    logs: list[str] = []
    case_results: list[TestCaseRunResult] = []


class TestCase(BaseModel):
    id: str
    title: str
    description: str
    preconditions: list[str] = []
    # Groups related cases for the frontend's collapsible suite sections
    # (e.g. "Login"). Cases with no suite fall back to "Other" in the UI
    # rather than requiring every YAML file to declare one.
    suite: Optional[str] = None
    # Hand-picked, load-bearing cases (one or two per suite) that cover a
    # core happy-path or foundational guard rail — not a fabricated
    # priority label (the source CSVs' Priority column was never carried
    # into these YAML files), just an honest "if this breaks, a lot else
    # is suspect too" judgment call per suite. Powers the frontend's
    # "Select essential" one-click filter for a fast smoke-test run.
    essential: bool = False


class TestCaseWrite(BaseModel):
    """Body for POST/PUT /test-cases — the fields a person can actually
    set when creating or editing a case. `id` is separate (path param on
    PUT, top-level field on POST) rather than embedded here twice."""

    title: str
    description: str
    preconditions: list[str] = []
    suite: Optional[str] = None
    essential: bool = False


class TestCaseCreate(TestCaseWrite):
    id: str


class SelectorStrategy(str, Enum):
    primary = "primary"
    fallback_role = "fallback_role"
    fallback_keyword = "fallback_keyword"
    fallback_nearby_label = "fallback_nearby_label"
    fallback_fuzzy = "fallback_fuzzy"
    fallback_cache = "fallback_cache"
    llm_selected = "llm_selected"


class StepType(str, Enum):
    navigate = "navigate"
    action = "action"  # click, fill, select, etc.
    assertion = "assertion"  # the verifier-flavored step
    observation = "observation"  # read-only DOM/screenshot capture, no page mutation


class PlannedStep(BaseModel):
    step_index: int
    intent: str
    step_type: StepType
    expected_outcome: Optional[str] = None


class ExecutionStep(BaseModel):
    id: str
    run_id: str
    test_case_id: str
    step_index: int
    step_type: StepType
    intent: str
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    selector_used: Optional[str] = None
    selector_strategy: Optional[SelectorStrategy] = None
    expected_outcome: Optional[str] = None
    actual_result: Optional[str] = None
    status: TestRunStatus
    confidence: Optional[float] = None
    screenshot_url: Optional[str] = None
    started_at: datetime
    finished_at: Optional[datetime] = None
    error_detail: Optional[str] = None


class VerificationResult(BaseModel):
    status: Literal["passed", "failed"]
    confidence: float = Field(ge=0.0, le=1.0)
    explanation: str


class AgentTrace(BaseModel):
    id: str
    run_id: str
    step_id: Optional[str] = None
    role: Literal["planner", "executor", "verifier"]
    message_type: Literal["reasoning", "tool_call", "tool_result", "final_answer"]
    content: str
    token_usage_input: Optional[int] = None
    token_usage_output: Optional[int] = None
    created_at: datetime = Field(default_factory=datetime.utcnow)
