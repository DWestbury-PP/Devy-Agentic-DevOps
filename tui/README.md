# `ask` — the Agentic DevOps TUI

A thin, native terminal client for the LLM-PROXY, written in Go with the
[Charm](https://charm.sh) stack (Glamour for Markdown, Lipgloss for styling). It
shares no code with the proxy — it just speaks the proxy's HTTP/SSE API — so it
ships as a single self-contained binary with no runtime dependencies.

## Build

```bash
cd tui
go build -o ask .
```

Then put it on your `PATH`. A **symlink is the nicest for development** — it points
at the built file, so future `go build`s are picked up with no re-copy, and it's
branch-agnostic (`ask` is a gitignored artifact that survives branch switches):

```bash
sudo ln -sf "$PWD/ask" /usr/local/bin/ask     # recommended for dev
ln -sf "$PWD/ask" ~/go/bin/ask                # no sudo if ~/go/bin is on your PATH
```

For a fixed install, copy it instead (re-copy after each rebuild):

```bash
sudo cp ask /usr/local/bin/ask
```

Cross-compile (the binary is static; drop it on any matching host):

```bash
GOOS=linux  GOARCH=amd64 go build -o ask-linux-amd64 .
GOOS=linux  GOARCH=arm64 go build -o ask-linux-arm64 .
GOOS=darwin GOARCH=arm64 go build -o ask-darwin-arm64 .
```

## Use

```bash
ask "is anything unhealthy on this box?"      # one-shot, streamed
ask --complete "summarize disk usage"          # one-shot, non-streaming
kubectl get pods | ask "anything wrong here?"   # piped stdin as context
ask                                             # interactive REPL
```

### Multi-turn follow-ups

Conversations are server-side (the proxy persists them). Each one-shot prints its
`session:` id to stderr, and you resume it two ways:

```bash
ask "sweep the vitals"                          # → prints `session: <id>`
ask --continue "dig into the Jetsam kill"        # resume the last convo from this CLI
ask --session <id> "and the disk writes?"        # resume a specific convo by id (-s)
```

`--session <id>` is the source of truth — exact, scriptable, safe across parallel
threads and other surfaces (web, etc.). `--continue` is a local convenience: it
caches the last id **this CLI** saw in `~/.config/agentic-devops/last-session`, so
it only ever resumes your own last terminal conversation — never another surface's.
The interactive REPL is multi-turn by default (and honors `--continue`/`--session`
as its starting point).

Flags: `--tier/-t <fast|balanced|deep>`, `--complete/-c`, `--max-chars N`,
`--url <proxy>`. The proxy URL defaults to `http://127.0.0.1:8765` and can also
be set with `$AGENTIC_DEVOPS_URL`.

REPL commands: `/model <tier>`, `/models`, `/tools`, `/new`, `/help`,
`/exit`.

## Design

The client is intentionally dumb: it streams from / posts to the proxy and
renders. All reasoning, tools, model selection, and context live in the proxy
(see the repository [README](../README.md) and [docs/JOURNEY.md](../docs/JOURNEY.md)).
