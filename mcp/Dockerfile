# The official MongoDB MCP server, hosted on its own AgentCore Runtime.
#
# We write no MCP server of our own — this is the published npm package with a
# connection string injected at boot. Every agent's read and write to Atlas
# lands here.
#
# AgentCore Runtime contract for MCP servers: linux/arm64, streamable-HTTP MCP
# on 0.0.0.0:8000/mcp.
FROM --platform=linux/arm64 node:22-alpine

WORKDIR /app

# Pinned, not @latest. Server 2.x made `connectionId` a required argument on
# every tool; an unpinned bump silently breaks every agent's data access, which
# is exactly what it did once. Bump deliberately, and re-check
# `list-connections` for the connection name when you do.
RUN npm install --no-audit --no-fund \
      mongodb-mcp-server@2.1.0 \
      @aws-sdk/client-secrets-manager

COPY mcp/entrypoint.mjs ./

ENV MDB_MCP_TRANSPORT=http \
    MDB_MCP_HTTP_PORT=8000 \
    MDB_MCP_HTTP_HOST=0.0.0.0 \
    MDB_MCP_TELEMETRY=disabled \
    MDB_MCP_READ_ONLY=false

# Destructive and irrelevant tools are removed at the server, not merely
# unmentioned in a prompt — an agent cannot call what the server does not expose.
ENV MDB_MCP_DISABLED_TOOLS="drop-database,drop-collection,drop-index,rename-collection,delete-many,export,mongodb-logs,connect,atlas-local-create-deployment,atlas-local-delete-deployment,atlas-local-connect-deployment,atlas-local-list-deployments,list-knowledge-sources,search-knowledge"

RUN addgroup -S mcp && adduser -S mcp -G mcp && chown -R mcp:mcp /app
USER mcp

EXPOSE 8000

CMD ["node", "entrypoint.mjs"]
