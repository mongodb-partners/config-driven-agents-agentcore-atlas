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

variable "mongodb_uri" {
  description = <<-EOT
    Bring your own cluster. Set this to a full SRV connection string — with the
    username and password in it — and the stack skips creating an Atlas project,
    cluster, database user, and IP access list entirely. It saves roughly ten
    minutes of a twenty-minute deploy, which is the whole point.

    Leave it empty to have Terraform create a dedicated project and cluster that
    `./deploy.sh destroy` removes completely.

    What you own when you bring your own:
      - network access — the Atlas project must allow the agent runtimes and the
        EC2 instance in (0.0.0.0/0 is what the managed path uses)
      - a database user with readWrite on `mongodb_db` and atlasAdmin on admin,
        which the seed needs to create Search indexes
      - the tier — this stack builds 7 vector indexes, so M0 (limit 3) will not
        do, and embedding_mode = "auto" needs M10+ with storage auto-scaling
      - the data — `destroy` leaves your cluster and its collections alone
  EOT
  type        = string
  sensitive   = true
  default     = ""

  validation {
    condition     = var.mongodb_uri == "" || can(regex("^mongodb(\\+srv)?://[^:]+:[^@]+@", var.mongodb_uri))
    error_message = "mongodb_uri must be a full connection string with credentials, e.g. mongodb+srv://user:pass@cluster.abcde.mongodb.net/."
  }

  validation {
    condition     = var.mongodb_uri != "" || (var.atlas_org_id != "" && var.atlas_public_key != "" && var.atlas_private_key != "")
    error_message = "Without mongodb_uri, Terraform creates the cluster and needs atlas_org_id, atlas_public_key, and atlas_private_key. Set those, or set mongodb_uri to use a cluster you already have."
  }
}

variable "atlas_public_key" {
  description = "Atlas programmatic API public key. Not needed when mongodb_uri is set."
  type        = string
  sensitive   = true
  default     = ""
}

variable "atlas_private_key" {
  description = "Atlas programmatic API private key. Not needed when mongodb_uri is set."
  type        = string
  sensitive   = true
  default     = ""
}

variable "atlas_org_id" {
  description = "Atlas organisation ID. A new project is created inside it. Not needed when mongodb_uri is set."
  type        = string
  default     = ""
}

variable "atlas_region" {
  description = "Atlas region name. Keep it close to aws_region — every agent call crosses this hop."
  type        = string
  default     = "US_EAST_1"
}

variable "atlas_cluster_tier" {
  description = <<-EOT
    Cluster tier for the cluster Terraform creates. M10 is the workshop default:
    the free M0 tier allows only three Atlas Search indexes and this stack needs
    seven. Ignored when mongodb_uri is set — you picked the tier already.
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

variable "embedding_mode" {
  description = <<-EOT
    Who turns text into vectors. Pick one before deploying — it decides the shape
    of the Atlas index, so it is not something you can flip per query.

      atlas-voyage  (default) The app calls a Voyage model through MongoDB's
                    Atlas Embedding and Reranking API and stores the vector.
                    Needs voyage_api_key.

      auto          MongoDB Atlas Automated Embedding. The app stores plain text,
                    Atlas embeds it in-cluster behind an `autoEmbed` index, and
                    queries go in as text. No API key, no rate limit, no
                    embedding code on the hot path. Preview feature; dedicated
                    cluster (M10+) with storage auto-scaling.

      titan         Bedrock Titan v2. No account beyond AWS — the fallback when
                    an attendee has no Atlas or Voyage key at all.
  EOT
  type        = string
  default     = "atlas-voyage"

  validation {
    condition     = contains(["atlas-voyage", "auto", "titan"], var.embedding_mode)
    error_message = "embedding_mode must be one of: atlas-voyage, auto, titan."
  }

  validation {
    condition     = var.embedding_mode != "atlas-voyage" || var.voyage_api_key != ""
    error_message = "embedding_mode = \"atlas-voyage\" needs voyage_api_key (an `al-…` Atlas model API key, or a `pa-…` Voyage key). Set it, or switch to \"auto\" (no key) or \"titan\"."
  }

  validation {
    # Automated Embedding accepts a narrower model list than the embeddings API,
    # and the failure lands mid-seed as an opaque index error otherwise.
    condition = (
      var.embedding_mode != "auto"
      || contains(["voyage-4", "voyage-4-large", "voyage-4-lite", "voyage-code-3"], var.voyage_embed_model)
    )
    error_message = "embedding_mode = \"auto\" supports only voyage-4, voyage-4-large, voyage-4-lite, or voyage-code-3."
  }

  validation {
    # Automated Embedding is not available on shared tiers, and needs the storage
    # auto-scaling that atlas.tf turns on for dedicated clusters. Unenforceable on
    # a bring-your-own cluster — deploy.sh warns instead.
    condition = (
      var.embedding_mode != "auto"
      || var.mongodb_uri != ""
      || can(regex("^M[1-9][0-9]", var.atlas_cluster_tier))
    )
    error_message = "embedding_mode = \"auto\" needs a dedicated cluster (M10 or larger). Free and Flex tiers cannot generate embeddings in-cluster."
  }
}

variable "voyage_api_key" {
  description = <<-EOT
    Voyage embedding key. Required when embedding_mode = "atlas-voyage", ignored
    by the other two modes.

    Two kinds exist and they hit different endpoints — an `al-…` key is issued by
    MongoDB Atlas (model API keys), a `pa-…` key comes from Voyage AI directly.
    The stack detects which from the prefix; using one against the other's
    endpoint returns 403.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "voyage_embed_model" {
  description = <<-EOT
    Voyage embedding model. Used by both Voyage-backed modes: "atlas-voyage"
    sends it to the embeddings API, "auto" writes it into the Atlas index
    definition.

    Verified at output_dimension=1024 for "atlas-voyage": voyage-4, voyage-4-lite,
    voyage-4-large, voyage-3.5, voyage-3.5-lite, voyage-3, voyage-3-large. Mode
    "auto" supports a narrower set — voyage-4, voyage-4-large, voyage-4-lite,
    voyage-code-3 — which the embedding_mode validations enforce.

    Changing it changes the vectors, so re-run the seed afterwards or queries
    will be compared against embeddings from a different model. Under "auto" the
    model name is part of the index definition, so a change there means dropping
    and rebuilding the vector indexes.
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
