# --- Connection pooler (PgBouncer, transaction mode) in front of the data stores ---
#
# The app's store sessions reach their stores here (DATA_STORE_POOLER_ADDRESS, in ecs.tf's
# backend_env), at pooler.<name>.internal:6432; the platform database, the provisioner and the
# checkpointer stay directly connected. One pool per (store database, role), so a store's
# connections are bounded here rather than by its roles' connection limit, which stays as a
# backstop (DATA_STORE_CONNECTION_LIMIT, 100). Sizing: docs/deploy.md, "Connection pooler".
#
# Clients authenticate as their own store roles: PgBouncer looks up each role's password with
# its auth query, run as the auth user on the platform database (AUTH_DBNAME), and logs in to
# the store with it. Deploy-only (not yet applied): see docs/deploy.md for what the auth user
# needs on RDS.

locals {
  pooler_port    = 6432
  pooler_address = "${aws_service_discovery_service.pooler.name}.${aws_service_discovery_private_dns_namespace.main.name}:${local.pooler_port}"
}

resource "aws_cloudwatch_log_group" "pooler" {
  name              = "/ecs/${var.name}/pooler"
  retention_in_days = 30
}

resource "aws_security_group" "pooler" {
  name   = "${var.name}-pooler"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "pooler_from_app" {
  security_group_id            = aws_security_group.pooler.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = local.pooler_port
  to_port                      = local.pooler_port
}

# To the database, and out through the NAT for its image.
resource "aws_vpc_security_group_egress_rule" "pooler_all" {
  security_group_id = aws_security_group.pooler.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "-1"
}

resource "aws_vpc_security_group_ingress_rule" "db_from_pooler" {
  security_group_id            = aws_security_group.db.id
  referenced_security_group_id = aws_security_group.pooler.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_service_discovery_service" "pooler" {
  name = "pooler"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 10
    }
  }

  health_check_custom_config {}
}

# The auth user's DSN, which the image turns into its database entry (`*`, any database, at the
# instance) and its auth file. Only the pooler's execution role can read it.
resource "aws_secretsmanager_secret" "pooler" {
  name                    = "${var.name}/pooler"
  recovery_window_in_days = 0
}

resource "aws_secretsmanager_secret_version" "pooler" {
  secret_id = aws_secretsmanager_secret.pooler.id
  secret_string = jsonencode({
    DATABASE_URL = "postgres://${aws_db_instance.main.username}:${random_password.db.result}@${aws_db_instance.main.endpoint}"
  })
}

resource "aws_iam_role" "execution_pooler" {
  name               = "${var.name}-ecs-execution-pooler"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

resource "aws_iam_role_policy_attachment" "execution_pooler" {
  role       = aws_iam_role.execution_pooler.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

data "aws_iam_policy_document" "read_pooler_secrets" {
  statement {
    actions   = ["secretsmanager:GetSecretValue"]
    resources = [aws_secretsmanager_secret.pooler.arn]
  }
}

resource "aws_iam_role_policy" "execution_pooler_secrets" {
  role   = aws_iam_role.execution_pooler.id
  policy = data.aws_iam_policy_document.read_pooler_secrets.json
}

resource "aws_ecs_task_definition" "pooler" {
  family                   = "${var.name}-pooler"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.pooler.cpu
  memory                   = var.pooler.memory
  execution_role_arn       = aws_iam_role.execution_pooler.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name         = "pooler"
    image        = var.pooler.image
    essential    = true
    portMappings = [{ containerPort = local.pooler_port }]
    environment = [
      for k, v in {
        LISTEN_PORT = tostring(local.pooler_port)
        POOL_MODE   = "transaction"
        # The app prepares no statements (aios.datastores.connections), so none are tracked.
        MAX_PREPARED_STATEMENTS = "0"
        AUTH_TYPE               = "scram-sha-256"
        AUTH_DBNAME             = aws_db_instance.main.db_name
        DEFAULT_POOL_SIZE       = tostring(var.pooler.default_pool_size)
        MAX_DB_CONNECTIONS      = tostring(var.pooler.max_db_connections)
        MAX_CLIENT_CONN         = tostring(var.pooler.max_client_conn)
        SERVER_TLS_SSLMODE      = "require"
      } : { name = k, value = v }
    ]
    secrets = [{
      name      = "DATABASE_URL"
      valueFrom = "${aws_secretsmanager_secret.pooler.arn}:DATABASE_URL::"
    }]
    logConfiguration = {
      logDriver = "awslogs"
      options = {
        awslogs-group         = aws_cloudwatch_log_group.pooler.name
        awslogs-region        = var.region
        awslogs-stream-prefix = "pooler"
      }
    }
  }])
}

resource "aws_ecs_service" "pooler" {
  name            = "pooler"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.pooler.arn
  desired_count   = var.run_services ? var.pooler.count : 0
  launch_type     = "FARGATE"

  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.pooler.id]
  }

  service_registries {
    registry_arn = aws_service_discovery_service.pooler.arn
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
