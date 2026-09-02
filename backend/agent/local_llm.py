"""
Local LLM backend (phase 8, docs/phase-08-local-llm-migration.md) — replaces
every direct `anthropic` SDK call in this codebase with calls to a
self-hosted Ollama server by default. No paid third-party model API is used
for real test runs unless explicitly opted into: USE_NVIDIA_BACKEND (see
app/config.py, agent/nvidia_client.py) routes _client() to NVIDIA's NIM API
instead, and USE_GEMINI_BACKEND (agent/gemini_client.py) routes it to
Google's Gemini API — both off by default. Live A/B testing found NVIDIA's
fast, free-tier-responsive model gives inconsistent selector/tool-call
judgments across identical runs, and Gemini's flash-latest model was
slower than local 14B on a representative call due to default thinking
overhead — both kept as opt-in comparison paths, not defaults, and real
staging credentials/page content leave the machine whenever either is on.

Every Ollama call passes think=False. Confirmed live: on qwen3.5 (a
"thinking" model family), the real Planner prompt/schema made both the 4B
and 9B variants generate exactly 3488 tokens of hidden reasoning and hit
their output limit WITHOUT EVER producing the actual answer — done_reason
"length", empty content, even with num_predict explicitly raised to 8000.
think=False eliminated this entirely (qwen3.5:4b went from a 71s/empty
failure to a 9.2s/correct plan) and is a no-op on non-thinking models like
qwen2.5 (confirmed harmless), so it's passed unconditionally rather than
gated by model name.

This module is the local equivalent of what the `anthropic` SDK provided
directly, matched one-for-one against what each call site actually used:

- `structured_chat()` replaces `client.messages.parse(..., output_format=X)`
  (used by planner.py, verifier.py, and playwright_tools.py's last-resort
  selector tier) — Ollama's `format=<json schema>` parameter constrains the
  model's raw output to valid JSON matching the schema, which is then
  validated into the Pydantic model.
- `local_tool` replaces `@anthropic.beta_async_tool` — infers a JSON schema
  from a function's type-hinted parameters via `pydantic.create_model`, so
  call sites only need to swap the decorator, not restructure the function.
- `run_tool_loop()` replaces `client.beta.messages.tool_runner()` — a
  hand-written ReAct loop (ADR: own this directly rather than pull in
  LangChain/LlamaIndex, matching this codebase's existing preference for no
  heavy agent-framework dependency — see docs/BUILD-PLAN.md § "Why two
  agent roles" for the same instinct applied to agent *count*, not just
  frameworks). Yields one ToolLoopTurn per turn so callers can trace/log as
  they go, mirroring how the old tool_runner's async iteration worked.
"""

import inspect
import json
import re
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Literal, Optional, Type, TypeVar

import ollama
from pydantic import BaseModel, ValidationError, create_model

from agent.gemini_client import GeminiBackendError
from agent.nvidia_client import NvidiaBackendError
from app.config import (
    GEMINI_MODEL,
    LOCAL_LLM_BASE_URL,
    LOCAL_LLM_KEEP_ALIVE,
    LOCAL_LLM_MODEL,
    NVIDIA_MODEL,
    USE_GEMINI_BACKEND,
    USE_NVIDIA_BACKEND,
)


def _default_model() -> str:
    """The model string every call site falls back to when it doesn't pass
    its own `model=` override. Must track the active backend — passing an
    Ollama model tag (e.g. "qwen2.5:14b-instruct") to NVIDIA's or Gemini's
    endpoint 404s, since it isn't in either catalog under that name."""
    if USE_NVIDIA_BACKEND:
        return NVIDIA_MODEL
    if USE_GEMINI_BACKEND:
        return GEMINI_MODEL
    return LOCAL_LLM_MODEL


# Detects non-Latin-script text (observed leaking in Thai, Chinese, AND
# Cyrillic/Ukrainian despite an explicit "always respond in English"
# system-prompt instruction in every prompt that uses this loop — a soft
# constraint the 14B model doesn't reliably follow, and evidently not
# confined to one script family). Thai block (U+0E00-U+0E7F), common CJK
# ranges (U+4E00-U+9FFF unified ideographs, U+3400-U+4DBF extension A),
# and Cyrillic (U+0400-U+04FF) — covers every script actually observed
# leaking in practice, not a general "is this English" classifier, to
# keep false positives (a stray accented Latin character) from triggering
# a needless retry call.
_NON_LATIN_RE = re.compile(r"[฀-๿一-鿿㐀-䶿Ѐ-ӿ]")

# Above this fraction of non-whitespace characters being non-Latin, treat
# the text as "leaked" rather than translating — a single stray character
# isn't worth a retry call, but a majority-non-English response is.
_NON_LATIN_LEAK_THRESHOLD = 0.15


def _looks_leaked(text: str) -> bool:
    if not text:
        return False
    stripped = re.sub(r"\s", "", text)
    if not stripped:
        return False
    non_latin_count = len(_NON_LATIN_RE.findall(stripped))
    return (non_latin_count / len(stripped)) > _NON_LATIN_LEAK_THRESHOLD


async def _translate_to_english(text: str, model: Optional[str] = None) -> str:
    """
    One quick follow-up call asking the model to restate the same message
    in English, used only on the rare turn _looks_leaked() actually flags
    — every other turn pays zero extra latency. Best-effort: if this call
    itself fails, the original text is returned rather than raising, since
    a cosmetic translation retry should never be able to fail a test step
    that otherwise succeeded.

    Retries once with a sharper instruction if the translation itself
    still looks leaked — observed live: asking this same model to
    "translate into English" is exactly the kind of instruction it
    already doesn't reliably follow (that's the whole reason this
    function exists), so a single blind attempt can come back still
    partially in the original script. A second, more insistent pass with
    a fresh (non-conversational) call catches most of those; if it's
    STILL leaked after that, the original text is returned rather than
    trying indefinitely — this is a cosmetic nicety, not worth unbounded
    retries against an already-struggling model.
    """
    client = _client()
    current = text
    for attempt in range(2):
        instruction = (
            "Translate the following message into English. Preserve its meaning and any "
            "RESULT: PASS/FAILED marker exactly. Respond with ONLY the English translation, "
            "no preamble, no explanation of what you're doing."
            if attempt == 0
            else "The text below still contains non-English words. Rewrite it ENTIRELY in "
            "English — every single word, with no exceptions. Respond with ONLY the fully "
            "English rewrite, nothing else."
        )
        try:
            response = await client.chat(
                model=model or _default_model(),
                messages=[
                    {"role": "system", "content": instruction},
                    {"role": "user", "content": current},
                ],
                keep_alive=LOCAL_LLM_KEEP_ALIVE,
                think=False,
            )
        except Exception:
            return text

        translated = (response.message.content or "").strip()
        if not translated:
            return text
        if not _looks_leaked(translated):
            return translated
        current = translated  # feed the partial translation back in, not the original

    return current


# Matches the start of a "name": "...", "arguments": { block the model
# sometimes emits as plain text in msg.content instead of a real,
# structured Ollama tool call — observed with the 14B model under this
# project's tool set in a couple of different malformed shapes: wrapped in
# a real {...} object, or preceded by a broken chat-template special token
# ("ICA" followed by a dangling `</tool_call>` closer, no opening brace).
# When this happens, msg.tool_calls is empty, the real tool function is
# never called, and the executor would otherwise treat the step as done
# with no action having actually happened — see the "leaked tool call"
# handling in run_tool_loop() below. Only the start of the arguments
# object is matched here; its extent is found by brace-balancing in
# _extract_leaked_tool_call, since a fixed-width regex can't reliably
# capture a nested JSON object of unknown depth.
_LEAKED_TOOL_CALL_START_RE = re.compile(r'"name"\s*:\s*"(\w+)"\s*,\s*"arguments"\s*:\s*\{', re.DOTALL)


def _leaked_tool_call_bare_re(tool_names: list[str]) -> re.Pattern:
    """A third observed malformed shape: `<tool_name>: {...}` with no
    "name"/"arguments" wrapper at all (e.g. `fill: { "selector_hint": ...,
    "value": ... }`), sometimes alongside a fully hallucinated fake
    <tool_response>/USER/ADMIN conversation the model role-plays instead of
    making a real call. Anchored on the actual tool names in scope for
    this loop (not \\w+) — matching an arbitrary bare `word: {` would false
    -positive on ordinary prose far too easily."""
    names = "|".join(re.escape(name) for name in tool_names)
    return re.compile(rf'\b({names})\s*:\s*\{{', re.DOTALL)


def _extract_leaked_tool_call(text: str, tool_names: list[str] = ()) -> Optional[tuple[str, dict]]:
    """Best-effort recovery of a tool call the model wrote as text instead
    of issuing as a real tool call. Returns (tool_name, arguments) or None.
    Not every malformed shape is recoverable (e.g. arguments JSON that's
    itself cut short mid-value) — that's fine, since executor.py's
    made_real_tool_call check now fails the step outright when no tool
    call, real or recovered, ever actually ran, rather than trusting the
    model's own narrated "RESULT: PASS". Callers are responsible for not
    re-dispatching an identical (tool_name, arguments) pair the loop has
    already recovered once this step — see run_tool_loop()'s
    already_recovered tracking, which exists because the model can keep
    re-narrating (and re-leaking) the same already-completed action turn
    after turn instead of ever producing a clean final answer."""
    if not text:
        return None

    patterns = [_LEAKED_TOOL_CALL_START_RE]
    if tool_names:
        patterns.append(_leaked_tool_call_bare_re(list(tool_names)))

    for pattern in patterns:
        match = pattern.search(text)
        if not match:
            continue
        tool_name = match.group(1)
        brace_start = match.end() - 1  # position of the opening '{'
        depth = 0
        for i in range(brace_start, len(text)):
            if text[i] == "{":
                depth += 1
            elif text[i] == "}":
                depth -= 1
                if depth == 0:
                    try:
                        arguments = json.loads(text[brace_start : i + 1])
                    except json.JSONDecodeError:
                        break  # try the next pattern rather than giving up entirely
                    if not isinstance(arguments, dict):
                        break
                    return tool_name, arguments
    return None  # unrecoverable, not a false match

T = TypeVar("T", bound=BaseModel)


class LocalLLMError(RuntimeError):
    """Raised for any failure talking to the local Ollama server — a
    stopped server, an unpulled model, or a response that didn't match the
    requested schema. Each call site (planner.py/verifier.py/executor.py)
    wraps this into its own existing domain error, so the error-handling
    contract at every call site is unchanged from the Anthropic-backed
    version."""


def _client():
    if USE_NVIDIA_BACKEND:
        from agent.nvidia_client import NvidiaClient  # local import: keeps httpx optional for the Ollama-only path

        return NvidiaClient()
    if USE_GEMINI_BACKEND:
        from agent.gemini_client import GeminiClient  # local import: keeps httpx optional for the Ollama-only path

        return GeminiClient()
    return ollama.AsyncClient(host=LOCAL_LLM_BASE_URL)


async def structured_chat(
    system: str,
    user_message: str,
    output_model: Type[T],
    model: Optional[str] = None,
) -> T:
    """One-shot call constrained to return JSON matching output_model's
    schema — the local equivalent of client.messages.parse(output_format=X)."""
    client = _client()
    try:
        response = await client.chat(
            model=model or _default_model(),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user_message},
            ],
            format=output_model.model_json_schema(),
            keep_alive=LOCAL_LLM_KEEP_ALIVE,
            think=False,
        )
    except (ollama.ResponseError, ConnectionError, TimeoutError) as exc:
        raise LocalLLMError(f"Could not reach the local model at {LOCAL_LLM_BASE_URL}: {exc}") from exc
    except (NvidiaBackendError, GeminiBackendError) as exc:
        raise LocalLLMError(str(exc)) from exc

    content = response.message.content or ""
    try:
        return output_model.model_validate_json(content)
    except ValidationError as exc:
        raise LocalLLMError(f"Local model response did not match the expected schema: {exc}") from exc


def local_tool(func: Callable) -> Callable:
    """
    Decorator mirroring anthropic.beta_async_tool's ergonomics closely
    enough that call sites only change the decorator, not the function body
    or how it's invoked (build_tools(ctx) still returns plain callables that
    tests already call directly as `await some_tool(**kwargs)`).

    Infers a JSON schema from the function's type-hinted parameters — every
    tool in this codebase already has plain str/int/float/bool params with
    a docstring, so a full JSON-Schema-from-Python-types library would be
    overkill; this covers exactly what's actually used.
    """
    sig = inspect.signature(func)
    fields: dict[str, Any] = {}
    for name, param in sig.parameters.items():
        annotation = param.annotation if param.annotation is not inspect.Parameter.empty else str
        default = param.default if param.default is not inspect.Parameter.empty else ...
        fields[name] = (annotation, default)
    schema_model = create_model(f"{func.__name__}_Params", **fields)  # type: ignore[call-overload]

    func.name = func.__name__  # type: ignore[attr-defined]
    func.description = (func.__doc__ or "").strip()  # type: ignore[attr-defined]
    func.input_schema = schema_model.model_json_schema()  # type: ignore[attr-defined]
    return func


def _tool_definition(tool: Callable) -> dict:
    schema = dict(tool.input_schema)  # type: ignore[attr-defined]
    schema.setdefault("type", "object")
    return {
        "type": "function",
        "function": {
            "name": tool.name,  # type: ignore[attr-defined]
            "description": tool.description,  # type: ignore[attr-defined]
            "parameters": schema,
        },
    }


@dataclass
class ToolLoopTurn:
    type: Literal["tool_call", "text"]
    tool_name: Optional[str] = None
    tool_input: Optional[dict] = None
    tool_result: Optional[str] = None
    text: Optional[str] = None
    # Ollama's rough token-count equivalents (prompt_eval_count/eval_count)
    # — same purpose as the old AgentTrace.token_usage_input/output fields,
    # kept for continuity even though "cost" doesn't apply the same way to
    # local, free inference.
    tokens_in: Optional[int] = None
    tokens_out: Optional[int] = None


# Tools where repeating an identical (tool_name, args) call within the same
# step is genuinely redundant — the action already happened, doing it again
# changes nothing but risks a double-submit. Deliberately NOT read_page or
# assert_condition, which this project's tool set is expected to grow to
# include more of over time — see run_tool_loop()'s completed_calls comment
# for why those need a fresh read every time, not a cached one.
_DEDUPABLE_TOOLS = frozenset({"click", "fill", "select_option", "navigate"})

# A dedupable tool can signal failure by RETURNING a string, not just by
# raising (agent/tools/playwright_tools.py's selector resolver does this —
# "Could not find an element matching...on the page." — when every
# mechanical/LLM tier fails to resolve a hint). Only the raised-TypeError
# path is otherwise excluded from completed_calls; without this check a
# failed resolution would get cached as if it had succeeded, and a later
# legitimate retry with corrected arguments would silently be answered
# from the stale failure instead of actually trying again. Prefix-matched,
# not a full-string comparison, since the exact wording continues past
# this point (the hint, "on the page.").
_KNOWN_TOOL_FAILURE_PREFIX = "Could not find an element matching"


async def run_tool_loop(
    system: str,
    messages: list[dict],
    tools: list[Callable],
    max_iterations: int = 6,
    model: Optional[str] = None,
) -> AsyncIterator[ToolLoopTurn]:
    """
    A ReAct loop over `tools` (each decorated with @local_tool): call the
    model, and if it requests a tool, actually invoke it and feed the
    result back as a `role: "tool"` message, repeating until the model
    responds with plain text or max_iterations is hit. Mutates `messages`
    in place, so the caller's own list carries the full conversation
    forward — no private-SDK-internals workaround needed for cross-call
    continuity (the old anthropic.beta.messages.tool_runner()-based
    executor.py had to reach into `runner._params["messages"]` for this;
    owning the loop makes that unnecessary).
    """
    client = _client()
    tool_defs = [_tool_definition(t) for t in tools]
    tools_by_name = {t.name: t for t in tools}  # type: ignore[attr-defined]
    # Tracks (tool_name, sorted-args-json) pairs already dispatched via the
    # leaked-tool-call recovery path this step — see the loop below. The
    # model can keep re-narrating (and re-leaking) the same
    # already-completed action turn after turn instead of ever producing
    # a clean final answer (observed live: a single fill step re-running
    # 6 times before max_iterations gave up) — once a given leaked call
    # has been recovered once, a later turn repeating the identical call
    # is almost certainly the model echoing what it already did, not a
    # new action to take, so it's treated as a final answer instead.
    already_recovered: set[tuple[str, str]] = set()
    # Tracks every REAL tool call this step has already executed and its
    # result, keyed the same way as already_recovered (tool_name,
    # sorted-args-json). Observed live (AD_LG_04): the model can issue a
    # second, genuine, structurally valid tool call identical to one that
    # already succeeded earlier in the same step — not a leaked/malformed
    # response (already_recovered's job), a real msg.tool_calls entry — even
    # though the prior call's result was a clear, unambiguous success message
    # and the system prompt explicitly says not to do this. Re-running an
    # identical fill/click against the live page a second time is redundant
    # at best (wasted round-trip + model call) and risky at worst (double-
    # submitting a form). A repeat is answered from this cache instead of
    # actually re-invoking the tool. Scoped to action tools only
    # (_DEDUPABLE_TOOLS below) — read_page/assert_condition are
    # deliberately excluded: the same args legitimately warrant a fresh
    # read if the page changed between calls (e.g. a toast that appeared
    # since the first read), so caching those risks a real correctness bug
    # to fix a performance one.
    completed_calls: dict[tuple[str, str], str] = {}

    for _ in range(max_iterations):
        try:
            response = await client.chat(
                model=model or _default_model(),
                messages=[{"role": "system", "content": system}] + messages,
                tools=tool_defs,
                keep_alive=LOCAL_LLM_KEEP_ALIVE,
                think=False,
            )
        except (ollama.ResponseError, ConnectionError, TimeoutError) as exc:
            raise LocalLLMError(f"Could not reach the local model at {LOCAL_LLM_BASE_URL}: {exc}") from exc
        except (NvidiaBackendError, GeminiBackendError) as exc:
            raise LocalLLMError(str(exc)) from exc

        msg = response.message
        tokens_in, tokens_out = response.prompt_eval_count, response.eval_count

        if msg.tool_calls:
            reasoning_text = msg.content or ""
            if _looks_leaked(reasoning_text):
                reasoning_text = await _translate_to_english(reasoning_text, model)
            messages.append({"role": "assistant", "content": reasoning_text})
            if reasoning_text:
                # The model can emit reasoning text alongside a tool call in
                # the same turn — surfaced as its own turn so callers can
                # trace it, same visibility the old Anthropic tool_runner
                # gave (it yielded every text block, not just the final one).
                yield ToolLoopTurn(type="text", text=reasoning_text, tokens_in=tokens_in, tokens_out=tokens_out)
            for call in msg.tool_calls:
                tool_name = call.function.name
                tool_input = dict(call.function.arguments)
                call_key = (tool_name, json.dumps(tool_input, sort_keys=True, default=str))
                tool = tools_by_name.get(tool_name)
                if tool_name in _DEDUPABLE_TOOLS and call_key in completed_calls:
                    # Identical (tool_name, args) already executed
                    # successfully earlier this step — see completed_calls'
                    # definition above. Answer from the cached result
                    # instead of re-invoking the tool against the live page.
                    result = completed_calls[call_key]
                elif tool is None:
                    result = f"Unknown tool: {tool_name!r}"
                else:
                    try:
                        result = await tool(**tool_input)
                    except TypeError as exc:
                        # The model can hallucinate an argument name/shape
                        # that doesn't match the tool's real signature (e.g.
                        # calling navigate(description=...) when it only
                        # accepts url) — previously this TypeError propagated
                        # all the way up and crashed the entire run instead
                        # of just this one step. Feeding the error back as a
                        # normal tool result lets the model see what went
                        # wrong and retry with corrected arguments, the same
                        # way a real tool-calling API returns a 4xx rather
                        # than crashing the caller.
                        result = f"Tool call failed: {exc}"
                    else:
                        if tool_name in _DEDUPABLE_TOOLS and not str(result).startswith(
                            _KNOWN_TOOL_FAILURE_PREFIX
                        ):
                            completed_calls[call_key] = result
                yield ToolLoopTurn(
                    type="tool_call", tool_name=tool_name, tool_input=tool_input, tool_result=result
                )
                messages.append({"role": "tool", "content": str(result)})
            continue

        text = msg.content or ""

        # Checked against the leak detector BEFORE translation — the
        # leaked-tool-call regexes look for literal JSON key names
        # ("name", "arguments", or a real tool name), which a translation
        # pass could otherwise mangle or paraphrase away.
        leaked = _extract_leaked_tool_call(text, tools_by_name.keys())
        leaked_key = (leaked[0], json.dumps(leaked[1], sort_keys=True)) if leaked else None
        if leaked and leaked[0] in tools_by_name and leaked_key not in already_recovered:
            # The model wrote out a tool call as JSON prose instead of
            # issuing a real structured tool call — msg.tool_calls is
            # empty, so without this the loop would fall straight through
            # to treating this as the step's final answer, silently
            # skipping the action the model clearly intended to take (e.g.
            # narrating "I clicked Login" while never actually clicking
            # it). Recovered and dispatched the same way a real tool call
            # is, so the intended action still actually happens. Recorded
            # in already_recovered so an identical call repeated in a
            # later turn (the model echoing what it already did) falls
            # through to the final-answer branch below instead of
            # re-running the same action forever.
            already_recovered.add(leaked_key)
            tool_name, tool_input = leaked
            display_text = await _translate_to_english(text, model) if _looks_leaked(text) else text
            messages.append({"role": "assistant", "content": display_text})
            yield ToolLoopTurn(type="text", text=display_text, tokens_in=tokens_in, tokens_out=tokens_out)
            tool = tools_by_name[tool_name]
            try:
                result = await tool(**tool_input)
            except TypeError as exc:
                result = f"Tool call failed: {exc}"
            yield ToolLoopTurn(
                type="tool_call", tool_name=tool_name, tool_input=tool_input, tool_result=result
            )
            messages.append({"role": "tool", "content": str(result)})
            continue

        if _looks_leaked(text):
            text = await _translate_to_english(text, model)
        messages.append({"role": "assistant", "content": text})
        yield ToolLoopTurn(type="text", text=text, tokens_in=tokens_in, tokens_out=tokens_out)
        return

    yield ToolLoopTurn(type="text", text="(max iterations reached without a final answer)")
