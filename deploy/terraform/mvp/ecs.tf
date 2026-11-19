# ECS layout (ADR-0024 decision 4). Container settings mirror deploy/full-stack-compose.yml,
# which stays the source of truth for service order, environment, and image digests.
# The deploy workflow (#97) registers newer revisions, so the service ignores task_definition.

resource "aws_ecs_cluster" "main" {
  name = var.name_prefix
  setting {
    name  = "containerInsights"
    value = "disabled" # cost: CloudWatch metrics are billed; Grafana Cloud is Tier 2 (ADR-0020)
  }
}

resource "aws_cloudwatch_log_group" "ecs" {
  name              = "/${var.name_prefix}/ecs"
  retention_in_days = var.log_retention_days
}

locals {
  param_arn = "arn:aws:ssm:${var.region}:${var.account_id}:parameter${local.parameter_prefix}"

  log_config = {
    logDriver = "awslogs"
    options = {
      "awslogs-group"         = aws_cloudwatch_log_group.ecs.name
      "awslogs-region"        = var.region
      "awslogs-stream-prefix" = "core"
    }
  }

  # OIDC/JWKS from Cognito (#86 spec, F-058, F-066): access tokens carry cognito:groups.
  # F-072: models/hub/... in the backups bucket mirrors the Hugging Face cache layout that
  # engine.retrieval reads offline; manifest.sha256 is written by scripts/aws_mvp_model_upload.py.
  model_snapshot    = "hub/models--Qdrant--multilingual-e5-large-onnx/snapshots/ac6781cd1cf88b8306a536d7c9d18a5bd57cc14b"
  model_sync_script = "set -eu; aws s3 sync --only-show-errors s3://${aws_s3_bucket.backups.bucket}/models/hub/ /model-cache/hub/ && cd /model-cache/${local.model_snapshot} && sha256sum -c --quiet manifest.sha256 && echo model-sync ok"

  oidc_issuer = "https://cognito-idp.${var.region}.amazonaws.com/${aws_cognito_user_pool.operators.id}"

  common_engine_env = merge(var.breaker_settings, var.telemetry_settings, var.engine_settings, {
    FULL_STACK_SYNTHETIC_ISOLATED = "YES"
    # The engine requires a UUID (fails closed otherwise, #97). Deterministic per stack name.
    FULL_STACK_INSTANCE_ID = uuidv5("url", "https://github.com/aymanaboghonim/odoo-agentic-retention-engine/${var.name_prefix}")
  })

  env = { for k, v in local.common_engine_env : k => v }

  pg_health = {
    odoo   = ["CMD-SHELL", "pg_isready -U odoo -d postgres"]
    engine = ["CMD-SHELL", "pg_isready -U engine -d engine"]
  }

  core_containers = [
    {
      name              = "odoo-db"
      image             = local.images.postgres
      essential         = true
      memoryReservation = 256
      stopTimeout       = 60
      environment       = [{ name = "POSTGRES_DB", value = "postgres" }, { name = "POSTGRES_USER", value = "odoo" }]
      secrets           = [{ name = "POSTGRES_PASSWORD", valueFrom = "${local.param_arn}/odoo-db-password" }]
      mountPoints       = [{ sourceVolume = "odoo-db", containerPath = "/var/lib/postgresql/data" }]
      healthCheck       = { command = local.pg_health.odoo, interval = 10, timeout = 5, retries = 10, startPeriod = 30 }
      logConfiguration  = local.log_config
    },
    {
      name              = "odoo"
      image             = local.images.odoo
      essential         = true
      memoryReservation = 768
      environment       = [{ name = "HOST", value = "odoo-db" }, { name = "USER", value = "odoo" }]
      secrets           = [{ name = "PASSWORD", valueFrom = "${local.param_arn}/odoo-db-password" }]
      # ADR-0019 decision 5 / F-042: website_sale_loyalty is installed with the demo baseline.
      command          = ["odoo", "--database=odoo", "--init=website_sale,website_sale_loyalty", "--with-demo", "--max-cron-threads=0"]
      links            = ["odoo-db:odoo-db"]
      portMappings     = [{ containerPort = 8069, hostPort = local.host_ports.odoo, protocol = "tcp" }]
      mountPoints      = [{ sourceVolume = "odoo-data", containerPath = "/var/lib/odoo" }]
      dependsOn        = [{ containerName = "odoo-db", condition = "HEALTHY" }]
      logConfiguration = local.log_config
    },
    {
      name              = "engine-db"
      image             = local.images.pgvector
      essential         = true
      memoryReservation = 256
      stopTimeout       = 60
      environment       = [{ name = "POSTGRES_DB", value = "engine" }, { name = "POSTGRES_USER", value = "engine" }]
      secrets           = [{ name = "POSTGRES_PASSWORD", valueFrom = "${local.param_arn}/engine-db-password" }]
      mountPoints       = [{ sourceVolume = "engine-db", containerPath = "/var/lib/postgresql/data" }]
      healthCheck       = { command = local.pg_health.engine, interval = 10, timeout = 5, retries = 10, startPeriod = 30 }
      logConfiguration  = local.log_config
    },
    {
      name              = "initialize"
      image             = local.images.engine
      essential         = false
      memoryReservation = 256
      command           = ["python", "-m", "engine.full_stack_init"]
      environment       = [{ name = "FULL_STACK_SYNTHETIC_ISOLATED", value = "YES" }]
      secrets = [
        { name = "FULL_STACK_ODOO_DB_PASSWORD", valueFrom = "${local.param_arn}/odoo-db-password" },
        { name = "FULL_STACK_ODOO_RO_PASSWORD", valueFrom = "${local.param_arn}/odoo-ro-password" },
        { name = "ENGINE_DATABASE_URL", valueFrom = "${local.param_arn}/engine-database-url" },
      ]
      links = ["odoo-db:odoo-db", "odoo:odoo", "engine-db:engine-db"]
      dependsOn = [
        { containerName = "odoo-db", condition = "HEALTHY" },
        { containerName = "odoo", condition = "START" },
        { containerName = "engine-db", condition = "HEALTHY" },
      ]
      logConfiguration = local.log_config
    },
    {
      name              = "worker"
      image             = local.images.engine
      essential         = true
      memoryReservation = 512
      command           = ["python", "-m", "engine.full_stack_worker"]
      environment       = [for k, v in local.env : { name = k, value = v }]
      secrets = [
        { name = "ENGINE_DATABASE_URL", valueFrom = "${local.param_arn}/engine-database-url" },
        { name = "ODOO_READONLY_DATABASE_URL", valueFrom = "${local.param_arn}/odoo-readonly-url" },
        { name = "PRIVACY_TOMBSTONE_PEPPER", valueFrom = "${local.param_arn}/privacy-tombstone-pepper" },
      ]
      links            = ["odoo-db:odoo-db", "engine-db:engine-db"]
      dependsOn        = [{ containerName = "initialize", condition = "SUCCESS" }]
      logConfiguration = local.log_config
    },
    {
      name              = "approval-api"
      image             = local.images.engine
      essential         = true
      memoryReservation = 256
      command           = ["python", "-m", "engine.approval_api"]
      # #126: loopback-only liveness (DB reachable). Unhealthy for 3 checks replaces the task.
      healthCheck = {
        command     = ["CMD", "python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=3)"]
        interval    = 30
        timeout     = 5
        retries     = 3
        startPeriod = 60
      }
      environment = [
        { name = "OIDC_ISSUER", value = local.oidc_issuer },
        { name = "OIDC_JWKS_URL", value = "${local.oidc_issuer}/.well-known/jwks.json" },
        { name = "OIDC_CLIENT_ID", value = aws_cognito_user_pool_client.operator_cli.id },
        { name = "OIDC_ROLES_CLAIM", value = "cognito:groups" },
        { name = "APPROVAL_API_HOST", value = "0.0.0.0" },
        { name = "APPROVAL_API_PORT", value = "8080" },
        { name = "APPROVAL_API_ROOT_PATH", value = "/api" },
        { name = "TELEMETRY_ENABLED", value = var.telemetry_settings["TELEMETRY_ENABLED"] },
      ]
      secrets = [
        { name = "ENGINE_DATABASE_URL", valueFrom = "${local.param_arn}/engine-database-url" },
        { name = "APPROVAL_ORIGIN_VERIFY_SECRET", valueFrom = "${local.param_arn}/approval-origin-verify" },
      ]
      links            = ["engine-db:engine-db"]
      portMappings     = [{ containerPort = 8080, hostPort = var.api_port, protocol = "tcp" }]
      dependsOn        = [{ containerName = "initialize", condition = "SUCCESS" }]
      logConfiguration = local.log_config
    },
    {
      # Synthetic delivery only; reached through SSM port forwarding (ADR-0024).
      name              = "mailpit"
      image             = local.images.mailpit
      essential         = false
      memoryReservation = 64
      # UI only, for SSM port forwarding. SMTP stays inside the task (operator link).
      portMappings     = [{ containerPort = 8025, hostPort = 18025, protocol = "tcp" }]
      logConfiguration = local.log_config
    },
    {
      # F-072: copy the pinned embedding model from S3 (dual-stack) and verify its hashes.
      name              = "model-sync"
      image             = local.images.aws_cli
      essential         = false
      memoryReservation = 128
      entryPoint        = ["sh", "-c"]
      command           = [local.model_sync_script]
      environment = [
        { name = "AWS_USE_DUALSTACK_ENDPOINT", value = "true" },
        { name = "AWS_REGION", value = var.region },
      ]
      mountPoints      = [{ sourceVolume = "model-cache", containerPath = "/model-cache", readOnly = false }]
      logConfiguration = local.log_config
    },
    {
      # ADR-0027: one-off operator commands (fixtures, policy ingest, worker --once, erp_cli)
      # run here with `docker exec` over SSM, inside the task network like the Compose
      # `proposal` service, so the engine's network guard (engine-db/odoo-db:5432) holds.
      name              = "operator"
      image             = local.images.engine
      essential         = false
      memoryReservation = 256
      command           = ["sleep", "infinity"]
      environment = concat(
        [for k, v in local.env : { name = k, value = v }],
        [
          { name = "ODOO_URL", value = "http://odoo:8069" },
          { name = "ODOO_DB_NAME", value = "odoo" },
          { name = "ODOO_ALLOWED_HOSTS", value = "odoo" },
          { name = "ERP_MAILPIT_SMTP_HOST", value = "mailpit" },
          { name = "HF_HOME", value = "/model-cache" },
          { name = "HF_HUB_OFFLINE", value = "1" },
        ],
      )
      secrets = [
        { name = "ENGINE_DATABASE_URL", valueFrom = "${local.param_arn}/engine-database-url" },
        { name = "ODOO_READONLY_DATABASE_URL", valueFrom = "${local.param_arn}/odoo-readonly-url" },
        { name = "PRIVACY_TOMBSTONE_PEPPER", valueFrom = "${local.param_arn}/privacy-tombstone-pepper" },
        { name = "OPENAI_API_KEY", valueFrom = "${local.param_arn}/openai-api-key" },
        { name = "TELEGRAM_BOT_TOKEN", valueFrom = "${local.param_arn}/telegram-bot-token" },
        { name = "ODOO_API_KEY", valueFrom = "${local.param_arn}/odoo-api-key" },
        { name = "ERP_TELEGRAM_CHAT_ID", valueFrom = "${local.param_arn}/telegram-chat-id" },
        { name = "TELEGRAM_ALLOWLIST_CHAT_IDS", valueFrom = "${local.param_arn}/telegram-chat-id" },
      ]
      links       = ["engine-db:engine-db", "odoo-db:odoo-db", "odoo:odoo", "mailpit:mailpit"]
      mountPoints = [{ sourceVolume = "model-cache", containerPath = "/model-cache", readOnly = true }]
      dependsOn = [
        { containerName = "initialize", condition = "SUCCESS" },
        { containerName = "model-sync", condition = "SUCCESS" },
      ]
      logConfiguration = local.log_config
    },
  ]
}

resource "aws_ecs_task_definition" "core" {
  family                   = "${var.name_prefix}-core"
  network_mode             = "bridge"
  requires_compatibilities = ["EC2"]
  execution_role_arn       = aws_iam_role.ecs_exec.arn
  # Read-only access to models/ in the backups bucket, for model-sync (F-072).
  task_role_arn         = aws_iam_role.task_model.arn
  container_definitions = jsonencode(local.core_containers)

  dynamic "volume" {
    for_each = toset(["odoo-db", "odoo-data", "engine-db", "model-cache"])
    content {
      name = volume.key
      docker_volume_configuration {
        scope         = "shared" # survives task restarts and host stop/start (root gp3)
        autoprovision = true
        driver        = "local"
      }
    }
  }
}

resource "aws_ecs_service" "core" {
  name            = "core"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.core.arn
  desired_count   = var.core_desired_count
  launch_type     = "EC2"
  # Single host with fixed host ports: stop the old task before starting the new one.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  lifecycle {
    ignore_changes = [task_definition] # the deploy workflow (#97) registers new revisions
  }
}
