"""A worker, and the loop that keeps one fed.

`Worker.execute` is post 002's outer half, in the protocol's own words:

    claim      -> task.acquire
    run                                     the function, from the top
    complete   -> task.fulfill              it returned
    subscribe
      + release-> task.suspend              it raised Blocked
    release    -> task.release              it raised something else

`Runtime` is what a bucket, a queue and a clock add up to in one process: it
hands execute messages to whichever worker serves the address, fires
deadlines that have come due, and stops when there is nothing left to do.
In production those three are Cloud Tasks and a timer; here they are a list
and a heap, which is what makes a whole run a unit test.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from typing import Any, Callable

from engine import Timeout
from kernel import (
    REJECTED, RESOLVED, Execute, PromiseCreate, PromiseSettle, TAG_TARGET,
    TaskAcquire, TaskFulfill, TaskRelease, TaskSuspend, Unblock, Value,
)
from sdk import (
    _FRAME, _INVOCATION, REGISTRY, Blocked, Failed, Invocation, _Call, dumps, loads, route,
)


@dataclass
class Clock:
    """Time, as something a test can move. A worker reads it; nothing else
    may, because a durable function that reads a clock between two calls is
    the one thing this model asks you not to do."""

    now: int = 0

    def __call__(self) -> int:
        return self.now

    def advance(self, by: int) -> int:
        self.now += by
        return self.now


class Worker:
    def __init__(self, engine, clock: Clock, pid: str, ttl: int = 30_000) -> None:
        self.engine, self.clock, self.pid, self.ttl = engine, clock, pid, ttl
        self.ran: list[str] = []  # which task ids this worker picked up, for tests

    def execute(self, task_id: str, version: int) -> str:
        """Run one task as far as it goes. The task is claimed once; the run
        itself may happen more than once, because a suspension that finds
        nothing left to wait for tells the caller to carry on."""
        reply = self.engine.process(
            TaskAcquire(task_id, version, self.pid, self.ttl), self.clock())
        if reply.status != 200:
            # Somebody else holds it, or it has moved on. Not an error: this
            # is what at-least-once delivery looks like from the inside.
            return "not mine"
        self.ran.append(task_id)
        task, promise = reply.data["task"], reply.data["promise"]
        v = task["version"]
        call = loads(promise["param"])
        fn = REGISTRY[call["f"]]

        while True:
            try:
                result = self._run(fn, call["a"], task_id, v)
            except Blocked as b:
                suspend = self.engine.process(
                    TaskSuspend(task_id, v, tuple(b.ids)), self.clock())
                if suspend.status == 300:
                    # One of them settled while we were deciding to wait for
                    # it. Nothing to wait for, so carry on from the top.
                    continue
                return "suspended"
            except Failed as e:
                self.engine.process(TaskFulfill(
                    task_id, v, PromiseSettle(task_id, REJECTED, dumps(str(e)))), self.clock())
                return "rejected"
            except Exception:
                # Not the function's answer: the worker could not produce one.
                # Hand the task back at the same version so it is offered again.
                self.engine.process(TaskRelease(task_id, v), self.clock())
                raise
            self.engine.process(TaskFulfill(
                task_id, v, PromiseSettle(task_id, RESOLVED, dumps(result))), self.clock())
            return "done"

    def _run(self, fn, args, task_id: str, version: int):
        """Drive one attempt of a durable function to its end or its block.

        A durable function is async, and a worker is not: something has to
        own the loop. It is here rather than around the whole runtime so a
        leaf that does real I/O can await it, while everything outside stays
        the ordinary synchronous shell it is in production.
        """
        async def attempt():
            _INVOCATION.set(Invocation(self.engine, task_id, version, self.clock))
            _FRAME.set(_Call(task_id))
            return await fn.invoke(*args)

        return asyncio.run(attempt())


@dataclass
class Runtime:
    engine: Any
    timers: Any
    transport: Any
    clock: Clock
    workers: dict[str, Worker] = field(default_factory=dict)
    #: Everything an `unblock` delivered, by promise id. What a client that
    #: registered a listener would have received.
    notified: dict[str, dict] = field(default_factory=dict)

    def serve(self, address: str, worker: Worker, *functions) -> None:
        """Put a worker at an address, and say which functions run there.
        The routing is deployment rather than definition: the same function
        is a local call on one machine and a remote one seen from another."""
        self.workers[address] = worker
        for fn in functions:
            route(fn, address)

    def start(self, id: str, fn, *args, timeout: int = 10 ** 9) -> None:
        """Create the run's own promise. It carries a target, so the engine
        dispatches it and a worker picks it up on the next drain."""
        from sdk import TARGETS
        target = TARGETS.get(fn.name)
        if target is None:
            raise RuntimeError(f"{fn.name} is not routed anywhere, so nothing can run it")
        self.engine.process(PromiseCreate(
            id, self.clock() + timeout, dumps({"f": fn.name, "a": args}),
            {TAG_TARGET: target}), self.clock())

    def step(self) -> bool:
        """One unit of work: deliver what is queued, or fire the nearest
        deadline that is due. False when there is nothing to do."""
        messages = self.transport.take()
        if messages:
            for address, msg in messages:
                if isinstance(msg, Execute):
                    worker = self.workers.get(address)
                    if worker is not None:
                        worker.execute(msg.task_id, msg.version)
                elif isinstance(msg, Unblock):
                    self.notified[msg.promise["id"]] = msg.promise
            return True
        due = self.timers.due(self.clock())
        if not due:
            return False
        name, origin = due[0]
        self.timers.armed.pop(name, None)
        self.engine.process(Timeout(origin), self.clock())
        return True

    def drain(self, budget: int = 500) -> int:
        """Step until there is nothing left. Returns how many steps it took,
        so a test can tell the difference between quiet and stuck."""
        for did in range(budget):
            if not self.step():
                return did
        raise AssertionError("the runtime did not settle down")
