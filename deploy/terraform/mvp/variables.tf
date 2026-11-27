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

variable "telemetry_settings" {
  description = "Non-secret telemetry settings (#126, #127, ADR-0029). The token comes from SSM (grafana-otlp-token)."
  type        = map(string)
  default = {
    TELEMETRY_ENABLED     = "YES"
    TELEMETRY_ENVIRONMENT = "aws-mvp"
    # F-079: AAAA verified 2026-11-20; the host has IPv6-only egress (ADR-0024).
    GRAFANA_OTLP_ENDPOINT = "https://otlp-gateway-prod-us-east-2.grafana.net/otlp"
    # Spans plus log records per process per UTC day (three engine processes).
    TELEMETRY_DAILY_SPAN_CAP = "20000"
    # #134: engine APM (FastAPI, psycopg, urllib) under its own cap per process per UTC day.
    TELEMETRY_APM                = "YES"
    TELEMETRY_DAILY_APM_SPAN_CAP = "30000"
  }
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

variable "reconcile_settings" {
  description = "Held-effect reconciliation (#131, ADR-0030). Required by the worker, the approval API, and the operator CLI; no engine default."
  type        = map(string)
  default = {
    # A `started` effect older than this is crash-left and counts as held (spec: Definitions).
    RECONCILE_STALE_STARTED_MINUTES = "30"
    # #132 (owner, 2026-11-27): the worker stage runs every 10 minutes and closes a run still
    # unverified after 24 hours as failed (reconciled_unverified); never a resend.
    RECONCILE_STAGE            = "YES"
    RECONCILE_INTERVAL_MINUTES = "10"
    RECONCILE_DEADLINE_HOURS   = "24"
  }
}

variable "langfuse_host" {
  description = "Langfuse Cloud region host behind the /lf-otel/* relay (#129, ADR-0029: US)."
  type        = string
  default     = "us.cloud.langfuse.com"
  validation {
    condition     = can(regex("^[a-z0-9.-]+\\.langfuse\\.com$", var.langfuse_host))
    error_message = "langfuse_host must be a langfuse.com host name."
  }
}

variable "langfuse_daily_unit_cap" {
  description = "Langfuse spans per engine process per UTC day (#150). Hobby includes 50k units a month (F-010): 3 processes x 300 x 30 days = 27k at most, which leaves room for the evaluation runs (#135)."
  type        = number
  default     = 300
  validation {
    condition     = var.langfuse_daily_unit_cap >= 1 && var.langfuse_daily_unit_cap <= 500
    error_message = "langfuse_daily_unit_cap must be 1 to 500 (3 x 500 x 30 stays under the 50k Hobby units)."
  }
}

variable "alloy_memory_mib" {
  description = "Hard memory limit of the Alloy container (ADR-0033, OBS-14), from the local measurement in F-098."
  type        = number
  default     = 128
  validation {
    condition     = var.alloy_memory_mib >= 64 && var.alloy_memory_mib <= 256
    error_message = "alloy_memory_mib must be 64..256 MiB (t3a.medium has 4 GiB for the whole task)."
  }
}
