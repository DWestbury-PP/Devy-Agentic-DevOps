"""Pydantic request/response models for the proxy API."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, Field


class ChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    tier: Optional[str] = None
    context: Optional[str] = None  # piped stdin / page context
    user_id: Optional[str] = None  # honor-system identity (X-User-Id header also accepted)


class CompleteRequest(BaseModel):
    prompt: str
    system: Optional[str] = None
    context: Optional[str] = None
    tier: Optional[str] = None
    max_chars: Optional[int] = None
    session_id: Optional[str] = None
    user_id: Optional[str] = None


class CompleteResponse(BaseModel):
    markdown: str
    tools_used: list[str] = Field(default_factory=list)
    usage: dict[str, Any] = Field(default_factory=dict)
    session_id: Optional[str] = None


class TierInfo(BaseModel):
    name: str
    label: str


class ToolInfo(BaseModel):
    name: str
    category: str
    when_to_use: str
    safety_tier: str


class SessionInfo(BaseModel):
    """A past conversation, for recall via GET /v1/sessions."""

    id: str
    user_id: Optional[str] = None
    title: Optional[str] = None
    updated_at: str
    turns: int
    preview: str


class SessionRename(BaseModel):
    title: str


class SessionDetail(BaseModel):
    """The faithful display transcript of one conversation (GET /v1/sessions/{id})."""

    id: str
    user_id: Optional[str] = None
    title: Optional[str] = None
    messages: list[dict[str, Any]] = Field(default_factory=list)


class AdminLogin(BaseModel):
    password: str


class AdminToken(BaseModel):
    token: str
    token_type: str = "bearer"
    expires_in: int


class HostBase(BaseModel):
    fqdn: str
    private_ip: Optional[str] = None
    public_ip: Optional[str] = None
    instance_id: Optional[str] = None
    aws_account: Optional[str] = None
    aws_region: Optional[str] = None
    mcp_port: int = 8780
    mcp_scheme: str = "https"
    address_preference: str = "private_ip"  # private_ip | public_ip | fqdn
    profile: Optional[str] = None
    active: bool = True
    labels: dict[str, Any] = Field(default_factory=dict)


class HostCreate(HostBase):
    token: Optional[str] = None  # per-host MCP bearer token (write-only; dev sets it, prod 403)
    secret_ref: Optional[str] = None  # override the manager name (prod out-of-band provisioning)


class HostUpdate(BaseModel):
    fqdn: Optional[str] = None
    private_ip: Optional[str] = None
    public_ip: Optional[str] = None
    instance_id: Optional[str] = None
    aws_account: Optional[str] = None
    aws_region: Optional[str] = None
    mcp_port: Optional[int] = None
    mcp_scheme: Optional[str] = None
    address_preference: Optional[str] = None
    profile: Optional[str] = None
    active: Optional[bool] = None
    labels: Optional[dict[str, Any]] = None
    token: Optional[str] = None  # only changed if the field is present in the request


class HostInfo(BaseModel):
    """A registered host — never includes the token (only ``has_token``)."""

    id: str
    fqdn: str
    private_ip: Optional[str] = None
    public_ip: Optional[str] = None
    instance_id: Optional[str] = None
    aws_account: Optional[str] = None
    aws_region: Optional[str] = None
    mcp_port: int
    mcp_scheme: str
    address_preference: str
    secret_ref: Optional[str] = None  # name of the token in the secrets manager (not the value)
    profile: Optional[str] = None
    active: bool
    labels: dict[str, Any] = Field(default_factory=dict)
    last_seen_at: Optional[str] = None
    last_status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    has_token: bool = False


# -- Document import (Phase 9c-2) -------------------------------------------

class DocumentInfo(BaseModel):
    """A registered document — never includes the raw content."""

    id: str
    corpus: str
    source_path: str
    title: str = ""
    doc_type: str = "doc"
    bytes: int = 0
    version: int = 1
    status: str = "pending"
    chunk_count: int = 0
    error: str = ""
    uploaded_by: str = ""
    job_id: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


class JobInfo(BaseModel):
    id: str
    corpus: str = ""
    status: str = "queued"
    total: int = 0
    done: int = 0
    error: str = ""
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# -- GitHub connector (Phase D-1) -------------------------------------------
class GitHubAccountCreate(BaseModel):
    label: str
    login: Optional[str] = None
    default_corpus: Optional[str] = None
    active: bool = True
    labels: dict[str, Any] = Field(default_factory=dict)
    token: Optional[str] = None  # read-only PAT (write-only; dev sets it, prod 403)
    secret_ref: Optional[str] = None  # override the manager name (prod out-of-band provisioning)


class GitHubAccountUpdate(BaseModel):
    label: Optional[str] = None
    login: Optional[str] = None
    default_corpus: Optional[str] = None
    active: Optional[bool] = None
    labels: Optional[dict[str, Any]] = None
    token: Optional[str] = None  # only changed if present in the request


class GitHubAccountInfo(BaseModel):
    """A registered GitHub account — never includes the token (only ``has_token``)."""

    id: str
    label: str
    login: Optional[str] = None
    secret_ref: Optional[str] = None  # name of the PAT in the secrets manager (not the value)
    default_corpus: Optional[str] = None
    active: bool
    labels: dict[str, Any] = Field(default_factory=dict)
    last_used_at: Optional[str] = None
    last_status: Optional[str] = None
    created_at: Optional[str] = None
    updated_at: Optional[str] = None
    has_token: bool = False


class RepoCrawlRequest(BaseModel):
    repo: str  # owner/name
    corpus: Optional[str] = None
    account: Optional[str] = None  # account id/label/login (optional when only one)


class RepoCrawlResult(BaseModel):
    corpus: str
    files_ingested: int
    files_skipped: int
    files_quarantined: int
    chunks_written: int
    secrets_redacted: int
    commit_sha: Optional[str] = None
    default_branch: Optional[str] = None


class RepoCrawlInfo(BaseModel):
    full_name: str
    corpus: str
    account_id: Optional[str] = None
    commit_sha: Optional[str] = None
    default_branch: Optional[str] = None
    files_ingested: int = 0
    chunks_written: int = 0
    files_quarantined: int = 0
    secrets_redacted: int = 0
    crawled_at: Optional[str] = None
    # Live KB footprint for the corpus (current totals, not last-run deltas).
    doc_count: int = 0
    chunk_count: int = 0


class DocgenTriggerRequest(BaseModel):
    repo: str  # owner/name
    components: Optional[list[str]] = None  # limit to these component paths
    brief: Optional[str] = None             # scan-brief guidance (stored + fed to generator)
    force: bool = False                     # regenerate even if unchanged


class DocgenComponentInfo(BaseModel):
    component_path: str
    component_name: str
    kind: str
    status: str
    arch_doc_path: Optional[str] = None
    last_doc_sha: Optional[str] = None


class DocgenInfo(BaseModel):
    full_name: str
    last_doc_sha: Optional[str] = None
    default_branch: Optional[str] = None
    scan_brief: str = ""
    components_doced: int = 0
    status: str = "idle"
    last_run_at: Optional[str] = None
    error: str = ""
    components: list[DocgenComponentInfo] = Field(default_factory=list)


class DocgenBrief(BaseModel):
    repo: str
    brief: str = ""


class UploadResult(BaseModel):
    job: JobInfo
    documents: list[DocumentInfo] = Field(default_factory=list)


# -- MCP Servers registry (Phase S-4) ---------------------------------------
class MCPServerCreate(BaseModel):
    name: str                              # unique; becomes the tool prefix + category
    url: str                               # streamable-HTTP MCP endpoint
    category: Optional[str] = None
    description: Optional[str] = None
    allow_writes: bool = False             # opt-in to register mutating tools
    enabled: bool = True
    secret_ref: Optional[str] = None       # override the manager name for the bearer token


class MCPServerUpdate(BaseModel):
    url: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    allow_writes: Optional[bool] = None
    enabled: Optional[bool] = None


class MCPServerInfo(BaseModel):
    id: str
    name: str
    url: str
    secret_ref: Optional[str] = None
    category: Optional[str] = None
    description: Optional[str] = None
    allow_writes: bool = False
    enabled: bool = True
    last_status: Optional[str] = None
    tool_count: int = 0
    write_tool_count: int = 0
    has_token: bool = False
    created_at: Optional[str] = None
    updated_at: Optional[str] = None


# -- Secrets / Connections (Phase S-2) --------------------------------------
class SecretEntry(BaseModel):
    """One credential in the inventory — never the value, only loaded-state."""

    service: str
    label: str
    ref: str
    category: str            # provider | github | host
    env: Optional[str] = None
    loaded: bool = False
    editable: bool = False   # settable from the Secrets tab (provider keys, dev only)
    testable: bool = True


class SecretCatalog(BaseModel):
    mode: str                # dev | prod
    writable: bool
    reachable: bool
    secrets: list[SecretEntry] = Field(default_factory=list)


class SecretSet(BaseModel):
    ref: str
    value: str


class SecretRefBody(BaseModel):
    ref: str


class SecretTestResult(BaseModel):
    ok: bool
    detail: str = ""


class CorpusInfo(BaseModel):
    name: str
    documents: int = 0
    chunks: int = 0


class HealthInfo(BaseModel):
    status: str
    version: str
    default_tier: str
