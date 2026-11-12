# Autoscaling: capacity follows demand, between each service's min and max (variables.tf).
#
# - The run service's executors scale on the age of the oldest ready interactive item (people
#   waiting in chat; target autoscaling.interactive_age_target seconds), and on the background and
#   sources lanes' depth per task.
# - Ingest workers scale on the ingest queue's depth per task; urgent workers on the urgent
#   queue's.
# - The api and web scale on CPU. The routine worker runs the beat, so stays at its one.
#
# The metrics are the queue metrics (aios.queue_metrics), published to CloudWatch by the routine
# worker's beat every var.queue_metrics_interval seconds. Scaling in waits
# autoscaling.scale_in_cooldown seconds (10 minutes) after the last change, so idle services
# return to their floor then; each stops gracefully (its stopTimeout, ecs.tf), so shrinking
# never fails a turn or loses an ingest.

locals {
  queue_metrics_namespace = "${var.name}/queues"

  # Per-task depth targets: the lane's or queue's depth, divided by the service's running tasks.
  depth_policies = {
    run-service-background = { service = "run-service", dimension = "Lane", value = "background", per_task = var.autoscaling.background_per_task }
    run-service-sources    = { service = "run-service", dimension = "Lane", value = "sources", per_task = var.autoscaling.sources_per_task }
    ingest-worker-ingest   = { service = "ingest-worker", dimension = "Queue", value = "ingest", per_task = var.autoscaling.ingest_per_task }
    urgent-worker-urgent   = { service = "urgent-worker", dimension = "Queue", value = "urgent", per_task = var.autoscaling.urgent_per_task }
  }
}

resource "aws_appautoscaling_target" "service" {
  for_each           = local.services
  service_namespace  = "ecs"
  scalable_dimension = "ecs:service:DesiredCount"
  resource_id        = "service/${aws_ecs_cluster.main.name}/${aws_ecs_service.this[each.key].name}"
  min_capacity       = var.run_services ? each.value.size.min : 0
  max_capacity       = var.run_services ? each.value.size.max : 0
}

# Executors: the oldest ready interactive item's age, kept at the target.
resource "aws_appautoscaling_policy" "run_service_interactive_age" {
  name               = "${var.name}-run-service-interactive-age"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.service["run-service"].service_namespace
  resource_id        = aws_appautoscaling_target.service["run-service"].resource_id
  scalable_dimension = aws_appautoscaling_target.service["run-service"].scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = var.autoscaling.interactive_age_target
    scale_in_cooldown  = var.autoscaling.scale_in_cooldown
    scale_out_cooldown = var.autoscaling.scale_out_cooldown

    customized_metric_specification {
      metric_name = "OldestReadyAge"
      namespace   = local.queue_metrics_namespace
      statistic   = "Maximum"
      unit        = "Seconds"
      dimensions {
        name  = "Lane"
        value = "interactive"
      }
    }
  }
}

# Depth per running task, kept at each policy's target. With several policies on a service, it
# scales out on whichever asks for more, and in only when all of them allow it.
resource "aws_appautoscaling_policy" "depth" {
  for_each           = local.depth_policies
  name               = "${var.name}-${each.key}-depth"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.service[each.value.service].service_namespace
  resource_id        = aws_appautoscaling_target.service[each.value.service].resource_id
  scalable_dimension = aws_appautoscaling_target.service[each.value.service].scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = each.value.per_task
    scale_in_cooldown  = var.autoscaling.scale_in_cooldown
    scale_out_cooldown = var.autoscaling.scale_out_cooldown

    customized_metric_specification {
      metrics {
        id          = "depth"
        return_data = false
        metric_stat {
          stat = "Average"
          metric {
            metric_name = "Depth"
            namespace   = local.queue_metrics_namespace
            dimensions {
              name  = each.value.dimension
              value = each.value.value
            }
          }
        }
      }
      metrics {
        id          = "tasks"
        return_data = false
        metric_stat {
          stat = "Average"
          metric {
            metric_name = "RunningTaskCount"
            namespace   = "ECS/ContainerInsights"
            dimensions {
              name  = "ClusterName"
              value = aws_ecs_cluster.main.name
            }
            dimensions {
              name  = "ServiceName"
              value = each.value.service
            }
          }
        }
      }
      metrics {
        id          = "per_task"
        label       = "${each.value.value} depth per task"
        expression  = "depth / IF(tasks < 1, 1, tasks)"
        return_data = true
      }
    }
  }
}

# The api and web, on CPU.
resource "aws_appautoscaling_policy" "cpu" {
  for_each           = toset(["api", "web"])
  name               = "${var.name}-${each.key}-cpu"
  policy_type        = "TargetTrackingScaling"
  service_namespace  = aws_appautoscaling_target.service[each.key].service_namespace
  resource_id        = aws_appautoscaling_target.service[each.key].resource_id
  scalable_dimension = aws_appautoscaling_target.service[each.key].scalable_dimension

  target_tracking_scaling_policy_configuration {
    target_value       = var.autoscaling.cpu_target
    scale_in_cooldown  = var.autoscaling.scale_in_cooldown
    scale_out_cooldown = var.autoscaling.scale_out_cooldown

    predefined_metric_specification {
      predefined_metric_type = "ECSServiceAverageCPUUtilization"
    }
  }
}

# The routine worker's runtime role: it publishes the queue metrics, to their namespace only.
resource "aws_iam_role" "routine_worker" {
  name               = "${var.name}-ecs-routine-worker"
  assume_role_policy = data.aws_iam_policy_document.ecs_assume.json
}

data "aws_iam_policy_document" "publish_queue_metrics" {
  statement {
    actions   = ["cloudwatch:PutMetricData"]
    resources = ["*"]
    condition {
      test     = "StringEquals"
      variable = "cloudwatch:namespace"
      values   = [local.queue_metrics_namespace]
    }
  }
}

resource "aws_iam_role_policy" "routine_worker_queue_metrics" {
  role   = aws_iam_role.routine_worker.id
  policy = data.aws_iam_policy_document.publish_queue_metrics.json
}
