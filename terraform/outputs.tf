output "ui_url" {
  description = "Open this. Seeding runs on first boot, so allow ~5 minutes before it answers."
  value       = "http://${aws_instance.ui.public_ip}:8501"
}

output "agent_runtimes" {
  description = "Every agent, from config/agents/*.agent.md. Add a file, re-apply, it appears here."
  value = {
    for id, r in aws_bedrockagentcore_agent_runtime.agents :
    id => r.agent_runtime_arn
  }
}

output "mcp_runtime_arn" {
  description = "The MongoDB MCP server runtime — the only path from agents to Atlas."
  value       = aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_arn
}

output "cloudwatch_log_groups" {
  description = "Per-runtime log groups. Agent reasoning, tool calls, and errors land here."
  value = merge(
    {
      for id, r in aws_bedrockagentcore_agent_runtime.agents :
      id => "/aws/bedrock-agentcore/runtimes/${r.agent_runtime_id}-DEFAULT"
    },
    {
      "mongodb-mcp" = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.mcp.agent_runtime_id}-DEFAULT"
    },
  )
}

output "atlas_cluster" {
  description = "Atlas cluster name and project. Browse the collections to see what the agents wrote."
  value = {
    project  = local.byo_atlas ? "(your own, not managed here)" : mongodbatlas_project.this[0].name
    cluster  = local.byo_atlas ? "(your own, not managed here)" : mongodbatlas_advanced_cluster.this[0].name
    tier     = local.byo_atlas ? "(your own, not managed here)" : var.atlas_cluster_tier
    database = var.mongodb_db
  }
}

output "mongodb_uri_secret_arn" {
  description = "Secrets Manager entry holding the Atlas connection string."
  value       = aws_secretsmanager_secret.mongodb_uri.arn
}

output "ssh_session_command" {
  description = "Reach the box without a key pair (needs the SSM plugin installed locally)."
  value       = "aws ssm start-session --target ${aws_instance.ui.id} --region ${var.aws_region}"
}
