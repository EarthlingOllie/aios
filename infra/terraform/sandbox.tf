# Sandbox hosts for the run_python tool (SANDBOX_BACKEND=gvisor, see aios.sandbox.broker).
#
# EC2 instances in the private subnets, running Docker with gVisor's runsc. Each runs one broker
# (an ECS daemon service, with the Docker socket) that starts one gVisor container per agent run
# on an internal Docker network, plus the egress proxy container, the sandboxes' only way out.
# The API starts sessions through an internal NLB and then calls the broker that answered
# directly, at the host's private IP.

locals {
  sandbox_port = 8090
  # The Docker network sandboxes join (`--internal`: no route out). Hosts drop traffic from it
  # to the host itself, so sandboxes can't reach the broker, the ECS agent or other host ports.
  sandbox_subnet = "172.30.0.0/16"

  sandbox_user_data = <<-EOT
    #!/bin/bash
    set -euo pipefail

    # Join the cluster. Only the broker daemon runs here, never privileged, and host-network
    # tasks get no task-role credentials.
    cat >> /etc/ecs/ecs.config <<'EOF'
    ECS_CLUSTER=${aws_ecs_cluster.main.name}
    ECS_DISABLE_PRIVILEGED=true
    ECS_ENABLE_TASK_IAM_ROLE_NETWORK_HOST=false
    EOF

    # gVisor, at a pinned release, checked against a pinned checksum. The release tarball holds
    # runsc and the sidecar binaries it expects next to it, so nothing is fetched at run time.
    dnf install -y bzip2
    workdir="$(mktemp -d)"
    curl -fsSL --retry 5 -o "$workdir/gvisor.tar.bz2" \
      "https://storage.googleapis.com/gvisor/releases/release/${var.gvisor_release}/x86_64/gvisor.tar.bz2"
    echo "${var.gvisor_sha512}  $workdir/gvisor.tar.bz2" | sha512sum --check --strict
    tar -xjf "$workdir/gvisor.tar.bz2" -C /usr/local/bin --no-same-owner runsc gvisor-bin
    rm -rf "$workdir"
    # Registers the `runsc` runtime in /etc/docker/daemon.json.
    /usr/local/bin/runsc install -download-sidecars=NEVER -require-sidecars=ALWAYS

    cat > /etc/systemd/system/aios-sandbox-firewall.service <<'EOF'
    [Unit]
    Description=Drop traffic from sandbox containers to this host
    Before=docker.service

    [Service]
    Type=oneshot
    RemainAfterExit=yes
    ExecStart=/usr/sbin/iptables -I INPUT -s ${local.sandbox_subnet} -j DROP

    [Install]
    WantedBy=multi-user.target
    EOF
    systemctl daemon-reload
    systemctl enable --now aios-sandbox-firewall.service
    systemctl restart docker
  EOT

  broker_env = [
    for k, v in {
      SANDBOX_BROKER_PORT    = tostring(local.sandbox_port)
      SANDBOX_DOCKER_RUNTIME = "runsc"
      SANDBOX_IMAGE          = local.image.sandbox
      SANDBOX_PROXY_IMAGE    = local.image.broker
      SANDBOX_NETWORK        = "aios-sandbox"
      SANDBOX_NETWORK_SUBNET = local.sandbox_subnet
      SANDBOX_MEMORY_MB      = tostring(var.sandbox_hosts.sandbox_memory_mb)
      SANDBOX_CPUS           = tostring(var.sandbox_hosts.sandbox_cpus)
      SANDBOX_MAX_SESSIONS   = tostring(var.sandbox_hosts.sessions_per_host)
    } : { name = k, value = v }
  ]
}

# --- Hosts ---

data "aws_ssm_parameter" "ecs_ami" {
  name = "/aws/service/ecs/optimized-ami/amazon-linux-2023/recommended/image_id"
}

data "aws_iam_policy_document" "ec2_assume" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ec2.amazonaws.com"]
    }
  }
}

# Joins the cluster and pulls from ECR (AmazonEC2ContainerServiceforEC2Role); SSM for patching
# and shell access without SSH. Containers can't use it: IMDSv2 with a hop limit of 1.
resource "aws_iam_role" "sandbox_host" {
  name               = "${var.name}-sandbox-host"
  assume_role_policy = data.aws_iam_policy_document.ec2_assume.json
}

resource "aws_iam_role_policy_attachment" "sandbox_host" {
  for_each = toset([
    "arn:aws:iam::aws:policy/service-role/AmazonEC2ContainerServiceforEC2Role",
    "arn:aws:iam::aws:policy/AmazonSSMManagedInstanceCore",
  ])
  role       = aws_iam_role.sandbox_host.name
  policy_arn = each.value
}

resource "aws_iam_instance_profile" "sandbox_host" {
  name = "${var.name}-sandbox-host"
  role = aws_iam_role.sandbox_host.name
}

resource "aws_launch_template" "sandbox" {
  name_prefix            = "${var.name}-sandbox-"
  image_id               = data.aws_ssm_parameter.ecs_ami.value
  instance_type          = var.sandbox_hosts.instance_type
  vpc_security_group_ids = [aws_security_group.sandbox_host.id]
  user_data              = base64encode(local.sandbox_user_data)

  iam_instance_profile {
    arn = aws_iam_instance_profile.sandbox_host.arn
  }

  # IMDSv2 only, one hop: the host can read its metadata, containers (one hop further) can't.
  metadata_options {
    http_endpoint               = "enabled"
    http_tokens                 = "required"
    http_put_response_hop_limit = 1
  }

  block_device_mappings {
    device_name = "/dev/xvda"
    ebs {
      volume_size = 50
      volume_type = "gp3"
      encrypted   = true
    }
  }

  monitoring {
    enabled = true
  }

  tag_specifications {
    resource_type = "instance"
    tags          = { Name = "${var.name}-sandbox" }
  }
}

# A new AMI (applied with the next `terraform apply`) or user data replaces hosts one by one.
resource "aws_autoscaling_group" "sandbox" {
  name_prefix         = "${var.name}-sandbox-"
  vpc_zone_identifier = aws_subnet.private[*].id
  min_size            = var.sandbox_hosts.min
  max_size            = var.sandbox_hosts.max
  desired_capacity    = var.sandbox_hosts.desired
  target_group_arns   = [aws_lb_target_group.broker.arn]

  launch_template {
    id      = aws_launch_template.sandbox.id
    version = aws_launch_template.sandbox.latest_version
  }

  instance_refresh {
    strategy = "Rolling"
    preferences {
      min_healthy_percentage = 50
    }
  }

  tag {
    key                 = "AmazonECSManaged"
    value               = "true"
    propagate_at_launch = true
  }
}

resource "aws_ecs_capacity_provider" "sandbox" {
  name = "${var.name}-sandbox"

  auto_scaling_group_provider {
    auto_scaling_group_arn         = aws_autoscaling_group.sandbox.arn
    managed_termination_protection = "DISABLED"
    managed_draining               = "ENABLED"

    # Only a daemon runs on these hosts, which managed scaling doesn't size for: the host count
    # comes from var.sandbox_hosts.
    managed_scaling {
      status = "DISABLED"
    }
  }
}

resource "aws_ecs_cluster_capacity_providers" "main" {
  cluster_name       = aws_ecs_cluster.main.name
  capacity_providers = ["FARGATE", aws_ecs_capacity_provider.sandbox.name]
}

# --- Broker ---

resource "aws_ecs_task_definition" "broker" {
  family                   = "${var.name}-broker"
  requires_compatibilities = ["EC2"]
  network_mode             = "host"
  execution_role_arn       = aws_iam_role.execution.arn
  # No task role: the broker needs no AWS access.

  volume {
    name      = "docker-socket"
    host_path = "/var/run/docker.sock"
  }

  container_definitions = jsonencode([
    {
      name              = "broker"
      image             = local.image.broker
      essential         = true
      memoryReservation = 256
      command = [
        "uvicorn", "aios.sandbox.broker:app", "--host", "0.0.0.0",
        "--port", tostring(local.sandbox_port),
      ]
      environment = local.broker_env
      secrets = [for k in ["SANDBOX_BROKER_TOKEN", "SANDBOX_PROXY_SECRET"] :
      { name = k, valueFrom = "${aws_secretsmanager_secret.app.arn}:${k}::" }]
      mountPoints      = [{ sourceVolume = "docker-socket", containerPath = "/var/run/docker.sock" }]
      logConfiguration = local.log_config.broker
    },
    {
      # Runs nothing: listing the sandbox image makes ECS pull it onto every host for each
      # release (the broker never pulls), using the execution role's ECR access.
      name                   = "sandbox-image"
      image                  = local.image.sandbox
      essential              = false
      memoryReservation      = 16
      command                = ["python", "-c", "pass"]
      user                   = "1001:1001"
      readonlyRootFilesystem = true
    },
  ])
}

resource "aws_ecs_service" "broker" {
  count               = var.run_services ? 1 : 0
  name                = "broker"
  cluster             = aws_ecs_cluster.main.id
  task_definition     = aws_ecs_task_definition.broker.arn
  launch_type         = "EC2"
  scheduling_strategy = "DAEMON"
  # Old and new brokers can't share port 8090: each host stops its old one first. A broker
  # restart removes that host's sandboxes; their runs get fresh ones on their next call.
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  depends_on = [aws_ecs_cluster_capacity_providers.main]
}

# --- Internal NLB in front of the brokers (new sessions only) ---

resource "aws_lb" "sandbox" {
  name                             = "${var.name}-sandbox"
  internal                         = true
  load_balancer_type               = "network"
  subnets                          = aws_subnet.private[*].id
  security_groups                  = [aws_security_group.sandbox_nlb.id]
  enable_cross_zone_load_balancing = true
}

resource "aws_lb_target_group" "broker" {
  name                 = "${var.name}-broker"
  port                 = local.sandbox_port
  protocol             = "TCP"
  target_type          = "instance"
  vpc_id               = aws_vpc.main.id
  deregistration_delay = 30

  health_check {
    protocol = "HTTP"
    path     = "/healthz"
    matcher  = "200"
  }
}

resource "aws_lb_listener" "broker" {
  load_balancer_arn = aws_lb.sandbox.arn
  port              = local.sandbox_port
  protocol          = "TCP"

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.broker.arn
  }
}

# --- Security groups ---

resource "aws_security_group" "sandbox_nlb" {
  name   = "${var.name}-sandbox-nlb"
  vpc_id = aws_vpc.main.id
}

resource "aws_vpc_security_group_ingress_rule" "sandbox_nlb_from_app" {
  security_group_id            = aws_security_group.sandbox_nlb.id
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = local.sandbox_port
  to_port                      = local.sandbox_port
}

resource "aws_vpc_security_group_egress_rule" "sandbox_nlb_to_hosts" {
  security_group_id            = aws_security_group.sandbox_nlb.id
  referenced_security_group_id = aws_security_group.sandbox_host.id
  ip_protocol                  = "tcp"
  from_port                    = local.sandbox_port
  to_port                      = local.sandbox_port
}

# Sandbox hosts. `db` and `redis` don't admit it.
resource "aws_security_group" "sandbox_host" {
  name   = "${var.name}-sandbox-host"
  vpc_id = aws_vpc.main.id
}

# The broker: from the API directly (and through the NLB, which keeps the API's address), and
# from the NLB itself for health checks.
resource "aws_vpc_security_group_ingress_rule" "sandbox_host_broker" {
  for_each = {
    app = aws_security_group.app.id
    nlb = aws_security_group.sandbox_nlb.id
  }
  security_group_id            = aws_security_group.sandbox_host.id
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = local.sandbox_port
  to_port                      = local.sandbox_port
}

# HTTPS only: the egress proxy's upstream connections, and ECS, ECR, CloudWatch Logs, SSM, the
# OS packages and the gVisor download.
resource "aws_vpc_security_group_egress_rule" "sandbox_host_https" {
  security_group_id = aws_security_group.sandbox_host.id
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}
