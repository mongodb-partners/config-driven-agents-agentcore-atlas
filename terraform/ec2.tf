# One EC2 instance running the Streamlit UI. There is no separate backend
# service — Streamlit invokes the orchestrator runtime directly with the
# instance role's credentials.
#
# The same instance seeds Atlas on first boot, which is why it holds
# bedrock:InvokeModel (Titan embeddings) and reads the connection secret.

data "aws_vpc" "default" {
  default = true
}

data "aws_subnets" "default" {
  filter {
    name   = "vpc-id"
    values = [data.aws_vpc.default.id]
  }
}

# Graviton types are not offered in every AZ — us-east-1e is the usual gap — and
# a default VPC has a subnet in every AZ. Picking a subnet blindly gets you
# "Unsupported: The requested configuration is currently not supported" from
# RunInstances, so intersect the two lists instead.
data "aws_ec2_instance_type_offerings" "ui" {
  location_type = "availability-zone"

  filter {
    name   = "instance-type"
    values = [var.ec2_instance_type]
  }
}

data "aws_subnet" "default" {
  for_each = toset(data.aws_subnets.default.ids)
  id       = each.value
}

locals {
  ui_subnet_ids = sort([
    for id, subnet in data.aws_subnet.default : id
    if contains(data.aws_ec2_instance_type_offerings.ui.locations, subnet.availability_zone)
  ])
}

data "aws_ssm_parameter" "al2023_arm64" {
  name = "/aws/service/ami-amazon-linux-latest/al2023-ami-kernel-default-arm64"
}

# ── App bundle ────────────────────────────────────────────────────────────────

resource "random_id" "bundle" {
  byte_length = 4
}

resource "aws_s3_bucket" "bundle" {
  bucket        = "${local.name_dns}-bundle-${random_id.bundle.hex}" # S3 forbids underscores
  force_destroy = true
}

resource "aws_s3_bucket_public_access_block" "bundle" {
  bucket                  = aws_s3_bucket.bundle.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Enumerated rather than source_dir + excludes: a directory exclusion that
# silently fails would sweep terraform/.terraform (~1GB of providers) into the
# bundle. This lists exactly what the instance needs and nothing else.
locals {
  bundle_files = [
    for f in concat(
      tolist(fileset(local.repo_root, "seed/**")),
      tolist(fileset(local.repo_root, "ui/**")),
      ["agent/embeddings.py"], # seed.py imports it, to keep dims and provider single-sourced
    ) : f if !can(regex("__pycache__|[.]pyc$|/venv/|[.]DS_Store", f))
  ]
}

data "archive_file" "bundle" {
  type        = "zip"
  output_path = "${path.module}/.bundle.zip"

  dynamic "source" {
    for_each = toset(local.bundle_files)
    content {
      content  = file("${local.repo_root}/${source.value}")
      filename = source.value
    }
  }
}

resource "aws_s3_object" "bundle" {
  bucket = aws_s3_bucket.bundle.id
  key    = "bundle-${data.archive_file.bundle.output_md5}.zip"
  source = data.archive_file.bundle.output_path
  etag   = data.archive_file.bundle.output_md5
}

# ── Instance ──────────────────────────────────────────────────────────────────

resource "aws_security_group" "ui" {
  name        = "${var.project_name}-ui"
  description = "Streamlit UI"
  vpc_id      = data.aws_vpc.default.id

  ingress {
    description = "Streamlit"
    from_port   = 8501
    to_port     = 8501
    protocol    = "tcp"
    cidr_blocks = [var.ui_allowed_cidr]
  }

  egress {
    description = "Atlas, Bedrock, ECR, S3"
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "ui" {
  ami                    = data.aws_ssm_parameter.al2023_arm64.value
  instance_type          = var.ec2_instance_type
  subnet_id              = local.ui_subnet_ids[0]
  vpc_security_group_ids = [aws_security_group.ui.id]
  iam_instance_profile   = aws_iam_instance_profile.ec2.name

  lifecycle {
    precondition {
      condition     = length(local.ui_subnet_ids) > 0
      error_message = "No default-VPC subnet sits in an AZ offering ${var.ec2_instance_type} in ${var.aws_region}. Pick another Graviton type (t4g.medium, m7g.medium) or another region via ec2_instance_type / aws_region."
    }
  }

  metadata_options {
    http_tokens   = "required" # IMDSv2 only
    http_endpoint = "enabled"
  }

  root_block_device {
    volume_size = 20
    encrypted   = true
  }

  user_data_replace_on_change = true
  user_data = templatefile("${path.module}/user_data.sh.tftpl", {
    region                 = var.aws_region
    bundle_uri             = "s3://${aws_s3_bucket.bundle.id}/${aws_s3_object.bundle.key}"
    orchestrator_arn       = aws_bedrockagentcore_agent_runtime.agents[local.orchestrator_id].agent_runtime_arn
    orchestrator_log_group = "/aws/bedrock-agentcore/runtimes/${aws_bedrockagentcore_agent_runtime.agents[local.orchestrator_id].agent_runtime_id}-DEFAULT"
    mongodb_db             = var.mongodb_db
    mongodb_secret_arn     = aws_secretsmanager_secret.mongodb_uri.arn
    embedding_mode         = var.embedding_mode
    voyage_api_key         = var.voyage_api_key
    voyage_embed_model     = var.voyage_embed_model
  })

  tags = { Name = "${var.project_name}-ui" }

  depends_on = [
    aws_ssm_parameter.runtime_map,
    aws_iam_role_policy.ec2,
    mongodbatlas_project_ip_access_list.public,
  ]
}
