# Operator identity (#86 spec, ADR-0019; F-066): Lite plan, admin-created users only,
# TOTP MFA required, groups viewer/approver/admin, PKCE public client for a loopback helper.

resource "aws_cognito_user_pool" "operators" {
  name                     = "${var.name_prefix}-operators"
  user_pool_tier           = "LITE"
  deletion_protection      = var.cognito_deletion_protection
  mfa_configuration        = "ON"
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  software_token_mfa_configuration {
    enabled = true
  }

  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  password_policy {
    minimum_length                   = 14
    require_lowercase                = true
    require_uppercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 3
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }
}

resource "aws_cognito_user_group" "role" {
  for_each     = toset(["viewer", "approver", "admin"])
  name         = each.key
  user_pool_id = aws_cognito_user_pool.operators.id
  description  = "Operator role ${each.key} (#86 RBAC)"
}

resource "random_id" "domain" {
  byte_length = 4
}

# Cognito prefix domain (no Route 53, ADR-0019); classic hosted UI on the Lite plan (F-066).
resource "aws_cognito_user_pool_domain" "operators" {
  domain                = "${var.name_prefix}-${random_id.domain.hex}"
  user_pool_id          = aws_cognito_user_pool.operators.id
  managed_login_version = 1
}

resource "aws_cognito_user_pool_client" "operator_cli" {
  name                                 = "${var.name_prefix}-operator-cli"
  user_pool_id                         = aws_cognito_user_pool.operators.id
  generate_secret                      = false # public client, authorization code + PKCE
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = ["openid", "email"]
  supported_identity_providers         = ["COGNITO"]
  callback_urls                        = ["http://localhost:${var.cognito_callback_port}/callback"]
  logout_urls                          = ["http://localhost:${var.cognito_callback_port}/logout"]
  explicit_auth_flows                  = ["ALLOW_REFRESH_TOKEN_AUTH"]
  prevent_user_existence_errors        = "ENABLED"
  enable_token_revocation              = true

  # Short access tokens limit a leaked token (ADR-0024 keeps the plain hop inside AWS anyway).
  access_token_validity  = 15
  id_token_validity      = 15
  refresh_token_validity = 12
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "hours"
  }
}
