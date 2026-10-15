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

# --- ECS sizing ---
variable "api" {
  type    = object({ cpu = number, memory = number, count = number })
  default = { cpu = 512, memory = 1024, count = 2 }
}

variable "web" {
  type    = object({ cpu = number, memory = number, count = number })
  default = { cpu = 256, memory = 512, count = 2 }
}

variable "worker" {
  description = "Docling is CPU/RAM hungry; give workers room."
  type        = object({ cpu = number, memory = number, count = number, concurrency = number })
  default     = { cpu = 2048, memory = 8192, count = 1, concurrency = 2 }
}

variable "workflow_worker" {
  description = "Runs workflows (mostly waiting on the model), on their own queue so ingests don't wait behind them."
  type        = object({ cpu = number, memory = number, count = number, concurrency = number })
  default     = { cpu = 512, memory = 2048, count = 1, concurrency = 2 }
}

variable "provisioning_worker" {
  description = "Creates and migrates data stores (the provisioning queue): small and mostly idle."
  type        = object({ cpu = number, memory = number, count = number, concurrency = number })
  default     = { cpu = 256, memory = 512, count = 1, concurrency = 1 }
}

# --- run_python sandboxes (sandbox.tf) ---
variable "sandbox" {
  description = "Size of each sandbox: one Fargate task per agent run that uses run_python."
  type        = object({ cpu = number, memory = number })
  default     = { cpu = 512, memory = 1024 }
}

variable "egress_proxy" {
  description = "The sandboxes' only way out. Two tasks, so one can fail or be replaced."
  type        = object({ cpu = number, memory = number, count = number })
  default     = { cpu = 256, memory = 512, count = 2 }
}
