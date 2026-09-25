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


DOCUMENT = TypeAdapter(Document)


def doc_key(origin: str, prefix: str = "") -> str:
    escaped = []
    for b in origin.encode("utf-8"):
        c = chr(b)
        escaped.append(c if (c.isalnum() and b < 128) or c in ".-" else f"%{b:02X}")
    return f"{prefix}wf/{''.join(escaped)}"


def encode(doc: Document) -> str:
    return DOCUMENT.dump_json(doc, by_alias=True).decode("utf-8")


def decode(raw: str) -> Document:
    return DOCUMENT.validate_json(raw)


def origin_of_msg(msg: Req | Timeout) -> str:
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
                new.timer_name = self.queue.create(
                    HERE, encode_message(Timeout(origin)), not_before=e.at)
        if new.timer_at is None:
            new.timer_name = None
        body = encode(new)
        if version is None:
            self.store.put(key, body, if_absent=True)
        else:
            self.store.put(key, body, if_match=version)
        for e in fx:
            if isinstance(e, DelTimeout) and doc.timer_name is not None:
                self.queue.delete(doc.timer_name)
        for e in fx:
            if isinstance(e, Send):
                self.queue.create(e.address, encode_message(e.msg))
        return reply
