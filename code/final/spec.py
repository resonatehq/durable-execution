"""What an engine is, and what it has to do to be one.

Three layers, because there are three things to name and they are not the
same thing:

    EngineP   an engine, once it exists: it processes
    EngineC   how one is made: ports in, engine out
    EngineM   a module that offers one, under the name `Engine`

`EngineM` is the useful one. A conformance suite cannot be handed a class,
because a real implementation may want to choose its class at import time,
and it cannot be handed an instance, because the suite has to supply the
world the engine runs in. It is handed the module, and reaches for `Engine`.

`EngineM.Engine` is a read-only property rather than a plain attribute, and
that is not a style choice: a protocol's mutable attribute is invariant, so
`Engine: EngineC` demands a value that is *exactly* `EngineC` and a type
checker rejects `class Engine:` for it. A property is covariant, and a class
object satisfies it by being callable with the right arguments, with no
adapter. `test_types.py` is what says so; the earlier, plainer form passed
every test in this project and was still wrong.

The types alone say nothing about behaviour. The rest of this file is the
part that does: `conformance` drives an engine through a script and holds
every state it commits to the catalogue in `properties.py`, which is the
specification's, not ours. An implementation that passes has the same
observable behaviour as the reference, whatever it does inside — a different
language, a different store, a kernel written from the specification rather
than transcribed from it.

    from spec import conformance
    import engine

    assert conformance(engine) == []
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

import properties as P
from codec import decode, doc_key
from engine import Timeout
from kernel import (
    RESOLVED, Document, KernelCfg, PromiseCreate, PromiseRegisterListener,
    PromiseSettle, Reply, Req, Send, TaskAcquire, TaskFulfill, TaskSuspend,
    Value, check_invariants,
)
from ports import Conflict, Fault, Violation
from queues import SWEEP, QueueP
from store import StoreP
from wire import decode_message


class _Recorded:
    """Any `StoreP`, with its writes written down.

    The effect order and the write law are claims about *when* the engine
    wrote, so the suite has to see the writes. Wrapping rather than
    requiring a particular store is what lets the same suite grade an engine
    over the simulated store, over a real bucket, or over anything else that
    passes `store.conformance`.
    """

    def __init__(self, inner: StoreP, fault: Fault) -> None:
        self.inner, self.fault = inner, fault

    def get(self, key):
        return self.inner.get(key)

    def put(self, key, body, **conditions):
        self.fault.tick(f"commit {key}")
        return self.inner.put(key, body, **conditions)

    def delete(self, key):
        return self.inner.delete(key)

    def list(self, prefix, limit):
        return self.inner.list(prefix, limit)


class _Watched:
    """Any `QueueP`, with what it was asked to carry written down.

    The catalogue reads an outbox of typed messages, and what actually goes
    on a queue is JSON at a URL. Decoding it back is not a detour around
    the seam, it is the seam being checked: what the engine really wrote
    has to be what the specification is talking about.
    """

    def __init__(self, inner: QueueP) -> None:
        self.inner, self.sent = inner, []

    def create(self, url, body, *, not_before=0):
        if not url.startswith(SWEEP):
            self.sent.append(Send(url, decode_message(body)))
        return self.inner.create(url, body, not_before=not_before)

    def delete(self, name):
        return self.inner.delete(name)

    def take(self) -> list:
        out, self.sent = self.sent, []
        return out


#: Everything an engine can be asked to do. A protocol request, which a
#: client sent, or a deadline coming due, which nobody did.
Msg = Req | Timeout


# ---------------------------------------------------------------------------
# The three layers
# ---------------------------------------------------------------------------


@runtime_checkable
class EngineP(Protocol):
    """An engine.

    One method, because there is one thing to do: a request and a deadline
    differ in which way the state machine is consulted and in nothing else,
    so a caller never has to know which kind of shell it is talking to.

    `process` returns the protocol's answer and raises only when the world
    refused: `Conflict` when the write lost its race, which the caller
    retries because every operation is idempotent, and whatever the ports
    raise when they cannot answer at all.

    One member, and deliberately no more. An engine has no name, no
    identity and no lifecycle to manage: everything it knows is in the
    bucket, so two of them are interchangeable and a conformance report
    names the module it was handed rather than asking the engine who it is.
    """

    def process(self, msg: Msg, now: int) -> Reply: ...


class EngineC(Protocol):
    """How an engine is made: the two ports, and the dials.

    The ports are arguments rather than imports because the suite below has
    to supply them. That is not a testing convenience — it is the same seam
    that lets one engine run over a bucket in production and over a dict in
    a simulation, and it is why a simulated run is a real run.
    """

    def __call__(self, store: StoreP, queue: QueueP,
                 cfg: KernelCfg = ..., prefix: str = ...) -> EngineP: ...

    # This signature is real: every engine takes the same two ports,
    # because a port is an interface rather than a configuration. `StoreC`
    # and `QueueC` cannot say as much, and say so.


class EngineM(Protocol):
    """A module that offers an engine."""

    @property
    def Engine(self) -> EngineC: ...


# ---------------------------------------------------------------------------
# The script every implementation is driven through
# ---------------------------------------------------------------------------

W = "http://w"
CFG = KernelCfg(retry_timeout=30_000)

#: One origin, because that is the unit an engine commits: every operation
#: the protocol admits is single-origin, which is the whole reason one
#: conditional write is enough. A multi-origin script would test the caller's
#: routing, not the engine.
ORIGIN = "run"


def _targeted(id, to=1_000_000):
    return PromiseCreate(id, to, Value(), {"resonate:target": W})


#: Births, a lease taken and expiring, a suspension, a wake, a settlement
#: that fans out to a listener, a replay, and a deadline that fires. Long
#: enough that an implementation cannot pass it by accident.
STANDARD_SCRIPT: list[tuple[Msg, int]] = [
    (_targeted("run"), 0),
    (TaskAcquire("run", 0, "w1", 5_000), 1),
    (_targeted("run:1"), 2),
    (PromiseRegisterListener("run:1", "http://l"), 3),
    (TaskSuspend("run", 1, ("run:1",)), 4),
    (TaskAcquire("run:1", 0, "w2", 5_000), 5),
    (Timeout(ORIGIN), 10_000),                      # the lease on run:1 expires
    (TaskAcquire("run:1", 1, "w3", 5_000), 10_001),
    (TaskFulfill("run:1", 2, PromiseSettle("run:1", RESOLVED, Value(data="42"))), 10_002),
    (TaskAcquire("run", 1, "w4", 5_000), 10_003),
    (_targeted("run:1"), 10_004),                   # replay: create reads back
    (PromiseCreate("run:2", 10_100, Value(), {"resonate:timer": "true"}), 10_005),
    (Timeout(ORIGIN), 200_000),                     # the timer resolves, the lease expires
    (TaskFulfill("run", 2, PromiseSettle("run", RESOLVED, Value(data="done"))), 200_001),
]


# ---------------------------------------------------------------------------
# Conformance
# ---------------------------------------------------------------------------


def _substance(doc: Document) -> tuple:
    """What the write law compares: the objects and the armed deadline.

    Not the bytes. The document's generation lives in its header, so a
    wasted write changes the bytes while changing nothing that matters, and
    a byte comparison would let it through. Not the engine's own notion of
    substance either, on purpose: a specification that borrowed the
    implementation's comparison would be checking that the implementation
    agrees with itself.
    """
    return ([(o.id, o.promise, o.task) for o in doc.objects], doc.timer_at)


def _kind(write: str) -> str:
    """What a line of the write log was.

    An arm and a send are the same call to the same port — `create` — so
    they are told apart the way the deployment tells them apart: by where
    the task is addressed. That is a better question than which method the
    engine reached for, because it is what the queue will actually see.
    """
    if write.startswith("commit "):
        return "commit"
    if write.startswith("delete "):
        return "disarm"
    if write.startswith("create "):
        return "arm" if write[len("create "):].startswith(SWEEP) else "send"
    return "?"


def _effect_order(segment: list[str]) -> str | None:
    """Arm, commit, disarm, send, and nothing out of place.

    The order is what makes every crash window recoverable, so it is part of
    the contract rather than an implementation detail. A decision that
    committed nothing owes nothing: no deadline armed for a document that did
    not change, and no message for a transition that did not happen.
    """
    kinds = [_kind(w) for w in segment]
    commits = [i for i, k in enumerate(kinds) if k == "commit"]
    if len(commits) > 1:
        return f"{len(commits)} writes for one message; a transition is one commit"
    if not commits:
        return None if not segment else f"effects without a commit: {segment}"
    before, after = kinds[: commits[0]], kinds[commits[0] + 1:]
    if any(k != "arm" for k in before):
        return f"something other than an arm before the commit: {segment}"
    i = 0
    while i < len(after) and after[i] == "disarm":
        i += 1
    if any(k != "send" for k in after[i:]):
        return f"a disarm after a send, or worse: {segment}"
    return None


def conformance(module: EngineM, script: list[tuple[Msg, int]] | None = None,
                cfg: KernelCfg = CFG, origin: str = ORIGIN,
                store: StoreP | None = None) -> list[Violation]:
    """Drive `module.Engine` through a script and return everything it broke.

    Three things are checked at every step, and they are independent:

    - **The catalogue.** Every document the engine commits is a state the
      specification admits, and every consecutive pair is a transition it
      admits. A `Timeout` step is additionally held to the sweeper
      properties, which are strictly stronger: a background sweep may
      re-pend, fulfil, resume or refresh, and may never acquire, suspend,
      halt or continue a task, nor settle a promise except by its deadline.
    - **The effect order**, because it is what the crash windows rest on.
    - **The write law**, because a read that writes is a read that costs a
      conditional write per poll, and on a store with a per-object write
      rate that is the difference between working and not.

    `store` is the world the engine is given. It defaults to the simulated
    one; hand it `store_gcp.Store` and the same suite grades the same engine
    through the seam it will really run on.
    """
    import queue_mem
    import store_mem  # here, so `store.py` may import this module's Violation

    script = STANDARD_SCRIPT if script is None else script
    fault = Fault()  # not injecting: used here only as the log of what was written
    store = _Recorded(store_mem.Store() if store is None else store, fault)
    queue = _Watched(queue_mem.Queue(fault=fault))
    engine = module.Engine(store, queue, cfg)
    out: list[Violation] = []
    state = P.State(Document(), retry_timeout=cfg.retry_timeout)
    key = doc_key(origin)

    def committed() -> Document:
        found = store.get(key)
        return Document() if found is None else decode(found[0].encode("utf-8"), origin)

    for step, (msg, now) in enumerate(script):
        mark, before = len(fault.log), committed()
        try:
            engine.process(msg, now)
        except Conflict as e:
            out.append(Violation(step, type(msg).__name__, f"unexpected conflict: {e}"))
            continue
        segment, doc = fault.log[mark:], committed()
        label = type(msg).__name__

        if (bad := _effect_order(segment)) is not None:
            out.append(Violation(step, label, bad))
        if _substance(doc) == _substance(before) and segment:
            out.append(Violation(step, label, f"nothing changed, yet it wrote: {segment}"))

        if (bad := check_invariants(doc)) is not None:
            out.append(Violation(step, label, f"committed a document that is not well formed: {bad}"))
        nxt = state.after(doc, queue.take())
        for bad in P.state_failures(now, nxt):
            out.append(Violation(step, label, bad))
        for bad in P.trans_failures(now, state, nxt):
            out.append(Violation(step, label, bad))
        if isinstance(msg, Timeout):
            for bad in P.internal_failures(now, state, nxt):
                out.append(Violation(step, label, bad))
        state = nxt

    return out
