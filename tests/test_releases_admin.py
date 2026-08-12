"""Release-ledger admin API (/v1/admin/releases) — read-only, admin-gated.

DB-backed (like test_secrets_admin): builds the full app, so it SKIPs without a live
DB. The SSM client is replaced with a fake ledger (no AWS) via monkeypatching the
builder, so the routes exercise real parsing against known ledger contents.
"""

import json

import bcrypt
import pytest
from fastapi.testclient import TestClient

import agentic_devops.proxy.releases as releases_mod
from agentic_devops.config import DatabaseConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.tools.router import ToolsRouter
from tests.conftest import make_fake_ledger


def _manifest(sha, branch="main", status="complete", built_at="2026-08-01T10:00:00Z"):
    return {
        "schema": 1, "sha": sha, "short_sha": sha[:7], "branch": branch, "ref": branch,
        "built_at": built_at, "label": f"{branch}-{sha[:7]}", "status": status,
        "components": {"proxy": {"image": f"acct/devy-proxy:{sha[:7]}", "tag": sha[:7],
                                 "digest": "sha256:a", "repository": "devy-proxy"}},
    }


def _seeded_params():
    return {
        "/devy/builds/by-commit/aaa1111": json.dumps(_manifest("aaa1111", built_at="2026-08-10T00:00:00Z")),
        "/devy/builds/by-commit/bbb2222": json.dumps(_manifest("bbb2222", status="partial", built_at="2026-07-01T00:00:00Z")),
        "/devy/builds/by-branch/main/latest": "aaa1111",
        "/devy/builds/components/proxy/latest": json.dumps(
            {"image": "acct/devy-proxy:aaa1111", "tag": "aaa1111", "digest": "sha256:a", "repository": "devy-proxy"}),
    }


@pytest.fixture()
def client(tmp_path, pool, pg_url, monkeypatch):
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    monkeypatch.setattr(releases_mod, "build_release_ledger",
                        lambda *a, **k: make_fake_ledger(_seeded_params()))
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(), router=ToolsRouter(),
    )
    c = TestClient(app)
    tok = c.post("/v1/admin/login", json={"password": "pw"}).json()["token"]
    c.headers.update({"Authorization": f"Bearer {tok}"})
    return c


def test_requires_admin(client):
    anon = TestClient(client.app)
    assert anon.get("/v1/admin/releases").status_code == 401
    assert anon.get("/v1/admin/releases/aaa1111").status_code == 401


def test_list_releases_newest_first(client):
    body = client.get("/v1/admin/releases").json()
    assert body["reachable"] is True and body["prefix"] == "/devy/builds"
    shas = [r["sha"] for r in body["releases"]]
    assert shas == ["aaa1111", "bbb2222"]  # sorted by built_at desc
    assert body["releases"][0]["complete"] is True
    assert body["releases"][1]["complete"] is False  # partial


def test_get_release_and_404(client):
    assert client.get("/v1/admin/releases/aaa1111").json()["short_sha"] == "aaa1111"
    assert client.get("/v1/admin/releases/nope").status_code == 404


def test_latest_by_branch(client):
    assert client.get("/v1/admin/releases/latest?branch=main").json()["sha"] == "aaa1111"
    assert client.get("/v1/admin/releases/latest?branch=absent").status_code == 404


def test_components_route(client):
    comps = client.get("/v1/admin/releases/components").json()
    assert comps["proxy"]["image"] == "acct/devy-proxy:aaa1111"


def test_components_and_latest_not_captured_by_sha_route(client):
    # /releases/components and /releases/latest must resolve to their own handlers,
    # not the {sha} route — verified by their distinct shapes above; here assert
    # the sha route still rejects a bogus sha with 404 (ordering intact).
    assert client.get("/v1/admin/releases/deadbeef").status_code == 404
