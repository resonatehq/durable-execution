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

from .kernel import (
    PENDING, REJECTED, RESOLVED, TAG_EXTERNAL, TAG_TARGET, TAG_TIMER,
)
from .types import PromiseCreate, PromiseSettle, TaskFence, Value, adapter, record
from .errors import Conflict, Unavailable

#: How long a promise this SDK creates has to settle before it times out.
DEFAULT_TIMEOUT = 24 * 60 * 60 * 1_000
#: What a function is when nobody says. Zero rather than one so that adding
#: a version to an existing function is the change, and having never
#: thought about versions is the default.
UNVERSIONED = 0


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


@dataclass
class Call:
    """What a promise records about the call it stands for: the function's
    name, its arguments, and its version. `v` is left out at version zero."""

    f: str
    a: list[Any]
    v: int = UNVERSIONED


@dataclass
class Rejection:
    """What a rejected promise records: the exception's type and message."""

    type: str = "rejected"
    message: str = ""


def call_param(fn: "Durable", args: tuple) -> Value:
    call = Call(fn.name, list(args), fn.version)
    return dumps(adapter(Call).dump_python(call, mode="json", exclude_defaults=True))


def called(param: dict) -> tuple[str, int, list]:
    """The other direction: what a worker was handed."""
    call = adapter(Call).validate_python(param)
    return call.f, call.v, call.a


def lookup(name: str, version: int = UNVERSIONED) -> "Durable":
    """The code for a dispatched call, or a readable account of why not."""
    found = REGISTRY.get((name, version))
    if found is not None:
        return found
    deployed = sorted(d.label for d in REGISTRY.values() if d.name == name)
    raise UnknownFunction(
        f"nothing deployed here answers to "
        f"{name if version == UNVERSIONED else f'{name}@{version}'}. "
        + (f"This container has {', '.join(deployed)}. A run created under a "
           "version you have retired cannot finish until that version is "
           "deployed again." if deployed else
           "This container has no function by that name at all -- check that "
           "the module defining it is imported by `main.py`."))


def loads(v: dict) -> Any:
    data = v.get("data")
    return None if data is None else json.loads(data)


def describe(e: BaseException) -> dict:
    return record(Rejection(type(e).__name__, str(e)))


def read_back(settled: dict) -> Any:
    """What a settled promise returns to the code that awaited it."""
    value = loads(settled["value"])
    if settled["state"] == RESOLVED:
        return value
    why = adapter(Rejection).validate_python(value if isinstance(value, dict) else {})
    raise Failed(f"{settled['id']}: {why.type}: {why.message}")


# ---------------------------------------------------------------------------
# The decorator
# ---------------------------------------------------------------------------

class DuplicateFunction(Exception):
    """Two functions claiming one name and version.

    The name is not a label, it is the protocol's identifier: a promise
    carries `{"f": "process"}` and a worker looks the code up by it. Two
    functions answering to it means a dispatch runs whichever module
    imported last, silently, and a task created for one executes the
    other's body. Deploy `billing.py` and `orders.py` each defining
    `process` and that is the bug.

    If they really are two generations of one function, say so with
    `@resonate(version=1)` and they coexist. If they are different
    functions, they need different names.
    """


class UnknownFunction(Exception):
    """A dispatch for code this worker does not have.

    Usually a version that has been retired while runs created under it
    were still in flight. The message lists what is deployed, because the
    useful question is which versions this container actually carries.
    """


#: Every durable function, by name *and version*. What a worker looks in to
#: find the code for a task it just claimed.
#:
#: Keyed by the pair because a run outlives the deploy that started it. A
#: promise records the position of every durable call its function made, and
#: replay reads those positions back; change the body -- insert a call,
#: reorder two -- and the positions move, so a run in flight resumes into
#: code that disagrees with its own history. Versions let the old body stay
#: deployed until the runs that need it are finished, which is the only way
#: to change a durable function without draining first.
REGISTRY: dict[tuple[str, int], "Durable"] = {}


#: Where each function runs when it is called with `.rpc`. Deployment, not
#: definition: the same function is a local call on one machine and a remote
#: one from another, and only the wiring knows which.
TARGETS: dict[str, str] = {}


def _where(fn: Callable) -> tuple[str, str]:
    """Which function this is, as something two imports of one file agree on.

    Not the function object: `functions_framework.create_app` loads
    `main.py` as a module object of its own, so a file that is also
    imported normally runs its decorators twice and builds two `Durable`s
    for one function. That is a re-import, not a collision, and the file
    and qualified name are what tell them apart.
    """
    code = getattr(fn, "__code__", None)
    if code is None:  # a callable that is not a plain function
        return (getattr(fn, "__module__", "?"), getattr(fn, "__qualname__", repr(fn)))
    return (code.co_filename, getattr(fn, "__qualname__", code.co_name))


class Durable:
    def __init__(self, fn: Callable, name: str, version: int = UNVERSIONED) -> None:
        if not isinstance(version, int) or isinstance(version, bool) or version < 0:
            raise ValueError(f"{name}: a version is a non-negative integer, not {version!r}")
        self.fn, self.name, self.version = fn, name, version
        self.where = _where(fn)

        prior = REGISTRY.get(self.key)
        if prior is not None and prior.where != self.where:
            raise DuplicateFunction(
                f"{self.label} is defined in two places:\n"
                f"  {prior.where[0]}: {prior.where[1]}\n"
                f"  {self.where[0]}: {self.where[1]}\n"
                "A promise carries this name, so a dispatch for one would run "
                "the other. Rename one, or give them versions.")
        REGISTRY[self.key] = self

    @property
    def key(self) -> tuple[str, int]:
        return (self.name, self.version)

    @property
    def label(self) -> str:
        """How this function is named to a person. The version is shown only
        when there is one, so a project that never versions anything never
        has to read about versions."""
        return self.name if self.version == UNVERSIONED else f"{self.name}@{self.version}"

    def __repr__(self) -> str:
        """Which function this is. The default carries a heap address,
        which is useless in a log and worse in a trace that is supposed to
        fingerprint the same in every process."""
        return f"@resonate {self.label}"

    async def __call__(self, *args) -> Any:
        """A local durable call: create, run if pending, settle, read back.

        The whole of post 001, with the bookkeeping under the language
        instead of at the call site.
        """
        inv, frame = current()
        id = frame.child()
        _, data = inv.fence(PromiseCreate(
            id, inv.now() + DEFAULT_TIMEOUT, call_param(self, args), {}))
        record = data["promise"]
        if record["state"] != PENDING:
            return read_back(record)

        token = _FRAME.set(_Call(id))
        try:
            try:
                value, state = dumps(await self.invoke(*args)), RESOLVED
            except (Blocked, *PLATFORM):
                # Not an answer. Nothing is recorded, and the attempt
                # unwinds -- `Blocked` is ordinary, the rest is not.
                raise
            except Exception as e:
                # An answer, and an unwelcome one. Recorded, so the next
                # run reads the same rejection rather than calling again.
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
            id, inv.now() + DEFAULT_TIMEOUT, call_param(self, args),
            {TAG_TARGET: target}))
        record = data["promise"]
        if record["state"] == PENDING:
            raise Blocked([id])
        return read_back(record)


def resonate(fn: Callable | None = None, *, version: int = UNVERSIONED):
    """Mark a function durable. That is the whole of the syntax.

        @resonate                 # version 0, and you need never think again
        async def research(q): ...

        @resonate(version=1)      # a second generation, deployed alongside
        async def research(q): ...

    Version when you change a durable function's body while runs of it are
    in flight. A run replays from the top and reads its previous calls back
    by *position*, so inserting a call or reordering two moves every
    position after it: an in-flight run resuming into the new body reads
    somebody else's answer. Deploying the new body under a new version
    leaves the old one to finish the runs that started under it, and new
    runs take the new one because that is what the caller now names.

    What does not need a version is anything a replay cannot see -- a
    faster query, a fixed typo in a prompt, a different model behind the
    same call. Position is what matters, not behaviour.
    """
    if fn is None:
        return lambda f: Durable(f, f.__name__, version)
    return Durable(fn, fn.__name__, version)


def route(fn: Durable, target: str) -> None:
    """Say where a function runs. Wiring, not definition."""
    TARGETS[fn.name] = target


async def sleep(ms: int) -> None:
    """Wait `ms` milliseconds, durably.

    A durable sleep is a promise that resolves rather than rejects when its
    deadline passes -- the kernel's `resonate:timer` tag -- so the passage of
    time settles it and the settlement wakes whoever awaited it. It carries
    no target, because there is nothing to dispatch: waiting is the whole of
    the work, and `promise_create` refuses a timer that names one.

    It takes a position like any other call, so a run that sleeps and is
    replayed reads the same promise back rather than sleeping again. A sleep
    already elapsed costs nothing on replay; a sleep still running blocks the
    worker exactly as an unfinished `rpc` does.
    """
    if ms < 0:
        raise ValueError("a sleep cannot be negative")
    inv, frame = current()
    id = frame.child()
    _, data = inv.fence(PromiseCreate(
        id, inv.now() + ms, dumps({"sleep": ms}), {TAG_TIMER: "true"}))
    if data["promise"]["state"] == PENDING:
        raise Blocked([id])
    return None


async def external(ask: Any = None, timeout: int = DEFAULT_TIMEOUT) -> Any:
    """Wait for something outside this system to answer.

    A person clicking approve, a webhook, another service, a form nobody
    has filled in yet. The run suspends: no coroutine parked, no thread, no
    row marked in-progress, no container kept alive. What is left is a
    pending promise in a bucket, and whoever has the answer settles it:

        POST /  {"kind": "promise.settle",
                 "data": {"id": "<the promise's id>", "state": "resolved",
                          "value": {"data": "\"yes\""}}}

    Its id is a position, like every other durable call, so it is the same
    on every replay and a client can find it by reading the document rather
    than by being told. `ask` is recorded in the parameter so whatever
    renders the question knows what is being asked.

    This is the whole of what other systems spell as a signal handler, a
    wait condition, and the mutable field between them. There is no handler
    because there is nothing to hold state in: the promise *is* the state,
    and it is in the bucket rather than in a process's memory.

    A deadline still applies. An external promise is not a timer, so an
    unanswered one is rejected rather than resolved when it expires, and
    the `await` raises `Failed`. That is usually what you want -- a
    confirmation nobody gave is not a confirmation -- and it is why the
    timeout is a parameter rather than a constant.
    """
    if timeout < 0:
        raise ValueError("a deadline cannot be in the past")
    inv, frame = current()
    id = frame.child()
    _, data = inv.fence(PromiseCreate(
        id, inv.now() + timeout, dumps({"ask": ask}), {TAG_EXTERNAL: "true"}))
    record = data["promise"]
    if record["state"] == PENDING:
        raise Blocked([id])
    return read_back(record)


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
