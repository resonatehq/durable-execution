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
2. **Decide**, purely: the kernel returns the reply and the effects, in
   the order below. A decision that changed nothing has no effects, and the
   engine returns the reply without writing.
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
5. **Disarm** the old deadline, by the name the loaded document recorded,
   *after* the commit that replaced it.
6. **Send**, strictly post-commit, so a message is always a consequence of
   committed state rather than of an intention.

The document's clock and generation are stamped only on a write that happens
anyway; advancing them alone never causes one, or every read would be a write.

What is left at each point the process can stop:

| stopped after | what is left | what repairs it |
|---|---|---|
| arming | a deadline nothing points at | it fires, the sweep finds nothing due and writes nothing |
| committing | the transition is durable, the old deadline still armed | it fires; the sweep does only what is due, usually nothing |
| disarming | durable, but the messages did not go | the task's retry deadline, committed before the message left |
| sending | the caller was told nothing | it retries, and every operation is idempotent |

Above the class are the three things a caller needs to find the document
and read it: `doc_key`, `encode`, `decode`. They were a module of their own
called `codec`, which named one topic and held two -- where a document
lives is not how it is written -- and whose `encode` had exactly one
caller, this file. They are here because this is the only production code
that reads or writes a document at all. The tests and the conformance suite
import them from here, because looking at what the engine wrote means
knowing where it wrote it.
"""

from __future__ import annotations


from pydantic import TypeAdapter

from .kernel import (
    DelTimeout, Document, KernelCfg, Send, SetDocument, SetTimeout,
    handle_external, handle_internal, origin_of,
)
from .types import (
    PromiseCreate, PromiseGet, PromiseRegisterCallback,
    PromiseRegisterListener, PromiseSettle, Reply, Req, TaskAcquire,
    TaskContinue, TaskCreate, TaskFence, TaskFulfill, TaskGet, TaskHalt,
    TaskHeartbeat, TaskRelease, TaskSuspend,
)
from .spec.queue import QueueP
from .spec.store import StoreP
from .types import HERE, Timeout, encode_message


#: The document, as the store keeps it. One adapter rather than one per
#: call: building it is the expensive half, and `by_alias` below is the
#: camelCase wire format, which is a property of the document rather than a
#: choice a caller gets to make.
DOCUMENT = TypeAdapter(Document)


def doc_key(origin: str, prefix: str = "") -> str:
    """Where an origin's document lives. The origin is percent-encoded: `/`
    would create a path segment and `:` is the origin separator this design
    reserves, so both are escaped along with everything non-alphanumeric."""
    escaped = []
    for b in origin.encode("utf-8"):
        c = chr(b)
        escaped.append(c if (c.isalnum() and b < 128) or c in ".-" else f"%{b:02X}")
    return f"{prefix}wf/{''.join(escaped)}"


def encode(doc: Document) -> str:
    """The document as the JSON a store is handed.

    Text, not bytes, because that is what `StoreP` takes: Pydantic dumps
    bytes and this is the one place that decodes them, rather than every
    caller doing it on the line after the call.
    """
    return DOCUMENT.dump_json(doc, by_alias=True).decode("utf-8")


def decode(raw: str) -> Document:
    """A document back from what a store returned."""
    return DOCUMENT.validate_json(raw)


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

    def process(self, msg: Req | Timeout, now: int) -> Reply:
        origin = origin_of_msg(msg)
        key = doc_key(origin, self.prefix)
        found = self.store.get(key)
        version = None if found is None else found[1]
        doc = Document() if found is None else decode(found[0])
        # Fold the clock forward rather than taking it: a caller whose clock
        # has regressed must not be able to un-expire anything.
        now = max(now, doc.clock)

        if isinstance(msg, Timeout):
            fx, reply = handle_internal(doc, now, self.cfg), Reply.ok({})
        else:
            fx, reply = handle_external(doc, msg, now, self.cfg)
        if not fx:
            return reply
        new = next(e.doc for e in fx if isinstance(e, SetDocument))

        new.clock, new.gen = now, doc.gen + 1
        for e in fx:
            if isinstance(e, SetTimeout):
                # A deadline is a timeout message to this service itself, and
                # the name it comes back with is the only handle anyone will
                # ever have on it.
                new.timer_name = self.queue.create(
                    HERE, encode_message(Timeout(origin)), not_before=e.at)
        if new.timer_at is None:
            # The name names the armed deadline. With nothing armed there is
            # nothing to name, and a leftover name is a handle on something
            # that no longer exists.
            new.timer_name = None
        body = encode(new)
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
