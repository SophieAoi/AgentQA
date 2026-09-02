"""
Keeps secret-looking field values out of persisted logs/traces. A fill()
call whose selector_hint suggests a password/secret field gets its value
redacted before it's written to ExecutionStep.tool_input or AgentTrace
content — both are held in the in-memory store and exposed via
GET /test-runs/{id}/trace, so a real password typed during a login test
case (e.g. LGN-006) must never land there in plaintext.

CREDENTIAL_PLACEHOLDERS: test case descriptions and the Executor's own tool
calls reference the valid test account via literal {{VALID_EMAIL}} /
{{VALID_PASSWORD}} tokens, never the real value — those tokens are the only
form of the credential that ever enters Claude's context, gets logged, or
gets traced. resolve_credential_placeholder() swaps a token for the real
config value at the last possible moment, immediately before the actual
Playwright fill() call, so the real secret exists as a bare Python string
for the duration of exactly one call and is never written to a log/trace.
"""

import re
from typing import Optional

from app.config import INFLUENCE_TEST_PASSWORD, INFLUENCE_TEST_USERNAME

SENSITIVE_HINT_KEYWORDS = ("password", "passcode", "secret", "totp", "otp", "pin", "2fa")

CREDENTIAL_PLACEHOLDERS = {
    "{{VALID_EMAIL}}": INFLUENCE_TEST_USERNAME or "",
    "{{VALID_PASSWORD}}": INFLUENCE_TEST_PASSWORD or "",
}

# Observed live: the model wrote {VALID_EMAIL} (single braces) instead of
# the required {{VALID_EMAIL}} — CREDENTIAL_PLACEHOLDERS' exact-match
# lookup silently missed it and resolve_credential_placeholder() fell
# through to returning the value unchanged, so the LITERAL string
# "{VALID_EMAIL}" got typed into the real email field instead of the
# actual test credential (this is how a real login attempt can fail with
# no credentials problem at all — the wrong text was typed, not wrong
# text-that-happens-to-be-a-password). Matches any brace count (one, two,
# or an accidental three) around the token name, not just the documented
# double-brace form, so a model slip like this resolves correctly instead
# of silently typing garbage into a real field.
_PLACEHOLDER_TOKEN_RE = re.compile(r"^\{+(VALID_EMAIL|VALID_PASSWORD)\}+$")

_TOKEN_NAME_TO_VALUE = {
    "VALID_EMAIL": INFLUENCE_TEST_USERNAME or "",
    "VALID_PASSWORD": INFLUENCE_TEST_PASSWORD or "",
}


def resolve_credential_placeholder(value: Optional[str]) -> Optional[str]:
    """Swaps a literal credential placeholder token for its real value.
    Only matches the whole value (not a substring replace) — the Executor
    is instructed to pass the token as the entire fill() value, never
    embedded in a longer string."""
    if value in CREDENTIAL_PLACEHOLDERS:
        return CREDENTIAL_PLACEHOLDERS[value]
    if value:
        match = _PLACEHOLDER_TOKEN_RE.match(value)
        if match:
            return _TOKEN_NAME_TO_VALUE[match.group(1)]
    return value


def is_sensitive_hint(selector_hint: str) -> bool:
    lowered = selector_hint.lower()
    return any(keyword in lowered for keyword in SENSITIVE_HINT_KEYWORDS)


def redact_tool_input(tool_name: str, tool_input: dict) -> dict:
    """Returns a copy of tool_input safe to persist — the original is never
    mutated, since the real value is still needed to actually perform the
    fill() call itself."""
    if tool_name != "fill":
        return tool_input
    hint = str(tool_input.get("selector_hint", ""))
    if not is_sensitive_hint(hint):
        return tool_input
    redacted = dict(tool_input)
    if "value" in redacted:
        redacted["value"] = "[REDACTED]"
    return redacted
