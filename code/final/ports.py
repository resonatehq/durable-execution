"""The three things the engine needs from the world, and in-memory versions.

A port is what the engine calls; everything about *where* the bytes go lives
behind it. There are three, and no more, because that is all one origin's
transition needs: somewhere to keep the document, something to arm a deadline
with, and something to carry a message.

The in-memory implementations are not toys. They have the same semantics the
real ones must have — a generation that a conditional write is checked
against, a timer named by whatever armed it, a send that happens once — and
they are what every test runs on. `Fault` is what makes them interesting: it
cuts the power between two effects, so the crash windows the engine claims to
survive are tested rather than argued.
"""

from __future__ import annotations

from typing import Any, Protocol


class Conflict(Exception):
    """The write lost a race: the object is not at the generation the write
    required. The decision was made against state that no longer exists, so it
    must be re-decided, never replayed."""


class Unavailable(Exception):
    """No answer. Nothing is known about whether the write landed."""


class Crash(Exception):
    """Injected: the process stopped here. Not something production raises."""


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
# Store
# ---------------------------------------------------------------------------


class Store(Protocol):
    def load(self, key: str) -> tuple[bytes | None, int]:
        """The body and its generation. Generation 0 means it is not there,
        which is also what a create writes against."""

    def commit(self, key: str, body: bytes, if_generation: int) -> int:
        """Replace the object only if it is still at `if_generation`, or
        create it when that is 0. Raises `Conflict` otherwise."""


class MemoryStore:
    """A dict with generations. Reads never fault, so a test can see exactly
    what landed after the power went out."""

    def __init__(self, fault: Fault | None = None) -> None:
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.fault = fault
        #: When set, a faulted commit writes and *then* raises, which is the
        #: window where nothing is known about whether the write landed.
        self.land_then_fail = False

    def load(self, key: str) -> tuple[bytes | None, int]:
        body, gen = self.objects.get(key, (None, 0))
        return body, gen

    def commit(self, key: str, body: bytes, if_generation: int) -> int:
        current = self.objects.get(key, (None, 0))[1]
        if current != if_generation:
            raise Conflict(f"{key} is at generation {current}, not {if_generation}")
        if self.land_then_fail and self.fault is not None and self.fault.budget == 0:
            self.objects[key] = (body, current + 1)
        if self.fault is not None:
            self.fault.tick(f"commit {key}")
        self.objects[key] = (body, current + 1)
        return current + 1


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
