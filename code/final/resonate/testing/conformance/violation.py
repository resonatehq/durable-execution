"""How all three conformance suites report."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Violation:
    """One claim an implementation did not honour.

    There are three conformance suites — `engine.py` for an engine,
    `store.py` for a store, `queue.py` for a queue — and they report the
    same way, so a caller can collect from all three and a reader learns
    one format. `step` is where in the suite it happened: which message of
    a script, or which claim of a contract.
    """

    step: int
    msg: str
    detail: str

    def __str__(self) -> str:
        return f"step {self.step} ({self.msg}): {self.detail}"
