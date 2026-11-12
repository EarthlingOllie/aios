# Applies the whole configuration to a mocked AWS (no account or credentials needed), and
# checks the lanes' workers and autoscaling: each service between its min and max, what each
# scales on, the scale-in cooldown, graceful shutdown, and who holds the provisioner's
# credentials. Run: terraform test

mock_provider "aws" {
  mock_data "aws_availability_zones" {
    defaults = { names = ["us-east-1a", "us-east-1b"] }
  }
  mock_data "aws_caller_identity" {
    defaults = { account_id = "123456789012" }
  }
  # The policy documents' JSON comes from the provider, so a mock has none to check; these
  # tests read the documents' statements instead.
  mock_data "aws_iam_policy_document" {
    defaults = { json = "{}" }
  }
  # Arguments that must look like ARNs (the mocks' are random strings otherwise).
  mock_resource "aws_iam_role" {
    defaults = { arn = "arn:aws:iam::123456789012:role/mock" }
  }
  mock_resource "aws_lb" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:loadbalancer/app/mock/1" }
  }
  mock_resource "aws_lb_target_group" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:targetgroup/mock/1" }
  }
  mock_resource "aws_lb_listener" {
    defaults = { arn = "arn:aws:elasticloadbalancing:us-east-1:123456789012:listener/app/mock/1/1" }
  }
  mock_resource "aws_service_discovery_service" {
    defaults = { arn = "arn:aws:servicediscovery:us-east-1:123456789012:service/srv-mock" }
  }
  mock_resource "aws_kms_key" {
    defaults = { arn = "arn:aws:kms:us-east-1:123456789012:key/data-store-files" }
  }
  mock_resource "aws_s3_bucket" {
    defaults = { arn = "arn:aws:s3:::aios-data-prod" }
  }
}

run "autoscaling" {
  command = apply

  # Every service has a configurable floor and ceiling: one to four, two for executors.
  assert {
    condition = alltrue([
      for k, t in aws_appautoscaling_target.service :
      t.min_capacity >= 1 && t.min_capacity <= t.max_capacity
    ])
    error_message = "Every service should scale between its min (at least 1) and max."
  }
  assert {
    condition     = aws_appautoscaling_target.service["run-service"].min_capacity == 2
    error_message = "The run service's executors should be at least two."
  }
  assert {
    condition     = aws_appautoscaling_target.service["ingest-worker"].max_capacity == 4
    error_message = "Ingest workers should scale up to four."
  }

  # Executors scale on the interactive lane's oldest ready age (5 seconds), and on the
  # background and sources lanes' depth; ingest workers on the ingest queue's.
  assert {
    condition = (
      one(one(aws_appautoscaling_policy.run_service_interactive_age.target_tracking_scaling_policy_configuration).customized_metric_specification).metric_name == "OldestReadyAge"
      && one(aws_appautoscaling_policy.run_service_interactive_age.target_tracking_scaling_policy_configuration).target_value == 5
    )
    error_message = "Executors should scale on the oldest interactive item's age, at 5 seconds."
  }
  assert {
    condition = (
      aws_appautoscaling_policy.depth["run-service-background"].resource_id == aws_appautoscaling_target.service["run-service"].resource_id
      && aws_appautoscaling_policy.depth["run-service-sources"].resource_id == aws_appautoscaling_target.service["run-service"].resource_id
      && aws_appautoscaling_policy.depth["ingest-worker-ingest"].resource_id == aws_appautoscaling_target.service["ingest-worker"].resource_id
    )
    error_message = "Executors should scale on background and sources depth, ingest workers on ingest depth."
  }

  # Idle services return to their floor after a 10 minute scale-in cooldown.
  assert {
    condition = alltrue(concat(
      [for p in aws_appautoscaling_policy.depth : one(p.target_tracking_scaling_policy_configuration).scale_in_cooldown == 600],
      [one(aws_appautoscaling_policy.run_service_interactive_age.target_tracking_scaling_policy_configuration).scale_in_cooldown == 600],
    ))
    error_message = "Scale-in should wait 10 minutes."
  }

  # Scale-in relies on graceful shutdown: the run service's stop timeout outlasts its grace.
  assert {
    condition     = jsondecode(aws_ecs_task_definition.run_service.container_definitions)[0].stopTimeout > 25
    error_message = "The run service should be given longer than its shutdown grace to stop."
  }

  # Only the maintenance workers (and the store migration) hold the provisioner's credentials.
  assert {
    condition = alltrue([
      for td in [aws_ecs_task_definition.urgent_worker, aws_ecs_task_definition.routine_worker] :
      contains([for s in jsondecode(td.container_definitions)[0].secrets : s.name], "DATA_STORE_PROVISIONER_URL")
    ])
    error_message = "The maintenance workers should hold the provisioner's credentials."
  }
  assert {
    condition = !anytrue([
      for td in [aws_ecs_task_definition.api, aws_ecs_task_definition.run_service, aws_ecs_task_definition.ingest_worker] :
      contains([for s in jsondecode(td.container_definitions)[0].secrets : s.name], "DATA_STORE_PROVISIONER_URL")
    ])
    error_message = "No other service should hold the provisioner's credentials."
  }
}
