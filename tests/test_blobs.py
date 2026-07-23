"""Content-addressed blob store (attachments Phase 1) — hermetic via a fake S3
client, plus the serve endpoint."""

import bcrypt
from fastapi.testclient import TestClient

import agentic_devops.proxy.app as app_mod
from agentic_devops.config import DatabaseConfig, Settings
from agentic_devops.proxy.app import create_app
from agentic_devops.proxy.blobs import BlobStore, build_blob_store, content_hash
from agentic_devops.tools.router import ToolsRouter

PNG = b"\x89PNG\r\n\x1a\n" + b"fake-pixels"


class _Body:
    def __init__(self, data): self._data = data
    def read(self): return self._data


class FakeS3:
    """In-memory S3 double — the injected-client seam, same as the secrets tests."""

    def __init__(self):
        self.objects = {}
        self.put_calls = 0

    def head_bucket(self, Bucket): return {}
    def create_bucket(self, Bucket): return {}

    def head_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Exception("NoSuchKey")
        return {}

    def put_object(self, Bucket, Key, Body, ContentType):
        self.put_calls += 1
        self.objects[Key] = (Body, ContentType)

    def get_object(self, Bucket, Key):
        if Key not in self.objects:
            raise Exception("NoSuchKey")
        body, mime = self.objects[Key]
        return {"Body": _Body(body), "ContentType": mime}


def test_content_hash_is_sha256_hex():
    h = content_hash(PNG)
    assert len(h) == 64 and h.isalnum() and content_hash(PNG) == h


def test_put_get_and_dedupe():
    store = BlobStore(FakeS3(), "b")
    h = store.put(PNG, "image/png")
    assert h == content_hash(PNG)
    assert store.exists(h) is True
    body, mime = store.get(h)
    assert body == PNG and mime == "image/png"
    # identical bytes → same key, no second upload (content-addressed dedupe)
    store.put(PNG, "image/png")
    assert store._client.put_calls == 1
    # missing → None
    assert store.get("0" * 64) is None


def test_build_blob_store_disabled_returns_none():
    s = Settings()
    s.attachments.enabled = False
    assert build_blob_store(s) is None


def test_build_blob_store_dev_without_endpoint_refuses_real_aws(monkeypatch):
    # The guard: dev mode + no AWS_ENDPOINT_URL must NOT build a real S3 client
    # (which would resolve ambient AWS creds and touch a real account).
    monkeypatch.delenv("AWS_ENDPOINT_URL", raising=False)
    s = Settings()  # secrets.mode defaults to 'dev', attachments enabled
    assert s.secrets.mode == "dev" and s.attachments.enabled
    assert build_blob_store(s) is None


def _admin_client(tmp_path, pg_url, monkeypatch, store):
    monkeypatch.setattr(app_mod, "build_blob_store", lambda settings: store)
    monkeypatch.setenv("DEVY_ADMIN_PASSWORD_HASH", bcrypt.hashpw(b"pw", bcrypt.gensalt()).decode())
    monkeypatch.setenv("DEVY_ADMIN_SECRET", "0" * 64)
    app = create_app(
        settings=Settings(database=DatabaseConfig(url=pg_url), trace_dir=tmp_path / "t"),
        provider=object(), router=ToolsRouter(),
    )
    return TestClient(app)


def test_blob_endpoint_serves_and_validates(tmp_path, pool, pg_url, monkeypatch):
    store = BlobStore(FakeS3(), "b")
    h = store.put(PNG, "image/png")
    c = _admin_client(tmp_path, pg_url, monkeypatch, store)

    # serves the bytes with the right content-type + immutable cache
    r = c.get(f"/v1/blobs/{h}")
    assert r.status_code == 200
    assert r.headers["content-type"].startswith("image/png")
    assert r.content == PNG
    assert "immutable" in r.headers.get("cache-control", "")

    # junk key rejected (not 64-hex), unknown-but-valid key → 404
    assert c.get("/v1/blobs/not-a-hash").status_code == 400
    assert c.get(f"/v1/blobs/{'a' * 64}").status_code == 404


def test_blob_endpoint_404_when_disabled(tmp_path, pool, pg_url, monkeypatch):
    c = _admin_client(tmp_path, pg_url, monkeypatch, None)  # attachments off
    assert c.get(f"/v1/blobs/{'a' * 64}").status_code == 404
