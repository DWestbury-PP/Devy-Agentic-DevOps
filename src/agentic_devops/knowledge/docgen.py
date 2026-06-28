"""Doc generation — the deterministic spine (Phase D-2-1).

This module is the *engine* under LLM doc generation, and it is intentionally
**pure**: no network, no model, no DB. It answers three questions from plain file
paths, so the whole efficiency story is unit-testable with zero cost:

1. **What are the components?** (`discover_components`) — the unit of an architecture
   doc. Dockerfiles are the primary seam; package manifests next; a brass-tacks
   single-component fallback for messy/containerless repos.
2. **What changed, and which components does it touch?** (`map_changes`) — the
   diff-driven core: only touched components regenerate; untouched cost nothing.
3. **Which files inform a component's doc?** (`select_signal_files`) — the bounded
   signal surface fed to the model later, so cost scales with change, not repo size.

The live orchestration (fetch tree, `compare`, call the model, write+ingest) layers
on top of these in a later increment, passing the GitHub client and provider in.
"""

from __future__ import annotations

import posixpath
from dataclasses import dataclass, field
from typing import Any

import yaml

# --- component-discovery signals ------------------------------------------

# Dockerfile is the strongest "this directory is a deployable component" signal.
def _is_dockerfile(name: str) -> bool:
    return name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".Dockerfile")


# Package manifests — each marks its directory as a component root.
_MANIFESTS: frozenset[str] = frozenset({
    "package.json", "pyproject.toml", "setup.py", "go.mod", "Cargo.toml",
    "pom.xml", "build.gradle", "build.gradle.kts",
})
_MANIFEST_SUFFIXES: tuple[str, ...] = (".csproj",)

# Compose / CI files attribute a change to the deployment docs, not a component.
def _is_compose(path: str) -> bool:
    name = posixpath.basename(path).lower()
    return name.startswith("docker-compose") or name.startswith("compose.") or name == "compose.yaml"


def _is_ci(path: str) -> bool:
    p = path.lower()
    return (
        p.startswith(".github/workflows/")
        or p in (".gitlab-ci.yml", ".gitlab-ci.yaml")
        or p.startswith("ci/")
        or posixpath.basename(p) in ("jenkinsfile",)
    )


@dataclass(frozen=True)
class Component:
    """A unit that gets one architecture doc. ``path`` is the repo-relative root
    directory ('' for a whole-repo / brass-tacks component)."""

    path: str
    name: str
    kind: str  # dockerfile | manifest | observed

    @property
    def label(self) -> str:
        return self.name or (self.path or "root")


@dataclass
class ChangeSet:
    """The result of mapping a set of changed paths onto components."""

    touched: list[Component] = field(default_factory=list)
    deployment_changed: bool = False
    unmatched: list[str] = field(default_factory=list)


def _dirname(path: str) -> str:
    return posixpath.dirname(path.strip("/"))


def _component_name(root: str, repo_name: str) -> str:
    return posixpath.basename(root) if root else repo_name


def discover_components(paths: list[str], *, repo_name: str = "root") -> list[Component]:
    """Identify components from a repo's file paths (the git tree).

    Order of authority: **Dockerfile dirs** (kind=``dockerfile``) > **manifest dirs**
    (kind=``manifest``). If neither delineates anything, fall back to a single
    **observed** component at the repo root (brass-tacks: document what's there).
    A Dockerfile and a manifest in the same dir → one component, kind ``dockerfile``.
    """
    docker_dirs: set[str] = set()
    manifest_dirs: set[str] = set()
    for p in paths:
        name = posixpath.basename(p)
        d = _dirname(p)
        if _is_dockerfile(name):
            docker_dirs.add(d)
        elif name in _MANIFESTS or name.endswith(_MANIFEST_SUFFIXES):
            manifest_dirs.add(d)

    roots: dict[str, str] = {}  # root -> kind (dockerfile wins)
    for d in manifest_dirs:
        roots[d] = "manifest"
    for d in docker_dirs:
        roots[d] = "dockerfile"

    if not roots:
        return [Component(path="", name=repo_name, kind="observed")]

    return [
        Component(path=r, name=_component_name(r, repo_name), kind=k)
        for r, k in sorted(roots.items())
    ]


def _owning_component(path: str, components: list[Component]) -> Component | None:
    """The component whose root is the longest prefix of ``path``. Root-level
    components ('') match anything, but only as a last resort (shortest prefix)."""
    best: Component | None = None
    best_len = -1
    for c in components:
        root = c.path
        if root == "":
            matches, rlen = True, 0
        else:
            matches = path == root or path.startswith(root + "/")
            rlen = len(root)
        if matches and rlen > best_len:
            best, best_len = c, rlen
    return best


def map_changes(changed_paths: list[str], components: list[Component]) -> ChangeSet:
    """Map changed file paths onto components (longest-prefix wins) and flag whether
    deployment-relevant files (compose / CI) changed. Untouched components are simply
    absent from ``touched`` — they cost nothing downstream."""
    touched: dict[str, Component] = {}
    unmatched: list[str] = []
    deployment = False
    for p in changed_paths:
        if _is_compose(p) or _is_ci(p):
            deployment = True
        owner = _owning_component(p, components)
        if owner is None:
            unmatched.append(p)
        else:
            touched.setdefault(owner.path, owner)
    ordered = [c for c in components if c.path in touched]
    return ChangeSet(touched=ordered, deployment_changed=deployment, unmatched=unmatched)


# --- signal-file selection (bounds the model's input) ----------------------

_SIGNAL_DOC = ("readme",)  # README* (any case)
_SIGNAL_CONFIG_PREFIXES = ("config", "settings")
_SIGNAL_ENTRY = ("main", "index", "app", "server")


def _under(component: Component, path: str) -> bool:
    return component.path == "" or path == component.path or path.startswith(component.path + "/")


def _signal_rank(path: str) -> int | None:
    """Lower rank = higher priority. ``None`` = not a signal file."""
    name = posixpath.basename(path)
    low = name.lower()
    stem = low.rsplit(".", 1)[0]
    if _is_dockerfile(name) or low in _MANIFESTS or name.endswith(_MANIFEST_SUFFIXES):
        return 0  # manifests + Dockerfile: the architecture skeleton
    if _is_compose(path):
        return 1
    if stem in _SIGNAL_DOC or low.startswith("readme"):
        return 1  # READMEs
    if low.endswith((".env.example", ".env.sample")) or stem in _SIGNAL_CONFIG_PREFIXES:
        return 2  # config / secret-shape
    if stem in _SIGNAL_ENTRY:
        return 3  # entry points
    if low.endswith((".md", ".markdown")):
        return 4  # other docs
    return None


def select_signal_files(
    component: Component, all_paths: list[str], *,
    components: list[Component] | None = None, max_files: int = 40,
) -> list[str]:
    """The bounded set of architecture-relevant files for a component — manifests,
    Dockerfile, compose, config, entry points, docs — in priority order, capped at
    ``max_files``. Cost is bounded by this surface, not the component's size.

    When ``components`` (the full set) is given, files are **partitioned by ownership**
    (longest-prefix wins), so a parent/root component does not swallow files that
    belong to a more-specific sub-component — each file informs exactly one doc.
    """
    scored: list[tuple[int, str]] = []
    for p in all_paths:
        owns = (
            _owning_component(p, components) == component
            if components is not None
            else _under(component, p)
        )
        if not owns:
            continue
        rank = _signal_rank(p)
        if rank is not None:
            scored.append((rank, p))
    scored.sort(key=lambda t: (t[0], t[1]))
    return [p for _, p in scored[:max_files]]


def head_is_current(last_doc_sha: str | None, head_sha: str | None) -> bool:
    """True when the repo is unchanged since the last docgen (skip → zero tokens).
    A missing checkpoint or HEAD is never 'current' (force a run)."""
    return bool(last_doc_sha) and bool(head_sha) and last_doc_sha == head_sha


# --- OKF assembly + synthesis prompt (pure) --------------------------------

_PER_FILE_CHARS = 3500   # cap each signal file in the prompt
_TOTAL_FILE_CHARS = 60000  # cap the whole signal bundle (the root component guard)


def arch_doc_path(full_name: str, component: Component) -> str:
    """Repo-relative path of a component's architecture doc within the docs corpus."""
    base = posixpath.join(full_name, component.path) if component.path else full_name
    return posixpath.join(base, "architecture.md")


def architecture_frontmatter(
    full_name: str, component: Component, *, commit_sha: str, model: str, generated_at: str,
) -> dict[str, Any]:
    """OKF frontmatter for a generated architecture doc — loud provenance, `type`
    present (producer conformance), `resource` binding the doc to the component."""
    return {
        "type": "architecture",
        "title": f"{component.label} — Architecture",
        "resource": f"{full_name}:{component.path}" if component.path else full_name,
        "generated": True,
        "source_commit": commit_sha,
        "model": model,
        "generated_at": generated_at,
        "status": "draft",
        "tags": [full_name, component.label, "generated", "architecture"],
    }


def assemble_okf(frontmatter: dict[str, Any], body: str) -> str:
    """A complete OKF markdown doc: a YAML frontmatter block + the body. Producer
    conformance: a non-empty `type` is required (raises if absent)."""
    if not frontmatter.get("type"):
        raise ValueError("OKF doc requires a non-empty 'type'")
    fm = yaml.safe_dump(frontmatter, sort_keys=False, allow_unicode=True).strip()
    return f"---\n{fm}\n---\n\n{body.strip()}\n"


_ARCH_INSTRUCTION = """\
You are a senior SRE documenting a software component for an operational knowledge \
base used during incident response. Produce a precise, factual **architecture** \
document in Markdown.

STRICT GROUNDING RULES (this is for incident response — wrong facts are dangerous):
- Use ONLY the files provided below. Do not invent ports, endpoints, dependencies, \
or secrets.
- When something is not evidenced by the files, write "Not evident from the \
available files." — do NOT guess.
- Never reproduce secret values. Describe only the NAME and SOURCE of a secret \
(e.g. "expects DATABASE_URL from the environment").

Output ONLY the Markdown body (no frontmatter, no surrounding code fence). Use these \
H2 sections, omitting a section only if you have a "Not evident" note for it:

## Component & services
## Ports & endpoints
## External services
## Architecture & dependencies
## Build toolchain
## Deployment
## Secret handling
## Informed by
(list the files you used)
"""


def architecture_prompt(
    full_name: str, component: Component, signal_files: dict[str, str], *, scan_brief: str = "",
) -> str:
    """Build the synthesis prompt: instruction + optional operator scan brief +
    the bounded signal files (path-labeled, per-file and total caps applied)."""
    parts = [_ARCH_INSTRUCTION, f"\nREPOSITORY: {full_name}\nCOMPONENT: {component.label}"]
    if component.path:
        parts.append(f"COMPONENT PATH: {component.path}/")
    if scan_brief.strip():
        parts.append(
            "\nOPERATOR GUIDANCE (authoritative context about this repo — trust it):\n"
            + scan_brief.strip()
        )
    parts.append("\n--- FILES ---")
    budget = _TOTAL_FILE_CHARS
    for path, content in signal_files.items():
        if budget <= 0:
            parts.append(f"\n(+ more files omitted for length)")
            break
        snippet = content[:_PER_FILE_CHARS]
        if len(content) > _PER_FILE_CHARS:
            snippet += "\n… (truncated)"
        snippet = snippet[:budget]
        budget -= len(snippet)
        parts.append(f"\n### FILE: {path}\n```\n{snippet}\n```")
    return "\n".join(parts)
