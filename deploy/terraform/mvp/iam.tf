# Instance role (ECS agent + SSM), task execution role (ECR pull, logs, SSM secrets),
# and an empty task role so containers never fall back to the instance role (INF-4).

data "aws_iam_policy_document" "ec2_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "instance" {
  name               = "${var.name_prefix}-instance"
  assume_role_policy = data.aws_iam_policy_document.ec2_trust.json
}

resource "aws_iam_role_policy_attachment" "instance_ecs" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role"
}

resource "aws_iam_role_policy_attachment" "instance_ssm" {
  role       = aws_iam_role.instance.name
  policy_arn = "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore"
}

resource "aws_iam_instance_profile" "instance" {
  name = "${var.name_prefix}-instance"
  role = aws_iam_role.instance.name
}

data "aws_iam_policy_document" "ecs_tasks_trust" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [var.account_id]
    }
  }
}

resource "aws_iam_role" "ecs_exec" {
  name               = "${var.name_prefix}-ecs-exec"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

resource "aws_iam_role_policy_attachment" "ecs_exec_managed" {
  role       = aws_iam_role.ecs_exec.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "ecs_exec_secrets" {
  statement {
    sid       = "ReadOwnParameters"
    actions   = ["ssm:GetParameters"]
    resources = ["arn:aws:ssm:${var.region}:${var.account_id}:parameter/${var.name_prefix}/*"]
  }
}

resource "aws_iam_role_policy" "ecs_exec_secrets" {
  name   = "read-own-parameters"
  role   = aws_iam_role.ecs_exec.id
  policy = data.aws_iam_policy_document.ecs_exec_secrets.json
}

# Proposal task only: read the pinned embedding model under models/ in the backups bucket
# (F-072: the model CDN is IPv4-only, so it is staged in S3 and read over dual-stack).
resource "aws_iam_role" "task_model" {
  name               = "${var.name_prefix}-task-model"
  assume_role_policy = data.aws_iam_policy_document.ecs_tasks_trust.json
}

data "aws_iam_policy_document" "task_model" {
  statement {
    sid       = "ReadModelObjects"
    actions   = ["s3:GetObject"]
    resources = ["${aws_s3_bucket.backups.arn}/models/*"]
  }
  statement {
    sid       = "ListModelPrefix"
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.backups.arn]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values   = ["models/*"]
    }
  }
}

resource "aws_iam_role_policy" "task_model" {
  name   = "read-model"
  role   = aws_iam_role.task_model.id
  policy = data.aws_iam_policy_document.task_model.json
}
