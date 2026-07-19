"""Provider-error classification → friendly message + failover decision."""

from agentic_devops.proxy.errors import ProviderError, classify


def _exc(name, message="", status=None):
    """Build a stand-in for a LiteLLM/vendor exception (name + message + status)."""
    cls = type(name, (Exception,), {})
    e = cls(message)
    if status is not None:
        e.status_code = status
    return e


def test_credit_exhaustion_is_failoverable():
    # The screenshot case: BadRequestError carrying the Anthropic billing message.
    exc = _exc(
        "BadRequestError",
        'AnthropicException - {"error":{"message":"Your credit balance is too low '
        'to access the Anthropic API. Please go to Plans & Billing"}}',
    )
    f = classify(exc)
    assert f.category == "credit_exhausted"
    assert f.failoverable is True
    assert "credit" in f.user_message.lower() or "billing" in f.user_message.lower()


def test_openai_insufficient_quota_is_credit_exhausted():
    f = classify(_exc("RateLimitError", "You exceeded your current quota (insufficient_quota)"))
    assert f.category == "credit_exhausted"
    assert f.failoverable is True


def test_auth_is_failoverable():
    f = classify(_exc("AuthenticationError", "invalid x-api-key", status=401))
    assert f.category == "auth"
    assert f.failoverable is True


def test_rate_limit_is_failoverable():
    f = classify(_exc("RateLimitError", "slow down", status=429))
    assert f.category == "rate_limit"
    assert f.failoverable is True


def test_overloaded_is_failoverable():
    assert classify(_exc("InternalServerError", "Overloaded", status=529)).failoverable is True
    assert classify(_exc("ServiceUnavailableError", "try later", status=503)).category == "overloaded"


def test_timeout_and_connection_are_failoverable():
    assert classify(_exc("Timeout", "request timed out")).category == "timeout"
    assert classify(_exc("APIConnectionError", "Connection error.")).category == "connection"


def test_context_window_is_not_failoverable():
    f = classify(_exc("ContextWindowExceededError", "maximum context length exceeded"))
    assert f.category == "context_window"
    assert f.failoverable is False


def test_content_policy_is_not_failoverable():
    f = classify(_exc("ContentPolicyViolationError", "blocked by content policy"))
    assert f.category == "content_policy"
    assert f.failoverable is False


def test_plain_bad_request_is_not_failoverable():
    # A malformed request (our fault) fails identically everywhere — don't retry.
    f = classify(_exc("BadRequestError", "tools[0].name: invalid", status=400))
    assert f.category == "bad_request"
    assert f.failoverable is False


def test_unknown_defaults_to_failoverable_with_safe_message():
    f = classify(_exc("SomethingWeird", "kaboom \x00 secret-ish"))
    assert f.category == "unknown"
    assert f.failoverable is True
    assert "secret-ish" not in f.user_message  # never leaks provider internals


def test_provider_error_carries_message_and_tried_backup_suffix():
    f = classify(_exc("RateLimitError", "429", status=429))
    plain = ProviderError(f, tried_backup=False)
    tried = ProviderError(f, tried_backup=True)
    assert plain.user_message == f.user_message
    assert str(plain) == plain.user_message  # safe even via a generic handler
    assert "backup model" in tried.user_message
    assert tried.category == "rate_limit"
