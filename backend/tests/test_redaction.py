from unittest.mock import patch

from agent.tools.redaction import is_sensitive_hint, redact_tool_input, resolve_credential_placeholder


def test_is_sensitive_hint_matches_password_field():
    assert is_sensitive_hint("the Password field")
    assert is_sensitive_hint("Enter your 6-digit TOTP code")
    assert is_sensitive_hint("2FA verification code")


def test_is_sensitive_hint_does_not_match_ordinary_fields():
    assert not is_sensitive_hint("the Email field")
    assert not is_sensitive_hint("Line Item Name")


def test_redact_tool_input_redacts_password_fill_value():
    result = redact_tool_input("fill", {"selector_hint": "the Password field", "value": "Test1234"})
    assert result["value"] == "[REDACTED]"
    assert result["selector_hint"] == "the Password field"


def test_redact_tool_input_leaves_non_sensitive_fill_untouched():
    result = redact_tool_input("fill", {"selector_hint": "the Email field", "value": "jeki2@jeki.com"})
    assert result["value"] == "jeki2@jeki.com"


def test_redact_tool_input_leaves_non_fill_tools_untouched():
    result = redact_tool_input("click", {"selector_hint": "the Password field"})
    assert result == {"selector_hint": "the Password field"}


def test_redact_tool_input_does_not_mutate_original():
    original = {"selector_hint": "Password", "value": "secret"}
    redact_tool_input("fill", original)
    assert original["value"] == "secret"


def test_resolve_credential_placeholder_resolves_the_documented_double_brace_form():
    # CREDENTIAL_PLACEHOLDERS is built once at import time from the real
    # configured INFLUENCE_TEST_USERNAME/PASSWORD — patching those names
    # after import doesn't retroactively change the already-built dict, so
    # this checks against the real .env-configured value rather than
    # mocking it (unlike the single/extra-brace tests below, which go
    # through the newer _TOKEN_NAME_TO_VALUE lookup and can be mocked).
    with patch("agent.tools.redaction.CREDENTIAL_PLACEHOLDERS", {"{{VALID_EMAIL}}": "real@example.com"}):
        assert resolve_credential_placeholder("{{VALID_EMAIL}}") == "real@example.com"


def test_resolve_credential_placeholder_tolerates_a_single_brace_slip():
    """
    Regression test: observed live — the model wrote {VALID_EMAIL} (single
    braces) instead of the documented {{VALID_EMAIL}} token. The OLD exact
    -match-only implementation silently fell through and returned the
    value unchanged, so the LITERAL string "{VALID_EMAIL}" got typed into
    the real email field on the real staging site instead of the actual
    test credential — a real login failure that looked like a credentials
    problem but wasn't. Must resolve regardless of brace count.
    """
    with patch("agent.tools.redaction._TOKEN_NAME_TO_VALUE", {"VALID_EMAIL": "real@example.com", "VALID_PASSWORD": "x"}):
        assert resolve_credential_placeholder("{VALID_EMAIL}") == "real@example.com"


def test_resolve_credential_placeholder_tolerates_extra_braces():
    with patch("agent.tools.redaction._TOKEN_NAME_TO_VALUE", {"VALID_EMAIL": "real@example.com", "VALID_PASSWORD": "x"}):
        assert resolve_credential_placeholder("{{{VALID_EMAIL}}}") == "real@example.com"


def test_resolve_credential_placeholder_leaves_ordinary_values_untouched():
    assert resolve_credential_placeholder("jeki2@jeki.com") == "jeki2@jeki.com"
    assert resolve_credential_placeholder("") == ""
    assert resolve_credential_placeholder(None) is None


def test_resolve_credential_placeholder_does_not_match_a_token_embedded_in_a_longer_string():
    """Only the whole value being the token resolves — the Executor is
    instructed to pass the token as the entire fill() value, never
    embedded in other text, and this must not start silently
    substring-matching."""
    with patch("agent.tools.redaction._TOKEN_NAME_TO_VALUE", {"VALID_EMAIL": "real@example.com", "VALID_PASSWORD": "x"}):
        assert resolve_credential_placeholder("prefix {{VALID_EMAIL}} suffix") == "prefix {{VALID_EMAIL}} suffix"
