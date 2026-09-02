"""
Opt-in alternate backend for agent/local_llm.py, talking to NVIDIA's NIM
API (build.nvidia.com) instead of a local Ollama server — gated behind
USE_NVIDIA_BACKEND (see app/config.py), off by default. Added to A/B test
speed vs. reliability against local qwen2.5:14b-instruct; live testing
found the fast, free-tier-responsive model (llama-3.1-8b-instruct) gives
inconsistent selector/tool-call judgments across identical runs — same
reliability gap seen with local 7B models. Kept as an opt-in comparison
path, not a default.

Mimics ollama.AsyncClient's .chat() call signature and response shape
(`.message.content`, `.message.tool_calls`, `.prompt_eval_count`,
`.eval_count`) so agent/local_llm.py's run_tool_loop()/structured_chat()
work against either backend unchanged.

NVIDIA's NIM endpoint speaks the OpenAI Chat Completions wire format, which
has two hard requirements Ollama's looser convention doesn't: every
assistant turn that made a tool call must carry a `tool_calls[]` array with
an id, and the following tool-result message must reference that id via
`tool_call_id`. agent/local_llm.py's run_tool_loop() builds messages
against Ollama's shape (no ids at all) since it never needed them for
Ollama — _fixup_messages() reconstructs the correlation here instead of
changing the shared loop, so the Ollama path stays untouched. Assumes one
outstanding tool call per turn, true for this codebase's loop.
"""

import json
from typing import Any, Optional

import httpx

from app.config import NVIDIA_API_KEY, NVIDIA_BASE_URL, NVIDIA_MODEL


class NvidiaBackendError(RuntimeError):
    """Raised for any failure talking to the NVIDIA NIM endpoint — mirrors
    LocalLLMError's role for the Ollama path."""


class _Function:
    __slots__ = ("name", "arguments")

    def __init__(self, name: str, arguments: dict):
        self.name = name
        self.arguments = arguments


class _ToolCall:
    __slots__ = ("function",)

    def __init__(self, name: str, arguments: dict):
        self.function = _Function(name, arguments)


class _Message:
    __slots__ = ("content", "tool_calls")

    def __init__(self, content: str, tool_calls: list[_ToolCall]):
        self.content = content
        self.tool_calls = tool_calls


class _Response:
    __slots__ = ("message", "prompt_eval_count", "eval_count")

    def __init__(self, message: _Message, prompt_eval_count: Optional[int], eval_count: Optional[int]):
        self.message = message
        self.prompt_eval_count = prompt_eval_count
        self.eval_count = eval_count


def _safe_json(value: Any) -> dict:
    if isinstance(value, dict):
        return value
    try:
        return json.loads(value)
    except (TypeError, ValueError):
        return {}


class NvidiaClient:
    """Drop-in stand-in for ollama.AsyncClient, scoped to just the .chat()
    method run_tool_loop()/structured_chat() actually call.

    agent/local_llm.py's _client() constructs a fresh instance per call —
    and agent/executor.py calls run_tool_loop() fresh for EVERY planned
    step of a test case, while `messages` itself is one list accumulated
    across the whole case. That means a NvidiaClient instance never lives
    long enough to remember a real tool_calls[].id from an earlier step,
    even though that step's assistant/tool messages are still sitten in
    the history being replayed on this step's request. An id-recording
    approach (what this class used before) can only ever cover calls made
    by the *current* instance, so it broke as soon as a later step's
    request had to replay an earlier step's tool call/result pair.
    _fixup_messages() below doesn't try to recover the real ids at all —
    it synthesizes fresh, self-consistent ones from message position alone,
    which is all OpenAI's API actually requires (the id only has to
    correlate an assistant turn's tool_calls[] entry with the tool
    message(s) that follow it inside ONE request; it doesn't need to match
    anything from a prior request or be semantically meaningful)."""

    def _fixup_messages(self, messages: list[dict]) -> list[dict]:
        """
        Finds each maximal run of consecutive bare {"role": "tool"}
        messages and attributes them, in order, to the nearest preceding
        assistant message — matching how run_tool_loop() actually builds
        history: one assistant turn's `for call in msg.tool_calls:` loop
        appends one bare tool message per call, back to back, before the
        next assistant turn. OpenAI requires all of a turn's calls listed
        together in that ONE assistant message's tool_calls[], not
        one-at-a-time — a run of N tool messages needs N ids attached to
        the same preceding assistant message, not N separate assistant
        messages each holding one. The tool name embedded in the synthetic
        tool_calls[] entry is a placeholder ("previous_tool_call") since
        the real name isn't recoverable from a bare tool message alone —
        harmless, because NVIDIA's API only uses it to correlate this
        request's own messages, not to actually invoke anything.
        """
        fixed: list[dict] = []
        synthetic_id_counter = 0
        pending_assistant_idx: Optional[int] = None
        for m in messages:
            if m.get("role") == "tool" and "tool_call_id" not in m:
                synthetic_id_counter += 1
                call_id = f"call_{synthetic_id_counter}"
                m = dict(m)
                m["tool_call_id"] = call_id
                if pending_assistant_idx is not None:
                    assistant_msg = dict(fixed[pending_assistant_idx])
                    assistant_msg["tool_calls"] = list(assistant_msg.get("tool_calls") or [])
                    assistant_msg["tool_calls"].append(
                        {
                            "id": call_id,
                            "type": "function",
                            "function": {"name": "previous_tool_call", "arguments": "{}"},
                        }
                    )
                    fixed[pending_assistant_idx] = assistant_msg
            else:
                if m.get("role") == "assistant":
                    pending_assistant_idx = len(fixed)
                elif m.get("role") != "tool":
                    pending_assistant_idx = None
            fixed.append(m)
        return fixed

    async def chat(
        self,
        model: Optional[str] = None,
        messages: Optional[list[dict]] = None,
        tools: Optional[list[dict]] = None,
        format: Optional[dict] = None,
        keep_alive: Optional[str] = None,
        **_ignored: Any,
    ) -> _Response:
        # **_ignored absorbs Ollama-only kwargs (e.g. think=) that
        # local_llm.py may pass unconditionally regardless of which
        # backend is active — NVIDIA has no equivalent concept, silently
        # dropping them here is correct, not a bug being masked.
        payload: dict[str, Any] = {
            "model": model or NVIDIA_MODEL,
            "messages": self._fixup_messages(messages or []),
            "max_tokens": 1024,
            "temperature": 0.2,
        }
        if tools:
            payload["tools"] = tools
        if format:
            # Real schema-constrained output (confirmed live against NIM —
            # its own nvext.guided_json extension is silently ignored, but
            # the standard OpenAI response_format shape works).
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {"name": "structured_output", "schema": format},
            }

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(
                    NVIDIA_BASE_URL,
                    headers={"Authorization": f"Bearer {NVIDIA_API_KEY}", "Content-Type": "application/json"},
                    json=payload,
                    timeout=90.0,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise NvidiaBackendError(f"Could not reach the NVIDIA API at {NVIDIA_BASE_URL}: {exc}") from exc

        if resp.status_code != 200:
            raise NvidiaBackendError(f"NVIDIA API returned {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        choice = data["choices"][0]
        msg = choice["message"]
        raw_tool_calls = msg.get("tool_calls") or []
        tool_calls = [
            _ToolCall(tc["function"]["name"], _safe_json(tc["function"]["arguments"])) for tc in raw_tool_calls
        ]
        usage = data.get("usage", {})
        return _Response(
            _Message(msg.get("content") or "", tool_calls),
            usage.get("prompt_tokens"),
            usage.get("completion_tokens"),
        )
