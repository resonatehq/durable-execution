"""The engine: load, decide, perform. One method.

    Engine.process(msg, now) -> Reply

`msg` is either a protocol request, which a client sent, or a `Timeout`,
which is what a deadline coming due looks like. The kernel has a function for
each — `handle_external` and `handle_internal` — and this is the only place
that chooses between them, because it is the only place that knows where a
message came from.

Everything else about the method is the same for both, and the order is the
part that matters:

1. **Load** the origin's document and the version it is at.
2. **Decide**, purely: the kernel returns the next document and its effects.
3. **Arm** the new deadline, *before* the commit, and record what it is
   called. A committed document whose deadline was never armed is the one
   state nothing repairs — the promise never times out and every answer about
   it stays correct forever — so the arm comes first and a failed arm fails
   the request rather than committing anyway.
4. **Commit**, as one conditional write against the version loaded in (1).
   A `Conflict` means the state moved and the decision is stale; it goes back
   to the caller, who retries, because every operation is idempotent. The
   engine never loops: a loop here would choose a retry policy before anything
   has said what it should be.
5. **Disarm** the old deadline, *after* the commit it belonged to is gone.
6. **Send**, strictly post-commit, so a message is always a consequence of
   committed state rather than of an intention.

The write law sits in front of all of it: if the decision left the objects
and the armed deadline untouched, nothing is written at all. The document's
clock is deliberately outside that comparison — it is a monotonicity hint,
not state, and paying a write to advance it would make every read a write.

What is left at each point the process can stop:

| stopped after | what is left | what repairs it |
|---|---|---|
| arming | a deadline nothing points at | it fires, the sweep finds nothing due and writes nothing |
| committing | the transition is durable, the old deadline still armed | it fires into a document that moved on, and is collected |
| disarming | durable, but the messages did not go | the task's retry deadline, committed before the message left |
| sending | the caller was told nothing | it retries, and every operation is idempotent |
"""

from __future__ import annotations

from dataclasses import dataclass

from codec import decode, doc_key, encode
from kernel import (
    DelTimeout, Document, KernelCfg, PromiseCreate, PromiseGet, PromiseRegisterCallback,
    PromiseRegisterListener, PromiseSettle, Reply, Req, Send, SetDocument,
    SetTimeout, TaskAcquire, TaskContinue, TaskCreate, TaskFence, TaskFulfill,
    TaskGet, TaskHalt, TaskHeartbeat, TaskRelease, TaskSuspend,
    handle_external, handle_internal, origin_of,
)
from spec.queue import SWEEP, QueueP
from spec.store import StoreP
from tracing import trace
from wire import encode_message


@dataclass(frozen=True)
class Timeout:
    """The internal message: a deadline for this origin came due. Not a
    protocol request — no client can send one — but a transition on the
    origin's document all the same."""

    origin: str


def origin_of_msg(msg: Req | Timeout) -> str:
    """Which document answers this. Every operation the protocol admits is
    single-origin, which is the whole reason one conditional write is enough,
    so there is always exactly one answer."""
    match msg:
        case Timeout():
            return msg.origin
        case TaskCreate():
            return origin_of(msg.action.id)
        case PromiseRegisterCallback() | PromiseRegisterListener():
            return origin_of(msg.awaited)
        case TaskHeartbeat():
            return origin_of(msg.tasks[0][0])
        case (PromiseGet() | PromiseCreate() | PromiseSettle() | TaskGet() | TaskAcquire()
              | TaskRelease() | TaskFulfill() | TaskSuspend() | TaskFence() | TaskHalt()
              | TaskContinue()):
            return origin_of(msg.id)
        case _:
            raise TypeError(f"no origin for {type(msg).__name__}")


def _substance(doc: Document) -> tuple:
    """What the write law compares: the objects and the armed deadline. The
    clock and the generation are excluded on purpose."""
    return ([(o.id, o.promise, o.task) for o in doc.objects], doc.timer_at)


class Engine:
    """Two ports and two dials.

    A deadline and a dispatch both go to the queue, because in production
    they are the same object: a task with an HTTP target and a time before
    which it must not be delivered. What keeps them apart is not two ports
    but the kernel's own effects, and the order they are performed in.
    """

    def __init__(self, store: StoreP, queue: QueueP,
                 cfg: KernelCfg = KernelCfg(), prefix: str = "") -> None:
        self.store, self.queue = store, queue
        self.cfg, self.prefix = cfg, prefix

    @trace
    def process(self, msg: Req | Timeout, now: int) -> Reply:
        origin = origin_of_msg(msg)
        key = doc_key(origin, self.prefix)
        # The store speaks text, because the body of an object in a bucket
        # is something a person can read; the codec speaks bytes, because a
        # document's canonical form is bytes. One `encode` each way is the
        # whole of the difference.
        found = self.store.get(key)
        version = None if found is None else found[1]
        doc = Document() if found is None else decode(found[0].encode("utf-8"), origin)
        # Fold the clock forward rather than taking it: a caller whose clock
        # has regressed must not be able to un-expire anything.
        now = max(now, doc.clock)

        if isinstance(msg, Timeout):
            fx, reply = handle_internal(doc, now, self.cfg), Reply.ok({})
        else:
            fx, reply = handle_external(doc, msg, now, self.cfg)
        new = next(e.doc for e in fx if isinstance(e, SetDocument))

        if _substance(new) == _substance(doc):
            assert not any(isinstance(e, (SetTimeout, Send)) for e in fx), \
                "a decision that changed nothing owes no effects"
            return reply

        new.clock, new.gen = now, doc.gen + 1
        for e in fx:
            if isinstance(e, SetTimeout):
                # A deadline is a task addressed to this service's own sweep
                # route, and the name it comes back with is the only handle
                # anyone will ever have on it.
                new.timer_name = self.queue.create(
                    f"{SWEEP}{origin}", {"origin": origin}, not_before=e.at)
        if new.timer_at is None:
            # The name names the armed deadline. With nothing armed there is
            # nothing to name, and a leftover name is a handle on something
            # that no longer exists. Found by the line schema.
            new.timer_name = None
        body = encode(new, origin).decode("utf-8")
        if version is None:
            self.store.put(key, body, if_absent=True)
        else:
            self.store.put(key, body, if_match=version)
        for e in fx:
            # By name, never by coordinates: the deadline being removed is the
            # one this document's predecessor armed, and a deadline that became
            # the nearest one again would otherwise be removed by someone
            # else's disarm.
            if isinstance(e, DelTimeout) and doc.timer_name is not None:
                self.queue.delete(doc.timer_name)
        for e in fx:
            if isinstance(e, Send):
                # No schedule: deliver as soon as you can, which is what an
                # immediate dispatch is. Its name is dropped, because a
                # dispatch is never cancelled — that is the whole of the
                # difference between it and the arm above.
                self.queue.create(e.address, encode_message(e.msg))
        return reply
