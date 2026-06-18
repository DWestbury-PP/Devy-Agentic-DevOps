"""Host-registry tools (Phase 9b): how Devy reaches registered hosts.

Generic-but-scoped, per the design: a cheap registry lookup + a single generic
runner (one check, scoped by its args) + a batched runner for health sweeps.
Devy supplies the host *identifier*; the proxy resolves it to an endpoint +
decrypted token (the agent never handles secrets), and the host MCP's allow-list
remains the authority on what actually runs.
"""

from __future__ import annotations

from typing import Any

from agentic_devops.proxy.hosts import HostStore
from agentic_devops.tools.base import ToolSpec

_CONN_FAIL = "ERROR: host check"  # prefix the host MCP client uses for connection failures


def _set_status_from_result(store: HostStore, host_id: str, result: str) -> None:
    store.set_status(host_id, "unreachable" if result.startswith(_CONN_FAIL) else "reachable")


def build_host_tools(store: HostStore, caller: Any) -> list[ToolSpec]:
    """Build the host-registry tools bound to a store + an MCP caller
    (``caller.call_tool(url, token, name, args)`` / ``caller.list_tools(url, token)``)."""

    def lookup(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip().lower()
        hosts = store.list(active_only=True)
        if query:
            tokens = query.split()
            def match(h):
                hay = " ".join(
                    str(x) for x in [
                        h.fqdn, h.instance_id, h.aws_region, h.aws_account,
                        h.private_ip, h.public_ip,
                        " ".join(f"{k}={v}" for k, v in (h.labels or {}).items()),
                    ] if x
                ).lower()
                return all(t in hay for t in tokens)
            hosts = [h for h in hosts if match(h)]
        if not hosts:
            base = "No active registered hosts match your query." if query else (
                "No hosts are registered yet. An admin can add them in the admin console."
            )
            return base
        lines: list[str] = []
        for h in hosts[:12]:
            bits = [h.fqdn]
            if h.private_ip:
                bits.append(f"private={h.private_ip}")
            if h.public_ip:
                bits.append(f"public={h.public_ip}")
            if h.aws_region:
                bits.append(f"region={h.aws_region}")
            if h.instance_id:
                bits.append(h.instance_id)
            if h.profile:
                bits.append(f"profile={h.profile}")
            bits.append(f"status={h.last_status or 'unknown'}")
            lines.append("- " + " · ".join(bits))
        if len(hosts) == 1:  # advertise the available checks for a single match
            rh = store.resolve(hosts[0].fqdn)
            checks = caller.list_tools(rh.url, rh.token) if rh else []
            lines.append(
                "  available checks: " + (", ".join(sorted(checks)) if checks
                                          else "(host not reachable right now)")
            )
        return (
            "Registered hosts (use the FQDN as the `host` argument to run_host_check):\n"
            + "\n".join(lines)
        )

    def run_one(args: dict[str, Any]) -> str:
        host = str(args.get("host", "")).strip()
        check = str(args.get("check", "")).strip()
        if not host or not check:
            return "ERROR: both 'host' and 'check' are required."
        rh = store.resolve(host)
        if rh is None:
            return f"No active host matches {host!r}. Call host_details_lookup to list registered hosts."
        result = caller.call_tool(rh.url, rh.token, check, args.get("args") or {})
        _set_status_from_result(store, rh.host.id, result)
        return result

    def run_many(args: dict[str, Any]) -> str:
        host = str(args.get("host", "")).strip()
        checks = args.get("checks") or []
        if not host or not isinstance(checks, list) or not checks:
            return "ERROR: 'host' and a non-empty 'checks' list are required."
        rh = store.resolve(host)
        if rh is None:
            return f"No active host matches {host!r}. Call host_details_lookup to list registered hosts."
        blocks, reachable = [], False
        for c in checks[:12]:
            result = caller.call_tool(rh.url, rh.token, str(c), {})
            if not result.startswith(_CONN_FAIL):
                reachable = True
            blocks.append(f"### {c}\n{result}")
        store.set_status(rh.host.id, "reachable" if reachable else "unreachable")
        return "\n\n".join(blocks)

    return [
        ToolSpec(
            name="host_details_lookup",
            category="hosts",
            description=(
                "List the hosts Devy can run diagnostics against (the host registry), "
                "optionally filtered by a query (FQDN, instance id, region, label). "
                "For a single match it also lists the checks available on that host."
            ),
            when_to_use=(
                "At the start of host/infrastructure work, to discover which hosts are "
                "registered and reachable and what checks they support — before running "
                "anything. Use the returned FQDN as the `host` argument to run_host_check."
            ),
            use_cases=[
                "what hosts can you reach", "list the servers", "is host X registered",
                "which hosts are in us-east-1", "what checks can I run on web-1",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Optional filter (FQDN/instance/region/label)."}
                },
            },
            handler=lookup,
            safety_tier="read-only",
        ),
        ToolSpec(
            name="run_host_check",
            category="hosts",
            description=(
                "Run ONE diagnostic check on a registered host via its MCP. The proxy "
                "resolves `host` to the endpoint + token; the host's allow-list governs "
                "what's permitted. Scope the result with `args` (e.g. lines/since/grep/container)."
            ),
            when_to_use=(
                "To inspect a specific host: disk/memory/cpu, processes, sockets, systemd "
                "logs, or read-only Docker checks. Find the host and its check names with "
                "host_details_lookup first."
            ),
            use_cases=[
                "check disk on web-1", "show docker logs for the api container on host X",
                "is memory healthy on this host", "tail the journal on server Y",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Host identifier (FQDN / instance-id)."},
                    "check": {"type": "string", "description": "The check name (see host_details_lookup)."},
                    "args": {"type": "object", "description": "Optional arguments scoping the check."},
                },
                "required": ["host", "check"],
            },
            handler=run_one,
            safety_tier="diagnostic",
        ),
        ToolSpec(
            name="run_host_checks",
            category="hosts",
            description=(
                "Run SEVERAL checks on one host in a single round-trip — a health sweep "
                "(e.g. disk + memory + cpu_load + processes). You choose the set."
            ),
            when_to_use=(
                "For a general health check of a host, to gather a panel of signals at once "
                "instead of one tool call per check."
            ),
            use_cases=[
                "general health check of web-1", "sweep disk memory and load on host X",
                "is this server healthy overall",
            ],
            input_schema={
                "type": "object",
                "properties": {
                    "host": {"type": "string", "description": "Host identifier (FQDN / instance-id)."},
                    "checks": {
                        "type": "array", "items": {"type": "string"},
                        "description": "Check names to run (see host_details_lookup).",
                    },
                },
                "required": ["host", "checks"],
            },
            handler=run_many,
            safety_tier="diagnostic",
        ),
    ]
