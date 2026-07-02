# Agentic DevOps — Host MCP

A small, **safe-by-design** MCP server you deploy onto a target host. It exposes
a fixed, **profile-gated** set of diagnostic commands as MCP tools — no shell, no
arbitrary execution. The [LLM-PROXY](../README.md) mounts it (stdio locally,
authenticated streamable-HTTP remotely) and the agent calls its tools through
the tools-router.

This is the production answer to "inspect a *remote* host": the proxy never gets
shell access — it can only invoke allow-listed checks, and only those permitted
by the host's active profile. That posture is what makes it adoptable.

## What it can check

At the default `diagnostic` profile, the packaged allow-list exposes **host** and
**Docker** diagnostics (all read-only, no shell):

- **Host:** `disk`, `memory`, `cpu_load`, `os_info`, `network` (listening
  sockets), `processes`, `top_snapshot`, and log sweeps `journal` / `journal_grep`
  (the systemd journal on Linux, the unified `log show` on macOS) plus
  `journal_unit` (systemd-specific).
- **Docker** (needs access to the Docker socket): `docker_ps`, `docker_ps_all`,
  `docker_logs`, `docker_inspect`, `docker_stats`, `docker_top`, `docker_images`,
  `docker_system_df`.

Deliberately **absent**: anything that mutates or grants a shell —
`docker exec/run/rm/stop`, arbitrary `cat`/`tail`, `dmesg`. The boundary is the
allow-list itself, not the socket's mount mode.

## One server, many operating systems

The host MCP is **a single server that auto-detects its host OS** — deploy the
same package on Linux or macOS and it adapts, with **no OS setting to configure**.
Each check is either a portable command or a per-OS `argv` map, resolved at
runtime from the host's reported OS (`platform.system()` — `Linux`, `Darwin`, …).
For example:

| Check | Linux | macOS |
|---|---|---|
| `memory` | `free -h` | `vm_stat` |
| `os_info` | `uname -a` | `sw_vers` |
| `network` | `ss -tuln` | `netstat -an -p tcp` |
| `processes` / `top_snapshot` | `ps` / `top -bn1` | `ps` / `top -l 1` |
| `journal` | `journalctl` | `log show` (unified log) |
| `journal_grep` | `journalctl --grep` | `grep … /var/log/system.log` |

A check with no variant for the detected OS (e.g. `journal_unit` and
`systemctl_status`, which are systemd-specific) reports *"not supported on
`<OS>`"* cleanly rather than running the wrong command. The `HOST_MCP_*` env vars
configure the *deployment* (profile, auth, transport) — never the OS.

## Safety model

- **Declarative allow-list** (YAML): each check is a fixed `argv` (or per-OS
  `argv`). Arguments can only fill a whole `{placeholder}` token, after passing
  type/pattern/enum/range constraints. No shell is ever invoked.
- **Profiles** — `read-only` < `diagnostic` < `elevated`. The server runs at one
  active profile and exposes only the checks at or below it.
- **Audit log** — every invocation (check, args, argv, exit, duration) can be
  appended to a JSONL file.

## Run

```bash
pip install -e .                 # stdio only
pip install -e '.[http]'         # + streamable-HTTP transport

# stdio (the proxy spawns it):
agentic-devops-host-mcp

# remote, over authenticated HTTP:
HOST_MCP_TRANSPORT=http HOST_MCP_PORT=8780 HOST_MCP_TOKEN=secret \
  agentic-devops-host-mcp
```

### Configuration (env)

| Variable | Meaning |
|---|---|
| `HOST_MCP_ALLOWLIST` | path to an allow-list YAML (default: packaged `default_allowlist.yaml`) |
| `HOST_MCP_PROFILE` | `read-only` \| `diagnostic` \| `elevated` (overrides the file) |
| `HOST_MCP_AUDIT` | path to a JSONL audit log |
| `HOST_MCP_TRANSPORT` | `stdio` (default) \| `http` |
| `HOST_MCP_HOST` / `HOST_MCP_PORT` | bind address for `http` (default `0.0.0.0:8780`) |
| `HOST_MCP_TOKEN` | bearer token required for `http` requests |

Copy [`allowlist.example.yaml`](allowlist.example.yaml) and adapt it to your
host. Front the HTTP transport with TLS in production (the bearer token is the
authn; the allow-list is the authz).

## Two ways to run it

### Containerized (the local demo)

The repo's [`docker-compose.yml`](../docker-compose.yml) runs `host-mcp` as a
service: the [`Dockerfile`](Dockerfile) installs only the **Docker CLI**
(`docker-ce-cli`, no engine) plus diag tools, and the host's
`/var/run/docker.sock` is mounted in. So the `docker_*` checks see the **host's
real containers**, served over authenticated HTTP on the compose network. The
proxy mounts it via `mcp_servers` (see [`config.example.yaml`](../config.example.yaml));
the shared `HOST_MCP_TOKEN` comes from a `.env` next to the compose file.

> **Caveat:** inside a container, the **host-level** checks (`disk`, `memory`,
> `processes`) reflect *the container*, not the host. The **Docker** checks are
> real (via the mounted socket). For true host-level inspection, deploy natively:

### Native (production target hosts)

Install on the host and run it there, where every check — host *and* Docker — is
real host-level. Give the running user Docker socket access (e.g. the `docker`
group) for the `docker_*` checks; no extra privilege is needed for the rest.

```bash
pipx install agentic-devops-host-mcp        # or pip install into a venv
HOST_MCP_TRANSPORT=http HOST_MCP_TOKEN=… agentic-devops-host-mcp
```
