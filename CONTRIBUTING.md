# Contributing to Devy

Thanks for your interest in **Devy / Agentic DevOps**. Contributions — issues,
docs, tools, fixes — are welcome. For substantial changes, please open an issue to
discuss the approach first.

## Ground rules

- **Design before code.** For anything non-trivial (a new subsystem, a new
  surface, a schema change), propose the design in an issue and surface the real
  trade-offs before building. The architecture is deliberately small and owned —
  keep it that way (see [docs/architecture.md](docs/architecture.md)).
- **Match the surrounding style.** Read the neighbouring code first; mirror its
  naming, comment density, and idioms.
- **Verify, don't assume.** Run both test suites and lint before opening a PR.
  Live-verify against a real provider when you have keys.
- **Never commit secrets.** `.env` is gitignored; keep keys out of `config.yaml`
  (use `${VAR}` expansion).

## Project layout

A quick map (full version in [docs/architecture.md](docs/architecture.md)):

```
src/agentic_devops/   the proxy (Python): proxy/ tools/ knowledge/ db/ cli/
host-mcp/             the deployable safe-allowlist host MCP (separate package)
web/  tui/            thin client surfaces (web chat; Go `ask` TUI)
corpora/              demo knowledge corpora
docs/                 architecture, extending, configuration, deployment, security, …
tests/  host-mcp/tests/   the two test suites
```

## Dev setup

The proxy is **Python 3.10+** (the dev venv uses 3.14). Always install via
`python -m pip`.

```bash
python -m pip install -e ".[dev]"

# Postgres + pgvector is REQUIRED (sessions, knowledge, memory). For tests,
# start a throwaway (the DB-backed suites skip with a hint if none is reachable):
docker run -d --name agentic-test-pg -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=agentic_test -p 5433:5432 pgvector/pgvector:pg16

python -m pytest -q                 # proxy suite (point AGENTIC_TEST_DATABASE_URL if not :5433)
python -m pytest -q host-mcp/tests  # host-MCP suite
ruff check src/ tests/              # lint
```

The Go `ask` TUI (Go 1.26+):

```bash
cd tui && go build -o ask . && go vet ./...
```

## Tests

- **Pure-logic** tests (chunking, router, harness, timeline, memory compaction,
  …) need no database.
- **DB-backed** tests (store, sessions, history, service) use a live pgvector
  instance via `AGENTIC_TEST_DATABASE_URL` (default `:5433`); they `skip` with a
  hint if none is reachable, so the suite stays runnable anywhere.
- Add tests for new behaviour. Prefer a fake provider/embedder (see
  `tests/test_harness.py`, `tests/test_history.py`) over network calls.

## Adding a tool, an MCP integration, or docs

- **Tools / MCP / surfaces:** see [docs/extending.md](docs/extending.md) — it
  shows the `ToolSpec` shape, the `wants_context` seam, and how to mount MCP
  servers. Good discovery metadata (`when_to_use`, `use_cases`) matters.
- **Docs:** component docs live next to the code (`host-mcp/`, `web/`, `tui/`,
  `corpora/` READMEs); cross-cutting docs live in `docs/`. Keep examples runnable
  and link them from the README index.

## Pull requests

- Branch off `main` (the project has used `feat/…`, `docs/…`, `chore/…` prefixes).
- Keep PRs focused; describe what you changed and how you verified it.
- Ensure both suites pass and `ruff` is clean.
- End commit messages with a trailer crediting any AI assistance you used.

## License

By contributing you agree your contributions are licensed under the project's
[Apache License 2.0](LICENSE).
