variable "langfuse_project_id" {
  description = "Langfuse project ID for trace links. Set in terraform.tfvars (gitignored); never in the public repo."
  type        = string
  nullable    = false
  validation {
    condition     = can(regex("^[a-z0-9]{20,40}$", var.langfuse_project_id))
    error_message = "langfuse_project_id must be the Langfuse project ID (lowercase letters and digits)."
  }
}
