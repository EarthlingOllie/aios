"""The run service's lanes (`aios.run_service.queue`): the `sources` lane beside interactive and
background, fair claiming between organisations within each lane, and interactive work's
reserved capacity, which neither background nor sources work takes."""

import asyncio

from test_run_service import claim, enqueue, items, org_and_store, recorder, tags  # noqa: F401

from aios.models import WorkLane
from aios.run_service import executor


def test_sources_work_is_claimed_fairly_between_organisations(db, user, new_user):
    big, small = org_and_store(db, user), org_and_store(db, new_user())
    enqueue(items(big, WorkLane.sources, 5, "big-sync"))
    enqueue(items(small, WorkLane.sources, 1, "small-sync"))
    [first] = claim(1)
    assert tags([first]) == ["big-sync"]
    # `big` now has a sync in flight and `small` none: `small`'s goes next, though it's newer.
    assert tags(claim(2)) == ["small-sync", "big-sync"]


def test_fairness_between_organisations_holds_within_each_lane(db, user, new_user):
    syncing, scheduling = org_and_store(db, user), org_and_store(db, new_user())
    enqueue(items(syncing, WorkLane.sources, 3, "sync"))
    enqueue(items(scheduling, WorkLane.background, 2, "schedule"))
    assert sorted(tags(claim(5))) == ["schedule"] * 2 + ["sync"] * 3
    # `syncing` has more in flight overall, but none in the background lane: its schedule goes
    # first there, though it's newer.
    enqueue(items(scheduling, WorkLane.background, 1, "scheduling-again"))
    enqueue(items(syncing, WorkLane.background, 1, "syncing-schedule"))
    assert tags(claim(1)) == ["syncing-schedule"]


def test_sources_work_never_takes_interactive_works_reserved_capacity(db, user, recorder):  # noqa: F811
    org = org_and_store(db, user)
    recorder.blocking = True
    enqueue(items(org, WorkLane.sources, 10, "sync"))
    enqueue(items(org, WorkLane.background, 10, "schedule"))

    async def go():
        process = executor.Executor(concurrency=6, reserve=2, sources_limit=10)
        await process.claim()
        await process.claim()
        # Background and sources work share the 4 slots that aren't reserved.
        assert len(process.running) == 4
        await asyncio.to_thread(enqueue, items(org, WorkLane.interactive, 3, "chat"))
        await process.claim()
        running = [item.payload["tag"] for item, _ in process.running.values()]
        assert running.count("chat") == 2
        recorder.release.set()
        await asyncio.gather(*(task for _, task in process.running.values()))

    asyncio.run(go())


def test_a_big_sync_waits_on_its_own_share_of_the_background_room(db, user, recorder):  # noqa: F811
    org = org_and_store(db, user)
    recorder.blocking = True
    enqueue(items(org, WorkLane.sources, 10, "sync"))

    async def go():
        process = executor.Executor(concurrency=6, reserve=1, sources_limit=2)
        await process.claim()
        assert len(process.running) == 2
        await asyncio.to_thread(enqueue, items(org, WorkLane.background, 10, "schedule"))
        await process.claim()
        running = sorted(item.payload["tag"] for item, _ in process.running.values())
        assert running == ["schedule"] * 3 + ["sync"] * 2
        recorder.release.set()
        await asyncio.gather(*(task for _, task in process.running.values()))

    asyncio.run(go())
