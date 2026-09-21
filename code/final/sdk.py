"""The programming model: ordinary Python, one decorator.

This is what the two posts promise, made real over the engine. A function
marked `@resonate` runs durably. Calling one from inside another is a
durable call, memoized by position. Calling one with `.rpc` dispatches it to
whatever worker serves its target and returns a handle. `gather` reads
several handles at once.

    @resonate
    def agent(question):
        response = prompt(messages)          # durable, and skipped on replay
        result = invoke.rpc(tool, args)      # somewhere else, and suspended on
        return gather(*[search.rpc(q) for q in queries])

## What derives an id

Position, not arguments. A run has an id, its first durable call is `:1`, its
second `:2`, and a call made from inside `:2` is `:2.1`. The ids are
hierarchical, they sort in the order the execution unfolds, and the same call
gets the same id on every replay. Keying by arguments would be wrong: with
side effects, the same call with the same arguments twice is two events.

The counter has to land on the same number every time, which is the one thing
this model asks of your code. Read a clock or roll dice inside a durable call,
never between two of them.

## Why `.rpc` returns a handle

In the posts the model is async and `await` is where a value is read, so the
handle is invisible. Here it is explicit: `.rpc(q)` dispatches and returns,
`.result()` reads. That is not a decoration. A fan-out has to dispatch every
branch *before* anything blocks, or the branches run one at a time — so
dispatching and reading have to be separable, and in a synchronous language
that means two calls.

## What happens when a value is not there yet

`Blocked` unwinds the stack, carrying the ids it is waiting for. The worker
turns it into one `task.suspend` naming all of them and releases the task.
Nothing waits anywhere: no coroutine parked on a socket, no thread, no row
marked in progress. What is left is a pending promise and a note to wake this
task when it settles.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable

from kernel import (
    PENDING, REJECTED, RESOLVED, PromiseCreate, PromiseSettle, TaskFence,
    TAG_TARGET, Value,
)

#: How long a promise this SDK creates has to settle before it times out.
DEFAULT_TIMEOUT = 24 * 60 * 60 * 1_000


class Blocked(Exception):
    """Not an error: how a frame says *I cannot make progress, and neither
    can anyone above me*. The only thing it does is unwind."""

    def __init__(self, ids: list[str]) -> None:
        super().__init__(", ".join(ids))
        self.ids = list(dict.fromkeys(ids))


class Failed(Exception):
    """A durable call that was recorded as rejected. Raised on the run that
    made it and on every replay after, because the rejection is the result."""


@dataclass
class _Call:
    """One frame of the durable call stack: which promise is running, and how
    many durable calls it has made so far."""

    id: str
    n: int = 0

    def child(self) -> str:
        self.n += 1
        # A bare root joins its first lineage segment with ':'; anything that
        # already carries lineage joins deeper segments with '.'.
        sep = "." if ":" in self.id else ":"
        return f"{self.id}{sep}{self.n}"


@dataclass
class Invocation:
    """What a worker is running: the task it holds, and the call stack."""

    engine: Any
    task_id: str
    version: int
    now: Callable[[], int]
    stack: list[_Call] = field(default_factory=list)
    corr: int = 0

    def fence(self, action):
        """Every write a running function makes goes through its task's
        version, so a worker that lost its lease cannot write. The action is
        always on a *child* of the task, never the task's own promise, which
        is what `task.fulfill` is for."""
        self.corr += 1
        reply = self.engine.process(
            TaskFence(self.task_id, self.version, f"c{self.corr}", action), self.now())
        if reply.status != 200:
            raise Failed(f"fence refused: {reply.status} {reply.data}")
        inner = reply.data["action"]
        return inner["head"]["status"], inner["data"]


_CURRENT: ContextVar[Invocation | None] = ContextVar("invocation", default=None)


def current() -> Invocation:
    inv = _CURRENT.get()
    if inv is None:
        raise RuntimeError("a durable function was called outside a durable execution")
    return inv


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def dumps(x: Any) -> Value:
    return Value(data=json.dumps(x))


def loads(v: dict) -> Any:
    data = v.get("data")
    return None if data is None else json.loads(data)


def read_back(record: dict) -> Any:
    """What a settled promise returns to the code that awaited it."""
    if record["state"] == RESOLVED:
        return loads(record["value"])
    raise Failed(f"{record['id']}: {loads(record['value'])}")


# ---------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------

REGISTRY: dict[str, "Durable"] = {}


class Future:
    """A dispatched remote call. Reading it is where the run may block."""

    def __init__(self, id: str) -> None:
        self.id = id

    def result(self) -> Any:
        return gather(self)[0]


class Durable:
    """A registered function. Called from inside a durable execution it is a
    durable call; called with `.rpc` it is dispatched somewhere else."""

    def __init__(self, fn: Callable, name: str, target: str | None) -> None:
        self.fn, self.name, self.target = fn, name, target
        REGISTRY[name] = self

    def __call__(self, *args) -> Any:
        """A local durable call: create, run if pending, settle, read back.

        The three lines of post 001, with the bookkeeping under the language
        instead of at the call site.
        """
        inv = current()
        id = inv.stack[-1].child()
        status, data = inv.fence(PromiseCreate(
            id, inv.now() + DEFAULT_TIMEOUT, dumps({"f": self.name, "a": args}), {}))
        record = data["promise"]
        if record["state"] != PENDING:
            return read_back(record)

        inv.stack.append(_Call(id))
        try:
            value, state = dumps(self.fn(*args)), RESOLVED
        except Failed as e:
            value, state = dumps(str(e)), REJECTED
        finally:
            inv.stack.pop()
        # Settle from what the store returns, never from the local result: if
        # another worker got there first, that outcome is the one that counts.
        _, data = inv.fence(PromiseSettle(id, state, value))
        return read_back(data["promise"])

    def rpc(self, *args) -> Future:
        """Dispatch to whatever serves this function's target, and return."""
        if self.target is None:
            raise RuntimeError(f"{self.name} has no target, so it cannot be called remotely")
        inv = current()
        id = inv.stack[-1].child()
        inv.fence(PromiseCreate(
            id, inv.now() + DEFAULT_TIMEOUT, dumps({"f": self.name, "a": args}),
            {TAG_TARGET: self.target}))
        return Future(id)


def resonate(fn: Callable | None = None, *, target: str | None = None, name: str | None = None):
    """Mark a function durable. `target` is where it runs when called with
    `.rpc`, and a function without one can only be called locally."""

    def wrap(f: Callable) -> Durable:
        return Durable(f, name or f.__name__, target)

    return wrap(fn) if fn is not None else wrap


def gather(*futures: Future) -> list[Any]:
    """Read several dispatched calls. Everything still pending is collected
    into one `Blocked`, so a fan-out suspends once rather than once per
    branch, and one settlement is enough to wake it."""
    inv = current()
    records, pending = [], []
    for f in futures:
        status, data = inv.fence(PromiseCreate(f.id, inv.now() + DEFAULT_TIMEOUT, Value(), {}))
        record = data["promise"]
        records.append(record)
        if record["state"] == PENDING:
            pending.append(f.id)
    if pending:
        raise Blocked(pending)
    return [read_back(r) for r in records]
