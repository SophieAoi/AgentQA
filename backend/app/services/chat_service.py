"""
Chat reasoning logic, extracted out of the chat router so it isn't business
logic living in the presentation layer. reply_to() calls the local model
(agent/local_llm.py) — see docs/phase-08-local-llm-migration.md.

Phase 6 (docs/phase-06-reporting-and-chat-trigger.md) added the one tool
this was always meant to have: start_test_run. This is the first real
instance of the agent deciding to act from chat rather than just talking
about it — deliberately scoped to exactly one tool, not a general-purpose
action surface.
"""

from agent.local_llm import LocalLLMError, local_tool, run_tool_loop
from agent.runner import list_test_cases
from app.models.schemas import MessageRole
from app.services.agent_runner import run_test_suite
from app.services.event_bus import EventBus
from app.services.store import StoreProtocol

SYSTEM_PROMPT = (
    "You are the INFLUENCE QA agent, embedded in a QA automation tool's chat "
    "sidebar. Help the user understand test cases, and use the start_test_run "
    "tool when they clearly ask you to run one or more test cases (e.g. \"run "
    "TC-001 and TC-002\", \"run the login tests\", \"run all of them\"). You do "
    "not need the user to spell out exact IDs — if they ask for \"all\", "
    "\"everything\", a named group (e.g. \"the login tests\"), or anything else "
    "that isn't a literal ID list, call list_test_cases first to see what's "
    "actually available, then pick the matching IDs yourself and call "
    "start_test_run with them. Only ask the user to clarify if list_test_cases "
    "doesn't make it reasonably clear which cases they mean. After "
    "start_test_run returns, tell the user the run has started and reference "
    "the real run_id it gives you; a run takes time to finish, so don't claim "
    "results you don't have. Be concise."
)

MAX_HISTORY_MESSAGES = 20
MAX_TOOL_ITERATIONS = 4


class ChatService:
    def __init__(self, store: StoreProtocol, event_bus: EventBus, background_tasks) -> None:
        self._store = store
        self._event_bus = event_bus
        self._background_tasks = background_tasks

    def _list_test_cases_tool(self):
        # Named list_test_cases_tool (not list_test_cases) to avoid shadowing
        # the module-level `list_test_cases` import inside its own closure
        # body, which would otherwise resolve to itself and recurse forever.
        # .name is overridden below so the model sees it advertised exactly
        # as "list_test_cases", matching the system prompt's wording.
        @local_tool
        async def list_test_cases_tool() -> str:
            """List every available test case (id and title), so you can figure out
            which IDs to pass to start_test_run when the user doesn't give exact IDs
            (e.g. "run all of them", "run the login tests")."""
            cases = list_test_cases()
            if not cases:
                return "No test cases are available."
            return "\n".join(f"{case['id']}: {case['title']}" for case in cases)

        list_test_cases_tool.name = "list_test_cases"
        return list_test_cases_tool

    def _start_test_run_tool(self):
        store = self._store
        event_bus = self._event_bus
        background_tasks = self._background_tasks

        @local_tool
        async def start_test_run(test_case_ids: list[str]) -> str:
            """Start a background test run for one or more test cases. Use the exact
            test case IDs (e.g. "TC-001", "LGN-006") — never invent one. Returns a
            confirmation with the real run_id once the run has been queued; the run
            itself happens asynchronously and is not finished when this returns."""
            known_ids = {case["id"] for case in list_test_cases()}
            unknown = [tid for tid in test_case_ids if tid not in known_ids]
            if unknown:
                return f"Could not start a run — unknown test case ID(s): {', '.join(unknown)}."
            if not test_case_ids:
                return "Could not start a run — no test case IDs were given."

            run = store.create_test_run(test_case_ids)
            background_tasks.add_task(run_test_suite, store, event_bus, run.run_id, test_case_ids)
            return (
                f"Started run {run.run_id} for {len(test_case_ids)} test case(s): "
                f"{', '.join(test_case_ids)}. It's running in the background — check the "
                f"Test Runs panel or GET /test-runs/{run.run_id} for progress."
            )

        return start_test_run

    async def reply_to(self, message: str) -> str:
        # The caller (chat router) already appended `message` to the store
        # as the latest user turn before calling this, so history alone —
        # not history + message — is the full conversation to send.
        messages = self._build_messages()
        try:
            final_text = ""
            async for turn in run_tool_loop(
                system=SYSTEM_PROMPT,
                messages=messages,
                tools=[self._list_test_cases_tool(), self._start_test_run_tool()],
                max_iterations=MAX_TOOL_ITERATIONS,
            ):
                if turn.type == "text" and turn.text:
                    final_text = turn.text
        except LocalLLMError as exc:
            return f"I couldn't reach the local reasoning model: {exc}"

        return final_text or "I didn't get a text response back — try rephrasing."

    def _build_messages(self) -> list[dict]:
        history = self._store.get_chat_history()[-MAX_HISTORY_MESSAGES:]
        return [
            {
                "role": "user" if msg.role == MessageRole.user else "assistant",
                "content": msg.content,
            }
            for msg in history
        ]
