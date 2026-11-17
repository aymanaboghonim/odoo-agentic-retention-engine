# ECR is the only runtime registry (ADR-0019). Tasks pull through the dual-stack registry
# name because the host has IPv6-only egress (ADR-0024, F-070).

locals {
  ecr_repositories = toset(["engine", "postgres", "odoo", "pgvector", "mailpit"])
  registry         = "${var.account_id}.dkr-ecr.${var.region}.on.aws"
  images = {
    engine   = "${local.registry}/${var.name_prefix}/engine:${var.engine_image_tag}"
    postgres = "${local.registry}/${var.name_prefix}/postgres@${var.third_party_image_digests["postgres"]}"
    odoo     = "${local.registry}/${var.name_prefix}/odoo@${var.third_party_image_digests["odoo"]}"
    pgvector = "${local.registry}/${var.name_prefix}/pgvector@${var.third_party_image_digests["pgvector"]}"
    mailpit  = "${local.registry}/${var.name_prefix}/mailpit@${var.third_party_image_digests["mailpit"]}"
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
