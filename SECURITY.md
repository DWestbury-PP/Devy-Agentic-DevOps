# Security policy

## Reporting a vulnerability

**Please report security issues privately — do not open a public issue.**

Use GitHub's **private vulnerability reporting** for this repository
(*Security → Report a vulnerability*), which opens a confidential advisory with
the maintainer. Include:

- a description and the impact,
- steps to reproduce (a proof of concept if possible),
- affected version/commit and configuration.

You can expect an acknowledgement within a few days. We'll work with you on a fix
and coordinate disclosure; please give us reasonable time before any public
write-up.

## Supported versions

Devy is pre-1.0 and evolving. Security fixes target the latest `main`. Pin a
commit if you need stability, and watch releases for advisories.

## Security posture (what to know before deploying)

The full model is in **[docs/security.md](docs/security.md)**. The essentials:

- **The agent never gets a shell on your hosts.** All host/Docker inspection goes
  through the [host MCP](host-mcp/README.md): a declarative, profile-gated,
  read-only **allow-list** with bearer auth and an audit log.
- **In the compose stack, the proxy and web chat bind to host loopback** and are
  not exposed on the network. Front them with SSO / a reverse proxy for shared
  use; use TLS for any remote host MCP.
- **Identity is honor-system today.** The `X-User-Id` header scopes history/recall
  but is **not authentication** — treat the current build as single-tenant /
  trusted-network until you wire real auth into the documented seam. This is a
  known limitation, not a vulnerability.
- **Your data stays in your database** and is only sent to the model/embedding
  **providers you configure**. Conversation-memory storage has an off-switch
  (`knowledge.history_enabled: false`), and deleting a session removes its memory.

## Known, by-design limitations (not vulnerabilities)

- The honor-system `user_id` (above) — documented; real auth is on the roadmap.
- Mounting a third-party MCP server or enabling the host MCP's `elevated` profile
  expands what the agent can do. Review those like any dependency — that's an
  operator decision, by design.
