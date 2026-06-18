"""Command-line entry points for the proxy.

`agentic-devops serve` runs the LLM-PROXY service (also the container
entrypoint). The user-facing `ask` TUI is a separate Go/Charm binary (see
`tui/`) that talks to the proxy over HTTP/SSE.
"""
