# AgentCore: one runtime for the MongoDB MCP server, plus one runtime per agent
# definition file.
#
# This is the mechanism behind "add an agent without touching code". `for_each`
# scans config/agents/, so dropping in a fourth `.agent.md` and re-applying
# creates a fourth runtime, rebuilds the shared image with the new definition
# baked in, and republishes the runtime map the orchestrator routes from.

locals {
  agent_dir = "${local.repo_root}/config/agents"

  # Filename is the agent id — agent_config.load_agent() resolves it the same way.
  agents = {
    for f in fileset(local.agent_dir, "*.agent.md") :
    trimsuffix(f, ".agent.md") => yamldecode(split("---\n", file("${local.agent_dir}/${f}"))[1])
  }

  orchestrator_id = one([for id, meta in local.agents : id if meta.role == "orchestrator"])

  runtime_map_param = "/${var.project_name}/agent-runtimes"
}

# ── Short-term memory ─────────────────────────────────────────────────────────

resource "aws_bedrockagentcore_memory" "short_term" {
  name                  = "${local.name_snake}_short_term"
  description           = "In-session turn buffer for the workshop agents"
  event_expiry_duration = 7 # the service minimum; long-term memory lives in Atlas
}

# ── MongoDB MCP server ────────────────────────────────────────────────────────

resource "aws_bedrockagentcore_agent_runtime" "mcp" {
  agent_runtime_name = "${local.name_snake}_mongodb_mcp"
  description        = "MongoDB MCP server — the only path from agents to Atlas"
  role_arn           = aws_iam_role.mcp_runtime.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.mcp.repository_url}:${local.mcp_hash}"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "MCP"
  }

  environment_variables = {
    MONGODB_URI_SECRET_ARN = aws_secretsmanager_secret.mongodb_uri.arn
  }

  # No authorizer_configuration: without one, AgentCore requires SigV4 on every
  # invocation. The agents' IAM role is the authentication on this path.

  depends_on = [
    null_resource.build_mcp,
    aws_secretsmanager_secret_version.mongodb_uri,
    aws_iam_role_policy.mcp_runtime,
  ]
}

# ── Agents ────────────────────────────────────────────────────────────────────

resource "aws_bedrockagentcore_agent_runtime" "agents" {
  for_each = local.agents

  agent_runtime_name = "${local.name_snake}_${replace(each.key, "-", "_")}"
  description        = try(each.value.name, each.key)
  role_arn           = aws_iam_role.agent_runtime.arn

  agent_runtime_artifact {
    container_configuration {
      container_uri = "${aws_ecr_repository.agent.repository_url}:${local.agent_hash}"
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  environment_variables = merge(
    {
      AGENT_ID            = each.key
      AGENT_REGION        = var.aws_region
      MCP_RUNTIME_ARN     = aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn
      AGENTCORE_MEMORY_ID = aws_bedrockagentcore_memory.short_term.id
      RUNTIME_MAP_PARAM   = local.runtime_map_param
      MONGODB_DB          = var.mongodb_db
    },
    var.voyage_api_key == "" ? {} : {
      VOYAGE_API_KEY     = var.voyage_api_key
      VOYAGE_EMBED_MODEL = var.voyage_embed_model
    },
  )

  depends_on = [
    null_resource.build_agent,
    aws_iam_role_policy.agent_runtime,
  ]
}

# The roster the orchestrator resolves handoff targets from. Written after every
# runtime exists, and read lazily by the orchestrator — so a newly added agent is
# routable without redeploying the orchestrator's image.
resource "aws_ssm_parameter" "runtime_map" {
  name        = local.runtime_map_param
  description = "agentId -> AgentCore runtime ARN"
  type        = "String"

  value = jsonencode({
    for id, runtime in aws_bedrockagentcore_agent_runtime.agents :
    id => runtime.agent_runtime_arn
  })
}
