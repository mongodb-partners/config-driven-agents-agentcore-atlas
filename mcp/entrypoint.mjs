/**
 * Fetch the Atlas connection string from Secrets Manager, then hand off to the
 * MongoDB MCP server.
 *
 * The URI carries a database password, so it is resolved at boot using the
 * runtime's IAM role rather than sitting in the runtime's environment-variable
 * config where anyone with GetAgentRuntime could read it.
 */
import { spawn } from "node:child_process";
import { SecretsManagerClient, GetSecretValueCommand } from "@aws-sdk/client-secrets-manager";

const secretId = process.env.MONGODB_URI_SECRET_ARN;
if (!secretId) {
  console.error("MONGODB_URI_SECRET_ARN is not set");
  process.exit(1);
}

const client = new SecretsManagerClient({ region: process.env.AWS_REGION });
const { SecretString } = await client.send(new GetSecretValueCommand({ SecretId: secretId }));

// The secret is a plain connection string; tolerate a JSON-wrapped one too.
let uri = SecretString.trim();
if (uri.startsWith("{")) uri = JSON.parse(uri).connectionString ?? JSON.parse(uri).uri;
if (!uri) {
  console.error("secret did not contain a connection string");
  process.exit(1);
}

// The locally installed binary, not npx — the container must not reach out to
// the npm registry at boot.
const child = spawn("/app/node_modules/.bin/mongodb-mcp-server", [], {
  stdio: "inherit",
  env: { ...process.env, MDB_MCP_CONNECTION_STRING: uri },
});

child.on("exit", (code) => process.exit(code ?? 1));
for (const sig of ["SIGTERM", "SIGINT"]) process.on(sig, () => child.kill(sig));
