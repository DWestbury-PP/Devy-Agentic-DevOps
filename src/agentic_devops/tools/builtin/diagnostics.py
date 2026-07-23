"""Safe-allowlist local host diagnostics — the flagship Phase 1 tool.

The agent can inspect the live health of the host it runs on, but ONLY through a
fixed set of allow-listed checks, each mapped to a specific argv (no shell, no
arbitrary commands). This is the security posture that makes the framework
adoptable — and it's deliberately the seed of the Phase 2 deployable host MCP
with tiered profiles. Every invocation is audit-logged.
"""

from __future__ import annotations

import json
import platform
import re
import shutil
import subprocess
import time
from pathlib import Path
from typing import Any, Optional

from agentic_devops.tools.base import ToolSpec

_CONTAINER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SINCE_RE = re.compile(r"^(\d+[smhdw]|\d{4}-\d{2}-\d{2}([ T]\d{2}:\d{2}(:\d{2})?)?)$")

# Static (no-argument) checks -> argv per platform. "any" applies to all.
_STATIC_CHECKS: dict[str, dict[str, list[str]]] = {
    "disk": {"any": ["df", "-h"]},
    "memory": {"Linux": ["free", "-h"], "Darwin": ["vm_stat"]},
    "cpu_load": {"any": ["uptime"]},
    "processes": {
        "Linux": ["ps", "-eo", "pid,ppid,%cpu,%mem,comm", "--sort=-%cpu"],
        "Darwin": ["ps", "-Ao", "pid,ppid,%cpu,%mem,comm", "-r"],
    },
    "docker_ps": {"any": ["docker", "ps", "--format", "{{.Names}}\t{{.Status}}\t{{.Image}}"]},
}

# Checks taking validated arguments are handled explicitly below.
_DYNAMIC_CHECKS = ("docker_logs", "recent_syslog")

ALLOWED_CHECKS = sorted([*_STATIC_CHECKS.keys(), *_DYNAMIC_CHECKS])

_DEFAULT_TIMEOUT = 20


def _truncate(text: str, max_lines: int = 60, max_chars: int = 4000) -> str:
    lines = text.splitlines()
    if len(lines) > max_lines:
        hidden = len(lines) - max_lines
        lines = lines[:max_lines] + [f"... ({hidden} more lines truncated)"]
    out = "\n".join(lines)
    if len(out) > max_chars:
        out = out[:max_chars] + "\n... (truncated)"
    return out


def _run(argv: list[str], timeout: int = _DEFAULT_TIMEOUT) -> tuple[Optional[int], str]:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout)
    except FileNotFoundError:
        return None, f"command not found: {argv[0]!r} (is it installed and on PATH?)"
    except subprocess.TimeoutExpired:
        return None, f"command timed out after {timeout}s: {' '.join(argv)}"
    output = proc.stdout or ""
    if proc.returncode != 0:
        err = (proc.stderr or "").strip()
        output = (output + ("\n" + err if err else "")).strip() or f"(exited with code {proc.returncode})"
    return proc.returncode, output


def _resolve_static_argv(check: str) -> Optional[list[str]]:
    mapping = _STATIC_CHECKS[check]
    if "any" in mapping:
        return list(mapping["any"])
    return list(mapping[platform.system()]) if platform.system() in mapping else None


def _build_argv(check: str, args: dict[str, Any]) -> tuple[Optional[list[str]], Optional[str]]:
    """Return (argv, error). Exactly one is non-None."""
    if check in _STATIC_CHECKS:
        argv = _resolve_static_argv(check)
        if argv is None:
            return None, f"check {check!r} is not supported on {platform.system()}"
        return argv, None

    if check == "docker_logs":
        container = str(args.get("container", "")).strip()
        if not _CONTAINER_RE.match(container):
            return None, "docker_logs requires a valid 'container' name (letters, digits, _.-)"
        since = str(args.get("since", "15m")).strip()
        if not _SINCE_RE.match(since):
            return None, "invalid 'since'; use forms like '15m', '1h', '2d' or an ISO timestamp"
        try:
            tail = int(args.get("tail", 200))
        except (TypeError, ValueError):
            return None, "'tail' must be an integer"
        tail = max(1, min(tail, 1000))
        return ["docker", "logs", "--tail", str(tail), "--since", since, container], None

    if check == "recent_syslog":
        # Be mindful of the host: macOS uses the unified log, modern Linux uses the
        # systemd journal, older Linux uses /var/log files. Pick what's actually there.
        if platform.system() == "Darwin":
            if shutil.which("log"):
                return ["log", "show", "--last", "5m", "--style", "compact"], None
            if Path("/var/log/system.log").exists():
                return ["tail", "-n", "100", "/var/log/system.log"], None
            return None, "no readable macOS system log — tried `log show` and /var/log/system.log"
        if shutil.which("journalctl"):
            return ["journalctl", "--no-pager", "-n", "100"], None
        for candidate in ("/var/log/syslog", "/var/log/messages"):
            if Path(candidate).exists():
                return ["tail", "-n", "100", candidate], None
        return None, (
            "no system log reachable from this environment — tried journalctl, "
            "/var/log/syslog, /var/log/messages. A containerized proxy has no host "
            "syslog. If a host MCP is mounted, use its host tools instead — "
            "host_journal (recent system logs) or host_reboot_history (reboot / "
            "shutdown history), discoverable via find_tools(category='host'). "
            "Otherwise run the host MCP on the target host (or a host with systemd)."
        )

    return None, None  # unreachable for allowed checks


def build_diagnostics_tool(
    audit_path: Optional[Path] = None, *, container_scoped: bool = False
) -> ToolSpec:
    """Construct the host-diagnostics ToolSpec.

    ``audit_path`` (optional) is a JSONL file every invocation is appended to.

    ``container_scoped`` re-scopes the tool when a real host MCP is mounted (the
    common containerized-proxy deployment). In that mode this builtin can only
    see the *proxy's own container*, not the target host — so it stops
    advertising itself as a host surface (distinct name/category/text) and the
    mounted ``host_*`` tools become the single, unambiguous host surface that
    ``find_tools`` routes host questions to. Deployed natively on a host (no host
    MCP), it keeps the original host-diagnostics identity.
    """

    def _audit(record: dict[str, Any]) -> None:
        if audit_path is None:
            return
        audit_path.parent.mkdir(parents=True, exist_ok=True)
        with audit_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record) + "\n")

    def handler(args: dict[str, Any]) -> str:
        check = str(args.get("check", "")).strip()
        if check not in ALLOWED_CHECKS:
            return (
                f"ERROR: unknown check {check!r}. Allowed checks: {', '.join(ALLOWED_CHECKS)}."
            )

        argv, error = _build_argv(check, args)
        if error is not None:
            _audit({"ts": time.time(), "check": check, "args": args, "error": error})
            return f"ERROR: {error}"

        # A containerized proxy has no Docker CLI (the host MCP is the Docker surface,
        # via its mounted socket). Fail with an actionable pointer instead of a bare
        # "command not found" so the agent can reach for the right tool.
        if argv and argv[0] == "docker" and shutil.which("docker") is None:
            msg = (
                "the Docker CLI isn't available in this environment. If the proxy is "
                "containerized, use the host MCP's Docker checks (e.g. host_docker_ps, "
                "host_docker_logs), which read the mounted Docker socket; otherwise "
                "install Docker on the host running the proxy."
            )
            _audit({"ts": time.time(), "check": check, "args": args, "error": msg})
            return f"ERROR: {msg}"

        started = time.monotonic()
        returncode, output = _run(argv)  # type: ignore[arg-type]
        duration_ms = round((time.monotonic() - started) * 1000)

        max_lines = 25 if check == "processes" else 60
        body = _truncate(output, max_lines=max_lines)

        _audit(
            {
                "ts": time.time(),
                "check": check,
                "args": args,
                "argv": argv,
                "returncode": returncode,
                "duration_ms": duration_ms,
            }
        )
        return f"$ {' '.join(argv)}\n\n{body}"  # type: ignore[arg-type]

    if container_scoped:
        name = "proxy_self_diagnostics"
        category = "proxy-diagnostics"
        description = (
            "Runs safe, allow-listed shell diagnostics inside the PROXY's OWN "
            "container. Scope is mixed and worth knowing: CPU/memory reflect the "
            "CONTAINER's cgroup view, but because parts of the host filesystem are "
            "bind-mounted in, disk usage and load average can reflect the HOST — so "
            "don't over-trust a single reading as purely container-local. There is no "
            "host syslog here. For authoritative host state — disk, memory, reboot "
            "history, system logs, services — use the mounted host MCP tools "
            "(host_journal, host_reboot_history, host_disk, …) via find_tools(category='host')."
        )
        when_to_use = (
            "Only when asked specifically about the proxy's OWN container/process "
            "health (is the proxy container itself OK). NOT for the host machine — the "
            "mounted 'host' MCP is the real host surface."
        )
        use_cases = [
            "is the proxy container itself healthy",
            "the proxy process's own resource use",
        ]
    else:
        name = "host_diagnostics"
        category = "host-diagnostics"
        description = (
            "Inspect the live health of the local host through a fixed set of safe, "
            "allow-listed checks (no arbitrary shell). Returns the command run and its "
            "(truncated) output."
        )
        when_to_use = (
            "When asked about the live state or health of this machine: disk space, "
            "memory, CPU/load, running processes, Docker containers or their logs, or "
            "recent system-log entries."
        )
        use_cases = [
            "is anything unhealthy on this box",
            "disk space and memory usage",
            "cpu load and busy processes",
            "docker container health and logs",
            "recent system errors",
        ]

    return ToolSpec(
        name=name,
        category=category,
        description=description,
        when_to_use=when_to_use,
        use_cases=use_cases,
        input_schema={
            "type": "object",
            "properties": {
                "check": {
                    "type": "string",
                    "enum": ALLOWED_CHECKS,
                    "description": "Which diagnostic to run.",
                },
                "container": {
                    "type": "string",
                    "description": "Container name/id (required for check='docker_logs').",
                },
                "since": {
                    "type": "string",
                    "description": "Time window for docker_logs, e.g. '15m', '1h', '2d' (default 15m).",
                },
                "tail": {
                    "type": "integer",
                    "description": "Max log lines for docker_logs (default 200, max 1000).",
                },
            },
            "required": ["check"],
        },
        handler=handler,
        safety_tier="diagnostic",
    )
