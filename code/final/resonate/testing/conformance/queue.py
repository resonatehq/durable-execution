"""What a queue has to do to be one: eight claims, in order.

`spec/queue.py` says what a queue *is* -- two operations, one of them
serving both deadlines and dispatches. This is the part a real queue can
fail.

    from resonate.testing.conformance.queue import conformance
    from resonate import queue_gcp
    from resonate.testing import queue_mem

    assert conformance(queue_mem) == []
    assert conformance(queue_gcp, project="p", location="l", queue="q",
                       base_url="https://svc") == []

## What the contract can and cannot claim

Only what a real queue can be asked to do without being watched. That a
deadline carries its instant, that a dispatch carries none, that the
thirty-day horizon is clamped -- those are claims about the *request* an
adapter builds, and they are checked against a double in
`test_conformance.py`. What is here is what both ends must agree on: names
come from the service, cancelling is idempotent, and nothing is refused for
being scheduled oddly.

A run against a real queue can be made harmless: a paused queue accepts
creation and deletion, which is the whole contract, and dispatches nothing,
so the eight claims run without a single POST escaping.
"""

from __future__ import annotations

from contextlib import contextmanager
from typing import Any, Callable

from ...spec.queue import QueueM, QueueP
from ...types import HERE
from .violation import Violation


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
