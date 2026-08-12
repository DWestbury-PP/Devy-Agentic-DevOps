"""Release-ledger tool — how Devy answers "what can I deploy?" in chat.

A single read-only tool over the CI→CD build ledger (:class:`ReleaseLedger`). It is
the assistant-plane face of the same reader the ``releases`` CLI and
``/v1/admin/releases`` API use — so Devy can reason about deployable builds without
any surface re-parsing the SSM schema.

Read-only and propose-nothing: this tool only *reports* what CI has built. Actually
triggering a deploy is a separate, human-gated act (the deploy workflow / a future
propose-only ``request_deploy`` under guarded actions) — never this tool.
"""

from __future__ import annotations

from typing import Any

from agentic_devops.proxy.releases import ReleaseLedger
from agentic_devops.tools.base import ToolSpec


def _format_release(r: Any, *, header: str) -> str:
    status = "complete" if r.complete else "PARTIAL (not a full platform set)"
    lines = [
        f"{header}:",
        f"- commit: {r.sha or '—'} ({r.short_sha})",
        f"- branch/ref: {r.branch or '—'} / {r.ref or '—'}",
        f"- built: {r.built_at or '—'} · status: {status}",
    ]
    if r.label:
        lines.append(f"- label: {r.label}")
    if r.components:
        lines.append("- components:")
        for name in sorted(r.components):
            lines.append(f"    {name}: {r.components[name].image}")
    return "\n".join(lines)


def build_release_tools(ledger: ReleaseLedger) -> list[ToolSpec]:
    """Build the release-ledger tool bound to a :class:`ReleaseLedger`."""

    def list_releases(args: dict[str, Any]) -> str:
        scope = str(args.get("scope", "recent")).strip().lower()
        branch = str(args.get("branch", "main")).strip() or "main"

        if not ledger.health():
            return (
                "The release ledger isn't reachable right now. This usually means the "
                "proxy's instance role can't read the build ledger in SSM Parameter Store "
                f"({ledger.prefix}/*). It's a deployment-side grant, not something to fix in chat."
            )

        if scope == "components":
            comps = ledger.components_latest()
            if not comps:
                return "No component builds are recorded in the ledger yet."
            lines = ["Newest image per component (the `assembled-latest` deploy set):"]
            for name in sorted(comps):
                lines.append(f"- {name}: {comps[name].image}")
            return "\n".join(lines)

        if scope == "branch":
            r = ledger.resolve_branch(branch)
            if r is None:
                return f"No build is recorded for branch {branch!r} yet."
            return _format_release(r, header=f"Newest build on {branch!r}")

        # default: recent
        releases = ledger.list_releases(limit=10)
        if not releases:
            return "No builds are recorded in the ledger yet — CI hasn't pushed a release."
        lines = ["Recorded builds (newest first — what's available to deploy):"]
        for r in releases:
            status = "complete" if r.complete else "PARTIAL (not a full platform set)"
            comps = ", ".join(sorted(r.components)) or "—"
            lines.append(
                f"- {r.short_sha or r.sha[:7]} on {r.branch or '—'} · {r.built_at or '—'} "
                f"· {status} · [{comps}]"
            )
        lines.append(
            "\nOnly `complete` whole-platform builds are deployable. To deploy, an operator "
            "runs the deploy workflow (source=newest-on-branch, or specific-commit for a pin/rollback)."
        )
        return "\n".join(lines)

    return [
        ToolSpec(
            name="list_releases",
            category="releases",
            description=(
                "Browse the CI→CD build ledger — the immutable releases CI has built and "
                "pushed to ECR, recorded in SSM Parameter Store. Reports what is available "
                "to deploy: recent builds, the newest build on a branch, or the newest image "
                "per component. READ-ONLY — it never triggers a deployment."
            ),
            when_to_use=(
                "When asked what can be deployed, what the latest build is, whether a commit "
                "was built, or what versions exist. Use scope='recent' for a list, "
                "scope='branch' (with a branch) for the newest on that branch, scope='components' "
                "for the per-component latest images."
            ),
            use_cases=[
                "what can I deploy", "what's the latest build", "list recent releases",
                "was this commit built", "what version is newest on main",
                "which images would assembled-latest pick",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "scope": {
                        "type": "string",
                        "enum": ["recent", "branch", "components"],
                        "description": "recent = newest builds; branch = newest on `branch`; components = latest image per component.",
                    },
                    "branch": {
                        "type": "string",
                        "description": "Branch name for scope='branch' (default 'main').",
                    },
                },
            },
            handler=list_releases,
            safety_tier="read-only",
        ),
    ]
