data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  thing_name = "dualmind-${var.device_id}"
}

# --- IoT Core: dispositivo, certificado y permisos ------------------------

data "aws_iot_endpoint" "data_ats" {
  endpoint_type = "iot:Data-ATS"
}

resource "aws_iot_thing" "device" {
  name = local.thing_name
}

# Certificado a partir de una CSR generada localmente con OpenSSL
# (ver infra/README.md). La llave privada nunca pasa por OpenTofu:
# vive solo en esp32/certs/private.pem.key.
resource "aws_iot_certificate" "device" {
  csr    = file("${path.module}/device.csr")
  active = true
}

# El certificado firmado se deja junto a la llave para el firmware.
resource "local_file" "device_cert" {
  content         = aws_iot_certificate.device.certificate_pem
  filename        = "${path.module}/../esp32/certs/device.pem.crt"
  file_permission = "0600"
}

resource "aws_iot_policy" "device" {
  name = "${local.thing_name}-policy"

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect   = "Allow"
        Action   = "iot:Connect"
        Resource = "arn:aws:iot:${var.region}:${local.account_id}:client/${local.thing_name}"
      },
      {
        Effect   = "Allow"
        Action   = "iot:Publish"
        Resource = "arn:aws:iot:${var.region}:${local.account_id}:topic/dualmind/${var.device_id}/*"
      },
    ]
  })
}

resource "aws_iot_policy_attachment" "device" {
  policy = aws_iot_policy.device.name
  target = aws_iot_certificate.device.arn
}

resource "aws_iot_thing_principal_attachment" "device" {
  thing     = aws_iot_thing.device.name
  principal = aws_iot_certificate.device.arn
}

# --- DynamoDB: telemetría --------------------------------------------------

resource "aws_dynamodb_table" "telemetry" {
  name         = "dualmind-telemetry"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "device_id"
  range_key    = "ts"

  attribute {
    name = "device_id"
    type = "S"
  }

  attribute {
    name = "ts"
    type = "N"
  }

  ttl {
    attribute_name = "expires_at"
    enabled        = true
  }
}

# --- Regla IoT y su rol ----------------------------------------------------

resource "aws_iam_role" "iot_rule" {
  name = "dualmind-iot-rule-role"

  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "iot.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.account_id } }
    }]
  })
}

resource "aws_iam_role_policy" "iot_rule_dynamodb" {
  name = "dynamodb-put"
  role = aws_iam_role.iot_rule.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = "dynamodb:PutItem"
      Resource = aws_dynamodb_table.telemetry.arn
    }]
  })
}

# --- CloudWatch: métricas y errores de la regla ------------------------------

locals {
  metrics_namespace = "DualMind"

  # Una métrica por lectura: nombre => { valor (sustitución IoT), unidad }
  telemetry_metrics = {
    temp_avg = { value = "$${readings.temp_c.avg}", unit = "None" }
    hum_avg  = { value = "$${readings.hum_pct.avg}", unit = "Percent" }
  }
}

resource "aws_cloudwatch_log_group" "iot_rule_errors" {
  name              = "/dualmind/iot-rule-errors"
  retention_in_days = 1
}

resource "aws_iam_role_policy" "iot_rule_cloudwatch" {
  name = "cloudwatch"
  role = aws_iam_role.iot_rule.id

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect    = "Allow"
        Action    = "cloudwatch:PutMetricData"
        Resource  = "*" # PutMetricData no admite ARN; se acota por namespace
        Condition = { StringEquals = { "cloudwatch:namespace" = local.metrics_namespace } }
      },
      {
        Effect   = "Allow"
        Action   = ["logs:CreateLogStream", "logs:PutLogEvents"]
        Resource = "${aws_cloudwatch_log_group.iot_rule_errors.arn}:*"
      },
    ]
  })
}

resource "aws_iot_topic_rule" "telemetry_to_ddb" {
  name        = "dualmind_telemetry_to_ddb"
  description = "DualMind: telemetria del dispositivo a DynamoDB (TTL ${var.telemetry_ttl_days} dias)"
  enabled     = true
  sql_version = "2016-03-23"
  sql         = "SELECT *, floor(timestamp() / 1000) + ${var.telemetry_ttl_days * 86400} AS expires_at FROM 'dualmind/+/telemetry'"

  dynamodbv2 {
    role_arn = aws_iam_role.iot_rule.arn
    put_item {
      table_name = aws_dynamodb_table.telemetry.name
    }
  }

  dynamic "cloudwatch_metric" {
    for_each = local.telemetry_metrics
    content {
      role_arn         = aws_iam_role.iot_rule.arn
      metric_namespace = local.metrics_namespace
      metric_name      = cloudwatch_metric.key
      metric_value     = cloudwatch_metric.value.value
      metric_unit      = cloudwatch_metric.value.unit
    }
  }

  # Si alguna acción falla (permisos, tabla, etc.) el motivo queda en este log.
  error_action {
    cloudwatch_logs {
      role_arn       = aws_iam_role.iot_rule.arn
      log_group_name = aws_cloudwatch_log_group.iot_rule_errors.name
    }
  }

  # Las acciones se ejecutan con el rol, así que sus permisos deben existir antes.
  depends_on = [
    aws_iam_role_policy.iot_rule_dynamodb,
    aws_iam_role_policy.iot_rule_cloudwatch,
  ]
}
