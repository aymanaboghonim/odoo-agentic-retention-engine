locals {
  # The dashboard JSON is plain Grafana JSON; only this placeholder is replaced, so Grafana's
  # own ${...} variables stay untouched (templatefile would read them as Terraform syntax).
  langfuse_trace_base = "https://us.cloud.langfuse.com/project/${var.langfuse_project_id}/traces/"
}

resource "grafana_folder" "aorte" {
  uid   = "aorte"
  title = "aorte engine"
}

resource "grafana_dashboard" "overview" {
  folder = grafana_folder.aorte.uid
  config_json = replace(
    file("${path.module}/dashboards/aorte-overview.json"),
    "__LANGFUSE_TRACE_BASE__",
    local.langfuse_trace_base
  )
}
