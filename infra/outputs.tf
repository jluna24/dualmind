output "iot_endpoint" {
  description = "Endpoint MQTT (ATS) para el firmware"
  value       = data.aws_iot_endpoint.data_ats.endpoint_address
}

output "thing_name" {
  value = aws_iot_thing.device.name
}

output "certificate_id" {
  value = aws_iot_certificate.device.id
}

output "telemetry_table" {
  value = aws_dynamodb_table.telemetry.name
}
