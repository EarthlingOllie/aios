variable "name" {
  description = "Prefix for all resources."
  type        = string
  default     = "aios"
}

variable "environment" {
  type    = string
  default = "prod"
}

variable "region" {
  description = "One region for everything: the app, Postgres and Redis live side by side."
  type        = string
  default     = "us-east-1"
}

variable "vpc_cidr" {
  type    = string
  default = "10.40.0.0/16"
}

variable "domain_name" {
  description = "Public hostname (e.g. aios.example.com). Empty = use the ALB's DNS name over HTTP."
  type        = string
  default     = ""
}

variable "certificate_arn" {
  description = "ACM certificate for domain_name, in the same region. Empty = HTTP only (dev)."
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "Tag of the api/worker/web/sandbox images in ECR to run."
  type        = string
  default     = "latest"
}

variable "migrate_image_tag" {
  description = "Tag the migration task runs. Null = image_tag. infra/deploy.sh sets it to the new release while image_tag still pins the services to the running release, so migrations finish before new code serves traffic."
  type        = string
  default     = null
}

variable "run_services" {
  description = "false = create/keep the ECS services at zero tasks. infra/deploy.sh uses it on the very first deploy, before the database has been migrated."
  type        = bool
  default     = true
}

variable "llm_model" {
  type    = string
  default = "anthropic:claude-sonnet-5-5"
}

variable "embedding_model" {
  type    = string
  default = "voyageai:voyage-4"
}

# Jev, the prompt-injection classifier (jev.tf): hosted, as laya-php uses it. Its key is
# JEV_API_KEY in the external secret.
variable "jev_url" {
  type    = string
  default = "https://api.typesafe.ai"
}

# Where the Jev-down alarm goes (an SNS topic's ARN); none: the alarm only shows in CloudWatch.
variable "alarm_actions" {
  type    = list(string)
  default = []
}

# --- Database ---
variable "db_instance_class" {
  type    = string
  default = "db.t4g.medium"
}

variable "db_allocated_storage" {
  type    = number
  default = 50
}

variable "db_engine_version" {
  description = "RDS PostgreSQL version (pgvector ships with RDS; the migration runs CREATE EXTENSION vector)."
  type        = string
  default     = "17"

  # Before 16, CREATEROLE (which the provisioner's role has) reaches every role but a superuser,
  # the master user's included. `python -m aios.db_roles` refuses such a server too.
  validation {
    condition     = can(regex("^[0-9]+(\\.[0-9]+)?$", var.db_engine_version)) ? tonumber(split(".", var.db_engine_version)[0]) >= 16 : false
    error_message = "db_engine_version must be PostgreSQL 16 or later (e.g. \"17\" or \"16.4\")."
  }
}

variable "db_multi_az" {
  type    = bool
  default = false
}

variable "deletion_protection" {
  type    = bool
  default = true
}

# --- Redis (Celery broker) ---
variable "redis_node_type" {
  type    = string
  default = "cache.t4g.micro"
}

# --- ECS sizing and autoscaling (autoscaling.tf) ---
# Each service runs between its min and max tasks, scaled by autoscaling.tf. Scale-in relies on
# each service's graceful shutdown (its stopTimeout), so shrinking never fails a turn.
variable "api" {
  type    = object({ cpu = number, memory = number, min = number, max = number })
  default = { cpu = 512, memory = 1024, min = 2, max = 4 }
  validation {
    condition     = 1 <= var.api.min && var.api.min <= var.api.max
    error_message = "api: 1 <= min <= max."
  }
}

variable "web" {
  type    = object({ cpu = number, memory = number, min = number, max = number })
  default = { cpu = 256, memory = 512, min = 2, max = 4 }
  validation {
    condition     = 1 <= var.web.min && var.web.min <= var.web.max
    error_message = "web: 1 <= min <= max."
  }
}

variable "run_service" {
  description = "The run service's executors: agent turns, handoffs, schedules, workflow nodes and syncs, many at once per task (concurrency), mostly waiting on the model. Scaled on the interactive lane's oldest ready age and the background and sources lanes' depth; at least two, so one can be replaced."
  type        = object({ cpu = number, memory = number, min = number, max = number, concurrency = number })
  default     = { cpu = 1024, memory = 2048, min = 2, max = 4, concurrency = 50 }
  validation {
    condition     = 2 <= var.run_service.min && var.run_service.min <= var.run_service.max
    error_message = "run_service: 2 <= min <= max."
  }
}

variable "ingest_worker" {
  description = "Document ingest (the ingest queue). Docling is CPU/RAM hungry; give workers room. Scaled on the ingest queue's depth."
  type        = object({ cpu = number, memory = number, min = number, max = number, concurrency = number })
  default     = { cpu = 2048, memory = 8192, min = 1, max = 4, concurrency = 2 }
  validation {
    condition     = 1 <= var.ingest_worker.min && var.ingest_worker.min <= var.ingest_worker.max
    error_message = "ingest_worker: 1 <= min <= max."
  }
}

variable "urgent_worker" {
  description = "Urgent maintenance (the urgent queue): applying approved changes, and restores. Small and mostly idle; scaled on the urgent queue's depth."
  type        = object({ cpu = number, memory = number, min = number, max = number, concurrency = number })
  default     = { cpu = 256, memory = 512, min = 1, max = 4, concurrency = 1 }
  validation {
    condition     = 1 <= var.urgent_worker.min && var.urgent_worker.min <= var.urgent_worker.max
    error_message = "urgent_worker: 1 <= min <= max."
  }
}

variable "routine_worker" {
  description = "Routine maintenance (the routine queue): provisioning, migrations, measuring, purging, expiry, retention, sweeps. Each runs the beat, so keep it at one (a second would only do everything twice)."
  type        = object({ cpu = number, memory = number, min = number, max = number, concurrency = number })
  default     = { cpu = 256, memory = 512, min = 1, max = 1, concurrency = 1 }
  validation {
    condition     = 1 <= var.routine_worker.min && var.routine_worker.min <= var.routine_worker.max
    error_message = "routine_worker: 1 <= min <= max."
  }
}

variable "autoscaling" {
  description = "What the services scale on (autoscaling.tf), from the queue metrics (aios.queue_metrics): the interactive lane's oldest ready age (seconds), and items or messages waiting per task. Scale-in waits scale_in_cooldown seconds."
  type = object({
    interactive_age_target = number
    background_per_task    = number
    sources_per_task       = number
    ingest_per_task        = number
    urgent_per_task        = number
    cpu_target             = number
    scale_in_cooldown      = number
    scale_out_cooldown     = number
  })
  default = {
    interactive_age_target = 5
    background_per_task    = 20
    sources_per_task       = 10
    ingest_per_task        = 4
    urgent_per_task        = 2
    cpu_target             = 60
    scale_in_cooldown      = 600
    scale_out_cooldown     = 60
  }
}

variable "queue_metrics_interval" {
  description = "How often (seconds) the routine worker's beat publishes the queue metrics."
  type        = number
  default     = 30
}

# --- run_python sandboxes (sandbox.tf) ---
variable "sandbox" {
  description = "Size of each sandbox: one Fargate task per agent run that uses run_python."
  type        = object({ cpu = number, memory = number })
  default     = { cpu = 512, memory = 1024 }
}

variable "pooler" {
  description = "PgBouncer in front of the data stores (pooler.tf; sizing in docs/deploy.md). pool sizes are per (store database, role); max_db_connections caps each store database."
  type = object({
    cpu                = number
    memory             = number
    count              = number
    image              = string
    default_pool_size  = number
    max_db_connections = number
    max_client_conn    = number
  })
  default = {
    cpu                = 256
    memory             = 512
    count              = 1
    image              = "edoburu/pgbouncer:v1.25.2-p0"
    default_pool_size  = 5
    max_db_connections = 10
    max_client_conn    = 2000
  }
}

variable "egress_proxy" {
  description = "The sandboxes' only way out. Two tasks, so one can fail or be replaced."
  type        = object({ cpu = number, memory = number, count = number })
  default     = { cpu = 256, memory = 512, count = 2 }
}
