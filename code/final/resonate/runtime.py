"""A worker, and the loop that keeps one fed.

A worker is post 002's two halves, under the post's own names.

`execute_until_blocked_outer` is the protocol half. It never runs a line
of anyone's function; it claims the task and then decides what the run's
outcome means, in the protocol's own words:

    claim      -> task.acquire
    run                                     the inner half, from the top
    complete   -> task.fulfill              it returned
    subscribe
      + release-> task.suspend              it raised Blocked
    release    -> task.release              it raised something else

`execute_until_blocked_inner` is the other half: one attempt at the
function itself, from the top, until it returns a value or stops for
something it does not have. It knows nothing about tasks or leases — only
how to run a durable function and let whatever happens propagate.

The split is the whole shape of the thing. The outer half is the same for
every function there will ever be; the inner half is the same whatever the
protocol does next. And the loop between them exists for one case: a
suspension that finds nothing left to wait for, which sends the inner half
round again rather than parking a task nothing will ever wake.

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

from .engine import Timeout
from . import otel
from .kernel import (
    REJECTED, RESOLVED, Execute, PromiseCreate, PromiseSettle, TAG_TARGET,
    TaskAcquire, TaskFulfill, TaskRelease, TaskSuspend, Unblock, Value, origin_of,
)
from .ports import Conflict, Unavailable
from .spec.queue import SWEEP
from .tracing import because, trace
from .sdk import (
    _FRAME, _INVOCATION, PLATFORM, Blocked, Invocation, _Call, call_param, called,
    describe, dumps, lookup,
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
    def execute_until_blocked_outer(self, task_id: str, version: int) -> str:
        """Claim one task and see its run through, whatever the run does.

        The task is claimed once; the inner half may run more than once,
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
            # One span per turn of this loop, because one turn is one attempt
            # at the function. The loop is why the attempt is the unit and
            # the delivery is not: a suspension with nothing left to wait for
            # runs the body again, in this same request.
            with otel.attempt(task_id, origin_of(task_id), self.clock,
                              name=fn.label, **{"de.worker": self.pid,
                                                 "de.task.version": v}) as span:
                try:
                    result = self.execute_until_blocked_inner(fn, args, task_id, v)
                except Blocked as b:
                    # Not a failure. Waiting for a value you do not have is
                    # how this system makes progress, and colouring it red
                    # would colour every fan-out red.
                    span["de.outcome"] = "suspended"
                    span["de.waiting"] = len(b.ids)
                    suspend = self.engine.process(
                        TaskSuspend(task_id, v, tuple(b.ids)), self.clock())
                    if suspend.status == 300:
                        # One of them settled while we were deciding to wait
                        # for it. Nothing to wait for, so carry on from the
                        # top -- and that is a new attempt, hence a new span.
                        continue
                    return "suspended"
                except PLATFORM as e:
                    # Not the function's answer: this attempt could not
                    # produce one. Hand the task back at the same version so
                    # it is offered again, to this worker or another.
                    span["de.outcome"] = "released"
                    span["de.error"] = type(e).__name__
                    span["status"] = otel.ERROR
                    self.engine.process(TaskRelease(task_id, v), self.clock())
                    return "released"
                except Exception as e:
                    # The function's answer, and an unwelcome one. A rejection
                    # is a result: it is recorded, it wakes whoever was
                    # awaiting it, and replay reads it back rather than
                    # running again.
                    span["de.outcome"] = "rejected"
                    span["de.error"] = type(e).__name__
                    span["status"] = otel.ERROR
                    self.engine.process(TaskFulfill(
                        task_id, v, PromiseSettle(task_id, REJECTED, dumps(describe(e)))),
                        self.clock())
                    return "rejected"
                span["de.outcome"] = "done"
                span["status"] = otel.OK
                self.engine.process(TaskFulfill(
                    task_id, v, PromiseSettle(task_id, RESOLVED, dumps(result))),
                    self.clock())
                return "done"

    @trace
    def execute_until_blocked_inner(self, fn, args, task_id: str, version: int):
        """One attempt at the function, from the top.

        Returns what it returned, or raises: `Blocked` when it stopped for
        a value it does not have yet, whatever the function itself raised,
        or a platform failure. Deciding what any of those mean is the outer
        half's business, not this one's.

        A durable function is async and a worker is not, so something has
        to own the event loop. It is here rather than around the whole
        runtime, so a leaf that does real I/O can await it while everything
        outside stays the ordinary synchronous shell it is in production.

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
        """What a client does to begin a run: create a promise with a
        target. Named as the route it would arrive on, because that is
        what it is."""
        from .sdk import TARGETS
        target = TARGETS.get(fn.name)
        if target is None:
            raise RuntimeError(f"{fn.name} is not routed anywhere, so nothing can run it")
        with because("POST /"):
            self.engine.process(PromiseCreate(
                id, self.clock() + timeout, call_param(fn, args),
                {TAG_TARGET: target}), self.clock())

    def handle(self, delivery) -> bool:
        """What the URL means. Returns whether the handler answered.

        The URL is named in the trace rather than left to be inferred,
        because in production this is a route on a service and the thing
        that caused the work is the delivery. `Routes.handle` does the
        same for a real request.
        """
        with because(delivery.url):
            return self._handle(delivery)

    def _handle(self, delivery) -> bool:
        if delivery.url.startswith(SWEEP):
            self.swept += 1
            self.engine.process(Timeout(delivery.url[len(SWEEP):]), self.clock())
            return True
        from .wire import decode_message

        msg = decode_message(delivery.body)
        if isinstance(msg, Execute):
            worker = self.workers.get(delivery.url)
            if worker is not None:
                # A refused acquire is still an answer: somebody else has it,
                # and delivering this again would not change that.
                worker.execute_until_blocked_outer(msg.task_id, msg.version)
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
