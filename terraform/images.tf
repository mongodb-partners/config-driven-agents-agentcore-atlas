# Container images for the two runtime kinds.
#
# AgentCore Runtime requires linux/arm64, so these are cross-built with buildx.
# On Apple Silicon that is native; on x86 it runs under emulation and takes a few
# minutes — which overlaps with the Atlas cluster coming up, so it is not on the
# critical path.

locals {
  repo_root = abspath("${path.module}/..")

  # Hashing the sources means `terraform apply` rebuilds exactly when something
  # changed — including a newly added config/agents/*.agent.md, which is baked
  # into the agent image.
  agent_hash = md5(join("", concat(
    [for f in fileset("${local.repo_root}/agent", "**") : filemd5("${local.repo_root}/agent/${f}")],
    [for f in fileset("${local.repo_root}/config", "**") : filemd5("${local.repo_root}/config/${f}")],
  )))

  mcp_hash = md5(join("", [
    for f in fileset("${local.repo_root}/mcp", "**") : filemd5("${local.repo_root}/mcp/${f}")
  ]))
}

resource "aws_ecr_repository" "agent" {
  name                 = "${var.project_name}/agent"
  image_tag_mutability = "MUTABLE"
  force_delete         = true # workshop stacks must destroy cleanly
}

resource "aws_ecr_repository" "mcp" {
  name                 = "${var.project_name}/mongodb-mcp"
  image_tag_mutability = "MUTABLE"
  force_delete         = true
}

resource "null_resource" "build_agent" {
  triggers = { source_hash = local.agent_hash }

  provisioner "local-exec" {
    working_dir = local.repo_root
    command = join(" ", [
      "./scripts/build-and-push.sh",
      var.aws_region,
      aws_ecr_repository.agent.repository_url,
      local.agent_hash,
      "agent/Dockerfile",
    ])
  }
}

resource "null_resource" "build_mcp" {
  triggers = { source_hash = local.mcp_hash }

  provisioner "local-exec" {
    working_dir = local.repo_root
    command = join(" ", [
      "./scripts/build-and-push.sh",
      var.aws_region,
      aws_ecr_repository.mcp.repository_url,
      local.mcp_hash,
      "mcp/Dockerfile",
    ])
  }
}
