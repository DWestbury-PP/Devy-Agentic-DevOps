"""GitHub repo tools (Phase D-1): read-only investigation for triage/RCA.

Native read-only REST wrappers (the official GitHub MCP server has write tools, so
per the connector rule we build native). Devy names a repo; the proxy resolves the
token from the encrypted ``github_accounts`` registry — the agent never handles it.
These feed RCA: a commit/deploy carries a timestamp, so "what shipped right before
the symptom" becomes another event for the timeline.
"""

from __future__ import annotations

from typing import Any

from agentic_devops.proxy.github import GitHubAccountStore
from agentic_devops.proxy.github_client import GitHubClient, GitHubError
from agentic_devops.tools.base import ToolSpec

_NO_ACCOUNT = (
    "No GitHub account is registered (or the choice is ambiguous). An admin can add "
    "a read-only PAT in the admin console."
)
_PATCH_CHARS = 1500
_FILE_CHARS = 4000


def _short(s: str, n: int) -> str:
    return s if len(s) <= n else s[:n] + "\n… (truncated)"


def build_repo_tools(store: GitHubAccountStore, client: GitHubClient) -> list[ToolSpec]:

    def _account(full_name: str = ""):
        return store.resolve_for_repo(full_name) if full_name else store.resolve()

    def lookup(args: dict[str, Any]) -> str:
        ra = _account()
        if ra is None or not ra.token:
            return _NO_ACCOUNT
        try:
            repos = client.list_repos(ra.token)
            store.touch(ra.account.id, "ok")
        except GitHubError as exc:
            store.touch(ra.account.id, "error")
            return f"ERROR: {exc}"
        query = str(args.get("query", "")).strip().lower()
        if query:
            toks = query.split()
            def match(r):
                hay = " ".join(str(x) for x in [
                    r.get("full_name"), r.get("description"), r.get("language"),
                ] if x).lower()
                return all(t in hay for t in toks)
            repos = [r for r in repos if match(r)]
        if not repos:
            return "No repositories matched your query." if query else "No repositories accessible."
        lines = []
        for r in repos[:20]:
            bits = [r.get("full_name", "?")]
            if r.get("private"):
                bits.append("private")
            if r.get("language"):
                bits.append(r["language"])
            if r.get("pushed_at"):
                bits.append(f"pushed {r['pushed_at'][:10]}")
            desc = (r.get("description") or "").strip()
            line = "- " + " · ".join(bits)
            if desc:
                line += f"\n    {desc[:140]}"
            lines.append(line)
        more = f"\n(+{len(repos) - 20} more; refine with a query)" if len(repos) > 20 else ""
        return "Accessible repositories (use full_name as the `repo` argument):\n" + "\n".join(lines) + more

    def history(args: dict[str, Any]) -> str:
        repo = str(args.get("repo", "")).strip()
        if not repo:
            return "ERROR: 'repo' (owner/name) is required."
        ra = _account(repo)
        if ra is None or not ra.token:
            return _NO_ACCOUNT
        path = (args.get("path") or "").strip() or None
        try:
            limit = max(1, min(int(args.get("limit", 15)), 50))
        except (TypeError, ValueError):
            limit = 15
        try:
            commits = client.list_commits(ra.token, repo, path=path, per_page=limit)
            store.touch(ra.account.id, "ok")
        except GitHubError as exc:
            store.touch(ra.account.id, "error")
            return f"ERROR: {exc}"
        if not commits:
            return f"No commits found for {repo}" + (f" touching {path}." if path else ".")
        lines = []
        for c in commits:
            sha = (c.get("sha") or "")[:7]
            commit = c.get("commit", {})
            author = (commit.get("author") or {}).get("name", "?")
            when = (commit.get("author") or {}).get("date", "")[:10]
            msg = (commit.get("message") or "").splitlines()[0][:100]
            lines.append(f"- {sha}  {when}  {author}: {msg}")
        head = f"Recent commits for {repo}" + (f" touching {path}" if path else "") + ":"
        return head + "\n" + "\n".join(lines)

    def diff(args: dict[str, Any]) -> str:
        repo = str(args.get("repo", "")).strip()
        if not repo:
            return "ERROR: 'repo' (owner/name) is required."
        sha = (args.get("sha") or "").strip()
        base = (args.get("base") or "").strip()
        head = (args.get("head") or "").strip()
        if not sha and not (base and head):
            return "ERROR: provide either 'sha' (one commit) or both 'base' and 'head' (a range)."
        ra = _account(repo)
        if ra is None or not ra.token:
            return _NO_ACCOUNT
        try:
            data = client.get_commit(ra.token, repo, sha) if sha else client.compare(ra.token, repo, base, head)
            store.touch(ra.account.id, "ok")
        except GitHubError as exc:
            store.touch(ra.account.id, "error")
            return f"ERROR: {exc}"
        files = data.get("files", []) or []
        if not files:
            return "No file changes found."
        header = f"{sha[:7]} on {repo}" if sha else f"{base}...{head} on {repo}"
        if sha:
            msg = (data.get("commit", {}).get("message") or "").splitlines()[0]
            header += f" — {msg}"
        blocks = [header, ""]
        for f in files[:20]:
            blocks.append(f"### {f.get('filename')} (+{f.get('additions', 0)}/-{f.get('deletions', 0)})")
            if f.get("patch"):
                blocks.append("```diff\n" + _short(f["patch"], _PATCH_CHARS) + "\n```")
        if len(files) > 20:
            blocks.append(f"(+{len(files) - 20} more files)")
        return "\n".join(blocks)

    def read_file(args: dict[str, Any]) -> str:
        repo = str(args.get("repo", "")).strip()
        path = str(args.get("path", "")).strip()
        if not repo or not path:
            return "ERROR: both 'repo' (owner/name) and 'path' are required."
        ra = _account(repo)
        if ra is None or not ra.token:
            return _NO_ACCOUNT
        ref = (args.get("ref") or "").strip() or None
        try:
            content = client.get_file(ra.token, repo, path, ref=ref)
            store.touch(ra.account.id, "ok")
        except GitHubError as exc:
            store.touch(ra.account.id, "error")
            return f"ERROR: {exc}"
        return f"{repo}:{path}" + (f"@{ref}" if ref else "") + "\n```\n" + _short(content, _FILE_CHARS) + "\n```"

    def search(args: dict[str, Any]) -> str:
        query = str(args.get("query", "")).strip()
        if not query:
            return "ERROR: 'query' is required."
        repo = (args.get("repo") or "").strip() or None
        ra = _account(repo or "")
        if ra is None or not ra.token:
            return _NO_ACCOUNT
        try:
            items = client.search_code(ra.token, query, full_name=repo)
            store.touch(ra.account.id, "ok")
        except GitHubError as exc:
            store.touch(ra.account.id, "error")
            return f"ERROR: {exc}"
        if not items:
            return f"No code matched {query!r}" + (f" in {repo}." if repo else ".")
        lines = [f"- {it.get('repository', {}).get('full_name', '?')}: {it.get('path')}" for it in items]
        return f"Code matches for {query!r}:\n" + "\n".join(lines)

    common_repo = {"type": "string", "description": "Repository as owner/name (see repo_lookup)."}
    return [
        ToolSpec(
            name="repo_lookup", category="repos",
            description=(
                "List the GitHub repositories Devy can read (via the registered read-only "
                "PAT), optionally filtered by a query (name, description, language). Use the "
                "returned full_name (owner/name) as the `repo` argument to the other repo tools."
            ),
            when_to_use=(
                "At the start of any code investigation or when you need to know which repos "
                "are available — before reading history, diffs, files, or searching code."
            ),
            use_cases=[
                "what repos can you see", "list my github projects",
                "find the repo for the pricing service", "which repos are in python",
            ],
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "Optional filter (name/description/language)."}}},
            handler=lookup, safety_tier="read-only",
        ),
        ToolSpec(
            name="repo_history", category="repos",
            description=(
                "Recent commit history for a repo (optionally for a specific file/path) — "
                "SHA, date, author, and subject. Use it to see what changed and when."
            ),
            when_to_use=(
                "During triage/RCA to find what shipped recently — especially right before a "
                "symptom's onset. The dates let you place commits on the incident timeline."
            ),
            use_cases=[
                "what changed in the api repo recently", "recent commits to config/settings.py",
                "what was deployed before the incident", "who last changed this file",
            ],
            input_schema={"type": "object", "properties": {
                "repo": common_repo,
                "path": {"type": "string", "description": "Optional file/dir path to scope history to."},
                "limit": {"type": "integer", "description": "Commits to return (default 15, max 50)."},
            }, "required": ["repo"]},
            handler=history, safety_tier="read-only",
        ),
        ToolSpec(
            name="repo_diff", category="repos",
            description=(
                "Show the diff for a single commit (`sha`) or between two refs (`base`+`head`) "
                "— changed files with their patches. The concrete 'what changed'."
            ),
            when_to_use=(
                "After repo_history narrows to a suspect commit/deploy, to see exactly what "
                "changed — the heart of correlating a code change with an incident."
            ),
            use_cases=[
                "show the diff for commit abc123", "what changed between v1.2 and v1.3",
                "diff the release that preceded the outage",
            ],
            input_schema={"type": "object", "properties": {
                "repo": common_repo,
                "sha": {"type": "string", "description": "A commit SHA (one commit)."},
                "base": {"type": "string", "description": "Base ref for a range diff."},
                "head": {"type": "string", "description": "Head ref for a range diff."},
            }, "required": ["repo"]},
            handler=diff, safety_tier="read-only",
        ),
        ToolSpec(
            name="repo_read_file", category="repos",
            description=(
                "Read a file from a repo at a given ref (default branch if omitted). For "
                "inspecting code/config during an investigation."
            ),
            when_to_use=(
                "To look at the actual implementation or configuration relevant to an "
                "incident — e.g. how a setting is read, what a function does."
            ),
            use_cases=[
                "show me config/database.yaml in the api repo", "read the Dockerfile",
                "what does the retry logic in worker.py look like",
            ],
            input_schema={"type": "object", "properties": {
                "repo": common_repo,
                "path": {"type": "string", "description": "File path within the repo."},
                "ref": {"type": "string", "description": "Optional branch/tag/SHA (default: default branch)."},
            }, "required": ["repo", "path"]},
            handler=read_file, safety_tier="read-only",
        ),
        ToolSpec(
            name="repo_search_code", category="repos",
            description=(
                "Search code across accessible repos (or one repo) for a symbol, string, or "
                "config key — returns matching repo/path locations."
            ),
            when_to_use=(
                "To find where something lives across the codebase — a function, an error "
                "string, an environment variable, a config key — when you don't know the file."
            ),
            use_cases=[
                "where is DATABASE_URL read", "find the function handle_timeout",
                "which repo defines this error message", "where is the pricing port configured",
            ],
            input_schema={"type": "object", "properties": {
                "query": {"type": "string", "description": "Code search query (GitHub code-search syntax allowed)."},
                "repo": {"type": "string", "description": "Optional owner/name to scope the search to one repo."},
            }, "required": ["query"]},
            handler=search, safety_tier="read-only",
        ),
    ]
