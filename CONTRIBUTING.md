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

The proxy is **Python 3.10+**; use **3.12** to match the `Dockerfile`. macOS system
Python is 3.9 — below the floor — so work in a venv rather than reordering `PATH`
(Homebrew's `python3` symlink follows the newest formula and shifts under you).

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"   # or: python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"

# Postgres + pgvector is REQUIRED (sessions, knowledge, memory). For tests, start a
# throwaway — NOTE this is separate from the compose Postgres, which publishes no
# host port. Without it ~187 of 475 tests SKIP and a bare run still looks green:
docker run -d --name agentic-test-pg -e POSTGRES_PASSWORD=postgres \
    -e POSTGRES_DB=agentic_test -p 5433:5432 pgvector/pgvector:pg16
export AGENTIC_TEST_DATABASE_URL="postgresql://postgres:postgres@localhost:5433/agentic_test"

python -m pytest -q                 # proxy suite → 475 passed
python -m pytest -q host-mcp/tests  # host-MCP suite → 51 passed
uvx ruff@0.6.9 check src/ tests/    # lint (pin the version — see below)
```

Two gotchas worth knowing before you burn time on them:

- **`python -m pytest`, not bare `pytest`.** `[tool.pytest.ini_options] pythonpath`
  lists `src` and `host-mcp/src` but not `.`, so the three modules importing
  `tests.conftest` fail to collect under the bare entry point. The module form puts
  the CWD on `sys.path`.
- **Pin ruff.** `[tool.ruff]` sets only `line-length` + `target-version` — no explicit
  rule selection — and the dev extra says `ruff>=0.4` with no upper bound. Ruff's
  default rule set has grown a lot since, so an unpinned modern ruff reports ~642
  findings against unchanged code (mostly `UP045`) versus 8 on 0.4–0.6.

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
