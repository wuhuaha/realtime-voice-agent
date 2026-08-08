# syntax=docker/dockerfile:1.7

FROM docker.io/astral/uv:python3.12-bookworm-slim@sha256:e5b65587bce7de595f299855d7385fe7fca39b8a74baa261ba1b7147afa78e58 AS builder

ARG UV_DEFAULT_INDEX=https://pypi.org/simple

ENV UV_DEFAULT_INDEX=${UV_DEFAULT_INDEX} \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy
WORKDIR /opt/rva/server

# Resolve third-party layers before copying source so application edits retain the dependency cache.
COPY server/pyproject.toml server/uv.lock ./
COPY server/apps/session_director/pyproject.toml apps/session_director/pyproject.toml
COPY server/apps/realtime_worker/pyproject.toml apps/realtime_worker/pyproject.toml
COPY server/packages/voice_contracts/pyproject.toml packages/voice_contracts/pyproject.toml
COPY server/packages/voice_testkit/pyproject.toml packages/voice_testkit/pyproject.toml
RUN uv sync --frozen --no-dev --all-packages --no-install-workspace

COPY server/apps apps
COPY server/packages packages
RUN uv sync --frozen --no-dev --all-packages --no-editable

FROM python:3.12.11-slim-bookworm AS runtime

ENV PATH="/opt/rva/server/.venv/bin:${PATH}" \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

RUN groupadd --gid 10001 rva \
    && useradd --uid 10001 --gid rva --no-create-home --home-dir /nonexistent --shell /usr/sbin/nologin rva

WORKDIR /opt/rva/server
COPY --from=builder --chown=rva:rva /opt/rva/server/.venv .venv
COPY --chown=rva:rva deployment/single-node/preflight.py /opt/rva/preflight.py

USER 10001:10001
EXPOSE 8080/tcp 8081/tcp 8092/udp
ENTRYPOINT ["python", "/opt/rva/preflight.py"]
