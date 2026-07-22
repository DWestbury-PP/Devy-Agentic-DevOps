"""Session persistence and context compaction (Postgres-backed, two-channel).

A conversation has two representations (Phase 7):

* **Display channel** (``messages``) — the lossless, append-only transcript of
  user prompts + Devy's final answers. It is what the UI renders and what
  "copy as markdown" exports. It is *never* trimmed.
* **Context channel** — Devy's derived working memory, kept small: a structured
  rolling ``summary_state`` plus distilled tool ``findings``. ``compacted_turns``
  marks how many leading exchanges have been folded into ``summary_state``;
  assembly uses the exchanges after that point + the summary + recent findings.

Compaction is token-triggered (off the active tier's context window) and folds
the oldest not-yet-folded exchanges into ``summary_state`` via a cheap LLM call —
the display transcript is left untouched. Findings are stored as plain text, so a
tool_call/result pair can never be split (the property the clean-history design
guaranteed). See docs/JOURNEY.md.
"""

from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from psycopg.types.json import Json
from psycopg_pool import ConnectionPool

from agentic_devops.config import ModelTier, Settings
from agentic_devops.proxy.providers import ProviderClient
from agentic_devops.proxy.tokens import count_tokens

# Structured summary sections. `objective` is a string; the rest are string lists.
# Tuned for DevOps/SRE work — "open_hypotheses" maps onto Devy's RCA reasoning.
SUMMARY_SECTIONS: list[tuple[str, str]] = [
    ("objective", "Objective"),
    ("confirmed_findings", "Confirmed findings"),
    ("decisions", "Decisions"),
    ("open_hypotheses", "Open hypotheses"),
    ("failed_attempts", "Failed attempts / ruled out"),
    ("key_facts", "Key host/service facts"),
    ("next_steps", "Next steps"),
]

_DISTILL_PROMPT = """\
You maintain a compact, structured working memory for a DevOps/SRE co-pilot so it \
can continue a long investigation after older turns are dropped from its context.

Merge the PRIOR MEMORY with the NEW CONVERSATION and TOOL EVIDENCE below into an \
updated memory. Preserve specifics that are easy to lose but matter: exact \
numbers, hostnames, container/service names, error strings, file paths, and any \
citations. Drop redundancy and chit-chat. Be concise and factual — do not invent.

Output ONLY a JSON object with these keys:
- "objective": string — what the user is ultimately trying to do
- "confirmed_findings": array of strings — established facts, with evidence/citations
- "decisions": array of strings
- "open_hypotheses": array of strings — still being investigated
- "failed_attempts": array of strings — things tried and ruled out
- "key_facts": array of strings — durable host/service/config facts
- "next_steps": array of strings

PRIOR MEMORY (JSON):
{prior}

NEW CONVERSATION:
{transcript}

TOOL EVIDENCE:
{findings}
"""


def _flatten_content(content: Any, digests: Optional[dict[str, str]] = None) -> str:
    """Collapse a structured (multimodal) message content to plain text for the
    context channel: text parts are kept; an ``image_ref`` part becomes a text
    stand-in — its pixels are NOT re-sent (the "process once" invariant). When a
    one-time ``digest`` is available (Phase 3) the stand-in carries the image's
    description + its id so the model can call ``view_image`` to see it again;
    otherwise it's a bare placeholder. A plain string passes through unchanged."""
    if not isinstance(content, list):
        return content or ""
    parts: list[str] = []
    for p in content:
        if not isinstance(p, dict):
            parts.append(str(p))
        elif p.get("type") == "text":
            parts.append(p.get("text", ""))
        elif p.get("type") == "image_ref":
            name = p.get("name") or "image"
            ref = p.get("ref", "")
            digest = (digests or {}).get(ref)
            if digest:
                parts.append(
                    f'[Image the user attached earlier — "{name}" (id: {ref}). '
                    f"Description: {digest}\n"
                    "Call view_image with this id to look at the actual image again.]"
                )
            else:
                parts.append(f'[Image the user attached earlier — "{name}" (id: {ref})]')
    return "\n".join(x for x in parts if x).strip()


def render_summary_state(state: dict[str, Any]) -> str:
    """Render the structured summary into a readable block for the system prompt."""
    if not state:
        return ""
    lines: list[str] = []
    for key, label in SUMMARY_SECTIONS:
        val = state.get(key)
        if not val:
            continue
        if isinstance(val, list):
            items = [str(v).strip() for v in val if str(v).strip()]
            if not items:
                continue
            lines.append(f"{label}:")
            lines.extend(f"- {it}" for it in items)
        else:
            lines.append(f"{label}: {val}")
    return "\n".join(lines)


def render_findings(findings: list[dict[str, Any]]) -> str:
    """Render distilled tool findings as a compact bullet list."""
    lines: list[str] = []
    for f in findings or []:
        detail = (f.get("finding") or f.get("result") or "").strip()
        if not detail:
            continue
        status = "" if f.get("ok", True) else " [failed]"
        lines.append(f"- {f.get('tool', 'tool')}{status}: {detail}")
    return "\n".join(lines)


@dataclass
class Session:
    id: str
    messages: list[dict[str, Any]] = field(default_factory=list)  # display transcript
    summary_state: dict[str, Any] = field(default_factory=dict)  # structured rolling summary
    findings: list[dict[str, Any]] = field(default_factory=list)  # context-channel tool evidence
    compacted_turns: int = 0  # leading exchanges folded into summary_state
    user_id: Optional[str] = None
    title: Optional[str] = None
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def add_user(self, content: Any) -> None:
        """Store a user turn. ``content`` is a plain string, or a list of parts
        ``[{type:"text",...}, {type:"image_ref", ref, mime, name}]`` when the turn
        carried attachments — the display transcript keeps the image *reference*
        (the blob hash), never the base64."""
        self.messages.append({"role": "user", "content": content, "ts": time.time()})

    def add_assistant(self, content: str) -> None:
        self.messages.append({"role": "assistant", "content": content, "ts": time.time()})
        self.updated_at = time.time()

    def add_findings(self, findings: list[dict[str, Any]], cap: int) -> None:
        """Append this turn's tool findings to the context channel (raw, truncated)."""
        turn = max(0, len(self.messages) // 2 - 1)
        for f in findings or []:
            self.findings.append(
                {
                    "turn": turn,
                    "tool": f.get("tool", "tool"),
                    "intent": f.get("intent", ""),
                    "result": (f.get("result") or "")[:cap],
                    "ok": bool(f.get("ok", True)),
                }
            )

    def recent_findings(self) -> list[dict[str, Any]]:
        return [f for f in self.findings if f.get("turn", 0) >= self.compacted_turns]

    def recent_image_refs(self) -> list[str]:
        """Image refs (hashes) in the non-compacted window — the ones
        ``working_context`` will render, so their digests are worth ensuring."""
        refs: list[str] = []
        for m in self.messages[2 * self.compacted_turns :]:
            content = m.get("content")
            if isinstance(content, list):
                refs.extend(p["ref"] for p in content
                            if isinstance(p, dict) and p.get("type") == "image_ref" and p.get("ref"))
        return refs

    def working_context(self, digests: Optional[dict[str, str]] = None) -> list[dict[str, Any]]:
        """The derived context channel: summary + recent display turns + findings.

        This is what the model sees — NOT the full display transcript. ``digests``
        maps an image ref → its one-time description, used to flatten past
        image-carrying turns (Phase 3); without it, past images are bare
        placeholders.
        """
        ctx: list[dict[str, Any]] = []
        summary_text = render_summary_state(self.summary_state)
        if summary_text:
            ctx.append(
                {"role": "system", "content": f"Summary of earlier conversation:\n{summary_text}"}
            )
        # Strip the per-message ``ts`` (a display-channel annotation, see add_user)
        # so the provider only ever sees role/content — never an unknown key. Also
        # FLATTEN any image-carrying turn to text (image_ref → "[image]" placeholder):
        # a PAST image is never re-sent as pixels — that's the "process once"
        # invariant (only the current turn inlines pixels; see assemble_messages).
        # Phase 3 replaces the placeholder with the image's cached digest.
        ctx.extend(
            {"role": m["role"], "content": _flatten_content(m.get("content"), digests)}
            for m in self.messages[2 * self.compacted_turns :]
        )
        findings_text = render_findings(self.recent_findings())
        if findings_text:
            ctx.append(
                {
                    "role": "system",
                    "content": (
                        "Tool evidence gathered earlier in this conversation (for your "
                        f"reference; the user does not see this):\n{findings_text}"
                    ),
                }
            )
        return ctx


@dataclass
class SessionSummary:
    """A lightweight descriptor for listing a user's conversations."""

    id: str
    user_id: Optional[str]
    title: Optional[str]
    updated_at: str  # ISO-8601
    turns: int
    preview: str  # first user message, truncated


def _parse_summary_state(text: str, prior: dict[str, Any]) -> Optional[dict[str, Any]]:
    """Parse the distiller's JSON; normalize to the section schema. None on failure."""
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
    except (json.JSONDecodeError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    state: dict[str, Any] = {}
    for key, _ in SUMMARY_SECTIONS:
        val = data.get(key)
        if key == "objective":
            state[key] = str(val).strip() if val else (prior or {}).get("objective", "")
        elif isinstance(val, list):
            state[key] = [str(v).strip() for v in val if str(v).strip()]
        elif val:
            state[key] = [str(val).strip()]
        else:
            state[key] = []
    return state


def _distill(
    provider: ProviderClient,
    tier: ModelTier,
    settings: Settings,
    prior_state: dict[str, Any],
    fold_msgs: list[dict[str, Any]],
    fold_findings: list[dict[str, Any]],
) -> Optional[dict[str, Any]]:
    """Fold a span of exchanges + their findings into an updated summary_state.

    Uses the cheap ``fast`` tier when configured. Returns None on any failure
    (callers then leave the session intact rather than risk losing context).
    """
    try:
        dtier = settings.resolve_tier("fast")
    except KeyError:
        dtier = tier
    transcript = "\n".join(f"{m['role']}: {m.get('content') or ''}" for m in fold_msgs)
    prompt = _DISTILL_PROMPT.format(
        prior=json.dumps(prior_state or {}, indent=2),
        transcript=transcript or "(none)",
        findings=render_findings(fold_findings) or "(none)",
    )
    try:
        result = provider.complete([{"role": "user", "content": prompt}], tier=dtier, tools=None)
        text = (result.text or "").strip()
    except Exception:  # noqa: BLE001 — distillation must never crash a turn
        return None
    if not text:
        return None
    return _parse_summary_state(text, prior_state)


_TITLE_PROMPT = (
    "Write a short title (2-5 words, Title Case, no quotes, no trailing "
    "punctuation) that captures what this conversation is about.\n\n"
    "User: {q}\nAssistant: {a}\n\nTitle:"
)


def generate_title(
    provider: ProviderClient, settings: Settings, first_user: str, first_answer: str
) -> Optional[str]:
    """Generate a short conversation title with the cheap ``fast`` tier.

    Best-effort: returns None on any failure (the caller leaves the title unset
    and retries on a later turn).
    """
    try:
        tier = settings.resolve_tier("fast")
    except KeyError:
        return None
    prompt = _TITLE_PROMPT.format(q=first_user[:500], a=first_answer[:500])
    try:
        result = provider.complete([{"role": "user", "content": prompt}], tier=tier, tools=None)
        text = (result.text or "").strip()
    except Exception:  # noqa: BLE001
        return None
    title = text.splitlines()[0].strip().strip('"').strip("'").strip() if text else ""
    return title[:80] or None


class PgSessionStore:
    """Conversation history in Postgres (``sessions`` table), two-channel."""

    def __init__(self, pool: ConnectionPool) -> None:
        self._pool = pool

    def new(self, user_id: Optional[str] = None) -> Session:
        return Session(id=uuid.uuid4().hex[:12], user_id=user_id)

    def load(self, session_id: Optional[str], user_id: Optional[str] = None) -> Session:
        if not session_id:
            return self.new(user_id=user_id)
        with self._pool.connection() as conn:
            row = conn.execute(
                "SELECT messages, summary_state, findings, compacted_turns, user_id, title "
                "FROM sessions WHERE id = %s",
                (session_id,),
            ).fetchone()
        if row is None:
            return Session(id=session_id, user_id=user_id)
        messages, summary_state, findings, compacted_turns, stored_user, title = row
        return Session(
            id=session_id,
            messages=list(messages or []),
            summary_state=dict(summary_state or {}),
            findings=list(findings or []),
            compacted_turns=int(compacted_turns or 0),
            user_id=stored_user or user_id,
            title=title,
        )

    def save(self, session: Session) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                """
                INSERT INTO sessions
                    (id, user_id, title, messages, summary_state, findings, compacted_turns, updated_at)
                VALUES (%s, %s, %s, %s, %s, %s, %s, now())
                ON CONFLICT (id) DO UPDATE SET
                    user_id         = COALESCE(EXCLUDED.user_id, sessions.user_id),
                    title           = COALESCE(EXCLUDED.title, sessions.title),
                    messages        = EXCLUDED.messages,
                    summary_state   = EXCLUDED.summary_state,
                    findings        = EXCLUDED.findings,
                    compacted_turns = EXCLUDED.compacted_turns,
                    updated_at      = now()
                """,
                (
                    session.id,
                    session.user_id,
                    session.title,
                    Json(session.messages),
                    Json(session.summary_state),
                    Json(session.findings),
                    session.compacted_turns,
                ),
            )

    def rename(self, session_id: str, title: str) -> None:
        with self._pool.connection() as conn:
            conn.execute(
                "UPDATE sessions SET title = %s, updated_at = now() WHERE id = %s",
                (title, session_id),
            )

    def delete(self, session_id: str) -> None:
        with self._pool.connection() as conn:
            conn.execute("DELETE FROM sessions WHERE id = %s", (session_id,))

    def list_for_user(self, user_id: str, limit: int = 50) -> list[SessionSummary]:
        """Most-recently-updated conversations for a user (for recall)."""
        with self._pool.connection() as conn:
            rows = conn.execute(
                """
                SELECT id, user_id, title, updated_at,
                       jsonb_array_length(messages) AS turns, messages
                FROM sessions
                WHERE user_id = %s
                ORDER BY updated_at DESC
                LIMIT %s
                """,
                (user_id, limit),
            ).fetchall()
        summaries: list[SessionSummary] = []
        for id_, uid, title, updated_at, turns, messages in rows:
            preview = ""
            for m in messages or []:
                if m.get("role") == "user":
                    preview = (m.get("content") or "")[:120]
                    break
            summaries.append(
                SessionSummary(
                    id=id_,
                    user_id=uid,
                    title=title,
                    updated_at=updated_at.isoformat(),
                    turns=turns or 0,
                    preview=preview,
                )
            )
        return summaries

    def compact_if_needed(
        self,
        session: Session,
        provider: ProviderClient,
        tier: ModelTier,
        settings: Settings,
    ) -> bool:
        """Fold older exchanges into summary_state when the context grows too large.

        Token-triggered off the active tier's window. The display transcript
        (``messages``) is never trimmed. Returns True if compaction happened;
        best-effort — on any distillation failure the session is left intact.
        """
        keep = settings.keep_recent_exchanges
        n_exchanges = len(session.messages) // 2
        if n_exchanges - session.compacted_turns <= keep:
            return False

        window = tier.context_window or settings.default_context_window
        threshold = int(window * settings.compaction_ratio)
        if count_tokens(session.working_context(), tier.model) < threshold:
            return False

        fold_to = n_exchanges - keep
        fold_msgs = session.messages[2 * session.compacted_turns : 2 * fold_to]
        fold_findings = [
            f for f in session.findings if session.compacted_turns <= f.get("turn", 0) < fold_to
        ]
        new_state = _distill(
            provider, tier, settings, session.summary_state, fold_msgs, fold_findings
        )
        if new_state is None:
            return False

        session.summary_state = new_state
        session.compacted_turns = fold_to
        session.findings = [f for f in session.findings if f.get("turn", 0) >= fold_to]
        return True
