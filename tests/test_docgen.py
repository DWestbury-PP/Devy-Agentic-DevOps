"""Doc-generation deterministic spine (Phase D-2-1): component discovery, diff
mapping, and signal selection (pure, no LLM/network), plus the checkpoint +
component stores on the live DB."""

import pytest

from agentic_devops.knowledge.docgen import (
    Component,
    discover_components,
    head_is_current,
    map_changes,
    select_signal_files,
)
from agentic_devops.proxy.docgen_store import DocComponentStore, RepoDocgenStore


# -- component discovery ----------------------------------------------------
def test_dockerfile_dirs_are_components():
    paths = [
        "services/api/Dockerfile", "services/api/main.py",
        "services/worker/Dockerfile", "services/worker/run.py",
        "README.md",
    ]
    comps = discover_components(paths, repo_name="trading")
    roots = {c.path: c.kind for c in comps}
    assert roots == {"services/api": "dockerfile", "services/worker": "dockerfile"}


def test_manifest_dirs_are_components():
    paths = ["apps/web/package.json", "packages/db/pyproject.toml", "go.mod"]
    comps = discover_components(paths, repo_name="repo")
    roots = {c.path: c.kind for c in comps}
    assert roots == {"apps/web": "manifest", "packages/db": "manifest", "": "manifest"}


def test_dockerfile_wins_over_manifest_in_same_dir():
    comps = discover_components(["svc/Dockerfile", "svc/package.json"], repo_name="r")
    assert len(comps) == 1 and comps[0].path == "svc" and comps[0].kind == "dockerfile"


def test_brass_tacks_fallback_when_nothing_delineates():
    # No Dockerfiles, no manifests (the messy / containerless repo).
    comps = discover_components(["src/a.rb", "lib/b.rb", "doc.md"], repo_name="legacy")
    assert comps == [Component(path="", name="legacy", kind="observed")]


def test_component_name_from_dir_basename():
    comps = discover_components(["apps/pricing-svc/package.json"], repo_name="r")
    assert comps[0].name == "pricing-svc"


# -- diff → touched components ----------------------------------------------
COMPONENTS = [
    Component(path="services/api", name="api", kind="dockerfile"),
    Component(path="services/worker", name="worker", kind="dockerfile"),
]


def test_changes_map_to_longest_prefix_component():
    cs = map_changes(["services/api/handlers/orders.py", "services/worker/run.py"], COMPONENTS)
    assert [c.path for c in cs.touched] == ["services/api", "services/worker"]
    assert cs.deployment_changed is False and cs.unmatched == []


def test_only_touched_components_returned():
    cs = map_changes(["services/api/x.py"], COMPONENTS)
    assert [c.path for c in cs.touched] == ["services/api"]  # worker untouched → absent


def test_compose_and_ci_flag_deployment():
    assert map_changes(["docker-compose.prod.yml"], COMPONENTS).deployment_changed is True
    assert map_changes([".github/workflows/deploy.yml"], COMPONENTS).deployment_changed is True


def test_unmatched_paths_when_no_root_component():
    cs = map_changes(["docs/notes.md"], COMPONENTS)
    assert cs.touched == [] and cs.unmatched == ["docs/notes.md"]


def test_root_component_catches_everything():
    root = [Component(path="", name="r", kind="observed")]
    cs = map_changes(["any/where/file.py"], root)
    assert [c.path for c in cs.touched] == [""] and cs.unmatched == []


# -- signal-file selection --------------------------------------------------
def test_signal_files_prioritized_and_capped():
    comp = Component(path="svc", name="svc", kind="dockerfile")
    paths = [
        "svc/Dockerfile", "svc/package.json", "svc/README.md", "svc/config.yaml",
        "svc/src/main.ts", "svc/src/util.ts", "svc/docs/design.md",
        "other/thing.py",  # outside the component
    ]
    sig = select_signal_files(comp, paths, max_files=4)
    assert "other/thing.py" not in sig
    # rank 0 (manifest/Dockerfile) come before entry points / other docs
    assert sig[0] in ("svc/Dockerfile", "svc/package.json")
    assert "svc/src/util.ts" not in sig  # not a signal file at all
    assert len(sig) == 4


def test_signal_files_root_component_scans_all():
    comp = Component(path="", name="r", kind="observed")
    sig = select_signal_files(comp, ["pyproject.toml", "README.md", "app.py", "x.txt"])
    assert "pyproject.toml" in sig and "README.md" in sig and "app.py" in sig
    assert "x.txt" not in sig


def test_signal_files_partition_by_ownership():
    # A root/workspace component must NOT swallow a sub-component's files.
    comps = [
        Component(path="", name="root", kind="manifest"),
        Component(path="apps/web", name="web", kind="manifest"),
    ]
    paths = ["package.json", "apps/web/package.json", "apps/web/README.md"]
    root_sig = select_signal_files(comps[0], paths, components=comps)
    web_sig = select_signal_files(comps[1], paths, components=comps)
    assert root_sig == ["package.json"]  # only the root's own manifest
    assert set(web_sig) == {"apps/web/package.json", "apps/web/README.md"}


# -- checkpoint short-circuit -----------------------------------------------
def test_head_is_current_short_circuit():
    assert head_is_current("abc", "abc") is True
    assert head_is_current("abc", "def") is False
    assert head_is_current(None, "abc") is False
    assert head_is_current("abc", None) is False


# -- stores (live DB) -------------------------------------------------------
def test_repo_docgen_checkpoint_and_brief(pool):
    store = RepoDocgenStore(pool)
    store.set_brief("me/api", "the matching engine lives in core/")
    assert store.get("me/api").scan_brief.startswith("the matching engine")
    store.checkpoint("me/api", "sha123", default_branch="main", components_doced=3)
    row = store.get("me/api")
    assert row.last_doc_sha == "sha123" and row.components_doced == 3
    assert row.status == "idle" and row.scan_brief.startswith("the matching")  # brief preserved


def test_repo_docgen_status_transitions(pool):
    store = RepoDocgenStore(pool)
    store.set_status("me/api", "running")
    assert store.get("me/api").status == "running"
    store.set_status("me/api", "error", error="boom")
    row = store.get("me/api")
    assert row.status == "error" and row.error == "boom"


def test_doc_component_upsert_and_list(pool):
    store = DocComponentStore(pool)
    c = Component(path="services/api", name="api", kind="dockerfile")
    store.upsert("me/api", c, arch_doc_path="me/api/services/api/architecture.md")
    rows = store.list("me/api")
    assert len(rows) == 1 and rows[0].component_name == "api" and rows[0].status == "draft"
    # re-upsert preserves the doc path (COALESCE) and updates status
    store.upsert("me/api", c, status="approved")
    got = store.get("me/api", "services/api")
    assert got.status == "approved" and got.arch_doc_path.endswith("architecture.md")


def test_doc_component_status_change(pool):
    store = DocComponentStore(pool)
    store.upsert("me/api", Component(path="", name="api", kind="observed"))
    store.set_status("me/api", "", "approved")
    assert store.get("me/api", "").status == "approved"
