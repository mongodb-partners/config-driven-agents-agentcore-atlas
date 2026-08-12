# MongoDB Atlas — operational store, vector store, memory, and trace sink.
#
# A dedicated project per workshop attendee, so `terraform destroy` takes the
# whole thing with it and nothing leaks into an existing project.

resource "mongodbatlas_project" "this" {
  name   = var.project_name
  org_id = var.atlas_org_id
}

resource "random_password" "atlas_user" {
  length  = 28
  special = false # keeps the SRV connection string free of percent-encoding
}

resource "mongodbatlas_advanced_cluster" "this" {
  project_id     = mongodbatlas_project.this.id
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
    }
  }
}

resource "mongodbatlas_database_user" "agent" {
  project_id         = mongodbatlas_project.this.id
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
  project_id = mongodbatlas_project.this.id
  cidr_block = "0.0.0.0/0"
  comment    = "Workshop: AgentCore runtimes and EC2 reach Atlas over public internet"
}

locals {
  # Inject credentials into the SRV string Atlas hands back.
  mongodb_uri = replace(
    mongodbatlas_advanced_cluster.this.connection_strings[0].standard_srv,
    "mongodb+srv://",
    "mongodb+srv://${mongodbatlas_database_user.agent.username}:${random_password.atlas_user.result}@"
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
