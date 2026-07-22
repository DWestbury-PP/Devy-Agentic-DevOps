"""Attachments Phase 3 — one-time vision digest + view_image. Hermetic (fake
provider + fake blob store); the DB-backed AttachmentStore uses the live pool."""

from types import SimpleNamespace

from agentic_devops.config import Settings
from agentic_devops.proxy.attachments import AttachmentStore, DigestService
from agentic_devops.proxy.sessions import Session, _flatten_content
from agentic_devops.tools.base import ToolResult
import pytest

from agentic_devops.tools.builtin.attachments import build_view_image_tool

REF = "a" * 64


@pytest.fixture(autouse=True)
def _clean_attachments(pool):
    with pool.connection() as c:
        c.execute("DELETE FROM attachments WHERE hash = ANY(%s)",
                  ([REF, "b" * 64, "c" * 64, "z" * 64],))
    yield


class FakeBlobs:
    """Returns bytes for any ref in ``known`` (default: everything)."""
    def __init__(self, known=None, data=b"\x89PNGpix", mime="image/png"):
        self._known, self._data, self._mime = known, data, mime
    def get(self, h):
        if self._known is not None and h not in self._known:
            return None
        return (self._data, self._mime)


class CountingProvider:
    def __init__(self):
        self.calls = 0
    def complete(self, messages, tier, tools=None):
        self.calls += 1
        assert any(isinstance(m.get("content"), list) and
                   any(p.get("type") == "image_url" for p in m["content"]) for m in messages)
        return SimpleNamespace(text="A red square on a white background.")


def _settings():
    s = Settings()
    s.attachments.digest_tier = "balanced"
    return s


# -- digest: generate once, cache, dedupe ------------------------------------
def test_digest_generated_once_then_cached(pool):
    store = AttachmentStore(pool)
    store.record(REF, "image/png", 7)
    prov = CountingProvider()
    svc = DigestService(store, FakeBlobs(), prov, _settings())

    d1 = svc.ensure(REF)
    assert d1 == "A red square on a white background." and prov.calls == 1
    # second call → served from the store, NO new vision call (process once)
    d2 = svc.ensure(REF)
    assert d2 == d1 and prov.calls == 1
    assert store.get_digest(REF) == d1


def test_digest_disabled_skips_generation(pool):
    ref = "b" * 64  # fresh — not digested elsewhere
    s = _settings(); s.attachments.digest_enabled = False
    prov = CountingProvider()
    svc = DigestService(AttachmentStore(pool), FakeBlobs(), prov, s)
    assert svc.ensure(ref) is None and prov.calls == 0


def test_digest_missing_blob_returns_none(pool):
    ref = "c" * 64
    svc = DigestService(AttachmentStore(pool), FakeBlobs(known=set()), CountingProvider(), _settings())
    assert svc.ensure(ref) is None  # blob store has no such key


# -- context flattening uses the digest + exposes the ref for view_image -----
def test_working_context_uses_digest_with_view_image_hint():
    s = Session(id="t")
    s.add_user([{"type": "text", "text": "what's here"},
                {"type": "image_ref", "ref": REF, "mime": "image/png", "name": "cli.png"}])
    s.add_assistant("A docker ps listing.")
    plain = _flatten_content(s.messages[0]["content"])
    assert "cli.png" in plain and REF in plain and "Description" not in plain
    rich = _flatten_content(s.messages[0]["content"], {REF: "docker ps: 7 containers, all healthy"})
    assert "docker ps: 7 containers" in rich and "view_image" in rich and REF in rich
    ctx = s.working_context({REF: "docker ps: 7 containers"})
    assert all(not isinstance(m.get("content"), list) for m in ctx)


# -- view_image returns the pixels as a ToolResult (rides the #55 path) -------
def test_view_image_tool_returns_image():
    tool = build_view_image_tool(FakeBlobs(known={REF}))
    out = tool.handler({"ref": REF})
    assert isinstance(out, ToolResult)
    assert out.images and out.images[0].mime == "image/png"
    assert "ERROR" in tool.handler({"ref": "z" * 64})  # unknown → friendly error
    assert "ERROR" in tool.handler({})                  # missing ref


def test_recent_image_refs_collects_from_window():
    s = Session(id="t")
    s.add_user([{"type": "text", "text": "a"}, {"type": "image_ref", "ref": REF, "mime": "image/png"}])
    s.add_assistant("ok")
    s.add_user("plain follow-up")  # no image
    assert s.recent_image_refs() == [REF]
