# run_python sandboxes: the API starts one Fargate task per agent run (aios.sandbox.fargate) and
# calls the runner inside it over the VPC. A sandbox has no AWS credentials and no route out:
# its only way out is the egress proxy, which allows HTTPS to the domains the run's tool
# settings list (carried in a signed proxy token) and nothing else.

data "aws_caller_identity" "current" {}

locals {
  account_id    = data.aws_caller_identity.current.account_id
  cluster_tasks = "arn:aws:ecs:${var.region}:${local.account_id}:task/${aws_ecs_cluster.main.name}/*"
  # How sandboxes reach the egress proxy (Cloud Map DNS), and how the API tells them to.
  proxy_address = "${aws_service_discovery_service.proxy.name}.${aws_service_discovery_private_dns_namespace.main.name}:3128"
}

# --- Network ---

# Sandboxes get subnets of their own (one IP per running sandbox) whose route table has no
# default route: no NAT, no internet gateway. Even a wrong security group rule can't give them
# a way out; only addresses inside the VPC and the S3 gateway endpoint are routable.
# DNS lookups through the VPC resolver still leave the VPC, and DNS Firewall rule groups apply
# to a whole VPC, not to subnets, so they aren't filtered here (see the README's known limits).
resource "aws_subnet" "sandbox" {
  count             = length(local.azs)
  vpc_id            = aws_vpc.main.id
  availability_zone = local.azs[count.index]
  cidr_block        = cidrsubnet(var.vpc_cidr, 8, count.index + 20)
  tags              = { Name = "${var.name}-sandbox-${local.azs[count.index]}" }
}

resource "aws_route_table" "sandbox" {
  vpc_id = aws_vpc.main.id
  tags   = { Name = "${var.name}-sandbox" }
}

resource "aws_route_table_association" "sandbox" {
  count          = length(aws_subnet.sandbox)
  subnet_id      = aws_subnet.sandbox[count.index].id
  route_table_id = aws_route_table.sandbox.id
}

# With no NAT, Fargate needs VPC endpoints to pull the sandbox image (ECR's API and registry,
# with the layers in S3) and to ship its logs. The interface endpoints cost about $0.01 an hour
# each per AZ (3 endpoints x 2 AZs, roughly $45 a month) plus $0.01/GB; the S3 gateway endpoint
# is free. Their private DNS applies to the whole VPC, so the other services use them too.
resource "aws_security_group" "endpoints" {
  name   = "${var.name}-endpoints"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "endpoints_https" {
  security_group_id = aws_security_group.endpoints.id
  cidr_ipv4         = var.vpc_cidr
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# Sandbox code can reach these endpoints too. It has no credentials of its own, and the policy
# refuses anyone else's, so they can't be used to ship data to another AWS account.
data "aws_iam_policy_document" "endpoints" {
  statement {
    actions   = ["*"]
    resources = ["*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:PrincipalAccount"
      values   = [local.account_id]
    }
  }
}

resource "aws_vpc_endpoint" "interface" {
  for_each            = toset(["ecr.api", "ecr.dkr", "logs"])
  vpc_id              = aws_vpc.main.id
  service_name        = "com.amazonaws.${var.region}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.private[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  policy              = data.aws_iam_policy_document.endpoints.json
  tags                = { Name = "${var.name}-${each.key}" }
}

# Only on the sandbox route table (the other subnets reach S3 through the NAT), and only for
# the bucket ECR keeps image layers in, so sandbox code can't use it to reach other buckets.
data "aws_iam_policy_document" "s3_endpoint" {
  statement {
    actions   = ["s3:GetObject"]
    resources = ["arn:aws:s3:::prod-${var.region}-starport-layer-bucket/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }
}

resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.main.id
  service_name      = "com.amazonaws.${var.region}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = [aws_route_table.sandbox.id]
  policy            = data.aws_iam_policy_document.s3_endpoint.json
  tags              = { Name = "${var.name}-s3" }
}

# --- Security groups ---

# Reached by the API only. Its egress is the proxy, plus what Fargate itself needs to start
# the task (the endpoints and S3). db and redis only admit `app`, so sandboxes can't reach them.
resource "aws_security_group" "sandbox" {
  name   = "${var.name}-sandbox"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "sandbox_from_app" {
  security_group_id            = aws_security_group.sandbox.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 8080
  to_port                      = 8080
}

resource "aws_vpc_security_group_egress_rule" "sandbox_to_proxy" {
  security_group_id            = aws_security_group.sandbox.id
  referenced_security_group_id = aws_security_group.egress_proxy.id
  ip_protocol                  = "tcp"
  from_port                    = 3128
  to_port                      = 3128
}

resource "aws_vpc_security_group_egress_rule" "sandbox_to_endpoints" {
  security_group_id            = aws_security_group.sandbox.id
  referenced_security_group_id = aws_security_group.endpoints.id
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}

resource "aws_vpc_security_group_egress_rule" "sandbox_to_s3" {
  security_group_id = aws_security_group.sandbox.id
  prefix_list_id    = aws_vpc_endpoint.s3.prefix_list_id
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

resource "aws_security_group" "egress_proxy" {
  name   = "${var.name}-egress-proxy"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "egress_proxy_from_sandbox" {
  security_group_id            = aws_security_group.egress_proxy.id
  referenced_security_group_id = aws_security_group.sandbox.id
  ip_protocol                  = "tcp"
  from_port                    = 3128
  to_port                      = 3128
}

# HTTPS only, which is all the proxy forwards. It refuses addresses inside the VPC itself.
resource "aws_vpc_security_group_egress_rule" "egress_proxy_https" {
  security_group_id = aws_security_group.egress_proxy.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# --- Egress proxy ---

# Sandboxes find the proxy at proxy.<name>.internal.
resource "aws_service_discovery_private_dns_namespace" "main" {
  name = "${var.name}.internal"
  vpc  = aws_vpc.main.id
}

resource "aws_service_discovery_service" "proxy" {
  name = "proxy"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.main.id
    routing_policy = "MULTIVALUE"

    dns_records {
      type = "A"
      ttl  = 10
    }
  }

  # ECS registers and deregisters the tasks, and reports their health.
  health_check_custom_config {}
}

# API image (aios.sandbox.egress_proxy). No task role: it makes no AWS calls, and it only gets
# the one secret it needs, not the API's.
resource "aws_ecs_task_definition" "egress_proxy" {
  family                   = "${var.name}-egress-proxy"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.egress_proxy.cpu
  memory                   = var.egress_proxy.memory
  execution_role_arn       = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name                   = "egress-proxy"
    image                  = local.image.api
    essential              = true
    command                = ["python", "-m", "aios.sandbox.egress_proxy", "--port", "3128"]
    portMappings           = [{ containerPort = 3128 }]
    readonlyRootFilesystem = true
    secrets = [{
      name      = "SANDBOX_PROXY_SECRET"
      valueFrom = "${aws_secretsmanager_secret.app.arn}:SANDBOX_PROXY_SECRET::"
    }]
    logConfiguration = local.log_config["egress-proxy"]
  }])
}

resource "aws_ecs_service" "egress_proxy" {
  name            = "egress-proxy"
  cluster         = aws_ecs_cluster.main.id
  task_definition = aws_ecs_task_definition.egress_proxy.arn
  desired_count   = var.run_services ? var.egress_proxy.count : 0
  launch_type     = "FARGATE"

  # The private subnets, which reach the internet through the NAT.
  network_configuration {
    subnets         = aws_subnet.private[*].id
    security_groups = [aws_security_group.egress_proxy.id]
  }

  service_registries {
    registry_arn = aws_service_discovery_service.proxy.arn
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}

# --- Sandbox task definition ---

# Started by the API with ecs:RunTask, never as a service. The runner (serve mode) takes calls
# on 8080, runs each one as an unprivileged user, and exits when idle or at its maximum
# lifetime, which stops the task.
resource "aws_ecs_task_definition" "sandbox" {
  family                   = "${var.name}-sandbox"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = var.sandbox.cpu
  memory                   = var.sandbox.memory
  # Only used by Fargate to pull the image and ship logs; its credentials never reach the
  # container. No task role, so code in the sandbox has no AWS credentials at all.
  execution_role_arn = aws_iam_role.execution.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  # Task-local storage, gone when the task stops: /work persists between a run's calls.
  volume {
    name = "work"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([{
    name                   = "sandbox"
    image                  = local.image.sandbox
    essential              = true
    readonlyRootFilesystem = true
    portMappings           = [{ containerPort = 8080 }]
    mountPoints = [
      { sourceVolume = "work", containerPath = "/work" },
      { sourceVolume = "tmp", containerPath = "/tmp" },
    ]
    linuxParameters = {
      # Reaps whatever the code leaves behind.
      initProcessEnabled = true
      # Docker's defaults, less all but what the runner needs as root: chown /work (CHOWN),
      # switch to the unprivileged user (SETUID, SETGID) and kill its processes (KILL).
      capabilities = {
        drop = [
          "AUDIT_WRITE", "DAC_OVERRIDE", "FOWNER", "FSETID", "MKNOD", "NET_BIND_SERVICE",
          "NET_RAW", "SETFCAP", "SETPCAP", "SYS_CHROOT",
        ]
      }
    }
    logConfiguration = local.log_config.sandbox
  }])
}

# --- IAM: the API and the workflow worker start and stop sandboxes ---

data "aws_iam_policy_document" "sandboxes" {
  # Only the sandbox task definition (any revision), only in this cluster.
  statement {
    actions   = ["ecs:RunTask"]
    resources = ["arn:aws:ecs:${var.region}:${local.account_id}:task-definition/${aws_ecs_task_definition.sandbox.family}:*"]
    condition {
      test     = "ArnEquals"
      variable = "ecs:cluster"
      values   = [aws_ecs_cluster.main.arn]
    }
  }

  # RunTask tags each sandbox with its session.
  statement {
    actions   = ["ecs:TagResource"]
    resources = [local.cluster_tasks]
    condition {
      test     = "StringEquals"
      variable = "ecs:CreateAction"
      values   = ["RunTask"]
    }
  }

  # Any task in the cluster: task ARNs don't say which task definition they run.
  statement {
    actions   = ["ecs:DescribeTasks", "ecs:StopTask"]
    resources = [local.cluster_tasks]
  }

  # RunTask passes the execution role to the sandbox task (it has no task role to pass).
  statement {
    actions   = ["iam:PassRole"]
    resources = [aws_iam_role.execution.arn]
    condition {
      test     = "StringEquals"
      variable = "iam:PassedToService"
      values   = ["ecs-tasks.amazonaws.com"]
    }
  }
}

resource "aws_iam_role_policy" "api_sandboxes" {
  role   = aws_iam_role.api.id
  policy = data.aws_iam_policy_document.sandboxes.json
}
