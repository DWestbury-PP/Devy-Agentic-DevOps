# The LLM-PROXY service image. The `ask` TUI is a separate native Go binary
# (see tui/) and is NOT part of this image.
FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    AGENTIC_DEVOPS_HOME=/config

WORKDIR /app

# procps gives `ps`, `free`, `uptime` so the builtin host_diagnostics checks work
# against the container itself (slim images omit them). Real host inspection is
# still the job of the host MCP deployed on the target host.
RUN apt-get update \
    && apt-get install -y --no-install-recommends procps \
    && rm -rf /var/lib/apt/lists/*

# Build the package. hatchling reads README.md (declared as the project readme).
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[langsmith]"

# Config, .env, sessions, and traces live under AGENTIC_DEVOPS_HOME (mounted).
VOLUME ["/config"]
EXPOSE 8765

# Bind 0.0.0.0 inside the container; compose maps it to host loopback only.
CMD ["agentic-devops", "serve", "--host", "0.0.0.0"]
