"""Read-only GitHub client + markdown crawl (Phase D-1) — no network, no DB-only.

The client is exercised through its request_fn seam with canned responses; the
crawl test uses a fake client + the live pgvector store (pool fixture) to prove
fetched markdown flows through the real OKF + redaction ingest pipeline.
"""

import base64
import hashlib

import pytest

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.redaction import Redactor
from agentic_devops.knowledge.store import PgVectorStore
from agentic_devops.proxy.github_client import GitHubClient, GitHubError
from agentic_devops.proxy.github_crawl import crawl_repo_markdown


def _client(routes):
    """routes: dict mapping URL-suffix -> (status, body)."""
    def request_fn(method, url, headers, params):
        for suffix, resp in routes.items():
            if url.endswith(suffix):
                return resp
        return 404, {"message": "no route"}
    return GitHubClient(request_fn=request_fn)


def test_list_repos_paginates():
    page1 = [{"full_name": f"me/r{i}"} for i in range(100)]
    page2 = [{"full_name": "me/last"}]
    def request_fn(method, url, headers, params):
        return (200, page1) if params.get("page") == 1 else (200, page2)
    client = GitHubClient(request_fn=request_fn)
    repos = client.list_repos("tok")
    assert len(repos) == 101 and repos[-1]["full_name"] == "me/last"


def test_auth_header_sent():
    seen = {}
    def request_fn(method, url, headers, params):
        seen.update(headers)
        return 200, {"login": "octocat"}
    GitHubClient(request_fn=request_fn).whoami("sekret")
    assert seen["Authorization"] == "Bearer sekret"


def test_error_status_mapping():
    with pytest.raises(GitHubError, match="unauthorized"):
        _client({"/user": (401, {"message": "bad creds"})}).whoami("x")
    with pytest.raises(GitHubError, match="not found"):
        _client({"/repos/me/x": (404, {"message": "nope"})}).get_repo("x", "me/x")


def test_get_file_decodes_base64():
    content = base64.b64encode(b"# Title\n\nhello").decode()
    client = _client({"/contents/README.md": (200, {"encoding": "base64", "content": content})})
    assert client.get_file("t", "me/r", "README.md") == "# Title\n\nhello"


def test_search_code_unwraps_items():
    client = _client({"/search/code": (200, {"items": [{"path": "a.py"}, {"path": "b.py"}]})})
    hits = client.search_code("t", "needle")
    assert [h["path"] for h in hits] == ["a.py", "b.py"]


# -- crawl into the live store ---------------------------------------------
_DIM = 64


def _fake_embed(texts, model, api_base):
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in t.lower().split():
            v[int(hashlib.sha256(tok.encode()).hexdigest(), 16) % _DIM] += 1.0
        out.append(v)
    return out


class _CrawlClient:
    """Minimal fake GitHubClient for the crawl path."""

    def __init__(self, files: dict[str, str]):
        self._files = files  # path -> content

    def get_repo(self, token, full_name):
        return {"default_branch": "main"}

    def get_tree(self, token, full_name, ref, recursive=True):
        return [{"type": "blob", "path": p} for p in self._files]

    def get_file(self, token, full_name, path, ref=None):
        return self._files[path]


def test_crawl_ingests_markdown_through_pipeline(pool):
    store = PgVectorStore(pool)
    embedder = Embedder(model="fake", embed_fn=_fake_embed)
    client = _CrawlClient({
        "README.md": "---\ntype: readme\ntags: [intro]\n---\n\n# Project\n\nDeploys the checkout service.\n",
        "docs/ops.md": "# Ops\n\nThe access key AKIAIOSFODNN7EXAMPLE is used by the job.\n",
        "src/main.py": "print('not markdown, skipped')\n",
    })
    stats = crawl_repo_markdown(
        client, "tok", "me/proj", store=store, embedder=embedder,
        corpus="me/proj", redactor=Redactor(),
    )
    assert stats.files_ingested == 2  # two markdown files; .py skipped
    assert stats.corpus == "me/proj"
    assert stats.secrets_redacted >= 1  # the AWS key in ops.md

    # Frontmatter (Phase B) flowed through.
    hits = store.hybrid_search("checkout deploy", embedder.embed_query("checkout deploy"), k=3)
    assert any(h.chunk.metadata.get("type") == "readme" for h in hits)
    # Redaction (Phase C) flowed through.
    ops = store.hybrid_search("access key job", embedder.embed_query("access key job"), k=3)
    assert ops and all("AKIAIOSFODNN7EXAMPLE" not in h.chunk.text for h in ops)


def test_crawl_no_markdown_is_noop(pool):
    store = PgVectorStore(pool)
    embedder = Embedder(model="fake", embed_fn=_fake_embed)
    client = _CrawlClient({"src/main.py": "code", "Makefile": "all:"})
    stats = crawl_repo_markdown(client, "tok", "me/code", store=store, embedder=embedder)
    assert stats.files_ingested == 0 and stats.chunks_written == 0
