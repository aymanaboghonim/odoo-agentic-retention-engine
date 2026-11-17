# Cost guardrails (ADR-0019 decision 3): a monthly budget with email alerts (free, F-067) and
# the nightly stop (INF-6). Budgets lag hours behind spend (F-053); the stop is the real limit.

resource "aws_budgets_budget" "monthly" {
  name         = "${var.name_prefix}-monthly"
  budget_type  = "COST"
  limit_amount = var.budget_limit_usd
  limit_unit   = "USD"
  time_unit    = "MONTHLY"

  dynamic "notification" {
    for_each = { actual_50 = ["ACTUAL", 50], actual_80 = ["ACTUAL", 80], actual_100 = ["ACTUAL", 100], forecast_100 = ["FORECASTED", 100] }
    content {
      comparison_operator        = "GREATER_THAN"
      notification_type          = notification.value[0]
      threshold                  = notification.value[1]
      threshold_type             = "PERCENTAGE"
      subscriber_email_addresses = [var.budget_email]
    }
  }
}

data "aws_iam_policy_document" "scheduler_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["scheduler.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "scheduler" {
  name               = "${var.name_prefix}-scheduler"
  assume_role_policy = data.aws_iam_policy_document.scheduler_trust.json
}

data "aws_iam_policy_document" "scheduler_stop" {
  statement {
    actions   = ["ec2:StopInstances"]
    resources = [aws_instance.host.arn]
  }
}

resource "aws_iam_role_policy" "scheduler_stop" {
  name   = "stop-host"
  role   = aws_iam_role.scheduler.id
  policy = data.aws_iam_policy_document.scheduler_stop.json
}

# INF-6: stop every night (owner: 23:00 Africa/Cairo). Start is manual (#96 aws_mvp_start.sh).
resource "aws_scheduler_schedule" "nightly_stop" {
  name                         = "${var.name_prefix}-nightly-stop"
  schedule_expression          = var.stop_cron
  schedule_expression_timezone = var.stop_timezone
  state                        = "ENABLED"

  flexible_time_window {
    mode = "OFF"
  }

  target {
    arn      = "arn:aws:scheduler:::aws-sdk:ec2:stopInstances"
    role_arn = aws_iam_role.scheduler.arn
    input    = jsonencode({ InstanceIds = [aws_instance.host.id] })
    retry_policy {
      maximum_retry_attempts = 3
    }
  }
}
