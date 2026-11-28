output "state_bucket" {
  description = "Put this in deploy/terraform/mvp/backend.hcl as bucket = \"...\"."
  value       = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN secret (not a variable: variables print in public logs) of the GitHub aws-mvp environment (#97, #142)."
  value       = aws_iam_role.deploy.arn
}

output "billing_role_arn" {
  description = "Set as the AWS_BILLING_ROLE_ARN secret of the GitHub billing environment (#130)."
  value       = aws_iam_role.billing.arn
}
