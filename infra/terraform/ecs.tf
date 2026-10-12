resource "aws_ecs_cluster" "main" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(["api", "worker", "web", "migrate", "egress-proxy"])
  name              = "/ecs/${var.name}/${each.key}"
  retention_in_days = 30
}

# --- IAM ---

data "aws_iam_policy_document" "ecs_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "execution" {
  name               = "${var.name}-ecs-execution"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution" {
  role       = aws_iam_role.execution.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.app.arn, aws_secretsmanager_secret.external.arn]
  }
}

resource "aws_iam_role_policy" "execution_secrets" {
  role   = aws_iam_role.execution.id
  policy = data.aws_iam_policy_document.read_secrets.json
}

# Runtime role for the api, worker, web and migrate containers. Its only permissions are for
# running sandboxes (sandbox.tf): invoking the sandbox function and keeping each session's /work
# in S3, which the API does for the run_python tool. The sandbox function and the egress proxy
# don't get it.
resource "aws_iam_role" "task" {
  name               = "${var.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# --- Task definitions ---

locals {
  backend_env = [
    for k, v in {
      PUBLIC_URL      = local.public_url
      REDIS_URL       = local.redis_url
      COOKIE_SECURE   = tostring(local.https)
      LLM_MODEL       = var.llm_model
      EMBEDDING_MODEL = var.embedding_model
      # run_python: one Lambda invocation per call, /work kept in S3 (sandbox.tf).
      AWS_REGION              = var.region
      SANDBOX_BACKEND         = "lambda"
      SANDBOX_LAMBDA_FUNCTION = aws_lambda_function.sandbox.function_name
      SANDBOX_WORK_BUCKET     = aws_s3_bucket.sandbox_work.bucket
      SANDBOX_PROXY_ADDRESS   = local.proxy_address
    } : { name = k, value = v }
  ]

  backend_secrets = concat(
    [for k in ["DATABASE_URL", "SESSION_SECRET", "SANDBOX_PROXY_SECRET"] :
    { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }],
    [for k in ["GITHUB_CLIENT_ID", "GITHUB_CLIENT_SECRET", "ANTHROPIC_API_KEY", "VOYAGE_API_KEY"] :
    { name = k, valueFrom = "${aws_secretsmanager_secret.external.arn}:${k}::" }],
  )

  image         = { for k, r in aws_ecr_repository.this : k => "${r.repository_url}:${var.image_tag}" }
  migrate_image = "${aws_ecr_repository.this["api"].repository_url}:${coalesce(var.migrate_image_tag, var.image_tag)}"

  log_config = {
    for k, g in aws_cloudwatch_log_group.this : k => {
      logDriver = "awslogs"
      options = {
        awslogs-group         = g.name
        awslogs-region        = var.region
        awslogs-stream-prefix = k
      }
    }
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${var.name}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.api.cpu
  memory                   = var.api.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name             = "api"
    image            = local.image.api
    essential        = true
    portMappings     = [{ containerPort = 8000 }]
    environment      = local.backend_env
    secrets          = local.backend_secrets
    logConfiguration = local.log_config.api
  }])
}

# One-off: `alembic upgrade head` (see infra/deploy.sh). API image, at migrate_image_tag.
resource "aws_ecs_task_definition" "migrate" {
  family                   = "${var.name}-migrate"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name             = "migrate"
    image            = local.migrate_image
    essential        = true
    command          = ["alembic", "upgrade", "head"]
    environment      = local.backend_env
    secrets          = local.backend_secrets
    logConfiguration = local.log_config.migrate
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker.cpu
  memory                   = var.worker.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  ephemeral_storage {
    size_in_gib = 40
  }

  container_definitions = jsonencode([{
    name      = "worker"
    image     = local.image.worker
    essential = true
    command = [
      "celery", "-A", "aios.worker", "worker", "--loglevel=INFO",
      "--concurrency=${var.worker.concurrency}",
    ]
    environment      = local.backend_env
    secrets          = local.backend_secrets
    logConfiguration = local.log_config.worker
  }])
}

resource "aws_ecs_task_definition" "web" {
  family                   = "${var.name}-web"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.web.cpu
  memory                   = var.web.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name             = "web"
    image            = local.image.web
    essential        = true
    portMappings     = [{ containerPort = 80 }]
    logConfiguration = local.log_config.web
  }])
}

# --- Services ---

locals {
  services = {
    api    = { task = aws_ecs_task_definition.api.arn, count = var.api.count, port = 8000, tg = aws_lb_target_group.api.arn }
    web    = { task = aws_ecs_task_definition.web.arn, count = var.web.count, port = 80, tg = aws_lb_target_group.web.arn }
    worker = { task = aws_ecs_task_definition.worker.arn, count = var.worker.count, port = null, tg = null }
  }
}

resource "aws_ecs_service" "this" {
  for_each        = local.services
  name            = each.key
  cluster         = aws_ecs_cluster.main.id
  task_definition = each.value.task
  desired_count   = var.run_services ? each.value.count : 0
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.app.id]
  }

  dynamic "load_balancer" {
    for_each = each.value.tg == null ? [] : [1]
    content {
      target_group_arn = each.value.tg
      container_name   = each.key
      container_port   = each.value.port
    }
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.http, aws_lb_listener_rule.api]
}
