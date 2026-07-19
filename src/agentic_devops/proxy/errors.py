"""Provider-error classification: raw LiteLLM/vendor exceptions → a friendly,
Devy-voiced message plus a *failover* decision.

Two consumers share this one classifier:

- :class:`~agentic_devops.proxy.providers.ProviderClient` uses ``failoverable``
  to decide whether a failed primary call is worth retrying on a backup model
  (transient/account-level failures) or is hopeless on any provider (the same
  input fails everywhere: context-too-large, content policy, malformed request).
- The FastAPI surfaces use ``user_message`` to show something human instead of a
  raw ``litellm.BadRequestError: AnthropicException — b'{...}'`` blob.

Classification is done by exception *class name* + message substrings + any
``status_code`` — deliberately WITHOUT importing ``litellm`` (keeps this module,
and the tests that use it, dependency-free and offline).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ProviderFailure:
    """The classified outcome of a failed model call."""

    category: str  # credit_exhausted | auth | rate_limit | overloaded | timeout |
    #                connection | context_window | content_policy | bad_request | unknown
    user_message: str  # safe to show a user; never leaks provider internals
    failoverable: bool  # True → worth retrying on a backup model


class ProviderError(Exception):
    """Raised when a model call fails and no (further) backup can help.

    Carries the friendly :attr:`user_message` so the surfaces can render it
    directly; ``str(exc)`` is the same message, so even a generic handler stays
    safe.
    """

    def __init__(self, failure: ProviderFailure, *, tried_backup: bool = False) -> None:
        self.category = failure.category
        self.failoverable = failure.failoverable
        message = failure.user_message
        if tried_backup:
            message += " I also tried a backup model, which failed too."
        self.user_message = message
        super().__init__(message)


def classify(exc: BaseException) -> ProviderFailure:
    """Map a provider exception to a :class:`ProviderFailure`.

    Order matters: billing/credit exhaustion often arrives *as* a
    ``BadRequestError`` or ``AuthenticationError`` (with a telltale message), so
    it's matched first — before the generic auth/bad-request buckets.
    """
    name = type(exc).__name__
    raw = getattr(exc, "message", None) or str(exc)
    low = raw.lower()
    status = getattr(exc, "status_code", None)

    # 1. Billing / credit / quota exhaustion — the screenshot case. Failoverable:
    #    a different provider has its own account, so the backup likely works.
    billing_markers = (
        "credit balance",
        "insufficient_quota",
        "insufficient quota",
        "exceeded your current quota",
        "billing",
        "plans & billing",
        "out of credits",
        "payment required",
    )
    if status == 402 or any(m in low for m in billing_markers):
        return ProviderFailure(
            "credit_exhausted",
            "My primary model provider isn't accepting requests right now "
            "(billing or credit limit reached).",
            failoverable=True,
        )

    # 2. Authentication / permission — the backup provider has its own key.
    if name in ("AuthenticationError", "PermissionDeniedError") or status in (401, 403):
        return ProviderFailure(
            "auth",
            "My primary model provider rejected the credentials.",
            failoverable=True,
        )

    # 3. Rate limited.
    if name == "RateLimitError" or status == 429:
        return ProviderFailure(
            "rate_limit",
            "My primary model provider is rate-limiting requests.",
            failoverable=True,
        )

    # 4. Provider overloaded / unavailable / server error.
    if (
        name in ("ServiceUnavailableError", "InternalServerError")
        or status in (500, 502, 503, 529)
        or "overloaded" in low
    ):
        return ProviderFailure(
            "overloaded",
            "The model provider is temporarily overloaded or unavailable.",
            failoverable=True,
        )

    # 5. Timeout.
    if name in ("Timeout", "APITimeoutError") or "timed out" in low or "timeout" in low:
        return ProviderFailure(
            "timeout",
            "The model provider didn't respond in time.",
            failoverable=True,
        )

    # 6. Network / connection.
    if name == "APIConnectionError" or "connection error" in low:
        return ProviderFailure(
            "connection",
            "I couldn't reach the model provider.",
            failoverable=True,
        )

    # 7. Context window exceeded — NOT failoverable: the same oversized input
    #    fails on every model. Steer the user to reset instead.
    if (
        name == "ContextWindowExceededError"
        or "context length" in low
        or "context window" in low
        or "maximum context" in low
        or "too many tokens" in low
    ):
        return ProviderFailure(
            "context_window",
            "This conversation has grown too large for the model — start a new "
            "chat or narrow the question.",
            failoverable=False,
        )

    # 8. Content policy — NOT failoverable.
    if name == "ContentPolicyViolationError" or "content policy" in low or "content_policy" in low:
        return ProviderFailure(
            "content_policy",
            "The request was blocked by the provider's content policy.",
            failoverable=False,
        )

    # 9. Other malformed request — NOT failoverable (our request, not the provider).
    if name == "BadRequestError" or status == 400:
        return ProviderFailure(
            "bad_request",
            "The model provider rejected the request as malformed.",
            failoverable=False,
        )

    # 10. Unknown — allow failover as a last resort; the message never leaks internals.
    return ProviderFailure(
        "unknown",
        "Something went wrong while contacting the model provider.",
        failoverable=True,
    )
