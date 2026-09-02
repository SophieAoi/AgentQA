"""
Opt-in alternate backend for agent/local_llm.py, talking to Google's
Gemini API instead of a local Ollama server — gated behind
USE_GEMINI_BACKEND (see app/config.py), off by default. Added per explicit
request to try it through the real UI; a single-call benchmark beforehand
found gemini-flash-latest slower than local qwen2.5:14b-instruct on a
representative verification prompt (4.39s vs 2.32s), driven by ~100+
tokens of default "thinking" overhead the API doesn't skip without extra
config — not a promising result, kept as an opt-in comparison path anyway.

Mimics ollama.AsyncClient's .chat() call signature and response shape
(`.message.content`, `.message.tool_calls`, `.prompt_eval_count`,
`.eval_count`) so agent/local_llm.py's run_tool_loop()/structured_chat()
work against any backend unchanged.

Gemini's wire format is genuinely different from both Ollama's and
NVIDIA's OpenAI-compatible one:
- No `role: "system"` message — a system prompt is a separate top-level
  `systemInstruction` field.
- No `role: "tool"` / `tool_calls[]` — a function call is a `functionCall`
  part on a `role: "model"` turn, and the result is a `functionResponse`
  part on a `role: "user"` turn (Gemini has only "user"/"model" roles).
- Every `functionCall` part carries an opaque `thoughtSignature` blob that
  MUST be echoed back verbatim on the following turn or the API 400s
  ("Function call is missing a thought_signature..." — confirmed live).
  Unlike NVIDIA's tool_call_id (which could be synthesized freely, see
  agent/nvidia_client.py), this signature is opaque server state Gemini
  actually validates — it has to be the real one from the matching
  functionCall, not a placeholder.

Carrying the signature forward can't go through `call.function.arguments`
— local_llm.py's run_tool_loop() does `tool_input = dict(call.function
.arguments)` and then `await tool(**tool_input)`, spreading those exact
keys as real keyword arguments into the actual Playwright tool function;
any extra key added there raises TypeError and silently breaks the real
tool call (caught by run_tool_loop()'s own except TypeError as "Tool call
failed", so it would look like a resolver failure, not what it actually
is). It also can't ride on the tool's `content` result string — that
string is the tool's own real return value (`result = await tool(
**tool_input)`), produced entirely outside GeminiClient.chat(), which
returns before the tool is even called — there is no hook to append
anything to it.

A bare {"role": "assistant", "content": ...} / {"role": "tool", "content":
...} pair (what local_llm.py actually appends) carries no tool name,
arguments, or id at all — only the assistant's reasoning text and the
tool's result text. There is nothing in either message to match a call
back to by content. What IS stable, exactly like agent/nvidia_client.py's
_tool_calls_by_order fix (see its docstring for the fuller version of
this reasoning): every {"role": "assistant"} + one-or-more {"role":
"tool"} run appears in `messages` in the same strict order calls were
actually issued and never gets reordered or removed. So every
functionCall this process has ever seen from Gemini is appended, in
order, to a MODULE-level list (not per-GeminiClient-instance — a fresh
instance is constructed for every run_tool_loop() call, see _client() in
local_llm.py, and the module scope is what actually survives across
those). Replaying history just walks that list by position instead of
trying to identify a specific call.
"""

import json
from typing import Any, Optional

import httpx

from app.config import GEMINI_API_KEY, GEMINI_BASE_URL, GEMINI_MODEL

# Module-level (not per-GeminiClient-instance — see docstring above): every
# functionCall this process has issued, in order:
# {"name", "arguments", "signature"}.
_calls_by_order: list[dict] = []


class GeminiBackendError(RuntimeError):
    """Raised for any failure talking to the Gemini API — mirrors
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


_UNSUPPORTED_SCHEMA_KEYS = ("title", "additionalProperties", "$defs", "$schema")


def _to_gemini_schema(schema: dict, defs: Optional[dict] = None) -> dict:
    """
    Converts a plain Pydantic/JSON-Schema output (what both
    output_model.model_json_schema() in structured_chat() and this
    codebase's @local_tool-decorated functions produce) into what Gemini's
    API actually accepts for both `tools[].functionDeclarations[].
    parameters` and `generationConfig.responseSchema`.

    Confirmed live (see AD_LG_01 planning failure): Gemini rejects
    standard JSON Schema's `$defs`/`$ref` reference mechanism outright
    ("Unknown name $defs... Cannot find field") — every nested Pydantic
    model (e.g. planner.py's PlannedStepList containing a list of
    PlannedStep, which itself references the StepType enum) produces
    exactly this shape, so this isn't an edge case, it's the normal output
    for anything beyond a flat schema. Fully inlines every $ref by
    resolving it against the top-level $defs before this function was
    first called, then recurses into every place a nested schema can
    appear: object properties, array items, and anyOf/oneOf branches
    (Pydantic's Optional[X] becomes anyOf: [X, {"type": "null"}], which
    Gemini also doesn't accept as a bare key at the top level of a
    property the way JSON Schema allows — collapsed to just the non-null
    branch, since Gemini has no null-union concept in this schema format
    and every field this codebase actually declares as Optional already
    has a real default making the strict non-null requirement harmless).
    """
    if not isinstance(schema, dict):
        return schema

    defs = defs if defs is not None else schema.get("$defs", {})

    if "$ref" in schema:
        ref_name = schema["$ref"].rsplit("/", 1)[-1]
        resolved = defs.get(ref_name, {})
        return _to_gemini_schema(resolved, defs)

    if "anyOf" in schema:
        # Optional[X] -> anyOf: [X_schema, {"type": "null"}] — take the
        # first non-null branch; every Optional field in this codebase's
        # models has a real default, so dropping the null branch doesn't
        # change what a valid response looks like in practice.
        non_null = [s for s in schema["anyOf"] if s.get("type") != "null"]
        if non_null:
            return _to_gemini_schema(non_null[0], defs)

    cleaned = {k: v for k, v in schema.items() if k not in _UNSUPPORTED_SCHEMA_KEYS and k != "anyOf"}
    if "properties" in cleaned:
        cleaned["properties"] = {k: _to_gemini_schema(v, defs) for k, v in cleaned["properties"].items()}
    if "items" in cleaned:
        cleaned["items"] = _to_gemini_schema(cleaned["items"], defs)
    return cleaned


class GeminiClient:
    """Drop-in stand-in for ollama.AsyncClient, scoped to just the .chat()
    method run_tool_loop()/structured_chat() actually call.

    agent/local_llm.py's _client() constructs a fresh instance per call,
    and agent/executor.py calls run_tool_loop() fresh per planned step
    while `messages` accumulates across the whole test case — so this
    can't rely on state living on `self` surviving between .chat() calls.
    See the module docstring for why thoughtSignatures are instead
    recovered from the module-level _calls_by_order list, matched to bare
    {"role": "tool"} messages purely by position."""

    def _to_gemini_contents(self, messages: list[dict]) -> tuple[Optional[str], list[dict]]:
        system_instruction: Optional[str] = None
        contents: list[dict] = []
        call_index = 0
        for m in messages:
            role = m.get("role")
            if role == "system":
                system_instruction = m.get("content") or ""
                continue
            if role == "user":
                contents.append({"role": "user", "parts": [{"text": m.get("content") or ""}]})
            elif role == "assistant":
                # local_llm.py appends a plain {"role": "assistant", "content": text}
                # turn even for a turn that made real tool calls — the
                # actual function-call parts, if any, are reconstructed
                # from the FOLLOWING tool message(s) below, not from this
                # message alone, since local_llm.py never attaches
                # structured tool-call info to the assistant message
                # itself.
                text = m.get("content") or ""
                parts = [{"text": text}] if text else []
                contents.append({"role": "model", "parts": parts})
            elif role == "tool":
                if call_index < len(_calls_by_order):
                    call = _calls_by_order[call_index]
                    call_index += 1
                    call_part = {
                        "functionCall": {"name": call["name"], "args": call["arguments"]},
                        "thoughtSignature": call["signature"],
                    }
                    if contents and contents[-1]["role"] == "model":
                        contents[-1]["parts"].append(call_part)
                    else:
                        contents.append({"role": "model", "parts": [call_part]})
                    contents.append(
                        {
                            "role": "user",
                            "parts": [
                                {
                                    "functionResponse": {
                                        "name": call["name"],
                                        "response": {"result": m.get("content") or ""},
                                    }
                                }
                            ],
                        }
                    )
                else:
                    # More bare tool messages in history than this process
                    # has ever recorded a real functionCall for — should
                    # not happen (see module docstring's ordering
                    # assumption), but degrade to a plain user turn rather
                    # than silently dropping the tool result or crashing.
                    contents.append({"role": "user", "parts": [{"text": str(m.get("content") or "")}]})
        return system_instruction, contents

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
        # backend is active — Gemini has no equivalent concept, silently
        # dropping them here is correct, not a bug being masked.
        model_name = model or GEMINI_MODEL
        system_instruction, contents = self._to_gemini_contents(messages or [])

        payload: dict[str, Any] = {"contents": contents}
        if system_instruction:
            payload["systemInstruction"] = {"parts": [{"text": system_instruction}]}
        if tools:
            declarations = [
                {
                    "name": t["function"]["name"],
                    "description": t["function"]["description"],
                    "parameters": _to_gemini_schema(t["function"]["parameters"]),
                }
                for t in tools
            ]
            payload["tools"] = [{"functionDeclarations": declarations}]
        if format:
            payload["generationConfig"] = {
                "responseMimeType": "application/json",
                "responseSchema": _to_gemini_schema(format),
            }

        try:
            async with httpx.AsyncClient() as http:
                resp = await http.post(
                    f"{GEMINI_BASE_URL}/{model_name}:generateContent",
                    headers={"x-goog-api-key": GEMINI_API_KEY, "Content-Type": "application/json"},
                    json=payload,
                    timeout=90.0,
                )
        except (httpx.TimeoutException, httpx.TransportError) as exc:
            raise GeminiBackendError(f"Could not reach the Gemini API at {GEMINI_BASE_URL}: {exc}") from exc

        if resp.status_code != 200:
            raise GeminiBackendError(f"Gemini API returned {resp.status_code}: {resp.text[:500]}")

        data = resp.json()
        candidates = data.get("candidates") or []
        if not candidates:
            raise GeminiBackendError(f"Gemini API returned no candidates: {data}")
        parts = candidates[0].get("content", {}).get("parts", [])

        text_chunks: list[str] = []
        tool_calls: list[_ToolCall] = []
        for part in parts:
            if "text" in part:
                text_chunks.append(part["text"])
            elif "functionCall" in part:
                fc = part["functionCall"]
                args = dict(fc.get("args") or {})
                tool_calls.append(_ToolCall(fc["name"], args))
                signature = part.get("thoughtSignature")
                if signature:
                    # Recorded in strict response order — matches the
                    # order run_tool_loop()'s `for call in msg.tool_calls:`
                    # appends bare {"role": "tool", ...} messages in, which
                    # is what _to_gemini_contents() walks by position.
                    _calls_by_order.append({"name": fc["name"], "arguments": args, "signature": signature})

        usage = data.get("usageMetadata", {})
        return _Response(
            _Message("".join(text_chunks), tool_calls),
            usage.get("promptTokenCount"),
            usage.get("candidatesTokenCount"),
        )
