"""GitHub account registry + repo tools (Phase D-1).

Store CRUD + Fernet round-trip + resolution on the live DB; the read-only tools
against a fake client.
"""

import bcrypt
import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient

from agentic_devops.config import DatabaseConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.proxy.encryption import TokenCipher
from agentic_devops.proxy.github import GitHubAccountStore, RepoCrawlStore
from agentic_devops.tools.builtin.repos import build_repo_tools
from agentic_devops.tools.router import ToolsRouter


@pytest.fixture()
def store(pool):
    return GitHubAccountStore(pool, TokenCipher(Fernet.generate_key().decode()))


# -- store + encryption -----------------------------------------------------
def test_create_resolve_roundtrips_token(store):
    a = store.create({"label": "home", "login": "octocat"}, token="ghp_secret")
    assert a.id and a.has_token is True
    resolved = store.resolve("home")
    assert resolved.token == "ghp_secret"  # decrypted via Fernet
    assert resolved.account.login == "octocat"


def test_resolve_without_identifier_needs_single_active(store):
    store.create({"label": "home"}, token="t1")
    assert store.resolve() is not None  # one active → unambiguous
    store.create({"label": "work"}, token="t2")
    assert store.resolve() is None  # two active → ambiguous, must name one
    assert store.resolve("work").token == "t2"


def test_resolve_for_repo_matches_owner_login(store):
    store.create({"label": "personal", "login": "alice"}, token="ta")
    store.create({"label": "org", "login": "acme"}, token="tb")
    assert store.resolve_for_repo("acme/widgets").token == "tb"
    assert store.resolve_for_repo("alice/dotfiles").token == "ta"


def test_token_not_exposed_in_public_view(store):
    a = store.create({"label": "x"}, token="hidden")
    got = store.get(a.id)
    assert got.has_token is True and not hasattr(got, "token")


def test_update_and_delete(store):
    a = store.create({"label": "x"}, token="old")
    store.update(a.id, {"default_corpus": "infra"})
    assert store.get(a.id).default_corpus == "infra"
    store.update(a.id, {}, token="new", set_token=True)
    assert store.resolve("x").token == "new"
    store.delete(a.id)
    assert store.get(a.id) is None


# -- repo crawl history -----------------------------------------------------
@pytest.fixture()
def crawls(pool):
    return RepoCrawlStore(pool)


def test_record_and_list_crawl(crawls):
    crawls.record(
        "me/api", "me/api", account_id="acct1", commit_sha="deadbeef1234",
        default_branch="main", files_ingested=12, chunks_written=200,
    )
    rows = crawls.list()
    assert len(rows) == 1
    r = rows[0]
    assert r.full_name == "me/api" and r.commit_sha == "deadbeef1234"
    assert r.files_ingested == 12 and r.chunks_written == 200
    assert r.crawled_at is not None


def test_record_upserts_on_rescan(crawls):
    crawls.record("me/api", "me/api", commit_sha="aaa111", files_ingested=5)
    crawls.record("me/api", "me/api", commit_sha="bbb222", files_ingested=9)
    rows = crawls.list()
    assert len(rows) == 1  # one row per repo
    assert rows[0].commit_sha == "bbb222" and rows[0].files_ingested == 9


def test_crawl_delete(crawls):
    crawls.record("me/api", "me/api")
    crawls.delete("me/api")
    assert crawls.get("me/api") is None


# -- tools ------------------------------------------------------------------
class _FakeClient:
    def list_repos(self, token, **kw):
        return [
            {"full_name": "me/api", "language": "Python", "description": "the API", "pushed_at": "2026-06-01T00:00:00Z"},
            {"full_name": "me/web", "language": "JS", "private": True, "pushed_at": "2026-05-01T00:00:00Z"},
        ]

    def list_commits(self, token, full_name, path=None, per_page=20):
        return [{"sha": "abc1234567", "commit": {"author": {"name": "Al", "date": "2026-06-02T10:00:00Z"},
                                                 "message": "fix timeout\n\nbody"}}]

    def get_commit(self, token, full_name, sha):
        return {"commit": {"message": "fix timeout"},
                "files": [{"filename": "worker.py", "additions": 3, "deletions": 1, "patch": "@@ -1 +1 @@\n-old\n+new"}]}

    def get_file(self, token, full_name, path, ref=None):
        return "line1\nline2\n"

    def search_code(self, token, query, full_name=None, per_page=10):
        return [{"path": "config.py", "repository": {"full_name": "me/api"}}]


@pytest.fixture()
def tools(store):
    store.create({"label": "home", "login": "me"}, token="tok")
    return {t.name: t for t in build_repo_tools(store, _FakeClient())}


def test_repo_lookup_lists_and_filters(tools):
    out = tools["repo_lookup"].handler({})
    assert "me/api" in out and "me/web" in out
    filtered = tools["repo_lookup"].handler({"query": "python"})
    assert "me/api" in filtered and "me/web" not in filtered


def test_repo_history_formats_commits(tools):
    out = tools["repo_history"].handler({"repo": "me/api"})
    assert "abc1234" in out and "fix timeout" in out and "Al" in out


def test_repo_diff_requires_sha_or_range(tools):
    assert tools["repo_diff"].handler({"repo": "me/api"}).startswith("ERROR")
    out = tools["repo_diff"].handler({"repo": "me/api", "sha": "abc1234567"})
    assert "worker.py" in out and "```diff" in out


def test_repo_read_file_and_search(tools):
    rf = tools["repo_read_file"].handler({"repo": "me/api", "path": "x.py"})
    assert "line1" in rf
    sc = tools["repo_search_code"].handler({"query": "needle"})
    assert "config.py" in sc


def test_tools_report_when_no_account(store):
    # No account registered → graceful message, not a crash.
    tools = {t.name: t for t in build_repo_tools(store, _FakeClient())}
    assert "No GitHub account" in tools["repo_lookup"].handler({})


def test_repo_tools_are_in_repos_category(tools):
    assert all(t.category == "repos" for t in tools.values())


# -- admin CRUD endpoints ---------------------------------------------------
@pytest.fixture()
def admin_client(tmp_path, pool, pg_url, monkeypatch):
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    monkeypatch.setenv("DEVY_ENCRYPTION_KEY", Fernet.generate_key().decode())
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(), router=ToolsRouter(),
    )
    client = TestClient(app)
    token = client.post("/v1/admin/login", json={"password": "pw"}).json()["token"]
    client.headers.update({"Authorization": f"Bearer {token}"})
    return client


def test_github_account_crud_endpoints(admin_client):
    assert TestClient(admin_client.app).get("/v1/admin/github/accounts").status_code == 401

    created = admin_client.post(
        "/v1/admin/github/accounts",
        json={"label": "home", "login": "octocat", "default_corpus": "infra", "token": "ghp_x"},
    )
    assert created.status_code == 201
    body = created.json()
    assert body["label"] == "home" and body["has_token"] is True and "token" not in body
    aid = body["id"]

    assert any(a["id"] == aid for a in admin_client.get("/v1/admin/github/accounts").json())
    patched = admin_client.patch(f"/v1/admin/github/accounts/{aid}", json={"active": False})
    assert patched.json()["active"] is False
    assert admin_client.delete(f"/v1/admin/github/accounts/{aid}").status_code == 200


def test_duplicate_label_conflicts(admin_client):
    admin_client.post("/v1/admin/github/accounts", json={"label": "dup", "token": "a"})
    second = admin_client.post("/v1/admin/github/accounts", json={"label": "dup", "token": "b"})
    assert second.status_code == 409


def test_crawls_endpoint_lists_history(admin_client, pool):
    assert TestClient(admin_client.app).get("/v1/admin/github/crawls").status_code == 401
    RepoCrawlStore(pool).record(
        "me/api", "me/api", commit_sha="cafe1234567", default_branch="main",
        files_ingested=7, chunks_written=88,
    )
    rows = admin_client.get("/v1/admin/github/crawls").json()
    assert len(rows) == 1
    assert rows[0]["full_name"] == "me/api" and rows[0]["commit_sha"] == "cafe1234567"
    assert rows[0]["files_ingested"] == 7 and rows[0]["chunks_written"] == 88
    # Live KB-footprint counts are present (0 here — no docs ingested into the corpus).
    assert rows[0]["doc_count"] == 0 and rows[0]["chunk_count"] == 0
