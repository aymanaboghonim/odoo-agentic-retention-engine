# ADR-0020 decision 4: private encrypted bucket for DB dumps, evaluation reports, and audit
# exports. Objects expire so forgotten dumps do not keep billing (ADR-0019 risks).

resource "aws_s3_bucket" "backups" {
  bucket_prefix = "${var.name_prefix}-backups-"
  force_destroy = false # emptied deliberately by scripts/aws_mvp_cleanup.sh (#96)
}

resource "aws_s3_bucket_ownership_controls" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_public_access_block" "backups" {
  bucket                  = aws_s3_bucket.backups.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_server_side_encryption_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "backups" {
  bucket = aws_s3_bucket.backups.id
  rule {
    # Only backups/ expires; models/ holds the pinned embedding model (#97, F-072).
    id     = "expire-backups"
    status = "Enabled"
    filter {
      prefix = "backups/"
    }
    expiration {
      days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}
