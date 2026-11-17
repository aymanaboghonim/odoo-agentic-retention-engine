# ECR is the only runtime registry (ADR-0019). Tasks pull through the dual-stack registry
# name because the host has IPv6-only egress (ADR-0024, F-070).

locals {
  # Single source for pinned third-party images (also read by scripts/aws_mvp_mirror_images.sh).
  third_party      = jsondecode(file("${path.module}/../../aws-mvp-images.json"))
  ecr_repositories = toset(["engine", "postgres", "odoo", "pgvector", "mailpit", "aws-cli"])
  registry         = "${var.account_id}.dkr-ecr.${var.region}.on.aws"
  images = {
    engine   = "${local.registry}/${var.name_prefix}/engine:${var.engine_image_tag}"
    postgres = "${local.registry}/${var.name_prefix}/postgres@${local.third_party["postgres"].digest}"
    odoo     = "${local.registry}/${var.name_prefix}/odoo@${local.third_party["odoo"].digest}"
    pgvector = "${local.registry}/${var.name_prefix}/pgvector@${local.third_party["pgvector"].digest}"
    mailpit  = "${local.registry}/${var.name_prefix}/mailpit@${local.third_party["mailpit"].digest}"
    aws_cli  = "${local.registry}/${var.name_prefix}/aws-cli@${local.third_party["aws-cli"].digest}"
  }
}

resource "aws_ecr_repository" "repo" {
  for_each             = local.ecr_repositories
  name                 = "${var.name_prefix}/${each.key}"
  image_tag_mutability = "IMMUTABLE"
  # Teardown goes through scripts/aws_mvp_cleanup.sh (#96); images are rebuilt from git.
  force_delete = true
  image_scanning_configuration {
    scan_on_push = true
  }
  encryption_configuration {
    encryption_type = "AES256"
  }
}

resource "aws_ecr_lifecycle_policy" "repo" {
  for_each   = aws_ecr_repository.repo
  repository = each.value.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Keep the 5 newest images (storage is billed per GB-month, F-052)"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 5
      }
      action = { type = "expire" }
    }]
  })
}
