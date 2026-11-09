terraform {
  required_version = ">= 1.8.0" # OpenTofu

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 6.0"
    }
    local = {
      source  = "hashicorp/local"
      version = "~> 2.5"
    }
  }

  # Estado local por ahora (infra/terraform.tfstate, fuera de git).
  # Más adelante se puede mover a un backend S3.
}

provider "aws" {
  region = var.region

  default_tags {
    tags = {
      project    = "dualmind"
      managed_by = "opentofu"
    }
  }
}
