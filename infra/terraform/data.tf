# --- Postgres (RDS) with pgvector ---

resource "aws_db_subnet_group" "main" {
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_db_parameter_group" "main" {
  name   = "${var.name}-pg${split(".", var.db_engine_version)[0]}"
  family = "postgres${split(".", var.db_engine_version)[0]}"

  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }
}

# The master user's: for break-glass access, and for creating the roles below (the db-roles
# task, which infra/deploy.sh runs). Nothing else connects as it.
resource "random_password" "db" {
  length  = 32
  special = false
}

# The platform database's roles, created by `aios.db_roles` (the db-roles task):
# - aios_app: the api, the workers and migrations. Owns the platform schema; no CREATEDB or
#   CREATEROLE.
# - aios_provisioner: CREATEDB and CREATEROLE, for creating data stores. Only the provisioning
#   worker will be given its secret (no service has it yet).
resource "random_password" "db_app" {
  length  = 32
  special = false
}

resource "random_password" "db_provisioner" {
  length  = 32
  special = false
}

resource "aws_db_instance" "main" {
  identifier     = var.name
  engine         = "postgres"
  engine_version = var.db_engine_version
  instance_class = var.db_instance_class

  db_name  = "aios"
  username = "aios"
  password = random_password.db.result

  allocated_storage     = var.db_allocated_storage
  max_allocated_storage = var.db_allocated_storage * 4
  storage_type          = "gp3"
  storage_encrypted     = true

  db_subnet_group_name   = aws_db_subnet_group.main.name
  vpc_security_group_ids = [aws_security_group.db.id]
  parameter_group_name   = aws_db_parameter_group.main.name
  publicly_accessible    = false
  multi_az               = var.db_multi_az

  backup_retention_period      = 7
  auto_minor_version_upgrade   = true
  performance_insights_enabled = true
  deletion_protection          = var.deletion_protection
  skip_final_snapshot          = !var.deletion_protection
  final_snapshot_identifier    = "${var.name}-final"
}

# --- Redis-compatible broker for Celery (ElastiCache Valkey) ---

resource "aws_elasticache_subnet_group" "main" {
  name       = var.name
  subnet_ids = aws_subnet.private[*].id
}

resource "aws_elasticache_replication_group" "main" {
  replication_group_id       = var.name
  description                = "${var.name} Celery broker"
  engine                     = "valkey"
  engine_version             = "8.0"
  node_type                  = var.redis_node_type
  num_cache_clusters         = 1
  port                       = 6379
  subnet_group_name          = aws_elasticache_subnet_group.main.name
  security_group_ids         = [aws_security_group.redis.id]
  at_rest_encryption_enabled = true
  transit_encryption_enabled = true
}

locals {
  db_location        = "${aws_db_instance.main.endpoint}/${aws_db_instance.main.db_name}?sslmode=require"
  database_url       = "postgresql+psycopg://aios_app:${random_password.db_app.result}@${local.db_location}"
  provisioner_url    = "postgresql+psycopg://aios_provisioner:${random_password.db_provisioner.result}@${local.db_location}"
  database_admin_url = "postgresql+psycopg://${aws_db_instance.main.username}:${random_password.db.result}@${local.db_location}"
  redis_url          = "rediss://${aws_elasticache_replication_group.main.primary_endpoint_address}:6379/0?ssl_cert_reqs=required"
}
