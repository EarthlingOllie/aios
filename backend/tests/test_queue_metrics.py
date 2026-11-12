"""Queue metrics (`aios.queue_metrics`), published every `queue_metrics_interval_seconds` by the
beat: per run service lane, the depth and the age of the oldest ready item; per Celery queue, its
depth. To CloudWatch where a namespace is set (on AWS), and logged either way."""

import logging
from datetime import timedelta

import pytest
from sqlalchemy import func, update
from test_run_service import claim, enqueue, items, org_and_store

from aios import queue_metrics, worker
from aios.config import get_settings
from aios.db import SessionLocal
from aios.models import WorkItem, WorkLane


class FakeBroker:
    """Celery's broker, standing in for Redis: its queues' lengths. (A worker left running
    against the test Redis would consume a real queue under the test.)"""

    def __init__(self) -> None:
        self.queues: dict[str, list[str]] = {}

    def rpush(self, name: str, *values: str) -> None:
        self.queues.setdefault(name, []).extend(values)

    def llen(self, name: str) -> int:
        return len(self.queues.get(name, []))


@pytest.fixture
def broker() -> FakeBroker:
    return FakeBroker()


def made_ready_ago(tag: str, seconds: float) -> None:
    with SessionLocal() as db:
        db.execute(
            update(WorkItem)
            .where(WorkItem.payload["tag"].astext == tag)
            .values(ready_at=func.now() - timedelta(seconds=seconds))
        )
        db.commit()


def as_dict(metrics: list[queue_metrics.Metric]) -> dict[tuple[str, str, str], float]:
    return {(m.name, *next(iter(m.dimensions.items()))): m.value for m in metrics}


def test_depth_and_oldest_ready_age_per_lane_and_depth_per_celery_queue(db, user, broker):
    org = org_and_store(db, user)
    enqueue(items(org, WorkLane.interactive, 1, "mention"))
    made_ready_ago("mention", 7)
    enqueue(items(org, WorkLane.background, 1, "fresh"))
    # The mention is claimed (interactive first): it's no longer waiting.
    assert [c.lane for c in claim(1, 1)] == [WorkLane.interactive]
    enqueue(items(org, WorkLane.background, 2, "schedule"))
    made_ready_ago("schedule", 40)
    enqueue(items(org, WorkLane.sources, 1, "later"))
    made_ready_ago("later", -600)  # not ready for ten minutes
    broker.rpush("ingest", "a", "b", "c")
    broker.rpush("provisioning", "left-over")

    with SessionLocal() as session:
        metrics = as_dict(queue_metrics.collect(session, broker))

    assert metrics[("Depth", "Lane", "interactive")] == 0
    assert metrics[("OldestReadyAge", "Lane", "interactive")] == 0
    # The fresh one and the two schedules are waiting, the oldest for 40 seconds.
    assert metrics[("Depth", "Lane", "background")] == 3
    assert 40 <= metrics[("OldestReadyAge", "Lane", "background")] < 60
    assert metrics[("Depth", "Lane", "sources")] == 0
    assert metrics[("Depth", "Queue", "ingest")] == 3
    assert metrics[("Depth", "Queue", "urgent")] == 0
    assert metrics[("Depth", "Queue", "routine")] == 0
    # The retired queue's, so an operator can see it's drained.
    assert metrics[("Depth", "Queue", "provisioning")] == 1


class FakeCloudWatch:
    def __init__(self) -> None:
        self.sent: list[dict] = []

    def put_metric_data(self, **kwargs) -> None:
        self.sent.append(kwargs)


def test_published_to_cloudwatch_where_a_namespace_is_set_and_logged(db, user, monkeypatch, caplog):
    fake = FakeCloudWatch()
    monkeypatch.setattr(queue_metrics, "cloudwatch", lambda: fake)
    org = org_and_store(db, user)
    enqueue(items(org, WorkLane.interactive, 2, "mention"))
    made_ready_ago("mention", 3)

    with caplog.at_level(logging.INFO, logger="aios.queue_metrics"):
        worker.publish_queue_metrics_task()
    # Locally (no namespace): logged only.
    assert fake.sent == []
    assert "queue metric Depth Lane=interactive: 2" in caplog.text

    monkeypatch.setattr(get_settings(), "queue_metrics_namespace", "aios/queues")
    worker.publish_queue_metrics_task()
    [sent] = fake.sent
    assert sent["Namespace"] == "aios/queues"
    data = {
        (d["MetricName"], d["Dimensions"][0]["Value"]): (d["Value"], d["Unit"])
        for d in sent["MetricData"]
    }
    assert data[("Depth", "interactive")] == (2, "Count")
    age, unit = data[("OldestReadyAge", "interactive")]
    assert unit == "Seconds" and 3 <= age < 30
    assert data[("Depth", "ingest")] == (0, "Count")


def test_the_beat_publishes_them_every_thirty_seconds_on_the_routine_worker():
    entry = worker.celery_app.conf.beat_schedule["publish-queue-metrics"]
    assert entry["task"] == "aios.publish_queue_metrics"
    assert entry["schedule"] == get_settings().queue_metrics_interval_seconds == 30
    assert worker.celery_app.conf.task_routes["aios.publish_queue_metrics"] == {"queue": "routine"}
