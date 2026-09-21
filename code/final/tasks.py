"""The queue, with the parts that bite.

In production there is one queue, not two mechanisms. A deadline and a
dispatch are the same object: a task with an HTTP target and a time before
which it must not be delivered. Our two ports, `Timers` and `Transport`,
are two uses of it, and modelling them as separate well-behaved gadgets
hides everything interesting. So this is one queue, and both ports adapt
onto it.

What a real queue does that a list does not:

- **At-least-once.** A delivery is retried until the handler acknowledges
  it, and an acknowledgement can be lost after the handler acted. Duplicate
  delivery is not an edge case, it is the contract.
- **No order.** Nothing promises that an earlier task is delivered first,
  and in practice newer ones commonly overtake older ones. An `execute` for
  version 0 can arrive after the task has already moved to version 1.
- **Not-before is a floor.** A scheduled time says when a task becomes
  eligible, not when it is delivered. Late is normal.
- **It gives up.** A task that has failed enough times is dropped. What was
  in it is gone.

Every one of those is a knob here, off by default so a test can turn on one
at a time and say which one it is about.

## What the last one costs, and it is worth stating plainly

A dropped `execute` is recoverable: the task's retry deadline was committed
before the message left, so the sweep offers it again. A dropped *sweep* is
not recoverable by anything in this design — the deadline it carried is the
only thing that was going to fire. A deployment needs either a retry policy
generous enough that this does not happen, or a periodic sweep over the
bucket that does not depend on any single queued task. `test_tasks.py`
demonstrates the hole rather than pretending it is not there.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True)
class Delivery:
    """One attempt to hand a task to its handler."""

    name: str
    url: str
    body: Any
    attempt: int


class Queue(Protocol):
    def create(self, url: str, body: Any, *, not_before: int = 0) -> str:
        """Enqueue, and return the name the service gave it.

        The name is the service's, not the caller's. A caller-chosen name
        leaves a tombstone after deletion, so re-creating the same name
        within the hour is refused, which is exactly the trap a deadline
        re-armed at the same instant would fall into.
        """

    def delete(self, name: str) -> None:
        """Cancel. Cancelling what is gone, or what is already out for
        delivery, succeeds and may be too late."""


@dataclass
class _Entry:
    url: str
    body: Any
    not_before: int
    attempts: int = 0


@dataclass
class MemoryQueue:
    """Cloud Tasks, as much of it as matters.

    Deterministic under a seed, so a run that finds something can be run
    again and find it again.
    """

    seed: int = 0
    #: Chance that an acknowledgement is lost, so the task is delivered
    #: again after the handler has already acted on it.
    duplicate: float = 0.0
    #: Whether eligible tasks come out in an order nobody promised.
    shuffle: bool = False
    #: How much later than its scheduled time a task may become eligible.
    lateness: int = 0
    #: Chance a delivery attempt never reaches the handler at all.
    lose: float = 0.0
    #: Attempts before the queue gives up and the task is gone.
    give_up_after: int = 20
    #: How long a failed attempt waits before the next one.
    backoff: int = 1_000

    entries: dict[str, _Entry] = field(default_factory=dict)
    dropped: list[str] = field(default_factory=list)
    delivered: int = 0
    _n: int = 0
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # -- the caller's side --------------------------------------------------

    def create(self, url: str, body: Any, *, not_before: int = 0) -> str:
        self._n += 1
        name = f"task-{self._n}"
        late = self._rng.randint(0, self.lateness) if self.lateness else 0
        self.entries[name] = _Entry(url, body, not_before + late)
        return name

    def delete(self, name: str) -> None:
        self.entries.pop(name, None)

    # -- the queue's side ---------------------------------------------------

    def take(self, now: int) -> Delivery | None:
        """The next task the queue chooses to deliver, or `None` when
        nothing is eligible. Which one it chooses is its business."""
        eligible = [n for n, e in self.entries.items() if e.not_before <= now]
        if not eligible:
            return None
        name = self._rng.choice(eligible) if self.shuffle else eligible[0]
        entry = self.entries[name]
        entry.attempts += 1
        self.delivered += 1
        return Delivery(name, entry.url, entry.body, entry.attempts)

    def ack(self, delivery: Delivery, now: int) -> None:
        """The handler answered. Usually that is the end of it.

        Sometimes the answer is lost on the way back. The queue cannot tell
        that from a handler that never answered, so it does the same thing:
        counts the attempt, waits, and delivers again to a handler that has
        already done the work. That is at-least-once, and it is why nothing
        downstream may assume it is asked once.
        """
        if self._rng.random() < self.duplicate:
            self.nack(delivery, now)
            return
        self.entries.pop(delivery.name, None)

    def nack(self, delivery: Delivery, now: int) -> None:
        """The handler did not answer, or answered badly. Try later, unless
        the queue has had enough."""
        entry = self.entries.get(delivery.name)
        if entry is None:
            return
        if entry.attempts >= self.give_up_after:
            self.dropped.append(delivery.name)
            self.entries.pop(delivery.name, None)
            return
        entry.not_before = now + self.backoff

    def loses_this_one(self) -> bool:
        return self._rng.random() < self.lose


# ---------------------------------------------------------------------------
# The two ports, over the one queue
# ---------------------------------------------------------------------------

#: The URL a deadline is delivered to. Everything after it is the origin to
#: sweep, exactly as a Cloud Run route would read it.
SWEEP = "sweep/"


class QueueTimers:
    """The engine's `Timers`, as scheduled tasks. Arming is creating one at
    the deadline; disarming is deleting it by the name the service gave."""

    def __init__(self, queue: Queue) -> None:
        self.queue = queue

    def arm(self, origin: str, at: int) -> str:
        return self.queue.create(f"{SWEEP}{origin}", {"origin": origin}, not_before=at)

    def disarm(self, name: str) -> None:
        self.queue.delete(name)


class QueueTransport:
    """The engine's `Transport`, as tasks with no schedule: deliver as soon
    as you can, which is what an immediate dispatch is."""

    def __init__(self, queue: Queue) -> None:
        self.queue = queue

    def send(self, address: str, msg: Any) -> None:
        self.queue.create(address, msg)
