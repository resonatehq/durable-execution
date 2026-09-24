"""What a queue is, and what it has to do to be one.

The same three layers `spec.py` draws around an engine and `store.py`
around a store:

    QueueP   a queue, once it exists: two operations
    QueueC   how one is made: its configuration in, a queue out
    QueueM   a module that offers one, under the name `Queue`

    from ...queue import conformance
    import queue_mem, queue_gcp

    assert conformance(queue_mem) == []
    assert conformance(queue_gcp, project="p", location="l", queue="q",
                       base_url="https://svc") == []

The file is plural and the concept is not. `queue.py` shadows the standard
library's own `queue`, and anything that imports the real one — Hypothesis,
for a start — breaks the moment it is on the path. The implementations need
no such apology: nothing is called `queue_mem`.

## One port, because there was only ever one thing

The engine used to take two: `timers`, to arm and disarm a deadline, and
`transport`, to send a message. They were the same thing wearing two
names. A deadline is a task with an HTTP target and a time before which it
must not be delivered; a dispatch is a task whose time is now. Both are
`create`, and the only difference is what the caller does with the name it
gets back: a deadline's is recorded in the document, because cancelling is
by name, and a dispatch's is dropped, because a dispatch is never
cancelled.

The tell was in the service's wiring, which built the two ports out of the same object
and handed it to the engine twice. So there is one port now, named for
what it is. What a deadline and a dispatch still do *not* share is when
they happen — arm before the commit, send after — and that is stated where
it belongs, in the kernel's effects (`SetTimeout`, `DelTimeout`, `Send`)
rather than in the shape of the world.

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

## What the contract can and cannot claim

Only what a real queue can be asked to do without being watched. That a
deadline carries its instant, that a dispatch carries none, that the
thirty-day horizon is clamped — those are claims about the *request* an
adapter builds, and they are checked against a double in
`test_conformance.py`. What is here is what both ends must agree on: names
come from the service, cancelling is idempotent, and nothing is refused for
being scheduled oddly.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable, Protocol

from ...types import HERE
from ...ports import QueueP
from .violation import Violation


# ---------------------------------------------------------------------------
# The three layers
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

#: Every claim, in order. A claim is handed a queue and a list to record
#: every name it creates in, so whatever it leaves behind is cancelled even
#: when the claim fails — which matters, because a task left in a real queue
#: is an HTTP request somebody is going to receive.
CLAIMS: list[tuple[str, Callable[[QueueP, list[str]], None]]] = []

#: Far enough out that a claim's task is never delivered while the contract
#: is still running, on any queue, real or simulated.
LATER = 10 ** 13


def claim(what: str):
    def keep(fn: Callable[[QueueP, list[str]], None]):
        CLAIMS.append((what, fn))
        return fn
    return keep


@contextmanager
def refused(error: type[BaseException], why: str):
    try:
        yield
    except error:
        return
    raise AssertionError(why)


def _make(queue: QueueP, made: list[str], **kw) -> str:
    name = queue.create(HERE, {"kind": "timeout", "origin": "o"}, not_before=LATER, **kw)
    made.append(name)
    return name


@claim("a create returns the name the service gave it")
def _named(queue: QueueP, made: list[str]) -> None:
    name = _make(queue, made)
    assert isinstance(name, str) and name, f"not a name: {name!r}"


@claim("two creates are two tasks")
def _distinct(queue: QueueP, made: list[str]) -> None:
    assert _make(queue, made) != _make(queue, made), \
        "the same name twice, which is the tombstone trap"


@claim("cancelling what was created succeeds")
def _cancel(queue: QueueP, made: list[str]) -> None:
    queue.delete(_make(queue, made))


@claim("cancelling is idempotent")
def _cancel_twice(queue: QueueP, made: list[str]) -> None:
    name = _make(queue, made)
    queue.delete(name)
    queue.delete(name)


@claim("cancelling what was never there succeeds")
def _cancel_ghost(queue: QueueP, made: list[str]) -> None:
    # Shaped like a name this queue would issue, so the claim is about
    # absence rather than about a malformed argument.
    queue.delete(_make(queue, made) + "-gone")


@claim("a dispatch carries no schedule and is still a task")
def _immediate(queue: QueueP, made: list[str]) -> None:
    name = queue.create("https://nowhere.invalid/", {"kind": "execute"})
    made.append(name)
    assert isinstance(name, str) and name


@claim("a schedule in the past is accepted rather than refused")
def _past(queue: QueueP, made: list[str]) -> None:
    made.append(queue.create("https://nowhere.invalid/", {}, not_before=1))


@claim("a schedule past any horizon is clamped rather than refused")
def _horizon(queue: QueueP, made: list[str]) -> None:
    made.append(queue.create(HERE, {}, not_before=10 ** 15))


def conformance(module: QueueM, **config: Any) -> list[Violation]:
    """Drive `module.Queue` through every claim and return what it broke.

    Everything a claim creates is cancelled afterwards, so running this
    against a live queue leaves nothing queued — which is the only reason it
    is safe to run against one at all.
    """
    queue = module.Queue(**config)
    out: list[Violation] = []
    for i, (what, check) in enumerate(CLAIMS):
        made: list[str] = []
        try:
            check(queue, made)
        except AssertionError as e:
            out.append(Violation(i, what, str(e) or "the claim did not hold"))
        except Exception as e:
            out.append(Violation(i, what, f"{type(e).__name__}: {e}"))
        finally:
            for name in made:
                try:
                    queue.delete(name)
                except Exception:
                    pass  # a queue too broken to tidy after is already failing
    return out
