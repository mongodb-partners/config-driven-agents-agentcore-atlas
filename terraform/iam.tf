# Three roles: agent runtimes, the MCP runtime, and the EC2 box.
#
# Split deliberately. The MCP runtime is the only thing that can read the Atlas
# credentials; the agents are the only things that can call Bedrock models. An
# agent cannot reach Atlas except by asking the MCP runtime, which is the point.

data "aws_iam_policy_document" "agentcore_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["bedrock-agentcore.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [data.aws_caller_identity.current.account_id]
    }

    condition {
      test     = "ArnLike"
      variable = "aws:SourceArn"
      values   = ["arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:*"]
    }
  }
}

# Every AgentCore runtime needs these: pull its image, emit logs, emit traces.
data "aws_iam_policy_document" "runtime_base" {
  statement {
    sid       = "EcrAuth"
    effect    = "Allow"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }

  statement {
    sid    = "EcrPull"
    effect = "Allow"
    actions = [
      "ecr:BatchGetImage",
      "ecr:GetDownloadUrlForLayer",
      "ecr:BatchCheckLayerAvailability",
    ]
    resources = [aws_ecr_repository.agent.arn, aws_ecr_repository.mcp.arn]
  }

  statement {
    sid    = "Observability"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogGroups",
      "logs:DescribeLogStreams",
      "xray:PutTraceSegments",
      "xray:PutTelemetryRecords",
      "xray:GetSamplingRules",
      "xray:GetSamplingTargets",
    ]
    resources = ["*"]
  }

  statement {
    sid       = "AgentCoreMetrics"
    effect    = "Allow"
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]

    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = ["bedrock-agentcore"]
    }
  }

  statement {
    sid    = "WorkloadIdentity"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:GetWorkloadAccessToken",
      "bedrock-agentcore:GetWorkloadAccessTokenForJWT",
      "bedrock-agentcore:GetWorkloadAccessTokenForUserId",
    ]
    resources = ["*"]
  }
}

# ── Agent runtimes ────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "agent_runtime" {
  source_policy_documents = [data.aws_iam_policy_document.runtime_base.json]

  statement {
    sid    = "InvokeModels"
    effect = "Allow"
    actions = [
      "bedrock:InvokeModel",
      "bedrock:InvokeModelWithResponseStream",
      # Required when an AWS Organizations guardrail policy applies a Bedrock
      # guardrail to member accounts: Converse/ConverseStream then fails with
      # AccessDenied on ApplyGuardrail unless the caller holds this. The
      # guardrail usually lives in another account, so it cannot be a fixed ARN
      # here — and accounts with no org guardrail are unaffected by granting it.
      "bedrock:ApplyGuardrail",
    ]
    # Cross-region inference profiles resolve to foundation models in several
    # regions, so this cannot be pinned to one region's ARNs.
    resources = ["*"]
  }

  statement {
    sid    = "InvokeOtherRuntimes"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:InvokeAgentRuntime",
    ]
    resources = [
      "arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:runtime/*",
    ]
  }

  statement {
    sid    = "ShortTermMemory"
    effect = "Allow"
    actions = [
      "bedrock-agentcore:CreateEvent",
      "bedrock-agentcore:ListEvents",
      "bedrock-agentcore:GetEvent",
      "bedrock-agentcore:ListSessions",
      "bedrock-agentcore:RetrieveMemoryRecords",
      "bedrock-agentcore:ListMemoryRecords",
      "bedrock-agentcore:GetMemoryRecord",
    ]
    # Sessions and events are sub-resources of the memory ARN.
    resources = [
      aws_bedrockagentcore_memory.short_term.arn,
      "${aws_bedrockagentcore_memory.short_term.arn}/*",
    ]
  }

  statement {
    sid     = "ReadRuntimeMap"
    effect  = "Allow"
    actions = ["ssm:GetParameter"]
    # Constructed rather than referenced: the parameter's value depends on the
    # runtimes, which depend on this role. Referencing the resource would cycle.
    resources = [
      "arn:aws:ssm:${var.aws_region}:${data.aws_caller_identity.current.account_id}:parameter${local.runtime_map_param}"
    ]
  }
}

resource "aws_iam_role" "agent_runtime" {
  name               = "${var.project_name}-agent-runtime"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json
}

resource "aws_iam_role_policy" "agent_runtime" {
  name   = "agent-runtime"
  role   = aws_iam_role.agent_runtime.id
  policy = data.aws_iam_policy_document.agent_runtime.json
}

# ── MCP runtime ───────────────────────────────────────────────────────────────

data "aws_iam_policy_document" "mcp_runtime" {
  source_policy_documents = [data.aws_iam_policy_document.runtime_base.json]

  statement {
    sid       = "ReadAtlasCredentials"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.mongodb_uri.arn]
  }
}

resource "aws_iam_role" "mcp_runtime" {
  name               = "${var.project_name}-mcp-runtime"
  assume_role_policy = data.aws_iam_policy_document.agentcore_assume.json
}

resource "aws_iam_role_policy" "mcp_runtime" {
  name   = "mcp-runtime"
  role   = aws_iam_role.mcp_runtime.id
  policy = data.aws_iam_policy_document.mcp_runtime.json
}

# ── EC2 (UI + seed script) ────────────────────────────────────────────────────

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

data "aws_iam_policy_document" "ec2" {
  statement {
    sid       = "InvokeOrchestrator"
    effect    = "Allow"
    actions   = ["bedrock-agentcore:InvokeAgentRuntime"]
    resources = ["arn:aws:bedrock-agentcore:${var.aws_region}:${data.aws_caller_identity.current.account_id}:runtime/*"]
  }

  statement {
    sid       = "ReadAtlasCredentials"
    effect    = "Allow"
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.mongodb_uri.arn]
  }

  statement {
    sid       = "FetchAppBundle"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.bundle.arn}/*"]
  }

  statement {
    sid    = "SeedEmbeddings"
    effect = "Allow"
    # The seed script embeds every document with Titan before writing it.
    # ApplyGuardrail for the same org-guardrail reason as the agent role.
    actions   = ["bedrock:InvokeModel", "bedrock:ApplyGuardrail"]
    resources = ["*"]
  }

  statement {
    sid    = "Logs"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
      "logs:DescribeLogStreams",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "ec2" {
  name               = "${var.project_name}-ec2"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy" "ec2" {
  name   = "ec2"
  role   = aws_iam_role.ec2.id
  policy = data.aws_iam_policy_document.ec2.json
}

# SSM Session Manager, so attendees can reach the box without a key pair or an
# inbound SSH rule.
resource "aws_iam_role_policy_attachment" "ec2_ssm" {
  role       = aws_iam_role.ec2.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "ec2" {
  name = "${var.project_name}-ec2"
  role = aws_iam_role.ec2.name
}
