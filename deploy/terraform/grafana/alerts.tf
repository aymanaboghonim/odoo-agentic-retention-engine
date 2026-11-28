# Alert rules (#163; spec docs/specs/observability-telemetry.md, OBS-19).
# Quiet while the stack is stopped (ADR-0026): every rule treats no data as OK, and "worker
# missing" fires only while another engine process still reports (peer presence).

locals {
  env = "deployment_environment_name=\"aws-mvp\""
  # Test mode (#133 fire tests only): held effects and drops fire on any data, then revert.
  test_floor = var.alert_test_mode ? -1 : 0
  rules = {
    worker_scan_stale = {
      title   = "Worker scan is stale"
      summary = "The worker has not finished a scan page for more than 3 scan intervals (180 s)."
      expr    = "max(aorte_worker_last_scan_age_seconds{${local.env}})"
      above   = 3 * var.scan_interval_seconds
      for     = "2m"
    }
    worker_missing = {
      title   = "Worker reports no data while the API or host runs"
      summary = "The approval API or the Alloy host metrics report but the worker does not; a stopped stack reports nothing and stays quiet."
      # Peers (#163, ADR-0033 decision 5): the approval API, or Alloy's host load.
      expr  = "(count(aorte_process_up_ratio{service_name=\"aorte-approval-api\",${local.env}}) or count(node_load1{service_name=\"aorte-alloy\",${local.env}})) unless on() count(aorte_worker_last_scan_age_seconds{${local.env}})"
      above = 0
      for   = "5m"
    }
    held_effects = {
      title   = "Held effects above 0"
      summary = "An effect has an unknown outcome; reconciliation (ADR-0030) resolves it."
      expr    = "max(aorte_execution_held{${local.env}})"
      above   = local.test_floor
      for     = "1m"
    }
    runtime_not_active = {
      title   = "Runtime state is not active"
      summary = "The engine is in fallback_fixed_rule or halted (an unreadable state counts as halted)."
      expr    = "count(aorte_runtime_state_ratio{aorte_runtime_state!=\"active\",${local.env}} == 1)"
      above   = 0
      for     = "1m"
    }
    reconcile_unverified = {
      title   = "Held run closed unverified"
      summary = "The reconcile stage closed a held run at the deadline without evidence (#132, ADR-0030): the coupon stays active and nothing was resent; check the run."
      expr    = "sum(increase({__name__=~\"aorte_reconcile_resolutions.*_total\",aorte_resolution=\"unverified\",${local.env}}[1h]))"
      above   = local.test_floor
      for     = "0s"
    }
    # #130, BR-1: the daily billing job pushes the latest finished day as current gauges
    # (Grafana takes samples at most 2 h old, F-102); 50 h covers one late or missed run.
    billing_drift = {
      title   = "Provider billing drift above USD 0.10"
      summary = "OpenAI's daily cost differs from the engine's estimate plus the merged evaluation reports by more than USD 0.10; see reports/billing/ in the backups bucket."
      expr    = "max(abs(last_over_time(aorte_billing_drift_usd{${local.env}}[50h])))"
      above   = 0.1
      for     = "0s"
    }
    billing_unknown = {
      title   = "Provider billing day unknown"
      summary = "The billing job could not get the provider cost or the engine estimate for its day; an unknown day is never read as 0 (BR-1)."
      expr    = "1 - max(last_over_time(aorte_billing_known{${local.env}}[50h]))"
      above   = 0
      for     = "0s"
    }
    telemetry_cap = {
      title   = "Telemetry dropped at the daily cap"
      summary = "An export cap was reached; telemetry stops until the next UTC day (OBS-13)."
      expr    = "sum(increase({__name__=~\"aorte_telemetry_dropped.*_total\",aorte_reason=\"daily_cap\",${local.env}}[30m]))"
      above   = local.test_floor
      for     = "0s"
    }
  }
}

resource "grafana_contact_point" "email" {
  name = "aorte-owner-email"
  email {
    addresses    = [var.alert_email]
    single_email = true
  }
}

resource "grafana_rule_group" "engine" {
  name             = "aorte-engine"
  folder_uid       = grafana_folder.aorte.uid
  interval_seconds = 60

  dynamic "rule" {
    for_each = local.rules
    content {
      name           = rule.value.title
      condition      = "B"
      for            = rule.value.for
      no_data_state  = "OK"
      exec_err_state = "Error"
      labels         = { service = "aorte", rule = rule.key }
      annotations    = { summary = rule.value.summary, runbook = "docs/runbooks/grafana.md" }
      notification_settings {
        contact_point = grafana_contact_point.email.name
      }
      data {
        ref_id         = "A"
        datasource_uid = "grafanacloud-prom"
        relative_time_range {
          from = 600
          to   = 0
        }
        model = jsonencode({ expr = rule.value.expr, refId = "A", instant = true })
      }
      data {
        ref_id         = "B"
        datasource_uid = "__expr__"
        relative_time_range {
          from = 0
          to   = 0
        }
        model = jsonencode({
          type       = "threshold"
          expression = "A"
          refId      = "B"
          conditions = [{ evaluator = { type = "gt", params = [rule.value.above] } }]
        })
      }
    }
  }
}
