"""The programming model: ordinary async/await, one decorator.

This is the target from the repository's README, and it is the whole claim:

    @resonate
    async def research(question: str):
        # Plan the searches
        queries = await agent(f"Plan the searches for: {question}")

        # Fan out the searches
        results = await gather(search.rpc(q) for q in queries)

        # Synthesize the results
        return await agent(f"Write a cited report. {question}: {results}")

No state machines, no workflow DSL, no context object threaded through every
call. `await agent(...)` runs durably in this process. `search.rpc(q)` runs
durably in another one, the same function on a different machine. `gather`
does what it always did.

## What derives an id

Position, not arguments. A run has an id, its first durable call is `:1`, its
second `:2`, and a call made from inside `:2` is `:2.1`. The ids are
hierarchical, they sort in the order the execution unfolds, and the same call
gets the same id on every replay. Keying by arguments would be wrong: with
side effects, the same call with the same arguments twice is two events.

The counter has to land on the same number every time, which is the one thing
this model asks of your code. Read a clock or roll dice inside a durable call,
never between two of them.

## Why async is not a detail

`gather` has to dispatch every branch before anything blocks, or the branches
run one at a time. In async that falls out: each branch is a coroutine, they
all run up to the point where their value is not there yet, and only then is
there anything to wait for. A synchronous model would need the dispatch and
the read to be two calls the programmer writes separately, and then this
would not be the program above.

A durable function may be `async def` or a plain `def`. A leaf that only
calls a model or an index has nothing to await, and should not have to
pretend it does.

## What happens when a value is not there yet

`Blocked` unwinds the stack, carrying the ids it is waiting for. The worker
turns it into one `task.suspend` naming all of them and hands the task back.
Nothing waits anywhere: no coroutine parked on a socket, no thread, no row
marked in progress. What is left is a pending promise and a note to wake this
task when it settles.
"""

from __future__ import annotations

import asyncio
import inspect
import json
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Callable

from kernel import (
    PENDING, REJECTED, RESOLVED, PromiseCreate, PromiseSettle, TAG_TARGET,
    TaskFence, Value,
)
from ports import Conflict, Unavailable

#: How long a promise this SDK creates has to settle before it times out.
DEFAULT_TIMEOUT = 24 * 60 * 60 * 1_000


class Blocked(Exception):
    """Not an error: how a frame says *I cannot make progress, and neither
    can anyone above me*. The only thing it does is unwind."""

    def __init__(self, ids: list[str]) -> None:
        super().__init__(", ".join(ids))
        self.ids = list(dict.fromkeys(ids))


class Failed(Exception):
    """A durable call recorded as rejected. Raised on the run that made it
    and on every replay after, because the rejection *is* the result, and a
    result is the one thing replay must not change."""


class LeaseLost(Exception):
    """A write was refused because this worker no longer holds the task.

    Not the function's answer: nothing was decided, somebody else is doing
    the work, and this attempt should stop rather than record anything.
    """


#: What the worker and the SDK must not mistake for an answer. Everything
#: else a durable function raises is its result, recorded as a rejection —
#: post 001's `except Exception: settle(id, REJECTED, e)`. These three mean
#: the attempt could not produce a result at all, so the task goes back and
#: somebody tries again.
PLATFORM = (LeaseLost, Conflict, Unavailable)


@dataclass
class _Call:
    """One frame of the durable call stack: which promise is running, and how
    many durable calls it has made so far.

    The counter is shared by everything that call makes, including branches
    running concurrently, which is what keeps `:1`, `:2`, `:3` in the order
    the program asked for them.
    """

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
    """What a worker is running: the task it holds, and the clock."""

    engine: Any
    task_id: str
    version: int
    now: Callable[[], int]
    corr: int = 0

    def fence(self, action):
        """Every write a running function makes goes through its task's
        version, so a worker that lost its lease cannot write."""
        self.corr += 1
        reply = self.engine.process(
            TaskFence(self.task_id, self.version, f"c{self.corr}", action), self.now())
        if reply.status != 200:
            raise LeaseLost(f"{self.task_id} at version {self.version}: {reply.data}")
        inner = reply.data["action"]
        return inner["head"]["status"], inner["data"]


#: The task being run, and the call inside it that is running now. Two
#: context variables rather than a stack object, because `asyncio` copies the
#: context into each task it creates: concurrent branches get their own
#: current frame for free, and cannot tread on each other's.
_INVOCATION: ContextVar[Invocation | None] = ContextVar("invocation", default=None)
_FRAME: ContextVar[_Call | None] = ContextVar("frame", default=None)


def current() -> tuple[Invocation, _Call]:
    inv, frame = _INVOCATION.get(), _FRAME.get()
    if inv is None or frame is None:
        raise RuntimeError("a durable function was called outside a durable execution")
    return inv, frame


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------


def dumps(x: Any) -> Value:
    return Value(data=json.dumps(x))


def loads(v: dict) -> Any:
    data = v.get("data")
    return None if data is None else json.loads(data)


def describe(e: BaseException) -> dict:
    """A rejection, as something that survives a round trip through JSON and
    still says what went wrong."""
    return {"type": type(e).__name__, "message": str(e)}


def read_back(record: dict) -> Any:
    """What a settled promise returns to the code that awaited it."""
    if record["state"] == RESOLVED:
        return loads(record["value"])
    why = loads(record["value"]) or {}
    raise Failed(f"{record['id']}: {why.get('type', 'rejected')}: {why.get('message', '')}")


# ---------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------

#: Every durable function, by name. What a worker looks in to find the code
#: for a task it just claimed.
REGISTRY: dict[str, "Durable"] = {}

#: Where each function runs when it is called with `.rpc`. Deployment, not
#: definition: the same function is a local call on one machine and a remote
#: one from another, and only the wiring knows which.
TARGETS: dict[str, str] = {}


class Durable:
    def __init__(self, fn: Callable, name: str) -> None:
        self.fn, self.name = fn, name
        REGISTRY[name] = self

    async def __call__(self, *args) -> Any:
        """A local durable call: create, run if pending, settle, read back.

        The whole of post 001, with the bookkeeping under the language
        instead of at the call site.
        """
        inv, frame = current()
        id = frame.child()
        _, data = inv.fence(PromiseCreate(
            id, inv.now() + DEFAULT_TIMEOUT, dumps({"f": self.name, "a": args}), {}))
        record = data["promise"]
        if record["state"] != PENDING:
            return read_back(record)

        token = _FRAME.set(_Call(id))
        try:
            value, state = dumps(await self.invoke(*args)), RESOLVED
        except (Blocked, *PLATFORM):
            # Not an answer. Nothing is recorded, and the attempt unwinds.
            raise
        except Exception as e:
            # An answer, and an unwelcome one. Recorded, so the next run
            # reads the same rejection rather than calling again.
            value, state = dumps(describe(e)), REJECTED
        finally:
            _FRAME.reset(token)
        # Settle from what the store returns, never from the local result: if
        # another worker got there first, that outcome is the one that counts.
        _, data = inv.fence(PromiseSettle(id, state, value))
        return read_back(data["promise"])

    async def invoke(self, *args) -> Any:
        """Call the user's function, whether or not it is a coroutine. A leaf
        that only prompts a model has nothing to await."""
        result = self.fn(*args)
        return await result if inspect.isawaitable(result) else result

    async def rpc(self, *args) -> Any:
        """Dispatch to wherever this function runs, and read the answer.

        Awaited on its own it dispatches and then blocks. Handed to `gather`
        it dispatches alongside its siblings, and they block together, which
        is the difference between a fan-out and a queue.
        """
        target = TARGETS.get(self.name)
        if target is None:
            raise RuntimeError(f"{self.name} is not routed anywhere, so it cannot be called remotely")
        inv, frame = current()
        id = frame.child()
        _, data = inv.fence(PromiseCreate(
            id, inv.now() + DEFAULT_TIMEOUT, dumps({"f": self.name, "a": args}),
            {TAG_TARGET: target}))
        record = data["promise"]
        if record["state"] == PENDING:
            raise Blocked([id])
        return read_back(record)


def resonate(fn: Callable) -> Durable:
    """Mark a function durable. That is the whole of the syntax."""
    return Durable(fn, fn.__name__)


def route(fn: Durable, target: str) -> None:
    """Say where a function runs. Wiring, not definition."""
    TARGETS[fn.name] = target


async def gather(*awaitables) -> list[Any]:
    """Await several durable calls at once.

    What it always did, with one thing added: everything still pending is
    collected into a single `Blocked`, so a fan-out suspends once rather than
    once per branch, and the run is woken by the first settlement rather than
    walked through them one at a time.

    A branch that failed does not propagate while another is still pending.
    Its rejection is recorded and will be raised on the next run, after the
    picture is complete — acting on half of a fan-out would mean acting on
    something the next replay might disagree with.
    """
    if len(awaitables) == 1 and not inspect.isawaitable(awaitables[0]):
        awaitables = tuple(awaitables[0])  # a generator, as the README passes one
    outcomes = await asyncio.gather(*awaitables, return_exceptions=True)
    blocked = [id for o in outcomes if isinstance(o, Blocked) for id in o.ids]
    if blocked:
        raise Blocked(blocked)
    for o in outcomes:
        if isinstance(o, BaseException):
            raise o
    return list(outcomes)
