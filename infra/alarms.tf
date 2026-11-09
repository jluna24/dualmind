# --- Alerta: el ESP32 dejó de mandar telemetría ------------------------------
# La regla IoT publica DualMind/temp_avg una vez por minuto. Si en 10 minutos
# no llega ningún dato (ESP32 apagado, sin WiFi o DHT22 fallando), avisa por
# correo; también avisa cuando se recupera.

resource "aws_sns_topic" "alerts" {
  name = "dualmind-alerts"
}

resource "aws_sns_topic_subscription" "alerts_email" {
  topic_arn = aws_sns_topic.alerts.arn
  protocol  = "email"
  endpoint  = var.alert_email
}

resource "aws_cloudwatch_metric_alarm" "telemetry_missing" {
  alarm_name        = "dualmind-telemetry-missing"
  alarm_description = "DualMind: sin telemetría del ESP32 (${var.device_id}) en 10 minutos. Revisar energía, WiFi y el DHT22."

  namespace   = local.metrics_namespace
  metric_name = "temp_avg"
  statistic   = "SampleCount"
  period      = 60

  evaluation_periods  = 10
  datapoints_to_alarm = 10
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"

  alarm_actions = [aws_sns_topic.alerts.arn]
  ok_actions    = [aws_sns_topic.alerts.arn]
}
