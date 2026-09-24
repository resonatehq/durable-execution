"""The vocabulary the two ports share.

There are two ports — a store and a queue — and neither is here. Each is an
interface with two implementations and a contract of its own, so each has a
module of its own: `store.py` and `queues.py`, the way the engine has
`spec.py`.

What is left is what all of them need and none of them can own, because the
owner would have to be imported by the others. This module imports nothing,
on purpose:

- **`Conflict` and `Unavailable`**, the two ways a world refuses. They are
  not one error because they demand opposite responses.
- **`Crash` and `Fault`**, which are how a simulated world stops answering
  mid-transition. Not production: this is the instrument the crash-window
  tests are built out of, and it counts writes across both ports, because a
  crash window is the gap between two *effects* and the effects are spread
  over the two.
- **`Violation`**, which is how all three conformance suites report.
"""

from __future__ import annotations

from dataclasses import dataclass


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

    Counting across both ports rather than per port is the point: a crash
    window is a gap between two *effects*, and the effects are spread over
    the store and the queue — the arm, then the commit, then the disarm and
    the sends. `crash_after(k)` stops the engine at the k-th write it
    attempts, whichever port that lands on, so a test can walk every window
    by looping k.
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

    There are three conformance suites — `spec.py` for an engine,
    `store.py` for a store, `queues.py` for a queue — and they report the
    same way, so a caller can collect from all three and a reader learns
    one format. `step` is where in the suite it happened: which message of
    a script, or which claim of a contract.
    """

    step: int
    msg: str
    detail: str

    def __str__(self) -> str:
        return f"step {self.step} ({self.msg}): {self.detail}"
