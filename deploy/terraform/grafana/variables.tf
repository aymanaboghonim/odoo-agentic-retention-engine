variable "langfuse_project_id" {
  description = "Langfuse project ID for trace links. Set in terraform.tfvars (gitignored); never in the public repo."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z0-9]{20,40}$", var.langfuse_project_id))
    error_message = "langfuse_project_id must be the Langfuse project ID (lowercase letters and digits)."
  }
}

variable "alert_email" {
  description = "Owner email for alert notifications. Set in terraform.tfvars (gitignored); never in the public repo."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[^@\\s]+@[^@\\s]+\\.[a-z]{2,}$", var.alert_email))
    error_message = "alert_email must be an email address."
  }
}

variable "scan_interval_seconds" {
  description = "The worker scan interval on AWS (FULL_STACK_SCAN_INTERVAL_SECONDS, default 60)."
  type        = number
  default     = 60
}

variable "alert_test_mode" {
  description = "Only for the #133 fire tests: held effects and drops fire on any data. Keep false."
  type        = bool
  default     = false
}
