"""Read-only GitHub REST client (Phase D-1).

Devy reads repos through a thin, **read-only** wrapper over the GitHub REST API —
the official GitHub MCP server exposes write tools, so per the connector rule
("mount only if genuinely read-only, else build native") we build native. Every
method is a GET; there is no code path that writes. The bearer token is resolved
from the encrypted ``github_accounts`` registry by the caller — the agent never
handles it.

The HTTP call goes through a ``request_fn`` seam (mirrors ``embeddings.embed_fn``
and ``providers.completion_fn``) so tests inject canned responses without a
network call.
"""

from __future__ import annotations

import base64
from typing import Any, Callable, Optional

_API = "https://api.github.com"
_ACCEPT = "application/vnd.github+json"
_API_VERSION = "2022-11-28"

# (method, url, headers, params) -> (status_code, parsed_json_or_text)
RequestFn = Callable[[str, str, dict, Optional[dict]], tuple[int, Any]]


class GitHubError(Exception):
    """A non-2xx GitHub response (or transport failure), surfaced to callers/tools."""


def _default_request_fn(method: str, url: str, headers: dict, params: Optional[dict]) -> tuple[int, Any]:
    import httpx

    resp = httpx.request(method, url, headers=headers, params=params, timeout=20.0)
    ctype = resp.headers.get("content-type", "")
    body: Any = resp.json() if "json" in ctype else resp.text
    return resp.status_code, body


class GitHubClient:
    """Read-only GitHub API access. One client, per-call token (multi-account safe)."""

    def __init__(self, request_fn: Optional[RequestFn] = None, base_url: str = _API) -> None:
        self._request = request_fn or _default_request_fn
        self._base = base_url.rstrip("/")

    # -- low-level ----------------------------------------------------------
    def _get(self, token: Optional[str], path: str, params: Optional[dict] = None) -> Any:
        headers = {"Accept": _ACCEPT, "X-GitHub-Api-Version": _API_VERSION}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        url = path if path.startswith("http") else f"{self._base}{path}"
        status, body = self._request("GET", url, headers, params)
        if status == 401:
            raise GitHubError("unauthorized (check the PAT and its scopes)")
        if status == 403:
            raise GitHubError("forbidden or rate-limited")
        if status == 404:
            raise GitHubError("not found (repo/path may be private or misspelled)")
        if status >= 400:
            msg = body.get("message") if isinstance(body, dict) else str(body)[:120]
            raise GitHubError(f"GitHub API error {status}: {msg}")
        return body

    # -- identity / discovery ----------------------------------------------
    def whoami(self, token: str) -> dict:
        """The authenticated user — used to verify a PAT and learn its login."""
        return self._get(token, "/user")

    def list_repos(self, token: str, *, limit: int = 200, per_page: int = 100) -> list[dict]:
        """Repos the PAT can see (owned, collaborator, org member), most-recent first."""
        out: list[dict] = []
        page = 1
        while len(out) < limit:
            batch = self._get(token, "/user/repos", {
                "affiliation": "owner,collaborator,organization_member",
                "sort": "pushed", "per_page": per_page, "page": page,
            })
            if not isinstance(batch, list) or not batch:
                break
            out.extend(batch)
            if len(batch) < per_page:
                break
            page += 1
        return out[:limit]

    def get_repo(self, token: str, full_name: str) -> dict:
        return self._get(token, f"/repos/{full_name}")

    # -- history / diffs ----------------------------------------------------
    def list_commits(self, token: str, full_name: str, *, path: Optional[str] = None,
                     per_page: int = 20) -> list[dict]:
        params: dict = {"per_page": per_page}
        if path:
            params["path"] = path
        return self._get(token, f"/repos/{full_name}/commits", params)

    def get_commit(self, token: str, full_name: str, sha: str) -> dict:
        """A commit with its changed files (each carries a ``patch`` diff)."""
        return self._get(token, f"/repos/{full_name}/commits/{sha}")

    def compare(self, token: str, full_name: str, base: str, head: str) -> dict:
        return self._get(token, f"/repos/{full_name}/compare/{base}...{head}")

    # -- contents -----------------------------------------------------------
    def get_file(self, token: str, full_name: str, path: str, ref: Optional[str] = None) -> str:
        params = {"ref": ref} if ref else None
        data = self._get(token, f"/repos/{full_name}/contents/{path}", params)
        if isinstance(data, dict) and data.get("encoding") == "base64":
            return base64.b64decode(data.get("content", "")).decode("utf-8", errors="replace")
        if isinstance(data, dict) and "content" in data:
            return str(data["content"])
        raise GitHubError(f"{path} is not a readable file")

    def get_tree(self, token: str, full_name: str, ref: str, *, recursive: bool = True) -> list[dict]:
        params = {"recursive": "1"} if recursive else None
        data = self._get(token, f"/repos/{full_name}/git/trees/{ref}", params)
        return data.get("tree", []) if isinstance(data, dict) else []

    # -- search -------------------------------------------------------------
    def search_code(self, token: str, query: str, *, full_name: Optional[str] = None,
                    per_page: int = 10) -> list[dict]:
        q = f"{query} repo:{full_name}" if full_name else query
        data = self._get(token, "/search/code", {"q": q, "per_page": per_page})
        return data.get("items", []) if isinstance(data, dict) else []
