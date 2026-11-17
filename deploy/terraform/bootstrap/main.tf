# Bootstrap root (#95, spec docs/specs/aws-mvp-deployment.md): applied once with local
# state through scripts/tf_safe.sh. Creates the encrypted, versioned state bucket for the
# mvp root and the GitHub Actions OIDC deploy role (ADR-0019, F-068).

terraform {
  required_version = "~> 1.17"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.73.0"
    }
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project   = var.name_prefix
      Issue     = "88"
      ManagedBy = "terraform-bootstrap"
    }
  }
}

locals {
  arn_prefix = "arn:aws"
  account    = var.account_id
  region     = var.region
  mvp        = var.name_prefix
}

# ---------------------------------------------------------------- state bucket

resource "aws_s3_bucket" "state" {
  bucket_prefix = "${var.name_prefix}-tfstate-"
  # Emptied and deleted only by scripts/aws_mvp_cleanup.sh --include-bootstrap (#96).
  force_destroy = false
}

resource "aws_s3_bucket_ownership_controls" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "state" {
  bucket                  = aws_s3_bucket.state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_versioning" "state" {
  bucket = aws_s3_bucket.state.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "state" {
  bucket = aws_s3_bucket.state.id
  rule {
    id     = "expire-old-state-versions"
    status = "Enabled"
    filter {}
    noncurrent_version_expiration {
      noncurrent_days = 90
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

data "aws_iam_policy_document" "state_tls_only" {
  statement {
    sid       = "DenyInsecureTransport"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.state.arn, "${aws_s3_bucket.state.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "state" {
  bucket = aws_s3_bucket.state.id
  policy = data.aws_iam_policy_document.state_tls_only.json
}

# ---------------------------------------------------------------- GitHub OIDC deploy role

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # F-068: AWS validates GitHub with its own trusted root CAs; no thumbprint is used.
}

data "aws_iam_policy_document" "deploy_trust" {
  statement {
    actions = ["sts:AssumeRoleWithWebIdentity"]
    principals {
      type        = "Federated"
      identifiers = [aws_iam_openid_connect_provider.github.arn]
    }
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }
    # Only the protected `aws-mvp` environment of this repository (owner approves each run).
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:sub"
      values   = ["repo:${var.github_repository}:environment:${var.github_environment}"]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = "${var.name_prefix}-gha-deploy"
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600
}

# Least privilege for .github/workflows/deploy-aws.yml (#97): push images to this project's
# ECR repositories, register task definitions, and update or run this cluster's services.
data "aws_iam_policy_document" "deploy" {
  statement {
    sid       = "EcrAuth"
    actions   = ["ecr:GetAuthorizationToken"]
    resources = ["*"]
  }
  statement {
    sid = "EcrPush"
    actions = [
      "ecr:BatchCheckLayerAvailability",
      "ecr:BatchGetImage",
      "ecr:CompleteLayerUpload",
      "ecr:DescribeImages",
      "ecr:GetDownloadUrlForLayer",
      "ecr:InitiateLayerUpload",
      "ecr:PutImage",
      "ecr:UploadLayerPart",
    ]
    resources = ["${local.arn_prefix}:ecr:${local.region}:${local.account}:repository/${local.mvp}/*"]
  }
  statement {
    sid       = "EcsTaskDefinitions"
    actions   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"]
    resources = ["*"] # these actions do not support resource-level permissions
  }
  statement {
    sid     = "EcsServices"
    actions = ["ecs:DescribeServices", "ecs:UpdateService"]
    resources = [
      "${local.arn_prefix}:ecs:${local.region}:${local.account}:service/${local.mvp}/*",
    ]
  }
  statement {
    sid       = "EcsRunProposal"
    actions   = ["ecs:RunTask", "ecs:DescribeTasks"]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = ["${local.arn_prefix}:ecs:${local.region}:${local.account}:cluster/${local.mvp}"]
    }
  }
  # #97 / ADR-0026: stage the pinned embedding model (F-072) from the workflow, which has
  # IPv4 and datacenter bandwidth. Only models/ in the project's backups bucket.
  statement {
    sid       = "StageModelObjects"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${local.arn_prefix}:s3:::${local.mvp}-backups-*/models/*"]
  }
  statement {
    sid       = "ListModelPrefix"
    actions   = ["s3:ListBucket"]
    resources = ["${local.arn_prefix}:s3:::${local.mvp}-backups-*"]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["models/*"]
    }
  }
  statement {
    sid     = "PassOnlyTaskRoles"
    actions = ["iam:PassRole"]
    resources = [
      "${local.arn_prefix}:iam::${local.account}:role/${local.mvp}-ecs-exec",
      "${local.arn_prefix}:iam::${local.account}:role/${local.mvp}-task",
      "${local.arn_prefix}:iam::${local.account}:role/${local.mvp}-task-model",
    ]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "deploy"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy.json
}
