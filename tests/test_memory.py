"""Two-channel memory: token estimate, working-context derivation, and
structured compaction. Pure-logic — no Postgres needed (compaction operates on
the Session object, not the pool)."""

from agentic_devops.config import ModelTier, Settings
from agentic_devops.proxy.providers import ProviderResponse
from agentic_devops.proxy.sessions import (
    PgSessionStore,
    Session,
    render_summary_state,
    SUMMARY_SECTIONS,
)
from agentic_devops.proxy.tokens import count_tokens

_SUMMARY_JSON = (
    '{"objective":"investigate the crash-loop",'
    '"confirmed_findings":["db pool exhausted 20/20 on web-1"],'
    '"decisions":[],"open_hypotheses":["OOM is a downstream symptom"],'
    '"failed_attempts":[],"key_facts":["host web-1, limit=20"],'
    '"next_steps":["raise pool size"]}'
)


class DistillProvider:
    """Stands in for the LLM during compaction; returns a fixed JSON summary."""

    def __init__(self, text=_SUMMARY_JSON):
        self.text = text
        self.calls = 0

    def complete(self, messages, tier, tools=None):
        self.calls += 1
        return ProviderResponse(text=self.text)


def _session_with_exchanges(n, content_len=60):
    s = Session(id="s1", user_id="u")
    for i in range(n):
        s.add_user("q" * content_len)
        s.add_assistant("a" * content_len)
        s.add_findings([{"tool": "host_diagnostics", "result": "r" * content_len, "ok": True}], cap=800)
    return s


# ---- token estimate ----------------------------------------------------------

def test_count_tokens_positive_and_monotonic():
    small = count_tokens([{"role": "user", "content": "x" * 100}], "no-such-model")
    big = count_tokens([{"role": "user", "content": "x" * 1000}], "no-such-model")
    assert isinstance(small, int) and small > 0
    assert big > small


# ---- working context (derived) ----------------------------------------------

def test_working_context_excludes_folded_turns():
    s = Session(
        id="s1",
        messages=[
            {"role": "user", "content": "OLD-Q"},
            {"role": "assistant", "content": "OLD-A"},
            {"role": "user", "content": "NEW-Q"},
            {"role": "assistant", "content": "NEW-A"},
        ],
        summary_state={"objective": "keep the cluster healthy"},
        findings=[
            {"turn": 0, "tool": "t", "result": "OLD-FINDING", "ok": True},
            {"turn": 1, "tool": "t", "result": "NEW-FINDING", "ok": True},
        ],
        compacted_turns=1,
    )
    blob = "\n".join(m["content"] for m in s.working_context())
    assert "keep the cluster healthy" in blob  # summary surfaced
    assert "NEW-Q" in blob and "NEW-A" in blob  # recent turns kept
    assert "NEW-FINDING" in blob  # recent finding kept
    assert "OLD-Q" not in blob and "OLD-A" not in blob  # folded turns gone
    assert "OLD-FINDING" not in blob  # folded finding gone


def test_render_summary_state_sections():
    out = render_summary_state({"objective": "X", "confirmed_findings": ["a", "b"]})
    assert "Objective: X" in out
    assert "- a" in out and "- b" in out
    assert render_summary_state({}) == ""


# ---- compaction --------------------------------------------------------------

def test_compaction_folds_old_and_preserves_display():
    store = PgSessionStore(pool=None)  # compact_if_needed doesn't touch the pool
    provider = DistillProvider()
    # Tiny window forces the trigger; default keep_recent_exchanges = 4.
    tier = ModelTier(model="fake", context_window=50)
    settings = Settings()
    s = _session_with_exchanges(6)
    n_messages_before = len(s.messages)

    did = store.compact_if_needed(s, provider, tier, settings)

    assert did is True
    assert provider.calls == 1
    # Display transcript is lossless — never trimmed.
    assert len(s.messages) == n_messages_before == 12
    # Folded the oldest exchanges, keeping the last 4.
    assert s.compacted_turns == 2
    # Structured summary populated from the distiller.
    assert s.summary_state["objective"] == "investigate the crash-loop"
    assert any(key in s.summary_state for key, _ in SUMMARY_SECTIONS)
    # Folded findings pruned; recent ones (turn >= 2) remain.
    assert all(f["turn"] >= 2 for f in s.findings)


def test_compaction_noop_under_threshold():
    store = PgSessionStore(pool=None)
    provider = DistillProvider()
    tier = ModelTier(model="fake", context_window=1_000_000)  # huge → never triggers
    s = _session_with_exchanges(6)

    assert store.compact_if_needed(s, provider, tier, Settings()) is False
    assert provider.calls == 0
    assert s.compacted_turns == 0


def test_compaction_aborts_on_bad_distill_output():
    store = PgSessionStore(pool=None)
    provider = DistillProvider(text="not json at all")
    tier = ModelTier(model="fake", context_window=50)
    s = _session_with_exchanges(6)

    # Trigger fires, distill fails to parse → session left intact (best-effort).
    assert store.compact_if_needed(s, provider, tier, Settings()) is False
    assert s.compacted_turns == 0
    assert s.summary_state == {}
