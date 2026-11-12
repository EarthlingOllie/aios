"""Queue metrics, for seeing load and for autoscaling (`infra/terraform/autoscaling.tf`):
published every `queue_metrics_interval_seconds` by the beat (`aios.worker`), on the routine
worker.

- Per run service lane (`aios.models.WorkLane`): `Depth`, the items ready and waiting to be
  claimed, and `OldestReadyAge`, how long (seconds) the oldest of them has been ready; 0 when
  none is. The interactive lane's is the latency people in chat see, which run service
  processes scale on, with the background and sources lanes' depth.
- Per Celery queue (`aios.worker.QUEUES`, and the retired ones until they're dropped):
  `Depth`, the messages waiting on it in the broker. Ingest workers scale on the ingest
  queue's.

Each is logged, and sent to CloudWatch under `queue_metrics_namespace` where it's set (on AWS).
"""

import logging
from dataclasses import dataclass

import boto3
import redis
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import WorkItem, WorkLane

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class Metric:
    name: str
    dimensions: dict[str, str]
    value: float
    unit: str = "Count"


def _lanes(db: Session) -> list[Metric]:
    waiting = (
        select(
            WorkItem.lane,
            func.count(),
            func.extract("epoch", func.now() - func.min(WorkItem.ready_at)),
        )
        .where(
            WorkItem.ready_at <= func.now(),
            (WorkItem.leased_until.is_(None)) | (WorkItem.leased_until <= func.now()),
        )
        .group_by(WorkItem.lane)
    )
    found = {lane: (depth, float(age or 0)) for lane, depth, age in db.execute(waiting)}
    metrics = []
    for lane in WorkLane:
        depth, age = found.get(lane, (0, 0.0))
        metrics.append(Metric("Depth", {"Lane": lane.value}, depth))
        metrics.append(Metric("OldestReadyAge", {"Lane": lane.value}, max(age, 0.0), "Seconds"))
    return metrics


def _celery_queues(broker: redis.Redis) -> list[Metric]:
    from aios.worker import QUEUES, RETIRED_QUEUES

    return [
        Metric("Depth", {"Queue": name}, broker.llen(name)) for name in (*QUEUES, *RETIRED_QUEUES)
    ]


def collect(db: Session, broker: redis.Redis) -> list[Metric]:
    """The metrics as they stand: the run service's lanes, from the platform database, and
    Celery's queues, from its broker."""
    return _lanes(db) + _celery_queues(broker)


def cloudwatch():
    return boto3.client("cloudwatch")


def publish() -> list[Metric]:
    """Collect the metrics, log them, and send them to CloudWatch where a namespace is set."""
    settings = get_settings()
    with SessionLocal() as db, redis.Redis.from_url(settings.redis_url) as broker:
        metrics = collect(db, broker)
    for m in metrics:
        dimension = ", ".join(f"{k}={v}" for k, v in m.dimensions.items())
        log.info("queue metric %s %s: %s", m.name, dimension, m.value)
    if namespace := settings.queue_metrics_namespace:
        cloudwatch().put_metric_data(
            Namespace=namespace,
            MetricData=[
                {
                    "MetricName": m.name,
                    "Dimensions": [{"Name": k, "Value": v} for k, v in m.dimensions.items()],
                    "Value": m.value,
                    "Unit": m.unit,
                }
                for m in metrics
            ],
        )
    return metrics
