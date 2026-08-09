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

The allow-list is **authored per-OS** — each host advertises only the checks
native to its operating system (see [One server, per-OS surfaces](#one-server-per-os-surfaces)).
At the default `diagnostic` profile it exposes **host** and **Docker**
diagnostics — no shell, no mutations:

- **Resource:** `disk` (usage + inodes on macOS), `memory` (pressure / free),
  `cpu_load`, `processes` (ranked by CPU), `top_snapshot` (load + memory totals,
  per-process state), `disk_io` (I/O throughput + iowait).
- **Network:** `network` (listening sockets), `connections` (sockets **with the
  owning process**), and reachability — `ping_host`, `dns_lookup` (the host's
  *real* resolver/cache), `dns_config`, `http_check` (endpoint status + DNS / TLS /
  total latency).
- **Logs:** on Linux, the systemd journal via **`journal_query`** (one rich,
  indexed query — an absolute `since`/`until` window + severity floor + unit +
  grep, composable in one call), plus `journal_kernel` (dmesg-style) and
  `journal_boot` (a specific boot); on macOS the unified-log store via
  **`log_query`** (predicate + severity + relative window or absolute start/end).
  `tail_file` reads a service's own redirected error log —
  but only one that resolves inside an operator-declared allow-list of log
  directories (default `/var/log`, `/opt/homebrew/var/log`, `/usr/local/var/log`,
  `/Library/Logs`). It exists for the case the journal/unified log can't cover: a
  launchd/Homebrew service on macOS whose stderr goes to a file (its
  `StandardErrorPath`, reported by `service_status`), where the reason a service
  crash-loops actually lives. The path is `realpath`-resolved and rejected unless
  it lands within an allowed root, so `..` traversal and symlink escapes can't reach
  anything else.
- **Boot / time / power / crash:** `reboot_history` (restart history), `boot_time`
  (exact boot = the *recovery* instant), `time_sync` (clock / timezone / NTP-sync —
  Linux; the local-vs-UTC offset to read before correlating with dashboards); macOS
  adds `panic_reports`, `thermal_status`, `power_settings`.
- **Services / daemons:** `services` (the manager's inventory + state),
  `service_status` (one named service — is it up, and why did it last exit?),
  `failed_units` (the failed set right now — Linux), cross-OS via systemd on Linux
  and launchd on macOS; plus `brew_services` (Homebrew-managed services, macOS).
- **Hardware:** `hardware_info` (CPU / cores / arch via `lscpu` on Linux;
  model / chip / RAM / serial on macOS).
- **Docker** (needs access to the Docker socket): `docker_ps`, `docker_ps_all`,
  `docker_logs`, `docker_inspect`, `docker_stats`, `docker_top`, `docker_images`,
  `docker_system_df`.

Deliberately **absent**: anything that mutates or grants a shell —
`docker exec/run/rm/stop`, an *arbitrary* `cat`/`tail` of any path, `dmesg`. File
reads are confined to `tail_file`'s allow-listed log roots (above); the boundary
is the allow-list itself, not the socket's mount mode.

## One server, per-OS surfaces

The host MCP is **a single package that auto-detects its host OS**
(`platform.system()`) and advertises **only the checks native to that OS** —
deploy the same package on Linux or macOS with **no OS setting to configure**.
Rather than force one lowest-common-denominator command set across every OS, each
check is authored to its platform's strengths. A check with no variant for the
detected OS is simply **not advertised** there (and, if invoked by name, reports
*"not supported on `<OS>`"* cleanly rather than running the wrong command). The
`HOST_MCP_*` env vars configure the *deployment* (profile, auth, transport) —
never the OS.

Genuinely portable checks share one command; the rest diverge per-OS:

| Check | Linux | macOS |
|---|---|---|
| `disk` / `cpu_load` | `df -h` / `uptime` | `df -h` (+inodes) / `uptime` |
| `memory` | `free -h` | `memory_pressure -Q` |
| `os_info` | `hostnamectl` | `sw_vers` |
| `network` | `ss -tuln` | `netstat -an -p tcp` |
| `connections` | `ss -tunap` | `lsof -nP -iTCP` |
| `processes` / `top_snapshot` | `ps` / `top -bn1` | `ps` / `top -l 1` |
| `reboot_history` | `last -n N reboot` | `last -n N reboot` |
| `services` / `service_status` | `systemctl …` | `launchctl …` |
| `restart_service` (gated) | `systemctl restart` | `brew services restart` |

**Linux-native** (systemd / journald) — indexed, server-side log queries, the
surgical way to pull an incident slice on a production host without dumping and
scanning: `journal_query` (one rich primitive — an absolute `since`/`until`
window, severity floor, unit, and grep, composable in one call), `journal_kernel`
(dmesg-style), `journal_boot` (a specific boot's log — default the previous one),
`failed_units` (the failed set right now), `time_sync` (clock / timezone / NTP),
and the gated `reload_config`.

**macOS-native** (unified log, `pmset`, launchd, BSD tools): `log_query`,
`panic_reports`, `thermal_status`, `power_settings`, `brew_services`.

> Authored independently on purpose: forcing one cross-OS command set had quietly
> amputated each OS's best diagnostics — macOS's unified-log severity/boundary
> power and `pmset` telemetry; Linux's journald filters and systemd/`hostnamectl`
> reach. Each OS now gets its native depth. *(Both surfaces are now authored;
> genuinely portable primitives — `df`, `uptime`, `last`, reachability via `curl`,
> the `docker_*` family — stay shared.)*

### macOS unified log — `log_query`

macOS's unified log is richer than journald: one predicate covers process,
subsystem, message text, **and** severity, over a relative window **or** an
absolute interval. It's the authoritative source for shutdown cause, power/sleep/
wake, kernel, and severity-filtered events, with far longer retention than
`/var/log/system.log` (which is effectively empty on modern macOS):

| Arg | Meaning |
|---|---|
| `predicate` | An NSPredicate — filter by `process`, `subsystem` (e.g. `com.apple.powerd`), `eventMessage`, or **severity** via `messageType` (16 = error, 17 = fault). One argument, never a shell. |
| `window` | Relative look-back, default `1h` (e.g. `30m`, `2h`, `3d`). |
| `start` / `end` | Absolute interval, `YYYY-MM-DD HH:MM:SS` — **supersedes** `window`. Targets a specific past slice, e.g. the minutes before a boot. |

Because it can scan a large store it carries a 90s timeout — prefer `start`/`end`
(or a narrow window) to keep queries cheap. Example onset-vs-recovery chain for an
outage: `boot_time` gives the *recovery* instant; `log_query --end <boot>` finds
the last activity *before* it (the failure **onset** — a distinct timestamp);
`panic_reports` + a bounded `Previous shutdown cause` / `messageType` query confirm
whether it was a crash, a thermal event, or a clean power cut.

## Safety model

- **Declarative allow-list** (YAML): each check is a fixed `argv` (or per-OS
  `argv`). Arguments fill a whole `{placeholder}` token, or — for an optional
  flag-arg — append a constrained `--flag value` pair (e.g. `log_query`'s
  `--start`/`--end`); every value must pass its type/pattern/enum/range constraint
  first. No shell is ever invoked.
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

The repo's [`docker-compose-local.yml`](../docker-compose-local.yml) runs `host-mcp` as a
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
