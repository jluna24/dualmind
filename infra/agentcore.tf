# --- Fase 3, paso 1: la mente LLM en AgentCore Runtime -----------------------
#
# El mismo evaluate() de la Pi, empaquetado como zip (agentcore/build.py) y
# desplegado con "despliegue directo de código" (sin ECR ni Docker).
# La Pi pasa a ser solo el disparador: InvokeAgentRuntime cada 10 min.
#
# Flujo: python agentcore/build.py  →  tofu plan/apply  (sube el zip y crea
# una versión nueva del Runtime solo si el zip cambió).

variable "agent_model" {
  description = "Clave de var.llm_models que usa el agente de AgentCore"
  type        = string
  default     = "nova-micro"
}

locals {
  agent_name = "dualmind_agent" # AgentCore: [a-zA-Z][a-zA-Z0-9_]{0,47}, sin guiones
  agent_zip  = "${path.module}/../agentcore/dist/agent.zip"
}

# --- Código del agente en S3 ---------------------------------------------------

resource "aws_s3_bucket" "agent_code" {
  bucket        = "dualmind-agentcore-code-${local.account_id}"
  force_destroy = true # para que `tofu destroy` lo borre con sus versiones
}

resource "aws_s3_bucket_public_access_block" "agent_code" {
  bucket                  = aws_s3_bucket.agent_code.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Con versionado, cada zip nuevo tiene un version_id distinto y el Runtime
# apunta a esa versión exacta.
resource "aws_s3_bucket_versioning" "agent_code" {
  bucket = aws_s3_bucket.agent_code.id
  versioning_configuration {
    status = "Enabled"
  }
}

# Solo se conservan las 3 versiones anteriores del zip (son ~20 MB cada una)
resource "aws_s3_bucket_lifecycle_configuration" "agent_code" {
  bucket     = aws_s3_bucket.agent_code.id
  depends_on = [aws_s3_bucket_versioning.agent_code]

  rule {
    id     = "old-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days           = 1
      newer_noncurrent_versions = 3
    }
  }
}

resource "aws_s3_object" "agent_zip" {
  bucket      = aws_s3_bucket.agent_code.id
  key         = "${local.agent_name}/agent.zip"
  source      = local.agent_zip
  source_hash = filemd5(local.agent_zip)
  depends_on  = [aws_s3_bucket_versioning.agent_code]
}

# --- Rol de ejecución del agente ----------------------------------------------

resource "aws_iam_role" "agent_runtime" {
  name = "dualmind-agentcore-runtime"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "bedrock-agentcore.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = {
        StringEquals = { "aws:SourceAccount" = local.account_id }
        ArnLike      = { "aws:SourceArn" = "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:*" }
      }
    }]
  })
}

resource "aws_iam_role_policy" "agent_runtime" {
  name = "dualmind-agentcore-runtime"
  role = aws_iam_role.agent_runtime.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "ReadTelemetryAndWriteLlmDecision"
        Effect   = "Allow"
        Action   = ["dynamodb:Query", "dynamodb:GetItem", "dynamodb:UpdateItem"]
        Resource = aws_dynamodb_table.telemetry.arn
      },
      {
        Sid      = "InvokeLlmViaInferenceProfiles"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = local.llm_invoke_resources
      },
      {
        Sid      = "ReadAgentCode"
        Effect   = "Allow"
        Action   = ["s3:GetObject", "s3:GetObjectVersion"]
        Resource = "${aws_s3_bucket.agent_code.arn}/*"
      },
      {
        Sid    = "RuntimeLogs"
        Effect = "Allow"
        Action = ["logs:CreateLogGroup", "logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
        Resource = [
          "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*",
          "arn:aws:logs:${var.region}:${local.account_id}:log-group:/aws/bedrock-agentcore/runtimes/*:log-stream:*",
        ]
      },
      {
        Sid      = "DescribeLogGroups"
        Effect   = "Allow"
        Action   = ["logs:DescribeLogGroups"]
        Resource = "arn:aws:logs:${var.region}:${local.account_id}:log-group:*"
      },
      {
        # Trazas y métricas para AgentCore Observability (paso siguiente)
        Sid      = "Tracing"
        Effect   = "Allow"
        Action   = ["xray:PutTraceSegments", "xray:PutTelemetryRecords", "xray:GetSamplingRules", "xray:GetSamplingTargets"]
        Resource = "*"
      },
      {
        Sid       = "Metrics"
        Effect    = "Allow"
        Action    = ["cloudwatch:PutMetricData"]
        Resource  = "*"
        Condition = { StringEquals = { "cloudwatch:namespace" = "bedrock-agentcore" } }
      },
      {
        Sid    = "WorkloadIdentityToken"
        Effect = "Allow"
        Action = ["bedrock-agentcore:GetWorkloadAccessToken", "bedrock-agentcore:GetWorkloadAccessTokenForJWT", "bedrock-agentcore:GetWorkloadAccessTokenForUserId"]
        Resource = [
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default",
          "arn:aws:bedrock-agentcore:${var.region}:${local.account_id}:workload-identity-directory/default/workload-identity/${local.agent_name}-*",
        ]
      },
    ]
  })
}

# --- Runtime ------------------------------------------------------------------

resource "aws_bedrockagentcore_agent_runtime" "dualmind" {
  agent_runtime_name = local.agent_name
  description        = "DualMind LLM mind"
  role_arn           = aws_iam_role.agent_runtime.arn

  agent_runtime_artifact {
    code_configuration {
      entry_point = ["agent.py"]
      runtime     = "PYTHON_3_13"
      code {
        s3 {
          bucket     = aws_s3_bucket.agent_code.id
          prefix     = aws_s3_object.agent_zip.key
          version_id = aws_s3_object.agent_zip.version_id
        }
      }
    }
  }

  network_configuration {
    network_mode = "PUBLIC"
  }

  protocol_configuration {
    server_protocol = "HTTP"
  }

  # Una invocación cada 10 min: la sesión no necesita vivir más de 1 min
  # inactiva (se cobra memoria mientras la sesión existe).
  lifecycle_configuration {
    idle_runtime_session_timeout = 60
    max_lifetime                 = 300
  }

  environment_variables = {
    DUALMIND_TABLE      = aws_dynamodb_table.telemetry.name
    DUALMIND_DEVICE_ID  = var.device_id
    DUALMIND_MODEL_ID   = aws_bedrock_inference_profile.llm[var.agent_model].arn
    DUALMIND_MODEL_NAME = var.agent_model
    DUALMIND_TZ         = "America/Tijuana"
    DUALMIND_HISTORY_H  = "12"
  }

  tags = {
    model = var.agent_model
  }

  depends_on = [aws_iam_role_policy.agent_runtime]
}

output "agent_runtime_arn" {
  description = "Poner en DUALMIND_AGENT_RUNTIME_ARN (pi/dualmind.env)"
  value       = aws_bedrockagentcore_agent_runtime.dualmind.agent_runtime_arn
}
