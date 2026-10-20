resource "aws_ecs_cluster" "main" {
  name = var.name

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_cloudwatch_log_group" "this" {
  for_each          = toset(["api", "worker", "workflow-worker", "provisioning-worker", "web", "migrate", "migrate-stores", "db-roles", "sandbox", "egress-proxy"])
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

# The db-roles task's: the only execution role that can read the master user's secret, so no
# other task definition can be given it.
resource "aws_iam_role" "execution_db_roles" {
  name               = "${var.name}-ecs-execution-db-roles"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_db_roles" {
  role       = aws_iam_role.execution_db_roles.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_db_roles_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.db_master.arn]
  }
}

resource "aws_iam_role_policy" "execution_db_roles_secrets" {
  role   = aws_iam_role.execution_db_roles.id
  policy = data.aws_iam_policy_document.read_db_roles_secrets.json
}

# The provisioning worker's and the migrate-stores task's: the only execution role that can read
# the provisioner's secret (besides the app's and the external one, like the shared role).
resource "aws_iam_role" "execution_provisioner" {
  name               = "${var.name}-ecs-execution-provisioner"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_provisioner" {
  role       = aws_iam_role.execution_provisioner.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_provisioner_secrets" {
  statement {
    actions = ["secretsmanager:GetSecretValue"]
    resources = [
      aws_secretsmanager_secret.app.arn,
      aws_secretsmanager_secret.external.arn,
      aws_secretsmanager_secret.provisioner.arn,
    ]
  }
}

resource "aws_iam_role_policy" "execution_provisioner_secrets" {
  role   = aws_iam_role.execution_provisioner.id
  policy = data.aws_iam_policy_document.read_provisioner_secrets.json
}

# Runtime role for the web, migrate and provisioning containers. It has no permissions: they
# make no AWS calls (their secrets come in through the execution role).
resource "aws_iam_role" "task" {
  name               = "${var.name}-ecs-task"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# Runtime role for the ingest worker. Its only permissions are for reading (and writing and
# deleting) uploaded files (data_store_files.tf).
resource "aws_iam_role" "worker" {
  name               = "${var.name}-ecs-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

# Runtime role for the api and workflow-worker containers. Its only permissions are for starting
# and stopping sandboxes (sandbox.tf), for the run_python tool, and for uploaded files
# (data_store_files.tf). Agent runs execute in the API
# process, and a workflow's agent nodes in the workflow worker, so only those need them; the
# ingest worker, which parses untrusted uploads, can't start or stop tasks. The sandbox and egress
# proxy tasks have no task role at all.
resource "aws_iam_role" "api" {
  name               = "${var.name}-ecs-api"
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
      # Uploaded files (data_store_files.tf).
      DATA_STORE_FILES_BUCKET = aws_s3_bucket.data_store_files.bucket
      # run_python: one Fargate task per agent run (sandbox.tf).
      SANDBOX_BACKEND         = "fargate"
      SANDBOX_CLUSTER         = aws_ecs_cluster.main.name
      SANDBOX_TASK_DEFINITION = aws_ecs_task_definition.sandbox.family
      SANDBOX_SUBNETS         = join(",", aws_subnet.sandbox[*].id)
      SANDBOX_SECURITY_GROUPS = aws_security_group.sandbox.id
      SANDBOX_PROXY_ADDRESS   = local.proxy_address
    } : { name = k, value = v }
  ]

  backend_secrets = concat(
    [for k in ["DATABASE_URL", "SESSION_SECRET", "SANDBOX_PROXY_SECRET", "DATA_STORE_CREDENTIALS_KEY"] :
    { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }],
    [for k in ["ANTHROPIC_API_KEY", "VOYAGE_API_KEY"] :
    { name = k, valueFrom = "${aws_secretsmanager_secret.external.arn}:${k}::" }],
  )

  # The provisioning worker's and migrate-stores': the backend's, plus the provisioner's DSN.
  provisioner_secrets = concat(local.backend_secrets, [
    {
      name      = "DATA_STORE_PROVISIONER_URL"
      valueFrom = "${aws_secretsmanager_secret.provisioner.arn}:DATA_STORE_PROVISIONER_URL::"
    },
  ])

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
  task_role_arn            = aws_iam_role.api.arn

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

# One-off, before each migration: `python -m aios.db_roles` (see infra/deploy.sh) creates the
# app's and the provisioner's roles as the master user, or updates their passwords, and hands the
# app's role anything the master user still owns. Re-running it changes nothing else.
resource "aws_ecs_task_definition" "db_roles" {
  family                   = "${var.name}-db-roles"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution_db_roles.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name      = "db-roles"
    image     = local.migrate_image
    essential = true
    command   = ["python", "-m", "aios.db_roles"]
    secrets = [
      for k in ["DATABASE_ADMIN_URL", "DATABASE_URL", "DATA_STORE_PROVISIONER_URL"] :
      { name = k, valueFrom = "${aws_secretsmanager_secret.db_master.arn}:${k}::" }
    ]
    logConfiguration = local.log_config["db-roles"]
  }])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${var.name}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.worker.cpu
  memory                   = var.worker.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.worker.arn

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

# Workflow runs, on their own queue (see aios.worker). API image: they need no Docling.
resource "aws_ecs_task_definition" "workflow_worker" {
  family                   = "${var.name}-workflow-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.workflow_worker.cpu
  memory                   = var.workflow_worker.memory
  execution_role_arn       = aws_iam_role.execution.arn
  task_role_arn            = aws_iam_role.api.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "workflow-worker"
    image     = local.image.api
    essential = true
    command = [
      "celery", "-A", "aios.worker", "worker", "-Q", "workflows", "--loglevel=INFO",
      "--concurrency=${var.workflow_worker.concurrency}",
    ]
    environment      = local.backend_env
    secrets          = local.backend_secrets
    logConfiguration = local.log_config["workflow-worker"]
  }])
}

# Creates and migrates data stores (the provisioning queue; see aios.worker), as the provisioner.
# API image: it needs no Docling. Its task role has no permissions.
resource "aws_ecs_task_definition" "provisioning_worker" {
  family                   = "${var.name}-provisioning-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.provisioning_worker.cpu
  memory                   = var.provisioning_worker.memory
  execution_role_arn       = aws_iam_role.execution_provisioner.arn
  task_role_arn            = aws_iam_role.task.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name      = "provisioning-worker"
    image     = local.image.api
    essential = true
    command = [
      "celery", "-A", "aios.worker", "worker", "-Q", "provisioning", "--loglevel=INFO",
      "--concurrency=${var.provisioning_worker.concurrency}",
      # Its beat retries stores whose provisioning gave up (aios.worker.RETRY_UNFINISHED_EVERY).
      "-B", "--schedule=/tmp/celerybeat-schedule",
    ]
    environment      = local.backend_env
    secrets          = local.provisioner_secrets
    logConfiguration = local.log_config["provisioning-worker"]
  }])
}

# One-off, after each migration: brings every ready data store to the store tree's head, and
# finishes provisioning any that never got there (see infra/deploy.sh). API image, at
# migrate_image_tag.
resource "aws_ecs_task_definition" "migrate_stores" {
  family                   = "${var.name}-migrate-stores"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execution_provisioner.arn
  task_role_arn            = aws_iam_role.task.arn

  container_definitions = jsonencode([{
    name             = "migrate-stores"
    image            = local.migrate_image
    essential        = true
    command          = ["python", "-m", "aios.datastores.provision", "migrate-all"]
    environment      = local.backend_env
    secrets          = local.provisioner_secrets
    logConfiguration = local.log_config["migrate-stores"]
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
    api                 = { task = aws_ecs_task_definition.api.arn, count = var.api.count, port = 8000, tg = aws_lb_target_group.api.arn }
    web                 = { task = aws_ecs_task_definition.web.arn, count = var.web.count, port = 80, tg = aws_lb_target_group.web.arn }
    worker              = { task = aws_ecs_task_definition.worker.arn, count = var.worker.count, port = null, tg = null }
    workflow-worker     = { task = aws_ecs_task_definition.workflow_worker.arn, count = var.workflow_worker.count, port = null, tg = null }
    provisioning-worker = { task = aws_ecs_task_definition.provisioning_worker.arn, count = var.provisioning_worker.count, port = null, tg = null }
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
