# `ask` — the Agentic DevOps TUI

A thin, native terminal client for the LLM-PROXY, written in Go with the
[Charm](https://charm.sh) stack (Glamour for Markdown, Lipgloss for styling). It
shares no code with the proxy — it just speaks the proxy's HTTP/SSE API — so it
ships as a single self-contained binary with no runtime dependencies.

## Build

```bash
cd tui
go build -o ask .
sudo mv ask /usr/local/bin/      # or anywhere on your PATH
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

Flags: `--tier/-t <fast|balanced|deep>`, `--complete/-c`, `--max-chars N`,
`--url <proxy>`. The proxy URL defaults to `http://127.0.0.1:8765` and can also
be set with `$AGENTIC_DEVOPS_URL`.

REPL commands: `/model <tier>`, `/models`, `/tools`, `/new`, `/help`,
`/exit`.

## Design

The client is intentionally dumb: it streams from / posts to the proxy and
renders. All reasoning, tools, model selection, and context live in the proxy
(see the repository [README](../README.md) and [docs/JOURNEY.md](../docs/JOURNEY.md)).
