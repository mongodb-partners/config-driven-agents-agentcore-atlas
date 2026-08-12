# Naming, normalised per service.
#
# `project_name` accepts hyphens and underscores. The services do not agree on
# which is legal, so each name below is spelled the way its service requires and
# the user never has to know:
#
#   S3 bucket             underscores forbidden
#   Atlas cluster         underscores forbidden
#   AgentCore runtime     hyphens forbidden
#   AgentCore memory      hyphens forbidden
#
# Everything else — IAM roles, ECR repositories, Secrets Manager, SSM
# parameters, security groups, tags — accepts both and uses `var.project_name`
# verbatim.

locals {
  name_dns   = replace(var.project_name, "_", "-")
  name_snake = replace(var.project_name, "-", "_")
}
