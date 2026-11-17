# MVP root (#95; ADR-0019, ADR-0020, ADR-0024; spec docs/specs/aws-mvp-deployment.md).
# Change it only through scripts/tf_safe.sh (saved plan, policy check, --confirm).

terraform {
  required_version = "~> 1.17"
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "= 6.73.0"
    }
    random = {
      source  = "hashicorp/random"
      version = "= 3.9.1"
    }
  }
  # bucket comes from backend.hcl (output of the bootstrap root); see backend.hcl.example.
  backend "s3" {
    key          = "mvp/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # F-062: S3-native locking; DynamoDB locking is deprecated
  }
}

provider "aws" {
  region              = var.region
  allowed_account_ids = [var.account_id]
  default_tags {
    tags = {
      Project   = var.name_prefix
      Issue     = "88"
      ManagedBy = "terraform"
    }
  }
}
