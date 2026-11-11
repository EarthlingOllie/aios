# Jev's health (backend aios.jev_health): the worker polls it every 30 seconds and writes a JevUp
# metric (1 up, 0 down) in CloudWatch's embedded metric format on its log, which CloudWatch turns
# into the aios/Jev namespace's metric. While Jev is down, documents wait unsearchable and checked
# tool results fail (it fails closed), so being down for three minutes raises this alarm, and so
# does the metric going missing (the poll itself has stopped).
resource "aws_cloudwatch_metric_alarm" "jev_down" {
  alarm_name          = "${var.name}-jev-down"
  alarm_description   = "Jev, the prompt-injection classifier, is down or no longer polled: documents wait unsearchable and agents' checked tool results fail."
  namespace           = "aios/Jev"
  metric_name         = "JevUp"
  statistic           = "Minimum"
  period              = 60
  evaluation_periods  = 3
  comparison_operator = "LessThanThreshold"
  threshold           = 1
  treat_missing_data  = "breaching"
  alarm_actions       = var.alarm_actions
  ok_actions          = var.alarm_actions
}
