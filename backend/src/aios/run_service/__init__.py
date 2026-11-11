"""The run service: agent work, out of the web servers.

A run service process (`python -m aios.run_service`, `aios.run_service.executor`) claims items
from a work queue in the platform database (`aios.run_service.queue`) and runs many at once in
one event loop: mentioned agents' turns, the handoff before every mention, and the scheduler's
firings (`aios.scheduler`). The queue serves people waiting in chat before background work, and
the organisation with the least in flight first, under a soft per-organisation cap admins can
override; each process keeps some of its slots for chat. Items are leased, their leases extended
while they run; one whose process dies is retried elsewhere, and failed after a few attempts
(`aios.run_service.work`). Invocations left queued without an item (a lost enqueue) are put back
by the requeue sweep (`aios.run_service.sweep`).

`drain_once` runs every ready item to completion in-process, for tests.
"""

from aios.run_service.errors import STOPPED, CantStart
from aios.run_service.queue import Work, enqueue, wake

__all__ = ["STOPPED", "CantStart", "Work", "enqueue", "wake"]
