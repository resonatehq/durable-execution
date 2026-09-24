"""One process playing Cloud Run and Cloud Tasks: a clock a test can move,
and a loop that delivers what the queue holds."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..kernel import TAG_TARGET
from ..sdk import call_param, route
from ..types import Execute, PromiseCreate, Timeout, Unblock, decode_message
from ..worker import Worker


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


@dataclass
class Runtime:
    """One process playing the parts Cloud Run and Cloud Tasks play.

    Everything is a delivery to a URL, which is what Cloud Tasks does: a
    deadline is a timeout message to this service, a dispatch is an execute
    message to a worker. The runtime is then only a loop that takes what the
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
        target."""
        from ..sdk import TARGETS
        target = TARGETS.get(fn.name)
        if target is None:
            raise RuntimeError(f"{fn.name} is not routed anywhere, so nothing can run it")
        self.engine.process(PromiseCreate(
            id, self.clock() + timeout, call_param(fn, args),
            {TAG_TARGET: target}), self.clock())

    def handle(self, delivery) -> bool:
        """Deliver one task. Returns whether the handler answered."""
        msg = decode_message(delivery.body)
        if isinstance(msg, Timeout):
            self.swept += 1
            self.engine.process(msg, self.clock())
            return True
        if isinstance(msg, Execute):
            worker = self.workers.get(delivery.url)
            if worker is not None:
                # A refused acquire is still an answer: somebody else has it,
                # and delivering this again would not change that.
                worker.run(msg.task_id, msg.version)
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
