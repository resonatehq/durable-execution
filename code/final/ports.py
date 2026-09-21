"""The vocabulary everything else shares, and two of the engine's ports.

This module imports nothing, on purpose. It is what `store.py`, `timer.py`,
`spec.py`, the engine and the kernel's shell can all reach for without any
of them reaching for each other: the two failures a world can hand back, the
fault injector that simulates the third, the thing a conformance suite
reports, and the two narrow ports the engine calls.

The store is not here. It is an interface with three implementations and a
contract of its own, so it has a module — `store.py` — the way the engine
has `spec.py`. What is left here are the two ports that are adapters rather
than implementations: arming a deadline and sending a message are both one
queue in production (`timer.py`), and the engine should not have to know
that.

The in-memory implementations are not toys. They have the same semantics the
real ones must have — a timer named by whatever armed it, a send that happens
once — and they are what every test runs on. `Fault` is what makes them
interesting: it cuts the power between two effects, so the crash windows the
engine claims to survive are tested rather than argued.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol


class Conflict(Exception):
    """The write lost a race: the object is not at the generation the write
    required. The decision was made against state that no longer exists, so it
    must be re-decided, never replayed."""


class Unavailable(Exception):
    """No answer. Nothing is known about whether the write landed."""


class Crash(BaseException):
    """Injected: the process stopped here. Not something production raises.

    A `BaseException` on purpose. A power cut is not an error the program
    gets to handle, and if it were an `Exception` the engine's own error
    handling would catch it and turn a dead process into a rejected promise,
    which is the opposite of what happened.
    """


class Fault:
    """A budget of writes, shared across the ports, then the power goes out.

    Counting across all three ports rather than per port is the point: a
    crash window is a gap between two *effects*, and the effects are spread
    over the store, the timers and the transport. `crash_after(k)` stops the
    engine at the k-th write it attempts, whichever port that lands on, so a
    test can walk every window by looping k.
    """

    def __init__(self) -> None:
        self.budget: int | None = None
        self.log: list[str] = []

    def crash_after(self, n: int) -> None:
        self.budget = n
        self.log = []

    def heal(self) -> None:
        self.budget = None

    def tick(self, what: str) -> None:
        if self.budget is not None:
            if self.budget <= 0:
                raise Crash(what)
            self.budget -= 1
        self.log.append(what)


# ---------------------------------------------------------------------------
# What a conformance suite reports
# ---------------------------------------------------------------------------


@dataclass
class Violation:
    """One claim an implementation did not honour.

    There are three conformance suites — `spec.py` for an engine, `store.py`
    for a store, `timer.py` for a timer — and they report the same way, so a
    caller can collect from all three and a reader learns one format. `step`
    is where in the suite it happened: which message of a script, or which
    claim of a contract.
    """

    step: int
    msg: str
    detail: str

    def __str__(self) -> str:
        return f"step {self.step} ({self.msg}): {self.detail}"


# ---------------------------------------------------------------------------
# Timers
# ---------------------------------------------------------------------------


class Timers(Protocol):
    def arm(self, origin: str, at: int) -> str:
        """Arrange for the origin to be swept at `at`, and return the name of
        the thing that will do it."""

    def disarm(self, name: str) -> None:
        """Remove it. Removing what is not there succeeds."""


class MemoryTimers:
    """A heap, near enough. Names are the engine's handle on what it armed:
    a writer removes the deadline its own predecessor wrote and nothing
    else."""

    def __init__(self, fault: Fault | None = None) -> None:
        self.armed: dict[str, tuple[str, int]] = {}
        self.fault = fault
        self._n = 0

    def arm(self, origin: str, at: int) -> str:
        if self.fault is not None:
            self.fault.tick(f"arm {origin} at {at}")
        self._n += 1
        name = f"timer-{self._n}"
        self.armed[name] = (origin, at)
        return name

    def disarm(self, name: str) -> None:
        if self.fault is not None:
            self.fault.tick(f"disarm {name}")
        self.armed.pop(name, None)

    def due(self, now: int) -> list[tuple[str, str]]:
        """(name, origin) for every deadline at or before `now`, nearest first."""
        return [(n, o) for n, (o, at) in sorted(self.armed.items(), key=lambda kv: kv[1][1])
                if at <= now]


# ---------------------------------------------------------------------------
# Transport
# ---------------------------------------------------------------------------


class Transport(Protocol):
    def send(self, address: str, msg: Any) -> None:
        """Deliver, at least once, strictly after the document committed."""


class MemoryTransport:
    def __init__(self, fault: Fault | None = None) -> None:
        self.sent: list[tuple[str, Any]] = []
        self.fault = fault

    def send(self, address: str, msg: Any) -> None:
        if self.fault is not None:
            self.fault.tick(f"send to {address}")
        self.sent.append((address, msg))

    def take(self) -> list[tuple[str, Any]]:
        out, self.sent = self.sent, []
        return out
