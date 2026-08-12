variable "aws_region" {
  description = "AWS region. Must have Bedrock AgentCore and the Claude/Titan models enabled."
  type        = string
  default     = "us-east-1"
}

variable "project_name" {
  description = <<-EOT
    Name prefix for every resource. Hyphens and underscores are both fine — mix
    them if you like.

    The services disagree with each other (S3 buckets and Atlas clusters reject
    underscores; AgentCore runtime names reject hyphens), so locals.tf derives
    the right spelling for each instead of making you pick a lowest common
    denominator.
  EOT
  type        = string
  default     = "riv-workshop"

  validation {
    # Mirrors ECR's repository-name rule, the strictest consumer: a separator
    # must sit between alphanumerics, so no leading, trailing, or doubled
    # separators ("riv__workshop" would fail at ECR, mid-deploy).
    condition = (
      can(regex("^[a-z][a-z0-9]*([_-][a-z0-9]+)*$", var.project_name))
      && length(var.project_name) >= 3
      && length(var.project_name) <= 30
    )
    # 30 chars leaves room for the longest derived AgentCore runtime name
    # ("<project>_company_intel") to stay under the service's 48-char limit.
    error_message = "Use lowercase letters and digits, separated by single hyphens or underscores, 3-30 chars, starting with a letter. E.g. \"riv_workshop_test_anuj\" or \"riv-workshop-anuj\"."
  }
}

# ── MongoDB Atlas ─────────────────────────────────────────────────────────────

variable "atlas_public_key" {
  description = "Atlas programmatic API public key."
  type        = string
  sensitive   = true
}

variable "atlas_private_key" {
  description = "Atlas programmatic API private key."
  type        = string
  sensitive   = true
}

variable "atlas_org_id" {
  description = "Atlas organisation ID. A new project is created inside it."
  type        = string
}

variable "atlas_region" {
  description = "Atlas region name. Keep it close to aws_region — every agent call crosses this hop."
  type        = string
  default     = "US_EAST_1"
}

variable "atlas_cluster_tier" {
  description = <<-EOT
    Cluster tier. M10 is the workshop default: the free M0 tier allows only three
    Atlas Search indexes and this stack needs seven.
  EOT
  type        = string
  default     = "M10"
}

variable "mongodb_db" {
  description = "Database name inside the cluster."
  type        = string
  default     = "riv_workshop"
}

# ── Models ────────────────────────────────────────────────────────────────────

variable "voyage_api_key" {
  description = <<-EOT
    Optional. Set this to embed with Voyage AI instead of Bedrock Titan. Leave
    empty to use Titan — no extra account needed. Both are 1024-dim, so the Atlas
    indexes are identical either way.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "voyage_embed_model" {
  description = <<-EOT
    Voyage embedding model, used only when voyage_api_key is set. Verified to
    work at output_dimension=1024: voyage-4, voyage-4-lite, voyage-4-large,
    voyage-3.5, voyage-3.5-lite, voyage-3, voyage-3-large.

    Changing it does not change the Atlas index definition — every option is
    1024-dim — but it does change the vectors, so re-run the seed afterwards or
    queries will be compared against embeddings from a different model.
  EOT
  type        = string
  default     = "voyage-4"

  validation {
    # Format check only, deliberately not an allow-list. A hardcoded enum here
    # lags the provider's catalogue and rejects models that work — which is worse
    # than the typo it was meant to catch. A wrong-but-well-formed name fails
    # fast at seed time with the API's own list of supported models.
    condition     = can(regex("^voyage-[a-z0-9.-]+$", var.voyage_embed_model))
    error_message = "Must look like a Voyage model name, e.g. \"voyage-4\" or \"voyage-3.5-lite\"."
  }
}

# ── Compute ───────────────────────────────────────────────────────────────────

variable "ec2_instance_type" {
  description = "Instance for the Streamlit UI. Graviton — matches the ARM64 agent images."
  type        = string
  default     = "t4g.small"
}

variable "ui_allowed_cidr" {
  description = <<-EOT
    CIDR allowed to reach the UI on port 8501. Defaults to open because attendees
    join from arbitrary networks; narrow it to your own IP/32 if you can.
  EOT
  type        = string
  default     = "0.0.0.0/0"
}
