FROM ghcr.io/astral-sh/uv:0.11.6-python3.13-trixie-slim@sha256:022ceea69be44c2699f5e63ebd64f5831ce72e44928b292242ed68832cef0e4a AS uv_source
FROM docker.io/tianon/gosu:1.19-trixie@sha256:3b176695959c71e123eb390d427efc665eeb561b1540e82679c15e992006b8b9 AS gosu_source

FROM uv_source AS build

ARG HERMES_INSTALL_EXTRAS=gateway-cloud

# Disable Python stdout buffering to ensure logs are printed immediately
ENV PYTHONUNBUFFERED=1
ENV UV_COMPILE_BYTECODE=1

RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    build-essential gcc git libffi-dev python3-dev && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /opt/hermes

COPY . .

RUN uv venv && \
    uv pip install --no-cache-dir -e ".[${HERMES_INSTALL_EXTRAS}]" && \
    find /opt/hermes -type d -name '__pycache__' -prune -exec rm -rf '{}' +

FROM docker.io/library/python:3.13-slim-trixie@sha256:a0779d7c12fc20be6ec6b4ddc901a4fd7657b8a6bc9def9d3fde89ed5efe0a3d

# Disable Python stdout buffering to ensure logs are printed immediately.
ENV PYTHONUNBUFFERED=1

# tini reaps orphaned zombie processes (MCP stdio subprocesses, git, etc.)
# that would otherwise accumulate when hermes runs as PID 1. See #15012.
RUN apt-get update && \
    apt-get install -y --no-install-recommends \
    ca-certificates curl git openssh-client procps tini && \
    rm -rf /var/lib/apt/lists/*

# Non-root user for runtime; UID can be overridden via HERMES_UID at runtime.
RUN useradd -u 10000 -m -d /opt/data hermes

COPY --chmod=0755 --from=gosu_source /gosu /usr/local/bin/

WORKDIR /opt/hermes
COPY --chown=hermes:hermes --from=build /opt/hermes /opt/hermes

# ---------- Runtime ----------
ENV HERMES_WEB_DIST=/opt/hermes/hermes_cli/web_dist
ENV HERMES_HOME=/opt/data
ENV PATH="/opt/hermes/.venv/bin:/opt/data/.local/bin:${PATH}"
VOLUME [ "/opt/data" ]
ENTRYPOINT [ "/usr/bin/tini", "-g", "--", "/opt/hermes/docker/entrypoint.sh" ]
