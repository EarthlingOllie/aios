"""Celery's queues (`aios.worker`): ingest, urgent maintenance and routine maintenance, each with
its own worker service (`docker-compose.yml`). Approved changes and restores run on the urgent
queue, never behind migrations or sweeps; the beat runs on one routine worker and only enqueues;
the provisioning queue is retired once drained; only the maintenance workers hold the
provisioner's credentials."""

from pathlib import Path

import yaml

from aios import worker

COMPOSE = Path(__file__).resolve().parents[2] / "docker-compose.yml"

URGENT = {"aios.apply_schema_change", "aios.restore_dropped", "aios.apply_agent_access"}
ROUTINE = {
    "aios.provision_data_store",
    "aios.retry_unfinished_data_stores",
    "aios.delete_data_stores",
    "aios.delete_due_organisations",
    "aios.measure_data_store_usage",
    "aios.purge_dropped",
    "aios.expire_schema_change_requests",
    "aios.prune_retention",
    "aios.sweep_events",
    "aios.repair_stores",
    "aios.drain_audit_outbox",
    "aios.publish_queue_metrics",
}
INGEST = {"aios.ingest_document", "aios.check_document", "aios.embed_search_items"}


def queue_of(task: str) -> str:
    return worker.celery_app.amqp.router.route({}, task)["queue"].name


def our_tasks() -> set[str]:
    return {name for name in worker.celery_app.tasks if name.startswith("aios.")}


def services() -> dict:
    return yaml.safe_load(COMPOSE.read_text())["services"]


def command(service: dict) -> list[str]:
    return service.get("command") or []


def queues_consumed(service: dict) -> set[str]:
    cmd = command(service)
    return set(cmd[cmd.index("-Q") + 1].split(",")) if "-Q" in cmd else set()


def test_every_task_is_on_one_of_the_three_queues():
    assert {queue_of(task) for task in our_tasks()} == {"ingest", "urgent", "routine"}
    assert {t for t in our_tasks() if queue_of(t) == "urgent"} == URGENT
    for task in ROUTINE:
        assert queue_of(task) == "routine", task
    for task in INGEST:
        assert queue_of(task) == "ingest", task


def test_approved_changes_and_restores_never_wait_behind_migrations_or_sweeps():
    scheduled = {entry["task"] for entry in worker.celery_app.conf.beat_schedule.values()}
    assert not scheduled & URGENT
    urgent_workers = [s for s in services().values() if "urgent" in queues_consumed(s)]
    assert len(urgent_workers) == 1
    # Its worker takes nothing but urgent work, and runs no beat.
    [urgent] = urgent_workers
    assert queues_consumed(urgent) == {"urgent"}
    assert "-B" not in command(urgent) and "beat" not in command(urgent)


def test_the_beat_runs_on_one_routine_worker_and_only_enqueues():
    beats = [
        name
        for name, s in services().items()
        if "-B" in command(s) or (len(command(s)) > 3 and command(s)[3] == "beat")
    ]
    assert beats == ["routine-worker"]
    assert "routine" in queues_consumed(services()["routine-worker"])
    # What it schedules is queued for the maintenance and ingest workers to run, never run in
    # the beat itself, and never on the urgent queue.
    for entry in worker.celery_app.conf.beat_schedule.values():
        assert queue_of(entry["task"]) in {"routine", "ingest"}, entry["task"]


def test_each_queue_has_its_own_worker_service():
    consumed = {
        name: queues_consumed(s)
        for name, s in services().items()
        if command(s)[:3] == ["celery", "-A", "aios.worker"]
        and "fullstack" not in s.get("profiles", [])
    }
    assert consumed == {
        "ingest-worker": {"ingest"},
        "urgent-worker": {"urgent"},
        "routine-worker": {"routine", "provisioning"},
    }


def test_the_provisioning_queue_is_retired_once_drained():
    # Nothing is queued on it any more; the routine worker drains what's left there from before.
    assert "provisioning" not in {queue_of(task) for task in our_tasks()}
    assert "provisioning" in worker.RETIRED_QUEUES
    assert "provisioning" in queues_consumed(services()["routine-worker"])
    assert "provisioning-worker" not in services()


def test_only_the_maintenance_workers_hold_the_provisioners_credentials():
    def has_credentials(service: dict) -> bool:
        return "DATA_STORE_PROVISIONER_URL" in (service.get("environment") or {})

    holders = {name for name, s in services().items() if has_credentials(s)}
    # The two maintenance workers, and the deploy's one-off tasks: making the roles, and
    # migrating the stores.
    assert holders == {"urgent-worker", "routine-worker", "db-roles", "migrate-stores"}
