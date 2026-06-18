"""Document-import admin API (Phase 9c-2): upload, list, jobs, corpora, delete."""

import hashlib

import bcrypt
import pytest
from fastapi.testclient import TestClient

from agentic_devops.config import DatabaseConfig, Settings
from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.store import PgVectorStore
from agentic_devops.proxy.app import create_app
from agentic_devops.proxy.documents import DocumentStore, JobStore
from agentic_devops.proxy.ingest_worker import IngestWorker
from agentic_devops.tools.router import ToolsRouter


def _fake_embed(texts, model, api_base):
    out = []
    for t in texts:
        v = [0.0] * 16
        for tok in t.lower().split():
            v[int(hashlib.sha256(tok.encode()).hexdigest(), 16) % 16] += 1.0
        out.append(v)
    return out


@pytest.fixture()
def client(tmp_path, pool, pg_url, monkeypatch):
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"hunter2", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(), router=ToolsRouter(),
    )
    return TestClient(app)


@pytest.fixture()
def auth(client):
    token = client.post("/v1/admin/login", json={"password": "hunter2"}).json()["token"]
    return {"Authorization": f"Bearer {token}"}


def _md(name, body):
    return ("files", (name, body.encode(), "text/markdown"))


def test_upload_registers_pending_docs_and_a_queued_job(client, auth):
    r = client.post(
        "/v1/admin/documents",
        data={"corpus": "kb"},
        files=[_md("a.md", "# A\n\n## S\n\nbody one"), _md("b.md", "# B\n\n## S\n\nbody two")],
        headers=auth,
    )
    assert r.status_code == 201
    body = r.json()
    assert body["job"]["status"] == "queued" and body["job"]["total"] == 2
    assert {d["source_path"] for d in body["documents"]} == {"a.md", "b.md"}
    assert all(d["status"] == "pending" for d in body["documents"])
    assert body["documents"][0]["title"] in {"A", "B"}

    listed = client.get("/v1/admin/documents?corpus=kb", headers=auth).json()
    assert len(listed) == 2


def test_upload_rejects_non_markdown_and_empty_corpus(client, auth):
    assert client.post(
        "/v1/admin/documents", data={"corpus": "kb"},
        files=[("files", ("notes.txt", b"hi", "text/plain"))], headers=auth,
    ).status_code == 400
    assert client.post(
        "/v1/admin/documents", data={"corpus": "  "},
        files=[_md("a.md", "# A\n\nx")], headers=auth,
    ).status_code == 400


def test_endpoints_require_admin(client):
    assert client.get("/v1/admin/documents").status_code == 401
    assert client.get("/v1/admin/corpora").status_code == 401
    assert client.post("/v1/admin/documents", data={"corpus": "k"},
                       files=[_md("a.md", "# A\n\nx")]).status_code == 401


def test_delete_document_and_corpora_listing(client, auth, pool):
    up = client.post(
        "/v1/admin/documents", data={"corpus": "kb"},
        files=[_md("a.md", "# A\n\n## S\n\nbody")], headers=auth,
    ).json()
    doc_id = up["documents"][0]["id"]

    # Process the batch with a fake-embedder worker (the app's worker uses a real
    # embedder and isn't auto-started under TestClient).
    worker = IngestWorker(DocumentStore(pool), JobStore(pool), PgVectorStore(pool),
                          Embedder(model="fake", embed_fn=_fake_embed))
    assert worker.run_once() is True

    job_id = up["job"]["id"]
    assert client.get(f"/v1/admin/jobs/{job_id}", headers=auth).json()["status"] == "done"

    corpora = client.get("/v1/admin/corpora", headers=auth).json()
    kb = next(c for c in corpora if c["name"] == "kb")
    assert kb["documents"] == 1 and kb["chunks"] >= 1

    assert client.delete(f"/v1/admin/documents/{doc_id}", headers=auth).status_code == 200
    assert client.get("/v1/admin/documents?corpus=kb", headers=auth).json() == []
    assert client.delete(f"/v1/admin/documents/{doc_id}", headers=auth).status_code == 404
