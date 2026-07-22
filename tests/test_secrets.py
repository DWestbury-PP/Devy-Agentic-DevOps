"""Secrets provider (Phase S-1) — the unified AWS-SM-shaped backend.

Hermetic: exercises the SecretsProvider against the in-memory _FakeSMClient
(from conftest) — no boto3, LocalStack, or network. Covers get/set/exists/delete,
DEV write-through + re-hydration (survives restarts), and PROD read-only posture.
"""

import json

import pytest

from agentic_devops.proxy.secrets import (
    SecretsProvider,
    github_secret_ref,
    host_secret_ref,
    provider_key_refs,
)
from tests.conftest import _FakeSMClient, make_fake_secrets


def test_set_get_exists_delete_roundtrip():
    s = make_fake_secrets(writable=True)
    assert s.get("devy/x") is None and s.exists("devy/x") is False
    s.set("devy/x", "v1")
    assert s.get("devy/x") == "v1" and s.exists("devy/x") is True
    s.set("devy/x", "v2")  # update-in-place (create then put)
    assert s.get("devy/x") == "v2"
    s.delete("devy/x")
    assert s.get("devy/x") is None and s.exists("devy/x") is False


def test_prod_is_read_only():
    s = make_fake_secrets(writable=False)
    assert s.writable is False
    with pytest.raises(PermissionError):
        s.set("devy/x", "v")
    with pytest.raises(PermissionError):
        s.delete("devy/x")
    # reads still work (provisioned out-of-band)
    s._client._d["devy/y"] = "provisioned"  # simulate IaC-provisioned secret
    assert s.get("devy/y") == "provisioned" and s.exists("devy/y") is True


def test_dev_write_through_persists_to_file(tmp_path):
    store_file = tmp_path / "secrets-store.json"
    s = SecretsProvider(_FakeSMClient(), writable=True, store_file=store_file)
    s.set("devy/github/home", "ghp_x")
    s.set("devy/provider/anthropic", "sk-y")
    on_disk = json.loads(store_file.read_text())
    assert on_disk == {"devy/github/home": "ghp_x", "devy/provider/anthropic": "sk-y"}
    s.delete("devy/github/home")
    assert json.loads(store_file.read_text()) == {"devy/provider/anthropic": "sk-y"}


def test_rehydrate_reseeds_a_fresh_store(tmp_path):
    """A fresh client (LocalStack lost state on restart) is re-seeded from the file."""
    store_file = tmp_path / "secrets-store.json"
    store_file.write_text(json.dumps({"devy/github/home": "ghp_x", "devy/host/web": "tok"}))
    fresh = SecretsProvider(_FakeSMClient(), writable=True, store_file=store_file)
    assert fresh.get("devy/github/home") is None  # not yet loaded
    n = fresh.rehydrate()
    assert n == 2
    assert fresh.get("devy/github/home") == "ghp_x" and fresh.get("devy/host/web") == "tok"


def test_rehydrate_is_noop_when_read_only(tmp_path):
    store_file = tmp_path / "s.json"
    store_file.write_text(json.dumps({"devy/x": "v"}))
    s = SecretsProvider(_FakeSMClient(), writable=False, store_file=store_file)
    assert s.rehydrate() == 0


def test_ref_helpers_sanitize_and_namespace():
    assert github_secret_ref("Home") == "devy/github/home"
    assert github_secret_ref("My Work Acct") == "devy/github/my-work-acct"
    assert host_secret_ref("web01.example.com") == "devy/host/web01.example.com"
    assert github_secret_ref("home", namespace="acme") == "acme/github/home"
    assert set(provider_key_refs().values()) == {
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "GEMINI_API_KEY",
        "TAVILY_API_KEY", "LANGSMITH_API_KEY",
    }


def test_provider_key_refs_matches_catalog():
    # The two provider lists (boot hydration vs admin catalog) must not drift.
    from agentic_devops.proxy.secrets_catalog import provider_specs

    catalog = {p["ref"]: p["env"] for p in provider_specs()}
    assert catalog == provider_key_refs()


def test_health_reflects_reachability():
    assert make_fake_secrets().health() is True


# -- prod hardening: TTL cache + audit (Phase S-3) --------------------------
class _CountingSM(_FakeSMClient):
    def __init__(self):
        super().__init__()
        self.gets = 0

    def get_secret_value(self, SecretId):
        self.gets += 1
        return super().get_secret_value(SecretId)


def test_ttl_cache_serves_within_window_and_refetches_after():
    clock = [0.0]
    client = _CountingSM()
    client._d["devy/x"] = "v1"
    s = SecretsProvider(client, writable=True, cache_ttl=100, clock=lambda: clock[0])
    assert s.get("devy/x") == "v1" and client.gets == 1
    client._d["devy/x"] = "v2"          # change underlying store directly (no invalidation)
    assert s.get("devy/x") == "v1" and client.gets == 1   # served from cache
    clock[0] = 101                       # TTL expires
    assert s.get("devy/x") == "v2" and client.gets == 2   # re-fetched


def test_set_and_delete_invalidate_cache():
    client = _CountingSM()
    s = SecretsProvider(client, writable=True, cache_ttl=100)
    s.set("devy/x", "v1")
    assert s.get("devy/x") == "v1"       # cached
    s.set("devy/x", "v2")                # write invalidates
    assert s.get("devy/x") == "v2"
    s.delete("devy/x")
    assert s.get("devy/x") is None


def test_zero_ttl_always_refetches():
    client = _CountingSM()
    client._d["devy/x"] = "v"
    s = SecretsProvider(client, writable=True, cache_ttl=0)
    s.get("devy/x"); s.get("devy/x")
    assert client.gets == 2


def test_audit_records_ops_without_value(tmp_path):
    from agentic_devops.proxy.secrets import SecretAudit

    audit = SecretAudit(tmp_path / "secrets-audit.jsonl")
    s = SecretsProvider(_FakeSMClient(), writable=True, audit=audit)
    s.set("devy/github/home", "ghp_supersecret")
    s.get("devy/github/home")
    s.delete("devy/github/home")
    lines = [json.loads(x) for x in (tmp_path / "secrets-audit.jsonl").read_text().splitlines()]
    actions = {(e["action"], e["ref"]) for e in lines}
    assert ("set", "devy/github/home") in actions
    assert ("resolve", "devy/github/home") in actions
    assert ("delete", "devy/github/home") in actions
    # the value is NEVER in the audit trail
    assert "ghp_supersecret" not in (tmp_path / "secrets-audit.jsonl").read_text()


# -- config-mounted MCP bearer surfaced on the Secrets tab (vault-mastered) --
def test_config_mount_refs_and_catalog_entry():
    from types import SimpleNamespace

    from agentic_devops.proxy.secrets_catalog import build_catalog, config_mount_refs

    servers = [
        SimpleNamespace(name="host", secret_ref="devy/mcp/host",
                        url="http://host.docker.internal:8781/mcp"),
        SimpleNamespace(name="legacy", secret_ref=None),  # inline token / no vault ref
    ]
    assert config_mount_refs(servers) == {"devy/mcp/host"}

    s = make_fake_secrets(writable=True)
    s.set("devy/mcp/host", "bearer-abc")
    empty = SimpleNamespace(list=lambda: [])
    entries = build_catalog("devy", s, empty, empty, empty, config_mcp_servers=servers)
    by_ref = {e["ref"]: e for e in entries}
    e = by_ref["devy/mcp/host"]
    assert e["category"] == "mcp" and e["loaded"] is True
    assert e["editable"] is True          # settable on the tab (dev)
    assert e["testable"] is True          # probeable against the live server (Test)
    # the label names the endpoint so the admin knows WHICH MCP this bearer is for
    assert "config-mounted" in e["label"]
    assert "host.docker.internal:8781" in e["label"]
