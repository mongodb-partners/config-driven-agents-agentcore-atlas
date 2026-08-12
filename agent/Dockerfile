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

COPY agent/*.py ./
COPY config ./config

RUN useradd -m -u 10001 agent && chown -R agent:agent /app
USER agent

EXPOSE 8080

# opentelemetry-instrument is what turns on AgentCore's built-in tracing.
CMD ["opentelemetry-instrument", "python", "main.py"]
