# run_python sandboxes: each call is one invocation of the sandbox Lambda function
# (aios.sandbox.aws_lambda, aios.sandbox.lambda_handler). Each session is invoked as its own
# Lambda tenant, so its execution environments (Firecracker microVMs) never serve another session.
# A session's /work lives in S3 between calls, reached through presigned URLs the API makes for
# each call. The function's code has no usable AWS credentials and no route out: its only way out
# is the egress proxy, which allows HTTPS to the domains the run's tool settings list (carried in
# a signed proxy token) and nothing else.

data "aws_caller_identity" "current" {}

locals {
  account_id = data.aws_caller_identity.current.account_id
  # Built from the name rather than read from the function, which needs the role that refers to it.
  sandbox_function_name = "${var.name}-sandbox"
  sandbox_function_arn  = "arn:aws:lambda:${var.region}:${local.account_id}:function:${local.sandbox_function_name}"
  # How long a session (and its /work) lasts, idle or not: nothing runs between calls, so only its
  # proxy token, which is valid this long, ends it. The other backends' 2-hour default suits
  # sandboxes that keep running; here a conversation can go on for a day.
  sandbox_max_session_seconds = 24 * 60 * 60
  # How sandboxes reach the egress proxy (Cloud Map DNS), and how the API tells them to.
  proxy_address = "${aws_service_discovery_service.proxy.name}.${aws_service_discovery_private_dns_namespace.main.name}:3128"
}

# --- Network ---

# The function's network interfaces go in subnets of their own whose route table has no default
# route: no NAT, no internet gateway. Even a wrong security group rule can't give them a way out;
# only addresses inside the VPC and the S3 gateway endpoint are routable. Lambda pulls the image
# and ships the logs itself, outside the VPC, so no other endpoints are needed.
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

# Only on the sandbox route table (the other subnets reach S3 through the NAT), and only for
# reading and writing sessions' /work, so sandbox code can't use it to reach other buckets.
data "aws_iam_policy_document" "s3_endpoint" {
  statement {
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.sandbox_work.arn}/sessions/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
  }

  # S3 answers a GET for a missing object (a session's first call) with 404 only to a requester
  # who may list the bucket, and with 403 otherwise.
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.sandbox_work.arn]
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

# The function's network interfaces. Nothing can connect to them (Lambda delivers invocations
# itself), so there's no ingress. Egress is the proxy, and S3 for /work.
resource "aws_security_group" "sandbox" {
  name   = "${var.name}-sandbox"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_egress_rule" "sandbox_to_proxy" {
  security_group_id            = aws_security_group.sandbox.id
  referenced_security_group_id = aws_security_group.egress_proxy.id
  ip_protocol                  = "tcp"
  from_port                    = 3128
  to_port                      = 3128
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

# --- Each session's /work ---

resource "aws_s3_bucket" "sandbox_work" {
  bucket_prefix = "${var.name}-sandbox-work-"
  force_destroy = !var.deletion_protection
}

resource "aws_s3_bucket_public_access_block" "sandbox_work" {
  bucket                  = aws_s3_bucket.sandbox_work.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "sandbox_work" {
  bucket = aws_s3_bucket.sandbox_work.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# SSE-S3, not KMS: the handler skips downloading a /work it already has by its MD5, which is an
# object's ETag only without KMS.
resource "aws_s3_bucket_server_side_encryption_configuration" "sandbox_work" {
  bucket = aws_s3_bucket.sandbox_work.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# A session lasts at most local.sandbox_max_session_seconds, and the API deletes its /work when it
# replaces it or its run is deleted. This catches whatever that misses, a day after the longest
# session could have ended, so it never removes a live session's /work.
resource "aws_s3_bucket_lifecycle_configuration" "sandbox_work" {
  bucket = aws_s3_bucket.sandbox_work.id
  rule {
    id     = "expire-sessions"
    status = "Enabled"
    filter {}
    expiration {
      days = ceil(local.sandbox_max_session_seconds / 86400) + 1
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 1
    }
  }
}

# Objects are only ever read and written through the sandbox subnets' endpoint, with URLs the API
# presigned for one call; a URL that leaks can't be used from anywhere else.
data "aws_iam_policy_document" "sandbox_work" {
  statement {
    sid       = "TLSOnly"
    effect    = "Deny"
    actions   = ["s3:*"]
    resources = [aws_s3_bucket.sandbox_work.arn, "${aws_s3_bucket.sandbox_work.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }

  statement {
    sid       = "ThroughTheSandboxEndpointOnly"
    effect    = "Deny"
    actions   = ["s3:GetObject", "s3:PutObject"]
    resources = ["${aws_s3_bucket.sandbox_work.arn}/*"]
    principals {
      type        = "*"
      identifiers = ["*"]
    }
    condition {
      test     = "StringNotEquals"
      variable = "aws:SourceVpce"
      values   = [aws_vpc_endpoint.s3.id]
    }
  }
}

resource "aws_s3_bucket_policy" "sandbox_work" {
  bucket = aws_s3_bucket.sandbox_work.id
  policy = data.aws_iam_policy_document.sandbox_work.json

  depends_on = [aws_s3_bucket_public_access_block.sandbox_work]
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

# API image (aios.sandbox.egress_proxy), at the migration's tag like the sandbox function, so on
# the first deploy that adds it, it doesn't start on a release without the proxy. No task role: it
# makes no AWS calls, and it only gets the one secret it needs, not the API's.
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
    image                  = local.migrate_image
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

# --- The sandbox function ---

data "aws_iam_policy_document" "lambda_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

# Lambda uses this role itself, from outside the sandbox, to manage the function's network
# interfaces and ship its logs. Its credentials are also in the function's environment, where
# the code can read them (it runs as the handler's user), so everything is denied to them: calls
# Lambda makes from inside the execution environment carry lambda:SourceFunctionArn, and calls it
# makes on its own behalf don't.
resource "aws_iam_role" "sandbox_function" {
  name               = "${var.name}-sandbox-function"
  assume_role_policy = data.aws_iam_policy_document.lambda_assume.json
}

resource "aws_iam_role_policy_attachment" "sandbox_function" {
  role       = aws_iam_role.sandbox_function.name
  policy_arn = "arn:aws:iam::aws:policy/service-role/AWSLambdaVPCAccessExecutionRole"
}

data "aws_iam_policy_document" "sandbox_function_code" {
  statement {
    sid       = "NothingFromTheSandbox"
    effect    = "Deny"
    actions   = ["*"]
    resources = ["*"]
    condition {
      test     = "ArnEquals"
      variable = "lambda:SourceFunctionArn"
      values   = [local.sandbox_function_arn]
    }
  }
}

resource "aws_iam_role_policy" "sandbox_function_code" {
  role   = aws_iam_role.sandbox_function.id
  policy = data.aws_iam_policy_document.sandbox_function_code.json
}

resource "aws_cloudwatch_log_group" "sandbox_function" {
  name              = "/aws/lambda/${local.sandbox_function_name}"
  retention_in_days = 30
}

# Lets Lambda pull the sandbox image for this account's functions.
data "aws_iam_policy_document" "sandbox_image" {
  statement {
    sid     = "LambdaPull"
    actions = ["ecr:BatchGetImage", "ecr:GetDownloadUrlForLayer"]
    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
    condition {
      test     = "StringLike"
      variable = "aws:sourceArn"
      values   = ["arn:aws:lambda:${var.region}:${local.account_id}:function:*"]
    }
  }
}

resource "aws_ecr_repository_policy" "sandbox" {
  repository = aws_ecr_repository.this["sandbox"].name
  policy     = data.aws_iam_policy_document.sandbox_image.json
}

resource "aws_lambda_function" "sandbox" {
  function_name = local.sandbox_function_name
  role          = aws_iam_role.sandbox_function.arn
  package_type  = "Image"
  # The sandbox-lambda image. At the migration's tag, not the services': on a deploy the function
  # moves to the new image during the migration step, so the first deploy that adds it can
  # create it (the release still running has no sandbox image to start it with).
  image_uri     = "${aws_ecr_repository.this["sandbox"].repository_url}:${coalesce(var.migrate_image_tag, var.image_tag)}"
  architectures = ["x86_64"]
  memory_size   = var.sandbox_function.memory
  timeout       = var.sandbox_function.timeout

  reserved_concurrent_executions = var.sandbox_function.reserved_concurrency

  ephemeral_storage {
    size = var.sandbox_function.ephemeral_storage
  }

  # One tenant per session: an execution environment only ever serves one session. Can only be
  # set when the function is created.
  tenancy_config {
    tenant_isolation_mode = "PER_TENANT"
  }

  vpc_config {
    subnet_ids         = aws_subnet.sandbox[*].id
    security_group_ids = [aws_security_group.sandbox.id]
  }

  logging_config {
    log_format = "Text"
    log_group  = aws_cloudwatch_log_group.sandbox_function.name
  }

  depends_on = [
    aws_iam_role_policy_attachment.sandbox_function,
    aws_iam_role_policy.sandbox_function_code,
    aws_ecr_repository_policy.sandbox,
  ]
}

# --- IAM: the API runs sandboxes ---

data "aws_iam_policy_document" "sandboxes" {
  statement {
    actions   = ["lambda:InvokeFunction"]
    resources = [aws_lambda_function.sandbox.arn]
  }

  # Presigning a call's restore (GET) and save (POST) URLs, which act with these permissions, and
  # deleting a session's /work when it ends.
  statement {
    actions   = ["s3:GetObject", "s3:PutObject", "s3:DeleteObject"]
    resources = ["${aws_s3_bucket.sandbox_work.arn}/sessions/*"]
  }

  # So that a restore URL for a session with nothing saved yet gets 404, not 403.
  statement {
    actions   = ["s3:ListBucket"]
    resources = [aws_s3_bucket.sandbox_work.arn]
  }
}

resource "aws_iam_role_policy" "task_sandboxes" {
  role   = aws_iam_role.task.id
  policy = data.aws_iam_policy_document.sandboxes.json
}
