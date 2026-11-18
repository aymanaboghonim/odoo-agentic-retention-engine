# SecureString parameters injected into task definitions (ADR-0019). INF-5: generated values
# come from ephemeral passwords through write-only value_wo, so they never enter state or
# plans. External secrets start as a placeholder and are set by scripts/aws_mvp_secrets.sh
# (#96); Terraform does not track value_wo, so that is not drift.

locals {
  parameter_prefix = "/${var.name_prefix}"
  # Host port for the Odoo UI over SSM port forwarding only (no security group rule).
  host_ports = {
    odoo = 18069
  }
}

ephemeral "random_password" "odoo_db" {
  length  = 40
  special = false
}

ephemeral "random_password" "odoo_ro" {
  length  = 40
  special = false
}

ephemeral "random_password" "engine_db" {
  length  = 40
  special = false
}

ephemeral "random_password" "pepper" {
  length  = 64
  special = false
}

# INF-5 exception (spec): CloudFront keeps this custom header value in the distribution
# config, so it is in state no matter how it is set. Defense in depth behind the VPC origin.
resource "random_password" "origin_verify" {
  length  = 48
  special = false
  keepers = { version = var.secrets_version }
}

locals {
  # Ephemeral local: usable only in write-only arguments, never stored.
  generated_values = {
    "odoo-db-password"         = ephemeral.random_password.odoo_db.result
    "odoo-ro-password"         = ephemeral.random_password.odoo_ro.result
    "engine-db-password"       = ephemeral.random_password.engine_db.result
    "engine-database-url"      = "postgresql://engine:${ephemeral.random_password.engine_db.result}@engine-db:5432/engine"
    "odoo-readonly-url"        = "postgresql://odoo_ro:${ephemeral.random_password.odoo_ro.result}@odoo-db:5432/odoo"
    "privacy-tombstone-pepper" = ephemeral.random_password.pepper.result
    "approval-origin-verify"   = random_password.origin_verify.result
  }
  generated_names = toset([
    "odoo-db-password", "odoo-ro-password", "engine-db-password", "engine-database-url",
    "odoo-readonly-url",
    "privacy-tombstone-pepper", "approval-origin-verify",
  ])
}

resource "aws_ssm_parameter" "generated" {
  for_each         = local.generated_names
  name             = "${local.parameter_prefix}/${each.key}"
  type             = "SecureString"
  value_wo         = local.generated_values[each.key]
  value_wo_version = var.secrets_version
}

resource "aws_ssm_parameter" "external" {
  for_each = toset(["openai-api-key", "telegram-bot-token", "telegram-chat-id", "odoo-api-key"])
  name     = "${local.parameter_prefix}/${each.key}"
  type     = "SecureString"
  # Placeholder only; scripts/aws_mvp_secrets.sh (#96) writes the real value outside Terraform.
  value_wo         = "unset"
  value_wo_version = 1
}
