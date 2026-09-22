"""A queue in a dict, with the parts that bite.

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

Every one of those is a knob, off by default so a test can turn on one at a
time and say which one it is about. Deterministic under a seed, so a run
that finds something can be run again and find it again.

`take`, `ack` and `nack` are not part of `queues.QueueP` and could not be:
Cloud Tasks is push-only, and taking delivery belongs to whatever is being
delivered to. They are here because something has to play the queue's own
side in a test.

## What giving up costs, and it is worth stating plainly

A dropped `execute` is recoverable: the task's retry deadline was committed
before the message left, so the sweep offers it again. A dropped *sweep* is
not recoverable by anything in this design — the deadline it carried is the
only thing that was going to fire. A deployment needs either a retry policy
generous enough that this does not happen, or a periodic sweep over the
bucket that does not depend on any single queued task. `test_queue.py`
demonstrates the hole rather than pretending it is not there.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any

from ports import Fault


@dataclass(frozen=True)
class Delivery:
    """One attempt to hand a task to its handler."""

    name: str
    url: str
    body: Any
    attempt: int


@dataclass
class _Entry:
    url: str
    body: Any
    not_before: int
    attempts: int = 0


@dataclass
class Queue:
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

    #: Where the power goes out. Half the engine's effects land here — the
    #: arm before the commit, the disarm and the sends after it — so the
    #: crash-window tests need this as much as the store does.
    fault: Fault | None = None

    entries: dict[str, _Entry] = field(default_factory=dict)
    #: Every task ever created, in order, kept after delivery. A test that
    #: asks what was sent is asking about the whole run, not about what
    #: happens to be waiting now.
    created: list[tuple[str, Any]] = field(default_factory=list)
    dropped: list[str] = field(default_factory=list)
    delivered: int = 0
    _n: int = 0
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.seed)

    # -- the caller's side --------------------------------------------------

    def create(self, url: str, body: Any, *, not_before: int = 0) -> str:
        if self.fault is not None:
            self.fault.tick(f"create {url}")
        self._n += 1
        self.created.append((url, body))
        name = f"task-{self._n}"
        late = self._rng.randint(0, self.lateness) if self.lateness else 0
        self.entries[name] = _Entry(url, body, not_before + late)
        return name

    def delete(self, name: str) -> None:
        if self.fault is not None:
            self.fault.tick(f"delete {name}")
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
