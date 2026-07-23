"""``request_action`` — the propose-only seam for guarded mutating actions (G-2b).

Devy has NO tool that executes a mutation. This tool writes a PROPOSED row and
returns "awaiting approval"; a human approves out-of-band and the proxy's
executor runs the verb. "Never self-approve" is therefore structural, not a
prompt rule. ``safety_tier="elevated"`` RBAC-gates who may even propose;
``wants_context`` stamps provenance (user/session) — never model-supplied.
"""

from __future__ import annotations

from typing import Any, Optional

from agentic_devops.proxy.actions import ACTION_CATALOG, ActionStore, action_public
from agentic_devops.tools.base import ToolResult, ToolSpec


def build_request_action_tool(
    store: ActionStore, *, ttl_seconds: int, default_host: Optional[str] = None
) -> ToolSpec:
    """Construct ``request_action`` bound to an ActionStore. ``default_host`` is
    used when the model omits a target (the single mounted host in the common case)."""
    verbs = ", ".join(sorted(ACTION_CATALOG))

    def handler(args: dict[str, Any], context: dict[str, Any]) -> str:
        verb = str(args.get("verb", "")).strip()
        spec = ACTION_CATALOG.get(verb)
        if spec is None:
            return f"ERROR: unknown action {verb!r}. Available: {verbs}."
        rationale = str(args.get("rationale", "")).strip()
        if not rationale:
            return (
                "ERROR: 'rationale' is required — explain WHY this action is needed "
                "(it is shown to the human approver)."
            )
        verb_args = dict((args.get("args") or {}))
        missing = [p for p in spec.required if not str(verb_args.get(p, "")).strip()]
        if missing:
            return (
                f"ERROR: {verb!r} requires {list(spec.required)}; missing: {missing}. "
                "Pass them under 'args'."
            )
        host = str(args.get("host", "")).strip() or default_host

        action = store.create(
            verb=verb, args=verb_args, rationale=rationale,
            reversibility=spec.reversibility, host=host,
            session_id=context.get("session_id"), user_id=context.get("user_id"),
            ttl_seconds=ttl_seconds,
        )
        tgt = f" {action.target}" if action.target else ""
        where = f" on {host}" if host else ""
        msg = (
            f"Proposed action {action.id}: {spec.label}{tgt}{where} — AWAITING HUMAN "
            "APPROVAL. I have NOT run anything. A human must approve it before it "
            "executes, and it expires if not approved in time. Tell the user exactly "
            "what you've proposed and why, and that they need to approve it."
        )
        # Carry an out-of-band UI signal so the surface can render an approval card
        # in real time; the model just sees `msg`.
        return ToolResult(text=msg, event={"type": "action_proposed", "action": action_public(action)})

    return ToolSpec(
        name="request_action",
        category="actions",
        description=(
            "PROPOSE a guarded, reversible remediation for a human to approve — you do "
            "NOT execute it yourself. Use this instead of saying you'll restart/reload/"
            "prune something (you have no tool that mutates directly). Available "
            "actions: " + verbs + ". The proposal is shown to a human who approves or "
            "denies it; only on approval does the proxy run it on the host. Always give "
            "a clear `rationale` — the human sees it."
        ),
        when_to_use=(
            "When the fix for a diagnosed problem IS one of the reversible remediations "
            "(restart a crash-looping service or container, reload a service's config, "
            "prune unused images to reclaim disk) and you want to carry it out rather "
            "than only recommend a command. Propose it; a human approves; the proxy "
            "executes. Never claim you restarted/changed something — you proposed it."
        ),
        use_cases=[
            "restart the crash-looping alloy service",
            "restart the api container",
            "reload nginx config after a change",
            "prune unused docker images to free disk",
        ],
        input_schema={
            "type": "object",
            "properties": {
                "verb": {
                    "type": "string",
                    "enum": sorted(ACTION_CATALOG),
                    "description": "Which reversible action to propose.",
                },
                "args": {
                    "type": "object",
                    "description": "Action arguments — restart_service / reload_config need "
                    "{name}, restart_container needs {container}, prune_images needs none.",
                },
                "host": {
                    "type": "string",
                    "description": "Optional target host identifier; omit for the default mounted host.",
                },
                "rationale": {
                    "type": "string",
                    "description": "Why this action is needed — shown to the human approver.",
                },
            },
            "required": ["verb", "rationale"],
        },
        handler=handler,
        safety_tier="elevated",
        wants_context=True,
    )
