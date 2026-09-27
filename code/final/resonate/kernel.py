from __future__ import annotations

import copy
import re
from bisect import insort
from dataclasses import dataclass, field, replace
from typing import Any
from urllib.parse import urlsplit

from .types import (
    Execute, PROTOCOL_VERSION, PromiseCreate,
    PromiseGet, PromiseRegisterCallback, PromiseRegisterListener, PromiseSettle,
    Reply, Req, TaskAcquire, TaskContinue,
    TaskCreate, TaskFence, TaskFulfill, TaskGet,
    TaskHalt, TaskHeartbeat, TaskRelease, TaskSuspend,
    Unblock, Value, record, wire,
)


PENDING = "pending"
RESOLVED = "resolved"
REJECTED = "rejected"
REJECTED_CANCELED = "rejected_canceled"
REJECTED_TIMEDOUT = "rejected_timedout"

T_PENDING = "pending"
T_ACQUIRED = "acquired"
T_SUSPENDED = "suspended"
T_HALTED = "halted"
T_FULFILLED = "fulfilled"

TAG_TARGET = "resonate:target"
TAG_TIMER = "resonate:timer"
TAG_DELAY = "resonate:delay"
TAG_BRANCH = "resonate:branch"
TAG_SCOPE = "resonate:scope"
TAG_EXTERNAL = "resonate:external"


@dataclass(frozen=True)
class KernelCfg:
    retry_timeout: int = 30_000
    preload_limit: int = 10


@wire
@dataclass
class Promise:
    state: str = PENDING
    param: Value = field(default_factory=Value)
    value: Value = field(default_factory=Value)
    tags: dict[str, str] = field(default_factory=dict)
    timeout_at: int = 0
    created_at: int = 0
    settled_at: int | None = None
    callbacks: list[str] = field(default_factory=list)
    listeners: list[str] = field(default_factory=list)

    def target(self) -> str | None:
        return self.tags.get(TAG_TARGET)

    def is_external(self) -> bool:
        return (
            self.tags.get(TAG_SCOPE) == "global"
            or self.tags.get(TAG_EXTERNAL) == "true"
            or TAG_TARGET in self.tags
            or self.tags.get(TAG_TIMER) == "true"
        )

    def timeout_state(self) -> str:
        return RESOLVED if self.tags.get(TAG_TIMER) == "true" else REJECTED_TIMEDOUT

    def timeout_armed(self) -> bool:
        return self.state == PENDING and self.is_external()

    def to_record(self, id: str) -> dict[str, Any]:
        return {"id": id, **record(self, exclude={"callbacks", "listeners"})}


@wire
@dataclass
class Task:
    state: str = T_PENDING
    version: int = 0
    pid: str | None = None
    ttl: int | None = None
    resumes: set[str] = field(default_factory=set)
    retry_at: int | None = None
    lease_at: int | None = None

    def to_record(self, id: str) -> dict[str, Any]:
        return {"id": id, **record(self, exclude={"retry_at", "lease_at"})}


@wire
@dataclass
class Object:
    id: str
    promise: Promise
    task: Task | None = None


def dewey(id: str) -> tuple[tuple[int, Any], ...]:
    return tuple(
        (0, int(seg)) if seg.isdigit() else (1, seg) for seg in re.split(r"[:.]", id)
    )


@wire
@dataclass
class Document:
    objects: list[Object] = field(default_factory=list)
    clock: int = 0
    gen: int = 0
    timer_at: int | None = None
    timer_name: str | None = None

    def get(self, id: str) -> Object | None:
        for o in self.objects:
            if o.id == id:
                return o
        return None

    def insert(self, o: Object) -> Object:
        insort(self.objects, o, key=lambda x: dewey(x.id))
        return o


def min_deadline(doc: Document) -> int | None:
    deadlines = []
    for o in doc.objects:
        if o.promise.timeout_armed():
            deadlines.append(o.promise.timeout_at)
        if o.task is not None:
            deadlines += [at for at in (o.task.retry_at, o.task.lease_at) if at is not None]
    return min(deadlines) if deadlines else None


@dataclass(frozen=True)
class SetDocument:
    doc: Document


@dataclass(frozen=True)
class SetTimeout:
    at: int


@dataclass(frozen=True)
class DelTimeout:
    at: int


@dataclass(frozen=True)
class Send:
    address: str
    msg: Execute | Unblock


Effect = SetDocument | SetTimeout | DelTimeout | Send


SETTLE_STATES = (RESOLVED, REJECTED, REJECTED_CANCELED)


def origin_of(id: str) -> str:
    return id.split(":", 1)[0]


def is_valid_address(address: str) -> bool:
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", address) is None:
        return False
    parts = urlsplit(address)
    if parts.scheme in ("http", "https", "ws", "wss", "ftp") and not parts.netloc:
        return False
    return True


@dataclass
class Commands:
    add: list[Object] = field(default_factory=list)
    send: list[Send] = field(default_factory=list)

    def merge(self, other: Commands) -> Commands:
        return Commands(self.add + other.add, self.send + other.send)

    def view(self, doc: Document) -> Document:
        objects = {o.id: o for o in doc.objects}
        for o in self.add:
            objects[o.id] = o
        return Document(sorted(objects.values(), key=lambda o: dewey(o.id)),
                        doc.clock, doc.gen, doc.timer_at, doc.timer_name)


NOTHING = Commands()


def execute(o: Object) -> list[Send]:
    address = o.promise.target()
    return [] if address is None else [Send(address, Execute(o.id, o.task.version))]


def pending(t: Task, retry_at: int) -> Task:
    return replace(t, state=T_PENDING, pid=None, ttl=None, retry_at=retry_at, lease_at=None)


def acquired(t: Task, pid: str, ttl: int, now: int) -> Task:
    return replace(t, state=T_ACQUIRED, version=t.version + 1, pid=pid, ttl=ttl,
                   resumes=set(), retry_at=None, lease_at=now + ttl)


def parked(t: Task, state: str) -> Task:
    return replace(t, state=state, pid=None, ttl=None, resumes=set(),
                   retry_at=None, lease_at=None)


def handle_internal(doc: Document, now: int, cfg: KernelCfg) -> list[Effect]:
    return commit(doc, sweep(doc, now, cfg))


def sweep(doc: Document, now: int, cfg: KernelCfg) -> Commands:
    expired = [o for o in doc.objects
               if o.promise.state == PENDING and now >= o.promise.timeout_at]
    c = Commands(add=[
        replace(o, promise=replace(o.promise, state=o.promise.timeout_state(),
                                   settled_at=o.promise.timeout_at))
        for o in expired])
    for o in expired:
        c = c.merge(trigger_settlement(c.view(doc), o.id, now, cfg))

    for o in c.view(doc).objects:
        t = o.task
        if t is not None and t.state == T_PENDING and t.retry_at is not None and t.retry_at <= now:
            o = replace(o, task=replace(t, retry_at=now + cfg.retry_timeout))
            c = c.merge(Commands([o], execute(o)))

    for o in c.view(doc).objects:
        t = o.task
        if t is not None and t.state == T_ACQUIRED and t.lease_at is not None and t.lease_at <= now:
            o = replace(o, task=pending(t, now + cfg.retry_timeout))
            c = c.merge(Commands([o], execute(o)))
    return c


def handle_external(doc: Document, req: Req, now: int, cfg: KernelCfg) -> tuple[list[Effect], Reply]:
    swept = sweep(doc, now, cfg)
    mid = swept.view(doc)
    match req:
        case PromiseGet():
            reply, c = promise_get(mid, req)
        case PromiseCreate():
            reply, c = promise_create(mid, req, now, cfg)
        case PromiseSettle():
            reply, c = promise_settle(mid, req, now, cfg)
        case PromiseRegisterCallback():
            reply, c = promise_register_callback(mid, req)
        case PromiseRegisterListener():
            reply, c = promise_register_listener(mid, req)
        case TaskGet():
            reply, c = task_get(mid, req)
        case TaskCreate():
            reply, c = task_create(mid, req, now, cfg)
        case TaskAcquire():
            reply, c = task_acquire(mid, req, now, cfg)
        case TaskRelease():
            reply, c = task_release(mid, req, now, cfg)
        case TaskFulfill():
            reply, c = task_fulfill(mid, req, now, cfg)
        case TaskSuspend():
            reply, c = task_suspend(mid, req, cfg)
        case TaskFence():
            reply, c = task_fence(mid, req, now, cfg)
        case TaskHeartbeat():
            reply, c = task_heartbeat(mid, req, now)
        case TaskHalt():
            reply, c = task_halt(mid, req)
        case TaskContinue():
            reply, c = task_continue(mid, req, now, cfg)

    final = c.view(mid)
    kept = []
    for e in swept.send:
        if isinstance(e.msg, Execute):
            o = final.get(e.msg.task_id)
            if o is None or o.task is None or o.task.state != T_PENDING or o.task.version != e.msg.version:
                continue
        kept.append(e)
    return commit(doc, Commands(swept.add + c.add, kept + c.send)), reply


def commit(doc: Document, c: Commands) -> list[Effect]:
    if not c.add:
        assert not c.send, "a decision that changed nothing owes no effects"
        return []
    new = c.view(doc)
    old, new.timer_at = doc.timer_at, min_deadline(new)
    fx: list[Effect] = []
    if old != new.timer_at and new.timer_at is not None:
        fx.append(SetTimeout(new.timer_at))
    fx.append(SetDocument(new))
    if old != new.timer_at and old is not None:
        fx.append(DelTimeout(old))
    fx.extend(c.send)
    return fx


def document_after(doc: Document, fx: list[Effect]) -> Document:
    return next((e.doc for e in fx if isinstance(e, SetDocument)), doc)


def promise_get(doc: Document, r: PromiseGet) -> tuple[Reply, Commands]:
    o = doc.get(r.id)
    if o is None:
        return Reply.err(404, "Promise not found"), NOTHING
    return Reply.ok({"promise": o.promise.to_record(r.id)}), NOTHING


def promise_create(doc: Document, r: PromiseCreate, now: int, cfg: KernelCfg) -> tuple[Reply, Commands]:
    address = r.tags.get(TAG_TARGET)
    if address is not None and not is_valid_address(address):
        return Reply.err(400, "Invalid resonate:target address"), NOTHING
    if r.tags.get(TAG_TIMER) == "true" and address is not None:
        return Reply.err(400, "A timer promise must not have a resonate:target tag"), NOTHING
    delay = r.tags.get(TAG_DELAY)
    if delay is not None:
        if not delay.isdigit():
            return Reply.err(400, "resonate:delay must be a non-negative integer"), NOTHING
        if int(delay) >= r.timeout_at:
            return Reply.err(400, "resonate:delay must be less than timeoutAt"), NOTHING
        if address is None:
            return Reply.err(400, "resonate:delay requires a resonate:target tag"), NOTHING
    o = doc.get(r.id)
    if o is not None:
        return Reply.ok({"promise": o.promise.to_record(r.id)}), NOTHING

    p = new_promise(r, now)
    reply = Reply.ok({"promise": p.to_record(r.id)})
    if p.target() is None:
        return reply, Commands([Object(r.id, p)])
    if p.state != PENDING:
        return reply, Commands([Object(r.id, p, Task(state=T_FULFILLED))])
    if delay is not None and now < int(delay):
        return reply, Commands([Object(r.id, p, Task(state=T_PENDING, retry_at=int(delay)))])
    o = Object(r.id, p, Task(state=T_PENDING, retry_at=p.created_at + cfg.retry_timeout))
    return reply, Commands([o], execute(o))


def promise_settle(doc: Document, r: PromiseSettle, now: int, cfg: KernelCfg) -> tuple[Reply, Commands]:
    if r.state not in SETTLE_STATES:
        return Reply.err(400, "Invalid settle state"), NOTHING
    o = doc.get(r.id)
    if o is None:
        return Reply.err(404, "Promise not found"), NOTHING
    if o.promise.state != PENDING:
        return Reply.ok({"promise": o.promise.to_record(r.id)}), NOTHING
    record, c = settle(doc, r.id, r.state, r.value, now, cfg)
    return Reply.ok({"promise": record}), c


def promise_register_callback(doc: Document, r: PromiseRegisterCallback) -> tuple[Reply, Commands]:
    if r.awaited == r.awaiter:
        return Reply.err(400, "Awaited and awaiter must be different promises"), NOTHING
    if origin_of(r.awaited) != origin_of(r.awaiter):
        return Reply.err(400, "Awaiter and awaited must belong to the same origin"), NOTHING
    awaited = doc.get(r.awaited)
    if awaited is None:
        return Reply.err(404, "Awaited promise not found"), NOTHING
    awaiter = doc.get(r.awaiter)
    if awaiter is None:
        return Reply.err(422, "Awaiter promise not found"), NOTHING
    if awaiter.promise.target() is None:
        return Reply.err(422, "Awaiter promise has no resonate:target tag"), NOTHING
    if not awaited.promise.is_external():
        return Reply.err(422, "Awaited promise is not awaitable"), NOTHING
    reply = Reply.ok({"promise": awaited.promise.to_record(r.awaited)})

    p = awaited.promise
    if p.state != PENDING or awaiter.promise.state != PENDING or r.awaiter in p.callbacks:
        return reply, NOTHING
    return reply, Commands([replace(awaited, promise=replace(p, callbacks=[*p.callbacks, r.awaiter]))])


def promise_register_listener(doc: Document, r: PromiseRegisterListener) -> tuple[Reply, Commands]:
    if not is_valid_address(r.address):
        return Reply.err(400, "Invalid listener address"), NOTHING
    o = doc.get(r.awaited)
    if o is None:
        return Reply.err(404, "Awaited promise not found"), NOTHING
    if not o.promise.is_external():
        return Reply.err(422, "Awaited promise is not awaitable"), NOTHING
    reply = Reply.ok({"promise": o.promise.to_record(r.awaited)})
    p = o.promise
    if p.state != PENDING or r.address in p.listeners:
        return reply, NOTHING
    return reply, Commands([replace(o, promise=replace(p, listeners=[*p.listeners, r.address]))])


def task_get(doc: Document, r: TaskGet) -> tuple[Reply, Commands]:
    o = doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found"), NOTHING
    return Reply.ok({"task": o.task.to_record(r.id)}), NOTHING


def claimed(doc: Document, o: Object, cfg: KernelCfg) -> Reply:
    return Reply.ok({
        "task": o.task.to_record(o.id),
        "promise": o.promise.to_record(o.id),
        "preload": preload(doc, o.id, cfg),
    })


def task_create(doc: Document, r: TaskCreate, now: int, cfg: KernelCfg) -> tuple[Reply, Commands]:
    a = r.action
    address = a.tags.get(TAG_TARGET)
    if address is None:
        return Reply.err(400, "Action must have a resonate:target tag"), NOTHING
    if not is_valid_address(address):
        return Reply.err(400, "Invalid resonate:target address"), NOTHING
    if a.tags.get(TAG_TIMER) == "true":
        return Reply.err(400, "A timer promise must not have a resonate:target tag"), NOTHING
    if TAG_DELAY in a.tags:
        return Reply.err(400, "Action must not have a resonate:delay tag"), NOTHING
    if r.ttl < 1:
        return Reply.err(400, "TTL must be a positive integer"), NOTHING
    o = doc.get(a.id)
    if o is not None and o.task is not None:
        if o.task.state == T_PENDING:
            o = replace(o, task=acquired(o.task, r.pid, r.ttl, now))
            c = Commands([o])
            return claimed(c.view(doc), o, cfg), c
        if o.task.state == T_FULFILLED:
            return Reply.ok({
                "task": o.task.to_record(a.id),
                "promise": o.promise.to_record(a.id),
                "preload": [],
            }), NOTHING
        return Reply.err(409, "Already exists"), NOTHING
    if o is not None:
        return Reply.err(422, "The promise does not have a resonate:target tag"), NOTHING

    p = new_promise(a, now)
    if p.state == PENDING:
        t = Task(state=T_ACQUIRED, version=1, pid=r.pid, ttl=r.ttl, lease_at=now + r.ttl)
    else:
        t = Task(state=T_FULFILLED)
    o = Object(a.id, p, t)
    c = Commands([o])
    return claimed(c.view(doc), o, cfg), c


def task_acquire(doc: Document, r: TaskAcquire, now: int, cfg: KernelCfg) -> tuple[Reply, Commands]:
    if r.ttl < 1:
        return Reply.err(400, "TTL must be a positive integer"), NOTHING
    o = doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found"), NOTHING
    if o.task.state != T_PENDING:
        return Reply.err(409, "Task is not pending"), NOTHING
    if o.task.version != r.version:
        return Reply.err(409, "Version mismatch"), NOTHING
    o = replace(o, task=acquired(o.task, r.pid, r.ttl, now))
    c = Commands([o])
    return claimed(c.view(doc), o, cfg), c


def task_release(doc: Document, r: TaskRelease, now: int, cfg: KernelCfg) -> tuple[Reply, Commands]:
    o = doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found"), NOTHING
    if o.task.state != T_ACQUIRED or o.task.version != r.version:
        return Reply.err(409, "Task version mismatch or invalid state"), NOTHING
    o = replace(o, task=pending(o.task, now + cfg.retry_timeout))
    return Reply.ok({}), Commands([o], execute(o))


def task_fulfill(doc: Document, r: TaskFulfill, now: int, cfg: KernelCfg) -> tuple[Reply, Commands]:
    if r.action.id != r.id:
        return Reply.err(400, "Action ID must match the task ID"), NOTHING
    if r.action.state not in SETTLE_STATES:
        return Reply.err(400, "Invalid settle state"), NOTHING
    o = doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found"), NOTHING
    if o.task.state != T_ACQUIRED or o.task.version != r.version:
        return Reply.err(409, "Task version mismatch or invalid state"), NOTHING
    if o.promise.state != PENDING:
        return (Reply.ok({"promise": o.promise.to_record(r.id)}),
                Commands([replace(o, task=parked(o.task, T_FULFILLED))]))
    record, c = settle(doc, r.id, r.action.state, r.action.value, now, cfg)
    return Reply.ok({"promise": record}), c


def task_suspend(doc: Document, r: TaskSuspend, cfg: KernelCfg) -> tuple[Reply, Commands]:
    if not r.awaited:
        return Reply.err(400, "Actions array cannot be empty"), NOTHING
    if r.id in r.awaited:
        return Reply.err(400, "Action awaited promise must not equal the task ID"), NOTHING
    if len(set(r.awaited)) != len(r.awaited):
        return Reply.err(400, "Awaited promise IDs must be unique"), NOTHING
    if any(origin_of(a) != origin_of(r.id) for a in r.awaited):
        return Reply.err(400, "Awaited promise must belong to the same origin as the task"), NOTHING
    o = doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found"), NOTHING
    if o.task.state != T_ACQUIRED or o.task.version != r.version:
        return Reply.err(409, "Task is not acquired or version mismatch"), NOTHING
    awaited = [doc.get(a) for a in r.awaited]
    if any(x is None for x in awaited):
        return Reply.err(422, "Awaited promise not found"), NOTHING
    if not all(x.promise.is_external() for x in awaited):
        return Reply.err(422, "Awaited promise is not awaitable"), NOTHING
    if any(x.promise.state != PENDING for x in awaited):
        c = Commands([replace(o, task=replace(o.task, resumes=set()))]) if o.task.resumes else NOTHING
        return Reply(300, {"preload": preload(doc, r.id, cfg)}), c
    rungs = [replace(x, promise=replace(x.promise, callbacks=[*x.promise.callbacks, r.id]))
             for x in awaited if r.id not in x.promise.callbacks]
    return Reply.ok({}), Commands([*rungs, replace(o, task=parked(o.task, T_SUSPENDED))])


def task_fence(doc: Document, r: TaskFence, now: int, cfg: KernelCfg) -> tuple[Reply, Commands]:
    if r.action.id == r.id:
        return Reply.err(400, "Action ID must not equal the task ID"), NOTHING
    o = doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found"), NOTHING
    if o.task.state != T_ACQUIRED or o.task.version != r.version:
        return Reply.err(409, "Version mismatch"), NOTHING
    match r.action:
        case PromiseCreate():
            kind, (nested, c) = "promise.create", promise_create(doc, r.action, now, cfg)
            if nested.status == 400:
                return nested, NOTHING
        case PromiseSettle():
            kind, (nested, c) = "promise.settle", promise_settle(doc, r.action, now, cfg)
        case _:
            return Reply.err(400, "Invalid fence action kind"), NOTHING
    return Reply.ok({
        "action": {
            "kind": kind,
            "head": {"corrId": r.corr_id, "status": nested.status, "version": PROTOCOL_VERSION},
            "data": nested.data,
        },
        "preload": preload(c.view(doc), r.id, cfg),
    }), c


def task_heartbeat(doc: Document, r: TaskHeartbeat, now: int) -> tuple[Reply, Commands]:
    if len({origin_of(id) for id, _ in r.tasks}) > 1:
        return Reply.err(400, "All tasks must belong to the same origin"), NOTHING
    c = NOTHING
    for id, version in r.tasks:
        o = c.view(doc).get(id)
        t = o.task if o is not None else None
        if (t is not None and t.state == T_ACQUIRED and t.version == version and t.pid == r.pid
                and t.ttl is not None and t.lease_at != now + t.ttl):
            c = c.merge(Commands([replace(o, task=replace(t, lease_at=now + t.ttl))]))
    return Reply.ok({}), c


def task_halt(doc: Document, r: TaskHalt) -> tuple[Reply, Commands]:
    o = doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found"), NOTHING
    if o.task.state == T_FULFILLED:
        return Reply.err(409, "Task is fulfilled"), NOTHING
    if o.task.state == T_HALTED:
        return Reply.ok({}), NOTHING
    halted = replace(o.task, state=T_HALTED, pid=None, ttl=None, retry_at=None, lease_at=None)
    return Reply.ok({}), Commands([replace(o, task=halted)])


def task_continue(doc: Document, r: TaskContinue, now: int, cfg: KernelCfg) -> tuple[Reply, Commands]:
    o = doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found"), NOTHING
    if o.task.state != T_HALTED:
        return Reply.err(409, "Task is not halted"), NOTHING
    o = replace(o, task=replace(o.task, state=T_PENDING, retry_at=now + cfg.retry_timeout))
    return Reply.ok({}), Commands([o], execute(o))


def settle(doc: Document, id: str, state: str, value: Value, now: int,
           cfg: KernelCfg) -> tuple[dict[str, Any], Commands]:
    o = doc.get(id)
    assert o is not None and o.promise.state == PENDING
    o = replace(o, promise=replace(o.promise, state=state, value=copy.deepcopy(value), settled_at=now))
    c = Commands([o])
    return o.promise.to_record(id), c.merge(trigger_settlement(c.view(doc), id, now, cfg))


def preload(doc: Document, id: str, cfg: KernelCfg) -> list[dict[str, Any]]:
    o = doc.get(id)
    branch = o.promise.tags.get(TAG_BRANCH) if o is not None else None
    if not branch:
        return []
    return [
        x.promise.to_record(x.id)
        for x in doc.objects
        if x.id != id and x.promise.tags.get(TAG_BRANCH) == branch
    ][: cfg.preload_limit]


def new_promise(r: PromiseCreate, now: int) -> Promise:
    p = Promise(param=copy.deepcopy(r.param), tags=dict(r.tags), timeout_at=r.timeout_at,
                created_at=now)
    if now >= r.timeout_at:
        p = replace(p, state=p.timeout_state(), created_at=r.timeout_at, settled_at=r.timeout_at)
    return p


def trigger_settlement(doc: Document, id: str, now: int, cfg: KernelCfg) -> Commands:
    o = doc.get(id)
    assert o is not None

    task = o.task
    if task is not None and task.state != T_FULFILLED:
        task = parked(task, T_FULFILLED)
    c = Commands([replace(o, task=task, promise=replace(o.promise, callbacks=[], listeners=[]))])

    for awaiter in o.promise.callbacks:
        ao = doc.get(awaiter)
        if ao is None or ao.task is None:
            continue
        if ao.promise.state != PENDING or now >= ao.promise.timeout_at:
            continue
        t = ao.task
        if t.state == T_SUSPENDED:
            ao = replace(ao, task=replace(t, state=T_PENDING, resumes={id},
                                          retry_at=now + cfg.retry_timeout))
            c = c.merge(Commands([ao], execute(ao)))
        elif t.state in (T_PENDING, T_ACQUIRED, T_HALTED) and id not in t.resumes:
            c = c.merge(Commands([replace(ao, task=replace(t, resumes=t.resumes | {id}))]))

    record = o.promise.to_record(id)
    return c.merge(Commands(send=[Send(address, Unblock(record)) for address in o.promise.listeners]))


def check_invariants(doc: Document) -> str | None:
    ids = {o.id for o in doc.objects}
    if [o.id for o in doc.objects] != sorted(ids, key=dewey):
        return "objects are not sorted by dewey id"
    for o in doc.objects:
        p, t = o.promise, o.task
        if p.state == PENDING and p.settled_at is not None:
            return f"promise {o.id}: pending but has settled_at"
        if p.state != PENDING and p.settled_at is None:
            return f"promise {o.id}: settled but has no settled_at"
        for awaiter in p.callbacks:
            if awaiter not in ids:
                return f"promise {o.id}: callback awaiter {awaiter} missing"
        if len(set(p.listeners)) != len(p.listeners):
            return f"promise {o.id}: duplicate listener"
        if t is None:
            continue
        if t.state == T_PENDING and (t.retry_at is None or t.lease_at is not None):
            return f"task {o.id}: pending without exactly a retry timer"
        if t.state == T_ACQUIRED and (t.lease_at is None or t.retry_at is not None):
            return f"task {o.id}: acquired without exactly a lease timer"
        if t.state in (T_SUSPENDED, T_HALTED, T_FULFILLED) and (t.retry_at or t.lease_at) is not None:
            return f"task {o.id}: {t.state} with an armed timer"
        if p.state != PENDING and t.state != T_FULFILLED:
            return f"task {o.id}: promise settled but task is {t.state}"
        if p.state == PENDING and t.state == T_FULFILLED:
            return f"task {o.id}: fulfilled but promise is pending"
        for awaited in t.resumes:
            if awaited not in ids:
                return f"task {o.id}: resume {awaited} missing"
    if doc.timer_at != min_deadline(doc):
        return f"timer_at {doc.timer_at} != min_deadline {min_deadline(doc)}"
    return None
