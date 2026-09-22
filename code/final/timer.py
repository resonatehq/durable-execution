"""What a timer is, and what it has to do to be one.

The same three layers `spec.py` draws around an engine and `store.py`
around a store:

    TimerP   a timer, once it exists: two operations
    TimerC   how one is made: its configuration in, a timer out
    TimerM   a module that offers one, under the name `Timer`

    from timer import conformance
    import timer_mem, timer_gcp

    assert conformance(timer_mem) == []
    assert conformance(timer_gcp, project="p", location="l", queue="q",
                       base_url="https://svc") == []

## Why a timer is also the transport

In production there is one queue, not two mechanisms. A deadline and a
dispatch are the same object: a task with an HTTP target and a time before
which it must not be delivered. A dispatch is simply one whose time is now.
So there is one interface here, named for the harder of its two jobs, and
the engine's two ports — `Timers` and `Transport` — are two uses of it.

Modelling them as separate well-behaved gadgets hides everything
interesting, which is why `timer_mem` is a queue with the failures a queue
really has rather than a heap.

## What the interface is not

There is no third operation for receiving. That is not an omission: Cloud
Tasks is push-only, a task is delivered by an HTTP POST to the URL it
carries, and that is why `app.py` exists and why a worker is a service
rather than a loop. Taking delivery belongs to whatever is being delivered
to; a simulator adds `take`/`ack`/`nack` for its own tests and the
interface stays two methods.

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
from typing import Any, Callable, Protocol, runtime_checkable

from ports import Violation
from wire import encode_message

#: The URL a deadline is delivered to. Everything after it is the origin to
#: sweep, exactly as a Cloud Run route would read it.
SWEEP = "sweep/"


# ---------------------------------------------------------------------------
# The three layers
# ---------------------------------------------------------------------------


@runtime_checkable
class TimerP(Protocol):
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


class TimerC(Protocol):
    """How a timer is made. Unpinned, for the reason `store.StoreC` gives
    at length: the arguments are a deployment rather than an interface, and
    the caller is the one that knows them."""

    def __call__(self, *config: Any, **keywords: Any) -> TimerP: ...


class TimerM(Protocol):
    """A module that offers a timer. A property rather than an attribute,
    for the reason `store.StoreM` gives."""

    @property
    def Timer(self) -> TimerC: ...


# ---------------------------------------------------------------------------
# The engine's two ports, over the one timer
# ---------------------------------------------------------------------------


class Timers:
    """The engine's `ports.Timers`. Arming is creating a task at the
    deadline; disarming is deleting it by the name the service gave."""

    def __init__(self, timer: TimerP) -> None:
        self.timer = timer

    def arm(self, origin: str, at: int) -> str:
        return self.timer.create(f"{SWEEP}{origin}", {"origin": origin}, not_before=at)

    def disarm(self, name: str) -> None:
        self.timer.delete(name)


class Transport:
    """The engine's `ports.Transport`: a task with no schedule, which means
    deliver as soon as you can, which is what an immediate dispatch is.

    The body is JSON, here as in production. A simulator that carried live
    Python objects would be testing a seam that does not exist.
    """

    def __init__(self, timer: TimerP) -> None:
        self.timer = timer

    def send(self, address: str, msg: Any) -> None:
        self.timer.create(address, encode_message(msg))


# ---------------------------------------------------------------------------
# The contract
# ---------------------------------------------------------------------------

#: Every claim, in order. A claim is handed a timer and a list to record
#: every name it creates in, so whatever it leaves behind is cancelled even
#: when the claim fails — which matters, because a task left in a real queue
#: is an HTTP request somebody is going to receive.
CLAIMS: list[tuple[str, Callable[[TimerP, list[str]], None]]] = []

#: Far enough out that a claim's task is never delivered while the contract
#: is still running, on any queue, real or simulated.
LATER = 10 ** 13


def claim(what: str):
    def keep(fn: Callable[[TimerP, list[str]], None]):
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


def _make(timer: TimerP, made: list[str], **kw) -> str:
    name = timer.create(f"{SWEEP}o", {"origin": "o"}, not_before=LATER, **kw)
    made.append(name)
    return name


@claim("a create returns the name the service gave it")
def _named(timer: TimerP, made: list[str]) -> None:
    name = _make(timer, made)
    assert isinstance(name, str) and name, f"not a name: {name!r}"


@claim("two creates are two tasks")
def _distinct(timer: TimerP, made: list[str]) -> None:
    assert _make(timer, made) != _make(timer, made), \
        "the same name twice, which is the tombstone trap"


@claim("cancelling what was created succeeds")
def _cancel(timer: TimerP, made: list[str]) -> None:
    timer.delete(_make(timer, made))


@claim("cancelling is idempotent")
def _cancel_twice(timer: TimerP, made: list[str]) -> None:
    name = _make(timer, made)
    timer.delete(name)
    timer.delete(name)


@claim("cancelling what was never there succeeds")
def _cancel_ghost(timer: TimerP, made: list[str]) -> None:
    # Shaped like a name this queue would issue, so the claim is about
    # absence rather than about a malformed argument.
    timer.delete(_make(timer, made) + "-gone")


@claim("a dispatch carries no schedule and is still a task")
def _immediate(timer: TimerP, made: list[str]) -> None:
    name = timer.create("https://nowhere.invalid/execute", {"kind": "execute"})
    made.append(name)
    assert isinstance(name, str) and name


@claim("a schedule in the past is accepted rather than refused")
def _past(timer: TimerP, made: list[str]) -> None:
    made.append(timer.create("https://nowhere.invalid/execute", {}, not_before=1))


@claim("a schedule past any horizon is clamped rather than refused")
def _horizon(timer: TimerP, made: list[str]) -> None:
    made.append(timer.create(f"{SWEEP}o", {}, not_before=10 ** 15))


def conformance(module: TimerM, **config: Any) -> list[Violation]:
    """Drive `module.Timer` through every claim and return what it broke.

    Everything a claim creates is cancelled afterwards, so running this
    against a live queue leaves nothing queued — which is the only reason it
    is safe to run against one at all.
    """
    timer = module.Timer(**config)
    out: list[Violation] = []
    for i, (what, check) in enumerate(CLAIMS):
        made: list[str] = []
        try:
            check(timer, made)
        except AssertionError as e:
            out.append(Violation(i, what, str(e) or "the claim did not hold"))
        except Exception as e:
            out.append(Violation(i, what, f"{type(e).__name__}: {e}"))
        finally:
            for name in made:
                try:
                    timer.delete(name)
                except Exception:
                    pass  # a timer too broken to tidy after is already failing
    return out
