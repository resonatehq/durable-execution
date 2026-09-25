"""What a queue is: two operations, and three layers.

    QueueP   a queue, once it exists: two operations
    QueueC   how one is made: its configuration in, a queue out
    QueueM   a module that offers one, under the name `Queue`

The layers mean what they mean in `store.py`, for the same reasons. What an
implementation has to do to pass is `testing/conformance/queue.py`.

## One port for deadlines and dispatches

A deadline is a task with an HTTP target and a time before which it must
not be delivered; a dispatch is a task whose time is now. Both are
`create`, told apart by the message's `kind` (`timeout` or `execute`), and
by what the caller does with the name it gets back: a deadline's is
recorded in the document, because cancelling is by name, and a dispatch's
is dropped, because a dispatch is never cancelled. When each happens -- arm
before the commit, send after -- is stated in the kernel's effects
(`SetTimeout`, `DelTimeout`, `Send`), not in the shape of the port.

## What the interface is not

There is no third operation for receiving. That is not an omission: Cloud
Tasks is push-only, a task is delivered by an HTTP POST to the URL it
carries, and that is why `server.py` exists and why a worker is a service
rather than a loop. Taking delivery belongs to whatever is being delivered
to; a simulator adds `take`/`ack`/`nack` for its own tests and the
interface stays two methods.

Modelling a deadline as a well-behaved gadget would hide everything
interesting, which is why `queue_mem` is a queue with the failures a queue
really has rather than a heap.
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

__all__ = ["QueueP", "QueueC", "QueueM"]


@runtime_checkable
class QueueP(Protocol):
    """A queue. The engine is written against this and nothing else."""

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


class QueueC(Protocol):
    """How a queue is made. Unpinned, for the reason `store.StoreC` gives
    at length: the arguments are a deployment rather than an interface, and
    the caller is the one that knows them."""

    def __call__(self, *config: Any, **keywords: Any) -> QueueP: ...


class QueueM(Protocol):
    """A module that offers a queue. A property rather than an attribute,
    for the reason `store.StoreM` gives."""

    @property
    def Queue(self) -> QueueC: ...
