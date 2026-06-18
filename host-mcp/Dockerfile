# Containerized host MCP for the local demo.
#
# Installs ONLY the Docker CLI (docker-ce-cli — no engine) plus common diag
# tools, then the host-mcp package. At runtime the host's /var/run/docker.sock is
# mounted in, so the allow-listed docker_* checks see the host's real containers.
#
# NOTE: host-level checks (df/free/ps) reflect THIS container, not the host —
# for true host inspection, deploy host-mcp natively on the target host (see
# host-mcp/README.md). The Docker checks, via the mounted socket, are real.

FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOST_MCP_TRANSPORT=http \
    HOST_MCP_HOST=0.0.0.0 \
    HOST_MCP_PORT=8780 \
    HOST_MCP_PROFILE=diagnostic

# Docker CLI (client only) via Docker's apt repo (arch-aware), plus iproute2 (ss)
# and procps (ps/top/free/uptime) so the host/diag checks have their binaries.
RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl gnupg iproute2 procps \
    && install -m 0755 -d /etc/apt/keyrings \
    && curl -fsSL https://download.docker.com/linux/debian/gpg -o /etc/apt/keyrings/docker.asc \
    && chmod a+r /etc/apt/keyrings/docker.asc \
    && echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/debian bookworm stable" \
        > /etc/apt/sources.list.d/docker.list \
    && apt-get update \
    && apt-get install -y --no-install-recommends docker-ce-cli \
    && apt-get purge -y gnupg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install --no-cache-dir ".[http]"

EXPOSE 8780
CMD ["agentic-devops-host-mcp"]
