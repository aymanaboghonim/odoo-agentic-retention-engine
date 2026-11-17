variable "account_id" {
  description = "AWS account id; pinned in allowed_account_ids so a plan cannot target another account."
  type        = string
  validation {
    condition     = can(regex("^[0-9]{12}$", var.account_id))
    error_message = "account_id must be 12 digits."
  }
}

variable "region" {
  description = "AWS region (facts F-050..F-070 are for us-east-1)."
  type        = string
  default     = "us-east-1"
}

variable "name_prefix" {
  description = "Prefix for every name and the Project tag; the mvp root must use the same value."
  type        = string
  default     = "aorte-mvp"
}

variable "github_repository" {
  description = "owner/repo allowed to assume the deploy role."
  type        = string
  default     = "aymanaboghonim/odoo-agentic-retention-engine"
}

variable "github_owner_id" {
  description = "Numeric GitHub owner id (public; immutable subject claims, F-068)."
  type        = string
  default     = "62246708"
}

variable "github_repository_id" {
  description = "Numeric GitHub repository id (public; immutable subject claims, F-068)."
  type        = string
  default     = "1423559255"
}

variable "github_environment" {
  description = "GitHub environment that gates deploys (owner is a required reviewer)."
  type        = string
  default     = "aws-mvp"
}
