"""Cutting the power between two effects."""

from __future__ import annotations


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
