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
  sockets), `connections` (sockets **with the owning process**), `processes`,
  `top_snapshot`, and log sweeps `journal` / `journal_grep` (the systemd journal
  on Linux, the unified `log show` on macOS) plus `journal_unit`
  (systemd-specific).
- **Services / daemons:** `services` (the manager's inventory + state),
  `service_status` (one named service — is it up, and why did it last exit?),
  cross-OS via systemd on Linux and launchd on macOS; plus `brew_services`
  (Homebrew-managed services, macOS-only).
- **Scoped log-file read:** `tail_file` reads the trailing lines of a log file —
  but only one that resolves inside an operator-declared allow-list of log
  directories (default `/var/log`, `/opt/homebrew/var/log`,
  `/usr/local/var/log`, `/Library/Logs`). It exists for the case the journal/unified
  log can't cover: a launchd/Homebrew service on macOS whose own stderr is
  redirected to a file (its `StandardErrorPath`, reported by `service_status`) —
  where the reason a service crash-loops actually lives. The path is
  `realpath`-resolved and rejected unless it lands within an allowed root, so `..`
  traversal and symlink escapes can't reach anything else.
- **Docker** (needs access to the Docker socket): `docker_ps`, `docker_ps_all`,
  `docker_logs`, `docker_inspect`, `docker_stats`, `docker_top`, `docker_images`,
  `docker_system_df`.

Deliberately **absent**: anything that mutates or grants a shell —
`docker exec/run/rm/stop`, an *arbitrary* `cat`/`tail` of any path, `dmesg`. File
reads are confined to `tail_file`'s allow-listed log roots (above); the boundary
is the allow-list itself, not the socket's mount mode.

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
| `connections` | `ss -tunap` | `lsof -nP -iTCP` |
| `processes` / `top_snapshot` | `ps` / `top -bn1` | `ps` / `top -l 1` |
| `journal` | `journalctl` | `log show` (unified log) |
| `journal_grep` | `journalctl --grep` | `grep … /var/log/system.log` |
| `reboot_history` | `last -n N reboot` | `last -n N reboot` |
| `services` | `systemctl list-units --type=service` | `launchctl list` |
| `service_status` | `systemctl status <unit>` | `launchctl list <label>` |

A check with no variant for the detected OS reports *"not supported on `<OS>`"*
cleanly rather than running the wrong command — this covers both the
systemd-specific checks on macOS (`journal_unit`, `systemctl_status`,
`journal_priority`, `journal_kernel`, `journal_boot`) **and** the macOS-specific
checks on Linux (`log_query`, `panic_reports`, `brew_services`, below). The `HOST_MCP_*` env vars
configure the *deployment* (profile, auth, transport) — never the OS.

**Linux journald filters (indexed, server-side).** On a production host, pull the
incident slice *at the source* instead of dumping everything and scanning — these
are cheap and surgical (Linux/systemd only):

| Check | Command | Use |
|---|---|---|
| `journal_priority` | `journalctl -p <sev> -n N` | Errors-and-worse only (or any severity floor) — indexed by journald. |
| `journal_kernel` | `journalctl -k -n N` | Kernel messages only (OOM kills, hardware, watchdog) — like dmesg. |
| `journal_boot` | `journalctl -b <off> -p <sev> -n N` | A specific boot's log (default the *previous* boot) — what happened before a reboot/crash. |

**macOS deep diagnostics.** `journal_grep` on macOS only sees `/var/log/system.log`
(short retention). For historical / richer queries — the authoritative source for
shutdown cause, power, sleep/wake, and kernel events — two macOS-only checks tap
the unified log binary store and crash reports:

| Check | Command | Use |
|---|---|---|
| `log_query` | `log show --last <window> --predicate <NSPredicate> --style compact` | Query the unified log over a time window (e.g. `eventMessage CONTAINS[c] "shutdown"`). Longer retention than `system.log`; 90s timeout since wide windows are slow. |
| `panic_reports` | `ls -lt /Library/Logs/DiagnosticReports` | List kernel-panic / crash reports newest-first — a `*.panic`/`*.ips` near a reboot signals an unclean shutdown. |

## Safety model

- **Declarative allow-list** (YAML): each check is a fixed `argv` (or per-OS
  `argv`). Arguments can only fill a whole `{placeholder}` token, after passing
  type/pattern/enum/range constraints. No shell is ever invoked.
- **Profiles** — `read-only` < `diagnostic` < `elevated`. The server runs at one
  active profile and exposes only the checks at or below it.
- **Audit log** — set `HOST_MCP_AUDIT=<path>` and every invocation (check, args,
  argv, exit code, `duration_ms`) is appended as JSONL. Recommended in production:
  it's the cheap observability that answers *"which diagnostic just cost 25s?"*
  (`jq '{check,duration_ms}' <path>`). The bundled compose service and the
  `deploy/` kit enable it by default.

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

#### macOS dev host + containerized proxy

macOS can't run Linux containers natively, so a *containerized* host-mcp only ever
sees a Linux VM — its host checks (and `log show`) can't reach the real Mac. Run
the host MCP **natively on the Mac** instead, and have the containerized proxy dial
it over the Docker gateway:

```bash
# 1. run the native sidecar (Darwin process → real `log show` / `last reboot`)
HOST_MCP_TRANSPORT=http HOST_MCP_PORT=8781 HOST_MCP_TOKEN=… agentic-devops-host-mcp
# 2. point the proxy's mcp_servers url at the host gateway
#    url: http://host.docker.internal:8781/mcp
```

For a durable sidecar that starts at login, use the launchd LaunchAgent in
[`deploy/`](deploy/) — it runs [`run-native-macos.sh`](deploy/run-native-macos.sh)
(which reads `HOST_MCP_TOKEN` from the repo `.env`, enables the audit, and adds
Docker Desktop's CLI to `PATH` so the `docker_*` checks work — a launchd agent's
minimal `PATH` omits `/usr/local/bin` otherwise) and keeps it alive:

```bash
sed "s#__REPO__#$PWD#g" host-mcp/deploy/com.agentic-devops.host-mcp.plist.example \
  > ~/Library/LaunchAgents/com.agentic-devops.host-mcp.plist
launchctl load ~/Library/LaunchAgents/com.agentic-devops.host-mcp.plist
```

> When a host MCP is mounted, the proxy's own `host_diagnostics` builtin re-scopes
> to the container (as `proxy_self_diagnostics`) so the mounted `host_*` tools are
> the single, unambiguous host surface.
