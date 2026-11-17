variable "account_id" {
  description = "AWS account id; pinned in allowed_account_ids (INF-7)."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be 12 digits."
  }
}

variable "region" {
  description = "AWS region; the facts register rows are for us-east-1."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for names and the Project tag; must match the bootstrap root."
  type        = string
  default     = "aorte-mvp"
}

variable "availability_zone_id" {
  description = "AZ id for the private subnet; VPC origins do not support use1-az3 (F-069)."
  type        = string
  default     = "use1-az1"
  validation {
    condition     = var.availability_zone_id != "use1-az3"
    error_message = "use1-az3 does not support CloudFront VPC origins (F-069)."
  }
}

variable "instance_type" {
  description = "ADR-0019: one 4 GiB x86_64 host."
  type        = string
  default     = "t3a.medium"
}

variable "root_volume_gib" {
  description = "Encrypted gp3 root volume; holds the Docker volumes with PostgreSQL data."
  type        = number
  default     = 30
}

variable "api_port" {
  description = "Approval API port on the host; the VPC origin and the security group use it."
  type        = number
  default     = 8080
}

variable "core_desired_count" {
  description = "0 until images are in ECR (#97); then 1."
  type        = number
  default     = 0
  validation {
    condition     = contains([0, 1], var.core_desired_count)
    error_message = "Single host: core_desired_count is 0 or 1."
  }
}

variable "engine_image_tag" {
  description = "Tag of the engine image in ECR; the deploy workflow registers newer revisions."
  type        = string
  default     = "bootstrap"
}

variable "log_retention_days" {
  description = "CloudWatch Logs retention (F-067 pricing)."
  type        = number
  default     = 7
}

variable "stop_cron" {
  description = "INF-6 nightly stop (owner, 2026-11-17: 23:00 Africa/Cairo)."
  type        = string
  default     = "cron(0 23 * * ? *)"
}

variable "stop_timezone" {
  description = "IANA timezone for stop_cron."
  type        = string
  default     = "Africa/Cairo"
}

variable "budget_limit_usd" {
  description = "ADR-0019 operating target (not a hard cap, F-053)."
  type        = string
  default     = "13"
}

variable "budget_email" {
  description = "Budget alert address; set only in the gitignored terraform.tfvars."
  type        = string
  sensitive   = true
  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[^@\\s]+$", var.budget_email))
    error_message = "budget_email must be an email address."
  }
}

variable "cognito_callback_port" {
  description = "Local loopback port of the operator PKCE login helper (#97)."
  type        = number
  default     = 8765
}

variable "secrets_version" {
  description = "Bump to rotate the generated SecureString values (value_wo_version)."
  type        = number
  default     = 1
}

variable "engine_settings" {
  description = "Extra non-secret engine settings (for example GATE_*). Empty keeps engine defaults, like the compose file."
  type        = map(string)
  default     = {}
}

variable "breaker_settings" {
  description = "Circuit breaker numbers from ADR-0023 (same defaults as deploy/full-stack-compose.yml)."
  type        = map(string)
  default = {
    BREAKER_ENABLED                  = "YES"
    BREAKER_WINDOW_MINUTES           = "15"
    BREAKER_MIN_SAMPLE               = "20"
    BREAKER_MAX_FAILURE_RATE_PERCENT = "10"
    BREAKER_MAX_P95_SECONDS          = "15"
  }
}
