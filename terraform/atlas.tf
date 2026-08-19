# MongoDB Atlas — operational store, vector store, memory, and trace sink.
#
# Two ways to get one:
#
#   Default        A dedicated project and cluster per attendee, so
#                  `terraform destroy` takes the whole thing with it and nothing
#                  leaks into an existing project. Costs ~10 minutes of the
#                  deploy, which on a two-hour workshop clock is real money.
#
#   mongodb_uri    Bring your own. Every resource below is skipped, the provided
#                  connection string goes straight into Secrets Manager, and the
#                  cluster outlives `destroy`. You own the network access list,
#                  the database user, and the tier.
#
# Everything downstream reads `local.mongodb_uri` and cannot tell the difference.

locals {
  # nonsensitive() because the *answer* — "did you bring a cluster?" — is not a
  # secret, and without it every value derived from this flag inherits the URI's
  # sensitivity and cannot be output or printed.
  byo_atlas   = nonsensitive(var.mongodb_uri != "")
  atlas_count = local.byo_atlas ? 0 : 1
}

resource "mongodbatlas_project" "this" {
  count = local.atlas_count

  name   = var.project_name
  org_id = var.atlas_org_id
}

resource "random_password" "atlas_user" {
  length  = 28
  special = false # keeps the SRV connection string free of percent-encoding
}

resource "mongodbatlas_advanced_cluster" "this" {
  count = local.atlas_count

  project_id     = mongodbatlas_project.this[0].id
  name           = local.name_dns # Atlas cluster names forbid underscores
  cluster_type   = "REPLICASET"
  backup_enabled = false

  replication_specs {
    region_configs {
      priority      = 7
      provider_name = "AWS"
      region_name   = var.atlas_region

      electable_specs {
        instance_size = var.atlas_cluster_tier
        node_count    = 3
      }

      # Automated Embedding (embedding_mode = "auto") requires this on dedicated
      # clusters: Atlas pauses embedding generation and marks the index Stale if
      # the disk fills. Harmless in the other modes.
      auto_scaling {
        disk_gb_enabled = true
      }
    }
  }
}

resource "mongodbatlas_database_user" "agent" {
  count = local.atlas_count

  project_id         = mongodbatlas_project.this[0].id
  username           = "${var.project_name}-agent"
  password           = random_password.atlas_user.result
  auth_database_name = "admin"

  roles {
    role_name     = "readWrite"
    database_name = var.mongodb_db
  }

  # The seed script creates Atlas Search indexes, which needs more than readWrite.
  roles {
    role_name     = "atlasAdmin"
    database_name = "admin"
  }
}

# Agents and the MCP runtime reach Atlas over the public internet (workshop
# simplicity — no VPC peering or PrivateLink), so the project must accept it.
resource "mongodbatlas_project_ip_access_list" "public" {
  count = local.atlas_count

  project_id = mongodbatlas_project.this[0].id
  cidr_block = "0.0.0.0/0"
  comment    = "Workshop: AgentCore runtimes and EC2 reach Atlas over public internet"
}

locals {
  # Inject credentials into the SRV string Atlas hands back. A bring-your-own URI
  # already carries its own.
  mongodb_uri = local.byo_atlas ? var.mongodb_uri : replace(
    mongodbatlas_advanced_cluster.this[0].connection_strings[0].standard_srv,
    "mongodb+srv://",
    "mongodb+srv://${mongodbatlas_database_user.agent[0].username}:${random_password.atlas_user.result}@"
  )
}

# The URI carries a password, so it lives in Secrets Manager and is fetched at
# boot by the MCP runtime and the UI using their IAM roles — not pasted into
# runtime environment variables where GetAgentRuntime would expose it.
resource "aws_secretsmanager_secret" "mongodb_uri" {
  name                    = "${var.project_name}/mongodb-uri"
  description             = "Atlas connection string for the multi-agent workshop"
  recovery_window_in_days = 0 # workshop stacks get destroyed and re-created often
}

resource "aws_secretsmanager_secret_version" "mongodb_uri" {
  secret_id     = aws_secretsmanager_secret.mongodb_uri.id
  secret_string = local.mongodb_uri
}
