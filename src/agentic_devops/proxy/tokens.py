"""Token estimation for compaction triggers.

The authoritative count is the provider's ``usage.input_tokens`` (returned per
call); this is for the *pre-call* estimate that decides whether to compact. We
prefer LiteLLM's per-model ``token_counter`` and fall back to a ~4-chars/token
heuristic when the model is unknown or litellm isn't importable (keeps tests
hermetic).
"""

from __future__ import annotations

from typing import Any


def count_tokens(messages: list[dict[str, Any]], model: str) -> int:
    """Estimate the token count of a message list for ``model``."""
    try:
        import litellm

        normalized = [
            {"role": m.get("role", "user"), "content": m.get("content") or ""} for m in messages
        ]
        return int(litellm.token_counter(model=model, messages=normalized))
    except Exception:  # noqa: BLE001 — estimation must never raise into the request path
        chars = sum(len(m.get("content") or "") for m in messages)
        return chars // 4
