# No secret values here (INF-5).

output "cloudfront_url" {
  value = "https://${aws_cloudfront_distribution.edge.domain_name}"
}

output "api_base_url" {
  value = "https://${aws_cloudfront_distribution.edge.domain_name}/api"
}

output "instance_id" {
  value = aws_instance.host.id
}

output "cluster_name" {
  value = aws_ecs_cluster.main.name
}

output "oidc_issuer" {
  value = local.oidc_issuer
}

output "cognito_client_id" {
  value = aws_cognito_user_pool_client.operator_cli.id
}

output "cognito_login_domain" {
  value = "https://${aws_cognito_user_pool_domain.operators.domain}.auth.${var.region}.amazoncognito.com"
}

output "ecr_registry" {
  description = "Dual-stack registry name used by the tasks (F-070)."
  value       = local.registry
}

output "backups_bucket" {
  value = aws_s3_bucket.backups.bucket
}

output "ui_bucket" {
  value = aws_s3_bucket.ui.bucket
}
