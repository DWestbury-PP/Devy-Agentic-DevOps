"""Secret redaction at ingest (Phase C): the Redactor + ingest/fact integration."""

import hashlib

import pytest

from agentic_devops.knowledge.embeddings import Embedder
from agentic_devops.knowledge.facts import FactStore
from agentic_devops.knowledge.ingest import ingest_path
from agentic_devops.knowledge.redaction import RedactionQuarantine, Redactor
from agentic_devops.knowledge.store import PgVectorStore
from agentic_devops.tools.builtin.facts import build_memory_add_tool

# A high-entropy, mixed-class token that no Tier-1 pattern matches and that isn't a
# known-safe shape (not 40/64-hex, not a UUID) — the Tier-2 trigger.
HE = "Zk9xQ2vL8mB4nR7tW1cY6pH3jD5sA0eGfUiObTlK"


# -- Tier 1: high-confidence patterns redacted inline ----------------------
@pytest.mark.parametrize("kind,secret", [
    ("aws_access_key", "AKIAIOSFODNN7EXAMPLE"),
    ("github_token", "ghp_" + "a" * 36),
    ("slack_token", "xoxb-123456789012-abcdefABCDEF"),
    ("google_api_key", "AIza" + "B" * 35),
    ("jwt", "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.dQw4w9WgXcQabcd1234"),
])
def test_tier1_patterns_redacted_inline(kind, secret):
    r = Redactor()
    out = r.scan(f"the credential is {secret} in config")
    assert secret not in out.text
    assert f"«REDACTED:{kind}»" in out.text
    assert out.findings.get(kind) == 1
    assert out.quarantine is False  # known patterns never quarantine


def test_pem_private_key_block_redacted():
    pem = "-----BEGIN RSA PRIVATE KEY-----\nMIIEoperhapslongbase64\n-----END RSA PRIVATE KEY-----"
    out = Redactor().scan(f"key:\n{pem}\nend")
    assert "PRIVATE KEY" not in out.text.replace("«REDACTED:pem_private_key»", "")
    assert out.findings.get("pem_private_key") == 1


def test_bearer_token_keeps_prefix_redacts_value():
    out = Redactor().scan("Authorization: Bearer abc123DEF456ghi789JKL")
    assert "Bearer" in out.text and "abc123DEF456ghi789JKL" not in out.text
    assert out.findings.get("bearer_token") == 1


def test_secret_assignment_keeps_key_redacts_value():
    out = Redactor().scan('aws_secret_access_key = "wJalrXUtnFEMI1234K7MDENGbPxRfiCYEXAMPLEKEY"')
    assert "aws_secret_access_key" in out.text
    assert "wJalrXUtnFEMI1234K7MDENGbPxRfiCYEXAMPLEKEY" not in out.text
    assert out.findings.get("secret_assignment") == 1


# -- safe shapes survive ----------------------------------------------------
@pytest.mark.parametrize("safe", [
    "a1b2c3d4e5f6a7b8c9d0e1f2a3b4c5d6e7f8a9b0",          # git SHA-1 (40 hex)
    "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855",  # SHA-256
    "550e8400-e29b-41d4-a716-446655440000",               # UUID
])
def test_safe_shapes_not_flagged(safe):
    out = Redactor().scan(f"reference {safe} here")
    assert out.total == 0 and out.quarantine is False
    assert safe in out.text


def test_plain_prose_untouched():
    out = Redactor().scan("Restart the checkout worker pool to recover from the latency spike.")
    assert out.total == 0 and out.quarantine is False


# -- Tier 2 posture ---------------------------------------------------------
def test_high_entropy_quarantines_fail_closed():
    out = Redactor(mode="fail_closed").scan(f"the artifact token is {HE} apparently")
    assert out.quarantine is True
    assert out.findings.get("high_entropy") == 1


def test_high_entropy_redacted_inline_best_effort():
    out = Redactor(mode="best_effort").scan(f"the artifact token is {HE} apparently")
    assert out.quarantine is False
    assert HE not in out.text and "«REDACTED:high_entropy»" in out.text


def test_entropy_disabled_lets_blob_through():
    out = Redactor(entropy_enabled=False).scan(f"the artifact token is {HE}")
    assert out.quarantine is False and HE in out.text


def test_redaction_is_idempotent():
    r = Redactor()
    once = r.scan("key AKIAIOSFODNN7EXAMPLE here")
    twice = r.scan(once.text)
    assert twice.total == 0  # placeholders aren't re-matched


# -- integration: ingest pipeline ------------------------------------------
_DIM = 64


def _fake_embed(texts, model, api_base):
    out = []
    for t in texts:
        v = [0.0] * _DIM
        for tok in t.lower().split():
            v[int(hashlib.sha256(tok.encode()).hexdigest(), 16) % _DIM] += 1.0
        out.append(v)
    return out


@pytest.fixture()
def embedder():
    return Embedder(model="fake", embed_fn=_fake_embed)


@pytest.fixture()
def store(pool):
    return PgVectorStore(pool)


def test_ingest_redacts_tier1_and_quarantines_tier2(tmp_path, store, embedder):
    d = tmp_path / "kb"
    d.mkdir()
    (d / "ok.md").write_text("# Cfg\n\nThe access key AKIAIOSFODNN7EXAMPLE is used by the job.\n")
    (d / "bad.md").write_text(f"# Blob\n\nMystery value {HE} found in the dump.\n")
    stats = ingest_path(d, store, embedder, corpus="red", redactor=Redactor(mode="fail_closed"))

    assert stats.files_ingested == 1       # ok.md
    assert stats.files_quarantined == 1    # bad.md held back
    assert stats.secrets_redacted >= 1

    hits = store.hybrid_search("access key job", embedder.embed_query("access key job"), k=5)
    assert hits
    assert all("AKIAIOSFODNN7EXAMPLE" not in h.chunk.text for h in hits)
    assert any("«REDACTED:aws_access_key»" in h.chunk.text for h in hits)
    # The quarantined blob never reached the store.
    assert all(HE not in h.chunk.text for h in hits)


def test_fact_deposit_redacts_and_quarantines(pool, embedder):
    fs = FactStore(pool, embedder, redactor=Redactor(mode="fail_closed"))
    # Tier-1 secret → stored, value redacted.
    res = fs.add_fact("the api key is ghp_" + "b" * 36, source="t", subject="svc:x", attribute="key")
    stored = fs.get(res.memory_id)
    assert stored and "ghp_" not in stored.content and "«REDACTED:github_token»" in stored.content
    # Tier-2 ambiguous → rejected.
    with pytest.raises(RedactionQuarantine):
        fs.add_fact(f"mystery token {HE}", source="t")


def test_memory_add_tool_reports_quarantine(pool, embedder):
    fs = FactStore(pool, embedder, redactor=Redactor(mode="fail_closed"))
    tool = build_memory_add_tool(fs)
    out = tool.handler({"content": f"the secret blob is {HE}"}, {"user_id": "u"})
    assert out.startswith("ERROR") and "secret" in out.lower()
