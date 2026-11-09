# --- Fase 2: mente LLM (Bedrock) y su worker en la Raspberry Pi -------------
#
# Bedrock on-demand no tiene endpoints que provisionar: aquí solo hay
#  - un perfil de inferencia de APLICACIÓN por modelo (copia del perfil `us.`
#    del sistema), para etiquetar el gasto por modelo en Cost Explorer, y
#  - el usuario IAM del worker con permisos mínimos.
# La access key del worker se crea por CLI (ver README) para que el secreto
# no quede en el estado de OpenTofu.

variable "llm_models" {
  description = "Modelos del LLM: clave corta => id del perfil de inferencia del sistema"
  type        = map(string)
  default = {
    nova-micro = "us.amazon.nova-micro-v1:0"
    # haiku-4-5 = "us.anthropic.claude-haiku-4-5-20251001-v1:0"  # segundo modelo, después
  }
}

data "aws_bedrock_inference_profile" "system" {
  for_each             = var.llm_models
  inference_profile_id = each.value
}

resource "aws_bedrock_inference_profile" "llm" {
  for_each = var.llm_models
  name     = "dualmind-${each.key}"
  # Bedrock solo acepta [0-9a-zA-Z:.] separados por un espacio, _ o -
  description = "DualMind copy of ${each.value}"

  model_source {
    copy_from = data.aws_bedrock_inference_profile.system[each.key].inference_profile_arn
  }

  tags = {
    model = each.key
  }
}

locals {
  # Un perfil `us.` puede enrutar a us-east-1, us-east-2 o us-west-2: el
  # permiso debe cubrir el modelo base en todas sus regiones destino.
  llm_foundation_model_arns = distinct(flatten([
    for p in data.aws_bedrock_inference_profile.system : [for m in p.models : m.model_arn]
  ]))

  # Lo que necesita bedrock:InvokeModel (lo usan el worker y el rol de AgentCore)
  llm_invoke_resources = concat(
    [for p in aws_bedrock_inference_profile.llm : p.arn],
    [for p in data.aws_bedrock_inference_profile.system : p.inference_profile_arn],
    local.llm_foundation_model_arns,
  )
}

# --- Usuario IAM del worker (Raspberry Pi) ------------------------------------

resource "aws_iam_user" "llm_worker" {
  name = "dualmind-llm-worker"
}

resource "aws_iam_user_policy" "llm_worker" {
  name = "dualmind-llm-worker"
  user = aws_iam_user.llm_worker.name

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
        # Converse usa el permiso bedrock:InvokeModel
        Sid      = "InvokeLlmViaInferenceProfiles"
        Effect   = "Allow"
        Action   = ["bedrock:InvokeModel"]
        Resource = local.llm_invoke_resources
      },
      {
        # Fase 3: la Pi solo dispara al agente de AgentCore (DUALMIND_ENGINE=agentcore)
        Sid    = "InvokeDualMindAgent"
        Effect = "Allow"
        Action = ["bedrock-agentcore:InvokeAgentRuntime"]
        Resource = [
          aws_bedrockagentcore_agent_runtime.dualmind.agent_runtime_arn,
          "${aws_bedrockagentcore_agent_runtime.dualmind.agent_runtime_arn}/runtime-endpoint/*",
        ]
      },
    ]
  })
}

output "llm_profile_arns" {
  description = "ARN del perfil de inferencia de aplicación por modelo (usar como modelId)"
  value       = { for k, p in aws_bedrock_inference_profile.llm : k => p.arn }
}

output "llm_worker_user" {
  value = aws_iam_user.llm_worker.name
}
