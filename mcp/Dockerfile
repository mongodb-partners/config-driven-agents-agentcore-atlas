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
#
# The basic-ftp override clears 3 high advisories (GHSA-c475-qrg2-pj4r,
# GHSA-5pm8-86m8-9m6r, GHSA-r4jh-29pc-gfqr) pulled in via PAC-proxy support. The
# upstream fix is mongodb-mcp-server 3.x; drop the override when bumping to it.
RUN npm init -y >/dev/null \
 && npm pkg set 'overrides.basic-ftp=^6.2.4' \
 && npm install --no-audit --no-fund \
      mongodb-mcp-server@2.1.0 \
      @aws-sdk/client-secrets-manager@3

COPY mcp/entrypoint.mjs ./

ENV MDB_MCP_TRANSPORT=http \
    MDB_MCP_HTTP_PORT=8000 \
    MDB_MCP_HTTP_HOST=0.0.0.0 \
    MDB_MCP_TELEMETRY=disabled \
    MDB_MCP_READ_ONLY=false

# Destructive and irrelevant tools are removed at the server, not merely
# unmentioned in a prompt — an agent cannot call what the server does not expose.
# The framework writes with insert-many and update-many only (mongo_mcp.py,
# memory.py); every other write and schema tool is off. The connection itself is
# readWrite on one database (atlas.tf), so this is the second layer, not the only.
ENV MDB_MCP_DISABLED_TOOLS="drop-database,drop-collection,drop-index,rename-collection,delete-many,create-index,create-collection,export,mongodb-logs,connect,atlas-local-create-deployment,atlas-local-delete-deployment,atlas-local-connect-deployment,atlas-local-list-deployments,list-knowledge-sources,search-knowledge"

RUN addgroup -S mcp && adduser -S mcp -G mcp && chown -R mcp:mcp /app
USER mcp

EXPOSE 8000

CMD ["node", "entrypoint.mjs"]
