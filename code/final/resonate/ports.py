"""The two interfaces the engine is written against: a store and a queue.

`testing/spec/` holds the contract each implementation is held to.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class StoreP(Protocol):
    def get(self, key: str) -> tuple[str, str] | None:
        """The body and its version, or `None`."""

    def put(self, key: str, body: str, *, if_match: str | None = None,
            if_absent: bool = False) -> str:
        """Write, conditionally, and return the new version."""

    def delete(self, key: str) -> None:
        """Remove. Removing what is not there succeeds."""

    def list(self, prefix: str, limit: int) -> list[str]:
        """At most `limit` keys under `prefix`, lexicographically ascending."""


@runtime_checkable
class QueueP(Protocol):
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
