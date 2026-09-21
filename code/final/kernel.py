"""The kernel: the protocol's state machine, as a pure function.

    handle_external(doc, req, now, cfg) -> (effects, reply)   one protocol request
    handle_internal(doc, now, cfg)      -> effects            the sweep of everything due

Neither reads a clock, generates an id, or does I/O. Everything a decision
implies comes back as an Effect for the shell to perform, in order: arm the new
timer, commit the document, clear the old timer, send. The transition *is*
`apply_effects(handle_external(doc, req, now, cfg)[0])`; there is no second
updater.

Transcribed from resonatehq/resonate, crates/resonate-server-blob/src/kernel.
"""

from __future__ import annotations

import copy
import re
from bisect import insort
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

# ---------------------------------------------------------------------------
# States and tags
# ---------------------------------------------------------------------------

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

TAG_TARGET = "resonate:target"  # makes a promise dispatchable: where its task goes
TAG_TIMER = "resonate:timer"  # an expiring promise resolves instead of rejecting
TAG_DELAY = "resonate:delay"  # defers a new task's first dispatch to an instant
TAG_BRANCH = "resonate:branch"  # groups promises preloaded together
TAG_SCOPE = "resonate:scope"
TAG_EXTERNAL = "resonate:external"


@dataclass(frozen=True)
class KernelCfg:
    retry_timeout: int = 30_000  # how long a pending task waits before re-dispatch
    preload_limit: int = 10  # how many branch siblings a task response carries


# ---------------------------------------------------------------------------
# The document
# ---------------------------------------------------------------------------


@dataclass
class Value:
    headers: dict[str, str] | None = None
    data: str | None = None

    def to_json(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.headers is not None:
            out["headers"] = dict(self.headers)
        if self.data is not None:
            out["data"] = self.data
        return out


@dataclass
class Promise:
    state: str = PENDING
    param: Value = field(default_factory=Value)
    value: Value = field(default_factory=Value)
    tags: dict[str, str] = field(default_factory=dict)
    timeout_at: int = 0
    created_at: int = 0
    settled_at: int | None = None
    callbacks: list[str] = field(default_factory=list)  # awaiter ids, registration order
    listeners: list[str] = field(default_factory=list)  # addresses, registration order, unique

    def target(self) -> str | None:
        return self.tags.get(TAG_TARGET)

    def is_external(self) -> bool:
        """Awaitable and armed: scope global, external, targeted, or a timer."""
        return (
            self.tags.get(TAG_SCOPE) == "global"
            or self.tags.get(TAG_EXTERNAL) == "true"
            or TAG_TARGET in self.tags
            or self.tags.get(TAG_TIMER) == "true"
        )

    def timeout_state(self) -> str:
        return RESOLVED if self.tags.get(TAG_TIMER) == "true" else REJECTED_TIMEDOUT

    def timeout_armed(self) -> bool:
        """Only a pending promise with a target has a deadline the sweep fires;
        an undispatched promise expires lazily, when someone reads it."""
        return self.state == PENDING and self.target() is not None

    def to_record(self, id: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": id,
            "state": self.state,
            "param": self.param.to_json(),
            "value": self.value.to_json(),
            "tags": dict(self.tags),
            "timeoutAt": self.timeout_at,
            "createdAt": self.created_at,
        }
        if self.settled_at is not None:
            out["settledAt"] = self.settled_at
        return out


@dataclass
class Task:
    state: str = T_PENDING
    version: int = 0  # the fencing token every task operation is checked against
    pid: str | None = None
    ttl: int | None = None
    resumes: set[str] = field(default_factory=set)  # awaited ids settled but not yet observed
    retry_at: int | None = None  # armed while pending
    lease_at: int | None = None  # armed while acquired

    def disarm(self) -> None:
        self.retry_at = None
        self.lease_at = None

    def arm_retry(self, at: int) -> None:
        self.retry_at = at
        self.lease_at = None

    def arm_lease(self, at: int) -> None:
        self.lease_at = at
        self.retry_at = None

    def to_record(self, id: str) -> dict[str, Any]:
        out: dict[str, Any] = {
            "id": id,
            "state": self.state,
            "version": self.version,
            "resumes": len(self.resumes),
        }
        if self.ttl is not None:
            out["ttl"] = self.ttl
        if self.pid is not None:
            out["pid"] = self.pid
        return out


@dataclass
class Object:
    """One promise, and its task if it has a target. A task's id is its promise's id."""

    id: str
    promise: Promise
    task: Task | None = None


def dewey(id: str) -> tuple[tuple[int, Any], ...]:
    """The sort key: ids compare segment by segment, numbers as numbers, so
    `o:2` sorts before `o:10` and a call's children sort under it."""
    return tuple(
        (0, int(seg)) if seg.isdigit() else (1, seg) for seg in re.split(r"[:.]", id)
    )


@dataclass
class Document:
    """One origin's entire state. `objects` is kept sorted by `dewey(id)`."""

    objects: list[Object] = field(default_factory=list)
    clock: int = 0  # latest `now` observed; the shell's, diagnostic
    gen: int = 0  # bumped by the shell per committed write; diagnostic
    timer_at: int | None = None  # the one deadline armed for this origin

    def get(self, id: str) -> Object | None:
        for o in self.objects:
            if o.id == id:
                return o
        return None

    def insert(self, o: Object) -> Object:
        insort(self.objects, o, key=lambda x: dewey(x.id))
        return o


def min_deadline(doc: Document) -> int | None:
    """The earliest deadline the document has armed: promise deadlines, task
    retries, and task leases. The shell keeps one timer per origin, here."""
    deadlines = []
    for o in doc.objects:
        if o.promise.timeout_armed():
            deadlines.append(o.promise.timeout_at)
        if o.task is not None:
            deadlines += [at for at in (o.task.retry_at, o.task.lease_at) if at is not None]
    return min(deadlines) if deadlines else None


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PromiseCreate:
    id: str
    timeout_at: int
    param: Value = field(default_factory=Value)
    tags: dict[str, str] = field(default_factory=dict)


Req = PromiseCreate  # widened as each request type lands

# ---------------------------------------------------------------------------
# Effects, messages, replies
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Execute:
    task_id: str
    version: int


@dataclass(frozen=True)
class Unblock:
    promise: dict[str, Any]


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


@dataclass(frozen=True)
class Reply:
    status: int
    data: Any

    @staticmethod
    def ok(data: Any) -> Reply:
        return Reply(200, data)

    @staticmethod
    def err(status: int, message: str) -> Reply:
        return Reply(status, message)


def is_valid_address(address: str) -> bool:
    """Any URI with a scheme. Deliberately shallow: what follows the scheme is
    the transport's business, and validation must be the same on every
    deployment."""
    if re.match(r"^[A-Za-z][A-Za-z0-9+.\-]*:", address) is None:
        return False
    parts = urlsplit(address)
    if parts.scheme in ("http", "https", "ws", "wss", "ftp") and not parts.netloc:
        return False
    return True


# ---------------------------------------------------------------------------
# A decision in progress
# ---------------------------------------------------------------------------


@dataclass
class Tx:
    doc: Document  # a copy of the input; mutated freely
    sends: list[Send] = field(default_factory=list)


def send_execute(tx: Tx, task_id: str, version: int) -> None:
    """Queue a dispatch. A promise with no target has nowhere to send."""
    o = tx.doc.get(task_id)
    address = o.promise.target() if o is not None else None
    if address is not None:
        tx.sends.append(Send(address, Execute(task_id, version)))


# ---------------------------------------------------------------------------
# The two entry points
# ---------------------------------------------------------------------------


def handle_external(doc: Document, req: Req, now: int, cfg: KernelCfg) -> tuple[list[Effect], Reply]:
    tx = Tx(doc=copy.deepcopy(doc))
    match req:
        case PromiseCreate():
            reply = promise_create(tx, req, now, cfg)
        case _:
            raise NotImplementedError(type(req).__name__)

    # Linearize the outcome in the order the shell must perform it: arm the new
    # timer, commit the document, clear the old timer, send.
    old, new = doc.timer_at, min_deadline(tx.doc)
    tx.doc.timer_at = new
    fx: list[Effect] = []
    if old != new and new is not None:
        fx.append(SetTimeout(new))
    fx.append(SetDocument(tx.doc))
    if old != new and old is not None:
        fx.append(DelTimeout(old))
    fx.extend(tx.sends)
    return fx, reply


# ---------------------------------------------------------------------------
# Promise operations
# ---------------------------------------------------------------------------


def promise_create(tx: Tx, r: PromiseCreate, now: int, cfg: KernelCfg) -> Reply:
    address = r.tags.get(TAG_TARGET)
    if address is not None and not is_valid_address(address):
        return Reply.err(400, "Invalid resonate:target address")
    try_timeout(tx, [r.id], now, cfg)
    o = tx.doc.get(r.id)
    if o is not None:
        # Create is idempotent on id alone: the stored promise wins.
        return Reply.ok({"promise": o.promise.to_record(r.id)})

    o = insert_promise(tx, r.id, r, now)
    record = o.promise.to_record(r.id)
    if o.promise.target() is None:
        # No target means no task and no armed deadline: such a promise only
        # ever expires lazily, when someone reads it.
        return Reply.ok({"promise": record})

    o.task = Task(state=T_FULFILLED, version=0)
    if o.promise.state != PENDING:
        # Born settled, so its task is born done.
        return Reply.ok({"promise": record})
    o.task.state = T_PENDING
    delay = r.tags.get(TAG_DELAY)
    delay_at = int(delay) if delay is not None and delay.lstrip("-").isdigit() else None
    if delay_at is not None and now < delay_at:
        # An absolute instant before which the task must not be dispatched:
        # arm the retry timer there and send nothing.
        o.task.arm_retry(delay_at)
    else:
        o.task.arm_retry(o.promise.created_at + cfg.retry_timeout)
        send_execute(tx, r.id, 0)
    return Reply.ok({"promise": record})


# ---------------------------------------------------------------------------
# Shared state transitions
# ---------------------------------------------------------------------------


def insert_promise(tx: Tx, id: str, r: PromiseCreate, now: int) -> Object:
    """Insert the promise alone, with no task and no dispatch.

    A promise created past its own deadline is born settled, resolved if it is
    a timer and timed out otherwise, with `created_at` and `settled_at` both
    stamped at the deadline rather than at `now`."""
    already_timedout = now >= r.timeout_at
    p = Promise(
        state=PENDING,
        param=copy.deepcopy(r.param),
        value=Value(),
        tags=dict(r.tags),
        timeout_at=r.timeout_at,
        created_at=r.timeout_at if already_timedout else now,
    )
    if already_timedout:
        p.state = p.timeout_state()
        p.settled_at = r.timeout_at
    return tx.doc.insert(Object(id=id, promise=p))


def try_timeout(tx: Tx, ids: list[str], now: int, cfg: KernelCfg) -> None:
    """Settle every named promise whose deadline has passed. `settled_at` is
    the deadline, not `now`, so the record is identical whenever the expiry is
    noticed."""
    for id in ids:
        o = tx.doc.get(id)
        if o is None or o.promise.state != PENDING or now < o.promise.timeout_at:
            continue
        o.promise.state = o.promise.timeout_state()
        o.promise.settled_at = o.promise.timeout_at
        trigger_settlement(tx, id, now, cfg)


def trigger_settlement(tx: Tx, id: str, now: int, cfg: KernelCfg) -> None:
    """The settlement chain, in one pass and in this order: fulfil the
    promise's own task, wake its awaiters, notify its listeners."""
    o = tx.doc.get(id)
    assert o is not None

    # settlement_enqueued: the settled promise's own task is done, and its
    # registrations against other promises are dropped.
    if o.task is not None and o.task.state != T_FULFILLED:
        o.task.state = T_FULFILLED
        o.task.pid = None
        o.task.ttl = None
        o.task.resumes.clear()
        o.task.disarm()
        for other in tx.doc.objects:
            other.promise.callbacks = [a for a in other.promise.callbacks if a != id]

    # resumption_enqueued: every awaiter registered against `id` observes the
    # settlement, in registration order. A settlement fanning out marks every
    # callback ready whatever state the awaiter's task is in, so a halted
    # awaiter buffers the resume and sees it when it continues.
    awaiters, o.promise.callbacks = o.promise.callbacks, []
    for awaiter in awaiters:
        ao = tx.doc.get(awaiter)
        if ao is None or ao.task is None:
            continue
        if ao.promise.state != PENDING or now >= ao.promise.timeout_at:
            # The awaiter is itself settled or past its deadline; a sweep will
            # fulfil it rather than resume it.
            continue
        t = ao.task
        if t.state == T_SUSPENDED:
            t.state = T_PENDING
            t.resumes = {id}
            t.arm_retry(now + cfg.retry_timeout)
            send_execute(tx, awaiter, t.version)
        elif t.state in (T_PENDING, T_ACQUIRED, T_HALTED):
            t.resumes.add(id)

    # listener_unblocked: hand the settled promise to everyone listening, then
    # forget them.
    listeners, o.promise.listeners = o.promise.listeners, []
    record = o.promise.to_record(id)
    for address in listeners:
        tx.sends.append(Send(address, Unblock(record)))


# ---------------------------------------------------------------------------
# Invariants
# ---------------------------------------------------------------------------


def check_invariants(doc: Document) -> str | None:
    """Structural invariants every committed document satisfies. Returns the
    first violation, or None."""
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
        # one_timer: a task's timeout is one deadline of one kind.
        if t.state == T_PENDING and (t.retry_at is None or t.lease_at is not None):
            return f"task {o.id}: pending without exactly a retry timer"
        if t.state == T_ACQUIRED and (t.lease_at is None or t.retry_at is not None):
            return f"task {o.id}: acquired without exactly a lease timer"
        if t.state in (T_SUSPENDED, T_HALTED, T_FULFILLED) and (t.retry_at or t.lease_at) is not None:
            return f"task {o.id}: {t.state} with an armed timer"
        # Settlement is terminal for the task that owns the promise.
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
