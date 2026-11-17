output "state_bucket" {
  description = "Put this in deploy/terraform/mvp/backend.hcl as bucket = \"...\"."
  value       = aws_s3_bucket.state.bucket
}

output "deploy_role_arn" {
  description = "Set as the AWS_DEPLOY_ROLE_ARN variable of the GitHub aws-mvp environment (#97)."
  value       = aws_iam_role.deploy.arn
}
