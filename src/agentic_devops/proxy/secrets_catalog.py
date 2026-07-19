"""Secret catalog + live probes for the admin Secrets/Connections view (Phase S-2).

The catalog is the inventory the admin panel renders: every credential Devy knows
about — provider/service keys (Anthropic, OpenAI, Tavily, LangSmith) plus the
connector tokens (GitHub accounts, hosts) — each as `service · ref · loaded`, and
never the value. In DEV every secret is settable here (the Secrets tab is the single
write-point for secret *values*); the connector tabs own the *metadata* (account/host
rows) and derive the `secret_ref`. Provider keys hydrate an env var; connector tokens
are resolved on-demand from the vault (no env var).

Probes are the safe "does this key actually work?" test: a lightweight
authenticated call per service that reveals nothing. They distinguish invalid
(401/403) from unreachable/other, so the UI can say something useful.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class ProviderSpec:
    service: str          # stable id: anthropic | openai | gemini | tavily | langsmith
    label: str            # display label
    env: str              # environment variable it hydrates
    probe: str            # which probe function to run


# Keep in sync with proxy.secrets.provider_key_refs (same ref → env mapping; that
# one drives boot-time env hydration, this one drives the admin catalog + probes).
_PROVIDERS: tuple[ProviderSpec, ...] = (
    ProviderSpec("anthropic", "Anthropic (chat models)", "ANTHROPIC_API_KEY", "anthropic"),
    # OpenAI powers embeddings AND is a chat-failover backup (config tier `fallbacks`).
    ProviderSpec("openai", "OpenAI (embeddings + chat fallback)", "OPENAI_API_KEY", "openai"),
    ProviderSpec("gemini", "Gemini (chat fallback)", "GEMINI_API_KEY", "gemini"),
    ProviderSpec("tavily", "Tavily (web search)", "TAVILY_API_KEY", "tavily"),
    ProviderSpec("langsmith", "LangSmith (tracing)", "LANGSMITH_API_KEY", "langsmith"),
)


def provider_specs(namespace: str = "devy") -> list[dict[str, str]]:
    """Provider entries with their computed secret ref."""
    return [
        {"service": p.service, "label": p.label, "env": p.env, "probe": p.probe,
         "ref": f"{namespace}/provider/{p.service}"}
        for p in _PROVIDERS
    ]


def settable_refs(namespace: str = "devy") -> set[str]:
    """Refs the Secrets tab may write (provider keys only; connector tokens are
    edited on their own tabs)."""
    return {p["ref"] for p in provider_specs(namespace)}


def build_catalog(
    namespace: str, secrets: Any, github_store: Any, host_store: Any, mcp_server_store: Any = None,
) -> list[dict[str, Any]]:
    """Assemble the full inventory with live loaded-state (value never included)."""
    entries: list[dict[str, Any]] = []
    for p in provider_specs(namespace):
        entries.append({
            "service": p["service"], "label": p["label"], "ref": p["ref"],
            "category": "provider", "env": p["env"],
            "loaded": secrets.exists(p["ref"]), "editable": secrets.writable,
            "testable": True,
        })
    for a in github_store.list():
        if not a.secret_ref:
            continue
        entries.append({
            "service": f"github:{a.label}", "label": f"GitHub · {a.label}",
            "ref": a.secret_ref, "category": "github", "env": None,
            "loaded": secrets.exists(a.secret_ref), "editable": secrets.writable, "testable": True,
        })
    for h in host_store.list():
        if not h.secret_ref:
            continue
        entries.append({
            "service": f"host:{h.fqdn}", "label": f"Host · {h.fqdn}",
            "ref": h.secret_ref, "category": "host", "env": None,
            "loaded": secrets.exists(h.secret_ref), "editable": secrets.writable, "testable": True,
        })
    for m in (mcp_server_store.list() if mcp_server_store is not None else []):
        if not m.secret_ref:
            continue
        entries.append({
            "service": f"mcp:{m.name}", "label": f"MCP · {m.name}",
            "ref": m.secret_ref, "category": "mcp", "env": None,
            "loaded": secrets.exists(m.secret_ref), "editable": secrets.writable, "testable": True,
        })
    return entries


# --- live probes -----------------------------------------------------------
# Each returns (ok, detail). Best-effort: a clear message beats a hard failure.

def _probe_http(method: str, url: str, *, headers: dict, json: Any = None) -> tuple[bool, str]:
    import httpx

    try:
        r = httpx.request(method, url, headers=headers, json=json, timeout=10.0)
    except Exception as exc:  # noqa: BLE001 — network/DNS/timeout
        return False, f"unreachable: {type(exc).__name__}"
    if r.status_code in (401, 403):
        return False, "invalid credentials (HTTP %d)" % r.status_code
    if 200 <= r.status_code < 300:
        return True, "valid"
    return False, f"unexpected HTTP {r.status_code}"


def probe_provider(service: str, value: str) -> tuple[bool, str]:
    """Validate a provider key with a minimal authenticated call (reveals nothing)."""
    if not value:
        return False, "not set"
    if service == "anthropic":
        return _probe_http(
            "GET", "https://api.anthropic.com/v1/models",
            headers={"x-api-key": value, "anthropic-version": "2023-06-01"},
        )
    if service == "openai":
        return _probe_http("GET", "https://api.openai.com/v1/models",
                           headers={"Authorization": f"Bearer {value}"})
    if service == "gemini":
        # Google Generative Language API. Auth via the x-goog-api-key header
        # (not a ?key= query param) so the key never lands in a URL.
        return _probe_http(
            "GET", "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": value},
        )
    if service == "tavily":
        return _probe_http(
            "POST", "https://api.tavily.com/search",
            headers={"Content-Type": "application/json"},
            json={"api_key": value, "query": "ping", "max_results": 1},
        )
    if service == "langsmith":
        return _probe_http(
            "GET", "https://api.smith.langchain.com/api/v1/sessions?limit=1",
            headers={"x-api-key": value},
        )
    return False, f"no probe for {service}"


def probe_github(client: Any, value: str) -> tuple[bool, str]:
    if not value:
        return False, "not set"
    try:
        user = client.whoami(value)
        return True, f"authenticated as {user.get('login', '?')}"
    except Exception as exc:  # noqa: BLE001
        return False, f"invalid: {exc}"
