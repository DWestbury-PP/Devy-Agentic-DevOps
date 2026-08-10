# The host MCP

The **host MCP** is how Devy gets safe "eyes and hands" on a real host. It's a
small, separately-deployable server ([`host-mcp/`](../host-mcp/README.md)) that
exposes a **declarative, profile-gated allow-list** of host + Docker diagnostics as
MCP tools — **no shell, no arbitrary execution**. The proxy mounts it and the agent
calls its checks through the tools-router like any other tool.

This page covers what it can do, how it stays safe, and — the part that matters most
in production — **how it's deployed**.

## Safety model (why it's adoptable)

- **Declarative allow-list.** Each check is a fixed `argv` (or per-OS `argv`) in
  YAML. An argument can only fill a whole `{placeholder}` token after passing its
  type/pattern/enum/range constraints. There is **no shell** and no way to inject one.
- **Profiles.** `read-only` < `diagnostic` < `elevated`. The server runs at one
  active profile and exposes only the checks at or below it.
- **Immutability is the default.** State-changing verbs are **off** unless the
  sidecar is started with an explicit switch (`--allow-mutations` / the
  `HOST_MCP_ALLOW_MUTATIONS` env). That switch is read **only at startup** — there is
  no runtime toggle — so "enhanced mode" is a deliberate, restart-bounded operator act
  that self-reverts on the next normal restart. Only **reversible Tier-A** verbs exist
  even then (restart/reload/prune — never stop, rm, or path/volume deletion).
- **Auth.** Over HTTP a **bearer token** is required; front it with TLS or a
  network boundary (a security group). In the Devy deployment the agent also can't
  self-approve a mutation — it *proposes*, a human *approves*, the proxy executes.

See [Security](security.md) for the full posture.

## What it can check (authored per-OS)

The allow-list is **authored per operating system** — each host advertises only the
checks native to it, authored to that OS's strengths rather than a
lowest-common-denominator subset. A check with no variant for the detected OS simply
isn't advertised there.

- **Linux** (Amazon Linux 2023 / RHEL / Fedora / Ubuntu — all systemd + journald):
  `journal_query` (one rich, indexed query — absolute `since`/`until` window +
  severity + unit + grep), `journal_kernel`, `journal_boot`, `failed_units`,
  `services`/`service_status`, `time_sync` (clock/TZ/NTP), `boot_time`, `disk_io`,
  `hardware_info` (`lscpu`), `os_info` (`hostnamectl`), reachability
  (`ping_host`/`dns_lookup`/`http_check`/`dns_config`).
- **macOS** (dev hosts): the unified log via `log_query` (predicate + severity +
  absolute bounds), `pmset` power/thermal (`thermal_status`/`power_settings`),
  `boot_time`, `panic_reports`, `brew_services`, reachability, `hardware_info`.
- **Docker** (any OS, via a mounted socket): `docker_ps`, `docker_logs`,
  `docker_inspect`, `docker_stats`, `docker_top`, `docker_images`, `docker_system_df`.

Full surface + argv: [`host-mcp/README.md`](../host-mcp/README.md).

## Deployment shapes

The **same package** auto-detects its OS; only the *deployment artifact* differs.
There are three shapes:

| Shape | Where | What it sees | How |
|---|---|---|---|
| **Native systemd unit** | Linux prod/edge hosts (AWS) | the **real host** | `host-mcp-deploy.yml` → a hardened, unprivileged unit |
| **Native launchd agent** | a macOS dev host | the **real Mac** | `host-mcp/deploy/` LaunchAgent |
| **Container** | the local demo only | *its own container* namespace (+ Docker socket) | `docker-compose-local.yml` |

The critical rule: **a containerized host MCP only sees its container's namespace**,
not the host it runs on. So for real host inspection it runs **natively**. The
container shape survives only as a zero-setup local demo (its Docker checks are real
via the mounted socket; its host checks reflect the container).

### Native on Linux (the AWS production shape)

On AWS the host MCP runs as a **hardened, unprivileged systemd service** on every
host — deployed by the [`host-mcp-deploy`](../.github/workflows/host-mcp-deploy.yml)
workflow (or [`install-native-linux.sh`](../host-mcp/deploy/install-native-linux.sh)
by hand). It runs as a dedicated `devy-hostmcp` user (in the `systemd-journal` and
`docker` groups — the *entire* privilege surface, **not root**), sandboxed by the
unit (`ProtectSystem=strict`, `NoNewPrivileges`, empty `CapabilityBoundingSet`,
`SystemCallFilter=@system-service`). See
[`agentic-devops-host-mcp.service.example`](../host-mcp/deploy/agentic-devops-host-mcp.service.example).

**AWS topology — native everywhere:**

```
                 ┌─────────────── devy-platform (EC2) ───────────────┐
 web / ask ──▶ proxy (container) ──host.docker.internal:8781──▶ host MCP (native systemd)
                     │                                          └─ sees the real EC2 host
                     │  http://<edge-ip>:8781  (SG: platform→edge only, + bearer)
                     ├──────────────▶ edge-al2023  host MCP (native systemd)
                     └──────────────▶ edge-ubuntu  host MCP (native systemd)
```

- The **co-located** sidecar on the platform is reached over the Docker host gateway
  (`host.docker.internal:8781`); the proxy compose service declares
  `extra_hosts: ["host.docker.internal:host-gateway"]`.
- **Edge** sidecars are reached across the private subnet. The edge security group
  allows inbound `:8781` **only from the platform's security group**, and the bearer
  token is the authn — defense in depth: allow-list (no shell) → unprivileged user →
  systemd sandbox → bearer → SG.
- All three carry the **same** vaulted bearer (`devy/mcp/host`).

### Mounting host MCPs in the proxy

The proxy mounts host MCPs via `config.yaml` `mcp_servers` (see
[Configuration](configuration.md#mcp-servers)). On AWS
([`deploy/config/config.aws.yaml`](../deploy/config/config.aws.yaml)):

```yaml
mcp_servers:
  - name: host                     # the co-located native sidecar
    transport: http
    url: http://host.docker.internal:8781/mcp
    secret_ref: devy/mcp/host       # vaulted bearer
  - name: edge_al2023               # a remote edge host (native sidecar)
    transport: http
    url: ${EDGE_AL2023_MCP_URL}      # account-specific → an Actions var, not source
    secret_ref: devy/mcp/host
  - name: edge_ubuntu
    transport: http
    url: ${EDGE_UBUNTU_MCP_URL}
    secret_ref: devy/mcp/host
```

Each mount's `name` prefixes its tools — so the agent sees `host_journal_query`,
`edge_al2023_failed_units`, `edge_ubuntu_disk`, and so on, and can target a specific
host. Mounts are **best-effort**: a stopped edge is an "unreachable" mount that never
blocks the proxy's startup and lights up when the host comes back.

> Edge URLs are private IPs → they live in **Actions variables**
> (`EDGE_AL2023_MCP_URL`, `EDGE_UBUNTU_MCP_URL`), rendered into the host `.env` by the
> deploy role — never hardcoded in the (public) config.

## Deploying / redeploying

```bash
# Deploy (or reconcile) the native sidecar to a host set. plan first, then apply.
gh workflow run host-mcp-deploy.yml -f hosts=role_platform -f ref=main -f mode=plan
gh workflow run host-mcp-deploy.yml -f hosts=edge-al2023   -f ref=main -f mode=apply
```

- `hosts`: `role_platform` · `role_edge` · a single host · `all`. The checked-out
  `ref` **is** the SHA pin — the role ships that source and pip-installs it (no GitHub
  egress needed on the target).
- **Stopped hosts are skipped**, not started: the inventory is running-only, so CI
  never starts a billable box. Start edge hosts by hand for a dev deploy, and stop
  them after.
- **Enhanced mode** (mutations) is opt-in per run (`-f allow_mutations=true`) and
  still needs a scoped OS privilege grant for the underlying `systemctl` verbs.

Deploying the sidecar is separate from the app-tier release
([`deploy.yml`](../.github/workflows/deploy.yml)) — different artifact (a systemd
service, not an image), different cadence. A change to the allow-list → re-run
`host-mcp-deploy`; a change to the proxy → run `build` + `deploy`.

## Extending it

Add a check by editing the allow-list YAML (a fixed `argv` with typed placeholders) —
see [Extending → the host MCP](extending.md#the-host-mcp) and
[`host-mcp/allowlist.example.yaml`](../host-mcp/allowlist.example.yaml). Keep it
allow-listed: that boundary is the whole point.
