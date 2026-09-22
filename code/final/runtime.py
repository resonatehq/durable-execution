"""A worker, and the loop that keeps one fed.

`Worker.execute` is post 002's outer half, in the protocol's own words:

    claim      -> task.acquire
    run                                     the function, from the top
    complete   -> task.fulfill              it returned
    subscribe
      + release-> task.suspend              it raised Blocked
    release    -> task.release              it raised something else

`Runtime` is what a store, a queue and a clock add up to in one process: it
takes what the queue is willing to deliver, turns each URL back into the
call the Cloud Run route would make, and acknowledges whatever answered. In
production that loop is Cloud Tasks and those routes are HTTP; here they
are a dict and a method call, which is what makes a whole run a unit test.
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
from ports import Conflict, Unavailable
from spec.queue import SWEEP
from tracing import trace
from sdk import (
    _FRAME, _INVOCATION, PLATFORM, REGISTRY, Blocked, Invocation, _Call, describe, dumps,
    loads, route,
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

    @trace
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
            except PLATFORM:
                # Not the function's answer: this attempt could not produce
                # one. Hand the task back at the same version so it is
                # offered again, to this worker or another.
                self.engine.process(TaskRelease(task_id, v), self.clock())
                return "released"
            except Exception as e:
                # The function's answer, and an unwelcome one. A rejection is
                # a result: it is recorded, it wakes whoever was awaiting it,
                # and replay reads it back rather than running again.
                self.engine.process(TaskFulfill(
                    task_id, v, PromiseSettle(task_id, REJECTED, dumps(describe(e)))),
                    self.clock())
                return "rejected"
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
    """One process playing the parts Cloud Run and Cloud Tasks play.

    Everything is a delivery to a URL, which is what Cloud Tasks does: a
    deadline is a POST to the sweep endpoint for an origin, a dispatch is a
    POST to a worker. The runtime is then only a loop that takes what the
    queue offers, calls the handler, and says whether it worked.

    A handler that answers is acknowledged. One that cannot is not, and the
    queue decides whether to try again or give up, exactly as it would.

    There used to be a second runtime here, carrying messages in a list and
    firing deadlines from a heap. It existed only because the engine had
    two ports and they had two well-behaved in-memory gadgets. The engine
    has one port now, so there is one runtime, and it is the one whose
    failures are real.
    """

    engine: Any
    queue: Any
    clock: Clock
    workers: dict[str, Worker] = field(default_factory=dict)
    notified: dict[str, dict] = field(default_factory=dict)
    swept: int = 0

    def serve(self, address: str, worker: Worker, *functions) -> None:
        self.workers[address] = worker
        for fn in functions:
            route(fn, address)

    def start(self, id: str, fn, *args, timeout: int = 10 ** 9) -> None:
        from sdk import TARGETS
        target = TARGETS.get(fn.name)
        if target is None:
            raise RuntimeError(f"{fn.name} is not routed anywhere, so nothing can run it")
        self.engine.process(PromiseCreate(
            id, self.clock() + timeout, dumps({"f": fn.name, "a": args}),
            {TAG_TARGET: target}), self.clock())

    def handle(self, delivery) -> bool:
        """What the URL means. Returns whether the handler answered."""
        if delivery.url.startswith(SWEEP):
            self.swept += 1
            self.engine.process(Timeout(delivery.url[len(SWEEP):]), self.clock())
            return True
        from wire import decode_message

        msg = decode_message(delivery.body)
        if isinstance(msg, Execute):
            worker = self.workers.get(delivery.url)
            if worker is not None:
                # A refused acquire is still an answer: somebody else has it,
                # and delivering this again would not change that.
                worker.execute(msg.task_id, msg.version)
            return True
        if isinstance(msg, Unblock):
            self.notified[msg.promise["id"]] = msg.promise
            return True
        return True

    def step(self) -> bool:
        delivery = self.queue.take(self.clock())
        if delivery is None:
            return False
        if self.queue.loses_this_one():
            self.queue.nack(delivery, self.clock())
            return True
        try:
            answered = self.handle(delivery)
        except Exception:
            self.queue.nack(delivery, self.clock())
            return True
        if answered:
            self.queue.ack(delivery, self.clock())
        else:
            self.queue.nack(delivery, self.clock())
        return True

    def drain(self, budget: int = 2_000) -> int:
        """Deliver everything the queue is willing to deliver at this
        instant. What is scheduled later stays there until the clock moves,
        which is the difference between a queue and a list."""
        for did in range(budget):
            if not self.step():
                return did
        raise AssertionError("the queue never ran out of eligible work")
