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
        "ANTHROPIC_API_KEY", "OPENAI_API_KEY", "TAVILY_API_KEY", "LANGSMITH_API_KEY",
    }


def test_health_reflects_reachability():
    assert make_fake_secrets().health() is True
