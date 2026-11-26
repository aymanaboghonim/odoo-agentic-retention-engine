# Grafana root (#139; ADR-0029; spec docs/specs/observability-telemetry.md).
# Change it only through scripts/tf_safe.sh (saved plan, policy check, --confirm).
# The provider reads GRAFANA_URL and GRAFANA_AUTH (a stack service account token) from the
# environment at apply time, so neither the stack name nor the token is in the repo or state.

terraform {
  required_version = "~> 1.17"
  required_providers {
    grafana = {
      source  = "grafana/grafana"
      version = "= 4.55.0"
    }
  }
  # bucket comes from backend.hcl (the bootstrap state bucket); see backend.hcl.example.
  backend "s3" {
    key          = "grafana/terraform.tfstate"
    region       = "us-east-1"
    encrypt      = true
    use_lockfile = true # F-062
  }
}

provider "grafana" {}
