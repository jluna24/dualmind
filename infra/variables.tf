variable "region" {
  description = "Región de AWS del proyecto"
  type        = string
  default     = "us-east-2"
}

variable "device_id" {
  description = "Identificador del dispositivo (va en los tópicos y en el JSON)"
  type        = string
  default     = "esp32-01"
}

variable "telemetry_ttl_days" {
  description = "Días que se conserva cada lectura en DynamoDB antes de que el TTL la borre"
  type        = number
  default     = 30
}

variable "alert_email" {
  description = "Correo que recibe las alertas de CloudWatch (hay que confirmar la suscripción). Se define en terraform.tfvars (no versionado)"
  type        = string
}
