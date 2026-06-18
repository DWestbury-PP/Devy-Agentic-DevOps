"""Agentic DevOps host MCP — a safe-allowlist diagnostics server for target hosts.

Deployed on a host, it exposes a fixed, profile-gated set of diagnostic commands
as MCP tools. The LLM-PROXY mounts it (stdio locally, authenticated HTTP
remotely). No shell, no arbitrary execution — the allow-list is the safety
boundary. See ../README.md.
"""

__version__ = "0.1.0"
