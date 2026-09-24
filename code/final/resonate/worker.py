"""A worker: claim a task, run its function until it blocks or finishes,
and tell the engine which.

    run        task.acquire, then loop over _attempt:
                 returned        -> task.fulfill (resolved)
                 raised Blocked  -> task.suspend  (again if nothing to wait for)
                 platform error  -> task.release
                 anything else   -> task.fulfill (rejected)
    _attempt   one run of the function from the top
"""

from __future__ import annotations

import asyncio
from typing import Callable

from .kernel import REJECTED, RESOLVED
from .sdk import (
    _FRAME, _INVOCATION, PLATFORM, Blocked, Invocation, _Call, called,
    describe, dumps, loads, lookup,
)
from .types import PromiseSettle, TaskAcquire, TaskFulfill, TaskRelease, TaskSuspend


class Worker:
    def __init__(self, engine, clock: Callable[[], int], pid: str, ttl: int = 30_000) -> None:
        self.engine, self.clock, self.pid, self.ttl = engine, clock, pid, ttl
        self.ran: list[str] = []  # which task ids this worker picked up, for tests

    def run(self, task_id: str, version: int) -> str:
        """Claim one task and see its run through, whatever the run does.

        The task is claimed once; `_attempt` may run more than once,
        because a suspension that finds nothing left to wait for carries on
        from the top rather than parking.
        """
        reply = self.engine.process(
            TaskAcquire(task_id, version, self.pid, self.ttl), self.clock())
        if reply.status != 200:
            # Somebody else holds it, or it has moved on. Not an error: this
            # is what at-least-once delivery looks like from the inside.
            return "not mine"
        self.ran.append(task_id)
        task, promise = reply.data["task"], reply.data["promise"]
        v = task["version"]
        # The version travels with the call, so a task created before a
        # deploy still names the body it was written against. A worker that
        # no longer carries it says so rather than running the nearest thing.
        name, version, args = called(loads(promise["param"]))
        fn = lookup(name, version)

        while True:
            try:
                result = self._attempt(fn, args, task_id, v)
            except Blocked as b:
                # Not a failure. Waiting for a value you do not have is
                # how this system makes progress.
                suspend = self.engine.process(
                    TaskSuspend(task_id, v, tuple(b.ids)), self.clock())
                if suspend.status == 300:
                    # One of them settled while we were deciding to wait
                    # for it. Nothing to wait for, so carry on from the top.
                    continue
                return "suspended"
            except PLATFORM:
                # Not the function's answer: this attempt could not
                # produce one. Hand the task back at the same version so
                # it is offered again, to this worker or another.
                self.engine.process(TaskRelease(task_id, v), self.clock())
                return "released"
            except Exception as e:
                # The function's answer, and an unwelcome one. A rejection
                # is a result: it is recorded, it wakes whoever was
                # awaiting it, and replay reads it back rather than
                # running again.
                self.engine.process(TaskFulfill(
                    task_id, v, PromiseSettle(task_id, REJECTED, dumps(describe(e)))),
                    self.clock())
                return "rejected"
            self.engine.process(TaskFulfill(
                task_id, v, PromiseSettle(task_id, RESOLVED, dumps(result))),
                self.clock())
            return "done"

    def _attempt(self, fn, args, task_id: str, version: int):
        """One attempt at the function, from the top.

        Returns what it returned, or raises: `Blocked` when it stopped for
        a value it does not have yet, whatever the function itself raised,
        or a platform failure. Deciding what any of those mean is `run`'s
        business, not this one's.

        A durable function is async and a worker is not, so something has
        to own the event loop. It is here, per attempt, so a leaf that does
        real I/O can await it while everything outside stays synchronous.

        The two context variables are what make a durable call durable: the
        invocation carries the task and the version every write is fenced
        at, and the frame carries the position the next call's id comes
        from. `asyncio` copies the context into each task it creates, so a
        `gather`'s branches each get their own frame and cannot tread on
        each other.
        """
        async def attempt():
            _INVOCATION.set(Invocation(self.engine, task_id, version, self.clock))
            _FRAME.set(_Call(task_id))
            return await fn.invoke(*args)

        return asyncio.run(attempt())
