"""Release ledger (CI→CD read layer) — the ReleaseLedger + the list_releases tool.

Hermetic: exercises ReleaseLedger against the in-memory _FakeSSMClient (from
conftest) — no boto3, LocalStack, or network. Covers manifest parsing, the three
ledger shapes (by-commit / by-branch / components), branch sanitization, the
assembled-latest mapping, best-effort degradation on a denied path, and the tool.
"""

import json

import pytest

from agentic_devops.proxy.releases import Release, ReleaseLedger, safe_branch
from agentic_devops.tools.builtin.releases import build_release_tools
from tests.conftest import make_fake_ledger


def _manifest(sha, branch="main", status="complete", built_at="2026-08-01T10:00:00Z", comps=("proxy", "chat-ui")):
    return {
        "schema": 1, "sha": sha, "short_sha": sha[:7], "branch": branch, "ref": branch,
        "built_at": built_at, "label": f"{branch}-{sha[:7]}", "run_url": "https://x/run/1",
        "actor": "tester", "status": status,
        "components": {
            c: {"image": f"acct.dkr.ecr/devy-{c}:{branch}-{sha[:7]}", "tag": f"{branch}-{sha[:7]}",
                "digest": f"sha256:{c}", "repository": f"devy-{c}"}
            for c in comps
        },
    }


def _params(*manifests, branch_latest=None, components=None, prefix="/devy/builds"):
    d = {}
    for m in manifests:
        d[f"{prefix}/by-commit/{m['sha']}"] = json.dumps(m)
    for b, sha in (branch_latest or {}).items():
        d[f"{prefix}/by-branch/{b}/latest"] = sha
    for c, entry in (components or {}).items():
        d[f"{prefix}/components/{c}/latest"] = json.dumps(entry)
    return d


# -- schema parsing ------------------------------------------------------------

def test_release_from_manifest_types_components():
    r = Release.from_manifest(_manifest("abc1234def"))
    assert r.sha == "abc1234def" and r.complete is True
    assert set(r.components) == {"proxy", "chat-ui"}
    assert r.components["proxy"].repository == "devy-proxy"
    assert r.to_dict()["complete"] is True


def test_partial_status_is_not_complete():
    r = Release.from_manifest(_manifest("f00", status="partial"))
    assert r.complete is False


# -- ledger reads --------------------------------------------------------------

def test_get_release_and_missing():
    led = make_fake_ledger(_params(_manifest("deadbeef01")))
    assert led.get_release("deadbeef01").short_sha == "deadbee"
    assert led.get_release("nope") is None


def test_resolve_branch_follows_pointer():
    led = make_fake_ledger(_params(
        _manifest("aaa1111"), branch_latest={"main": "aaa1111"},
    ))
    r = led.resolve_branch("main")
    assert r is not None and r.sha == "aaa1111"
    assert led.resolve_branch("no-such-branch") is None


def test_list_releases_sorted_newest_first():
    led = make_fake_ledger(_params(
        _manifest("old", built_at="2026-07-01T00:00:00Z"),
        _manifest("mid", built_at="2026-08-01T00:00:00Z"),
        _manifest("new", built_at="2026-08-10T00:00:00Z"),
    ))
    got = [r.sha for r in led.list_releases()]
    assert got == ["new", "mid", "old"]
    assert [r.sha for r in led.list_releases(limit=2)] == ["new", "mid"]


def test_components_latest_and_assembled():
    led = make_fake_ledger(_params(components={
        "proxy": {"image": "acct/devy-proxy:x", "tag": "x", "digest": "sha256:a", "repository": "devy-proxy"},
        "chat-ui": {"image": "acct/devy-chat-ui:y", "tag": "y", "digest": "sha256:b", "repository": "devy-chat-ui"},
    }))
    comps = led.components_latest()
    assert set(comps) == {"proxy", "chat-ui"}
    assert comps["proxy"].image == "acct/devy-proxy:x"
    # assembled-latest keys by repository sans devy- (exactly like deploy.yml)
    assert led.assembled_latest() == {"proxy": "acct/devy-proxy:x", "chat-ui": "acct/devy-chat-ui:y"}


def test_pagination_loop_collects_all():
    # 25 commits > MaxResults(10) → forces the NextToken loop in _by_path.
    manifests = [_manifest(f"c{i:03d}", built_at=f"2026-08-{(i % 27) + 1:02d}T00:00:00Z") for i in range(25)]
    led = make_fake_ledger(_params(*manifests))
    assert len(led.list_releases(limit=0)) == 25


def test_malformed_manifest_is_skipped_not_fatal():
    params = {"/devy/builds/by-commit/bad": "{not json", "/devy/builds/by-commit/good": json.dumps(_manifest("good"))}
    led = make_fake_ledger(params)
    shas = [r.sha for r in led.list_releases()]
    assert shas == ["good"]


# -- degradation ---------------------------------------------------------------

def test_denied_path_degrades_best_effort():
    led = make_fake_ledger(_params(_manifest("x")), deny=True)
    assert led.health() is False
    assert led.list_releases() == []
    assert led.get_release("x") is None
    assert led.components_latest() == {}


def test_health_true_when_reachable():
    assert make_fake_ledger(_params(_manifest("x"))).health() is True


# -- branch sanitization -------------------------------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("main", "main"),
    ("feat/foo", "feat-foo"),
    ("release/v1.2", "release-v1.2"),
    ("weird@#$name", "weirdname"),
    ("", "detached"),
])
def test_safe_branch(raw, expected):
    assert safe_branch(raw) == expected


def test_resolve_branch_uses_sanitized_key():
    # pointer stored under the sanitized branch, queried with the raw slashy name
    led = make_fake_ledger(_params(
        _manifest("z9", branch="feat-foo"), branch_latest={"feat-foo": "z9"},
    ))
    assert led.resolve_branch("feat/foo").sha == "z9"


# -- the agent tool ------------------------------------------------------------

def _tool(led: ReleaseLedger):
    (spec,) = build_release_tools(led)
    return spec


def test_tool_spec_is_read_only():
    spec = _tool(make_fake_ledger({}))
    assert spec.name == "list_releases"
    assert spec.category == "releases"
    assert spec.safety_tier == "read-only"


def test_tool_recent_lists_builds():
    led = make_fake_ledger(_params(_manifest("aaa1111"), _manifest("bbb2222", status="partial")))
    out = _tool(led).handler({"scope": "recent"})
    assert "aaa1111"[:7] in out and "PARTIAL" in out


def test_tool_branch_and_components_scopes():
    led = make_fake_ledger(_params(
        _manifest("aaa1111"), branch_latest={"main": "aaa1111"},
        components={"proxy": {"image": "acct/devy-proxy:x", "repository": "devy-proxy"}},
    ))
    assert "Newest build on 'main'" in _tool(led).handler({"scope": "branch", "branch": "main"})
    assert "proxy" in _tool(led).handler({"scope": "components"})


def test_tool_reports_unreachable_ledger():
    out = _tool(make_fake_ledger(_params(_manifest("x")), deny=True)).handler({"scope": "recent"})
    assert "isn't reachable" in out and "instance role" in out
