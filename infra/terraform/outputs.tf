output "url" {
  description = "Public URL. Register <url>/auth/callback as the GitHub OAuth app's callback."
  value       = local.public_url
}

output "alb_dns_name" {
  description = "Point domain_name at this (CNAME / alias record)."
  value       = aws_lb.main.dns_name
}

output "ecr_repositories" {
  value = { for k, r in aws_ecr_repository.this : k => r.repository_url }
}

output "ecs_cluster" {
  value = aws_ecs_cluster.main.name
}

output "external_secret_id" {
  description = "Put GitHub OAuth + Anthropic + Voyage credentials here."
  value       = aws_secretsmanager_secret.external.name
}

output "migrate_task" {
  description = "Inputs for `aws ecs run-task` (used by infra/deploy.sh)."
  value = {
    task_definition = aws_ecs_task_definition.migrate.family
    subnets         = aws_subnet.private[*].id
    security_group  = aws_security_group.app.id
  }
}

output "db_endpoint" {
  value = aws_db_instance.main.endpoint
}

output "region" {
  value = var.region
}
