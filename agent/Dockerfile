# One image for every agent. AGENT_ID at runtime decides which
# config/agents/*.agent.md this container becomes.
#
# AgentCore Runtime contract: linux/arm64, HTTP on 0.0.0.0:8080 with
# POST /invocations and GET /ping. BedrockAgentCoreApp provides both.
#
# Build context is the repo root, so `config/` can be baked in.
FROM --platform=linux/arm64 python:3.12-slim

WORKDIR /app

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CONFIG_DIR=/app/config

COPY agent/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# ── Magenta Memory SDK, from the private registry ─────────────────────────────
# `agentic-platform-memory` is not on PyPI. It is served from the Agentic
# Platform's package index, which authenticates with a short-lived OAuth token
# minted from a client id/secret pair.
#
# Both arrive as BuildKit secrets — never build args, which are substituted into
# the image history in plaintext. The client id is as sensitive as the secret:
# it doubles as the runtime credential for the Memory service (agentcore.tf
# feeds it to AGENTIC_MEMORY_API_KEY).
#
# The fingerprint IS a build arg, and exists only to be one. BuildKit
# deliberately keeps secret *contents* out of the cache key, so a build with
# credentials would otherwise reuse the cached no-credentials layer and silently
# skip the install. A truncated SHA-256 of the id carries that cache key without
# carrying the id.
#
# No credentials → the package is skipped and the image still builds. Only agents
# with `memory.backend: magenta` need it, and platform_memory.py already degrades
# with a clear message when the import is missing.
ARG MAGENTA_CREDS_FINGERPRINT=""
RUN --mount=type=secret,id=magenta_client_id \
    --mount=type=secret,id=magenta_client_secret \
    set -eu; \
    echo "magenta creds fingerprint: ${MAGENTA_CREDS_FINGERPRINT:-none}"; \
    if [ ! -s /run/secrets/magenta_client_id ] || [ ! -s /run/secrets/magenta_client_secret ]; then \
      echo "No Magenta credentials — skipping agentic-platform-memory. Agents with"; \
      echo "memory.backend: magenta will fail on their first turn; atlas agents are unaffected."; \
      exit 0; \
    fi; \
    apt-get update; \
    apt-get install -y --no-install-recommends curl jq; \
    CLIENT_ID="$(cat /run/secrets/magenta_client_id)"; \
    CLIENT_SECRET="$(cat /run/secrets/magenta_client_secret)"; \
    ACCESS_TOKEN=$(curl -sf -X POST https://agentic-platform.mongodb.com/api/v1/oauth/token \
      -d "grant_type=client_credentials&client_id=$CLIENT_ID&client_secret=$CLIENT_SECRET" \
      | jq -r .access_token); \
    [ -n "$ACCESS_TOKEN" ] && [ "$ACCESS_TOKEN" != "null" ] || \
      { echo "ERROR: Magenta token request failed — check magenta_client_id/secret." >&2; exit 1; }; \
    pip install --no-cache-dir uv; \
    uv pip install --system "agentic-platform-memory>=0.8.0,<0.9" \
      --extra-index-url "https://ignore:${ACCESS_TOKEN}@agentic-platform.mongodb.com/api/v1/packages/python/simple/"; \
    apt-get purge -y curl jq; apt-get autoremove -y; rm -rf /var/lib/apt/lists/*

COPY agent/*.py ./
COPY config ./config

RUN useradd -m -u 10001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

# opentelemetry-instrument is what turns on AgentCore's built-in tracing.
CMD ["opentelemetry-instrument", "python", "main.py"]
