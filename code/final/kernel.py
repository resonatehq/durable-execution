"""The kernel: the protocol's state machine, as a pure function.

    handle_internal(doc, now, cfg)      -> effects            the sweep: everything whose deadline has passed
    handle_external(doc, req, now, cfg) -> (effects, reply)   the sweep, then one protocol request

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
    #: What the shell called the timer it armed at `timer_at`. The kernel
    #: carries it and never reads it: the name comes back from whatever
    #: armed the deadline, which is I/O, and a writer must be able to remove
    #: the object its own predecessor wrote rather than one by coordinates.
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


PROTOCOL_VERSION = "2026-04-01"


@dataclass(frozen=True)
class PromiseGet:
    id: str


@dataclass(frozen=True)
class PromiseCreate:
    id: str
    timeout_at: int
    param: Value = field(default_factory=Value)
    tags: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class PromiseSettle:
    id: str
    state: str  # RESOLVED | REJECTED | REJECTED_CANCELED
    value: Value = field(default_factory=Value)


@dataclass(frozen=True)
class PromiseRegisterCallback:
    awaited: str
    awaiter: str  # same origin as `awaited`, and not equal to it


@dataclass(frozen=True)
class PromiseRegisterListener:
    awaited: str
    address: str


@dataclass(frozen=True)
class TaskGet:
    id: str


@dataclass(frozen=True)
class TaskCreate:
    pid: str
    ttl: int
    action: PromiseCreate  # must carry resonate:target, must not carry resonate:delay


@dataclass(frozen=True)
class TaskAcquire:
    id: str
    version: int
    pid: str
    ttl: int


@dataclass(frozen=True)
class TaskRelease:
    id: str
    version: int


@dataclass(frozen=True)
class TaskFulfill:
    id: str
    version: int
    action: PromiseSettle  # action.id == id


@dataclass(frozen=True)
class TaskSuspend:
    id: str
    version: int
    awaited: tuple[str, ...]  # unique, same origin, none equal to `id`; on the wire, one register_callback action each


@dataclass(frozen=True)
class TaskFence:
    id: str
    version: int
    corr_id: str  # the envelope's, echoed in the nested response head
    action: PromiseCreate | PromiseSettle


@dataclass(frozen=True)
class TaskHeartbeat:
    pid: str
    tasks: tuple[tuple[str, int], ...]  # (id, version), all one origin


@dataclass(frozen=True)
class TaskHalt:
    id: str


@dataclass(frozen=True)
class TaskContinue:
    id: str


Req = (
    PromiseGet | PromiseCreate | PromiseSettle | PromiseRegisterCallback | PromiseRegisterListener
    | TaskGet | TaskCreate | TaskAcquire | TaskRelease | TaskFulfill | TaskSuspend | TaskFence
    | TaskHeartbeat | TaskHalt | TaskContinue
)

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


SETTLE_STATES = (RESOLVED, REJECTED, REJECTED_CANCELED)  # rejected_timedout is server-owned


def origin_of(id: str) -> str:
    """Everything before the first ':'. The routing key, and the reason one
    document can answer any single operation."""
    return id.split(":", 1)[0]


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


def handle_internal(doc: Document, now: int, cfg: KernelCfg) -> list[Effect]:
    """Sweep every deadline at or before `now`, in one pass.

    Four phases, each reading the state the previous one left: settle every
    expired promise, run their settlement chains, re-dispatch pending tasks
    past their retry deadline, reclaim acquired tasks past their lease. A
    promise without a target expires here like any other; its chain simply
    has nobody to fulfil, wake, or notify, so it sends nothing."""
    tx = Tx(doc=copy.deepcopy(doc))

    # Phase 1: settle first, all of them, so an awaiter that is itself expiring
    # is already settled when its awaited promise fans out, and is skipped
    # rather than resumed. `settled_at` is the deadline, not `now`.
    expired = [o for o in tx.doc.objects if o.promise.state == PENDING and now >= o.promise.timeout_at]
    for o in expired:
        o.promise.state = o.promise.timeout_state()
        o.promise.settled_at = o.promise.timeout_at
    # Phase 2: the chains, in id order.
    for o in expired:
        trigger_settlement(tx, o.id, now, cfg)

    # Phase 3: re-dispatch pending tasks whose retry deadline has passed. Read
    # after phase 2: a task the settlement just fulfilled has no timer left.
    for o in tx.doc.objects:
        t = o.task
        if t is not None and t.state == T_PENDING and t.retry_at is not None and t.retry_at <= now:
            t.arm_retry(now + cfg.retry_timeout)
            send_execute(tx, o.id, t.version)

    # Phase 4: expire leases. The holder is presumed gone, so the task goes
    # back to pending at the *same* version and is re-dispatched; whoever picks
    # it up bumps the version and fences the old holder out.
    for o in tx.doc.objects:
        t = o.task
        if t is not None and t.state == T_ACQUIRED and t.lease_at is not None and t.lease_at <= now:
            t.state = T_PENDING
            t.pid = None
            t.ttl = None
            t.arm_retry(now + cfg.retry_timeout)
            send_execute(tx, o.id, t.version)

    # Linearize in the order the shell performs it: arm the new timer, commit
    # the document, clear the old timer, send.
    old, new = doc.timer_at, min_deadline(tx.doc)
    tx.doc.timer_at = new
    fx: list[Effect] = []
    if old != new and new is not None:
        fx.append(SetTimeout(new))
    fx.append(SetDocument(tx.doc))
    if old != new and old is not None:
        fx.append(DelTimeout(old))
    fx.extend(tx.sends)
    return fx


def handle_external(doc: Document, req: Req, now: int, cfg: KernelCfg) -> tuple[list[Effect], Reply]:
    """Sweep, then decide one request against the swept document, then merge.

    The sweep's own timer effects are discarded: the merged timer transition
    is from the document that came in to the document that goes out, so an
    intermediate deadline the request then moved is never armed. Sends keep
    their order, the sweep's first, then the request's, except that a sweep
    dispatch the request overtook is dropped."""
    swept = handle_internal(doc, now, cfg)
    tx = Tx(doc=next(e.doc for e in swept if isinstance(e, SetDocument)))
    match req:
        case PromiseGet():
            reply = promise_get(tx, req)
        case PromiseCreate():
            reply = promise_create(tx, req, now, cfg)
        case PromiseSettle():
            reply = promise_settle(tx, req, now, cfg)
        case PromiseRegisterCallback():
            reply = promise_register_callback(tx, req, now, cfg)
        case PromiseRegisterListener():
            reply = promise_register_listener(tx, req)
        case TaskGet():
            reply = task_get(tx, req)
        case TaskCreate():
            reply = task_create(tx, req, now, cfg)
        case TaskAcquire():
            reply = task_acquire(tx, req, now, cfg)
        case TaskRelease():
            reply = task_release(tx, req, now, cfg)
        case TaskFulfill():
            reply = task_fulfill(tx, req, now, cfg)
        case TaskSuspend():
            reply = task_suspend(tx, req, cfg)
        case TaskFence():
            reply = task_fence(tx, req, now, cfg)
        case TaskHeartbeat():
            reply = task_heartbeat(tx, req, now)
        case TaskHalt():
            reply = task_halt(tx, req)
        case TaskContinue():
            reply = task_continue(tx, req, now, cfg)

    old, new = doc.timer_at, min_deadline(tx.doc)
    tx.doc.timer_at = new
    fx: list[Effect] = []
    if old != new and new is not None:
        fx.append(SetTimeout(new))
    fx.append(SetDocument(tx.doc))
    if old != new and old is not None:
        fx.append(DelTimeout(old))
    for e in swept:
        if not isinstance(e, Send):
            continue
        if isinstance(e.msg, Execute):
            # A dispatch the request overtook is not sent: the task it names
            # is no longer pending at that version (the request settled its
            # promise, or acquired it), so the message could only be refused.
            o = tx.doc.get(e.msg.task_id)
            if o is None or o.task is None or o.task.state != T_PENDING or o.task.version != e.msg.version:
                continue
        fx.append(e)
    fx.extend(tx.sends)
    return fx, reply


# ---------------------------------------------------------------------------
# Promise operations
# ---------------------------------------------------------------------------


def promise_get(tx: Tx, r: PromiseGet) -> Reply:
    o = tx.doc.get(r.id)
    if o is None:
        return Reply.err(404, "Promise not found")
    return Reply.ok({"promise": o.promise.to_record(r.id)})


def promise_create(tx: Tx, r: PromiseCreate, now: int, cfg: KernelCfg) -> Reply:
    address = r.tags.get(TAG_TARGET)
    if address is not None and not is_valid_address(address):
        return Reply.err(400, "Invalid resonate:target address")
    if r.tags.get(TAG_TIMER) == "true" and address is not None:
        return Reply.err(400, "A timer promise must not have a resonate:target tag")
    delay = r.tags.get(TAG_DELAY)
    if delay is not None:
        if not delay.isdigit():
            return Reply.err(400, "resonate:delay must be a non-negative integer")
        if int(delay) >= r.timeout_at:
            return Reply.err(400, "resonate:delay must be less than timeoutAt")
        if address is None:
            return Reply.err(400, "resonate:delay requires a resonate:target tag")
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
    if delay is not None and now < int(delay):
        # An absolute instant before which the task must not be dispatched:
        # arm the retry timer there and send nothing.
        o.task.arm_retry(int(delay))
    else:
        o.task.arm_retry(o.promise.created_at + cfg.retry_timeout)
        send_execute(tx, r.id, 0)
    return Reply.ok({"promise": record})


def promise_settle(tx: Tx, r: PromiseSettle, now: int, cfg: KernelCfg) -> Reply:
    if r.state not in SETTLE_STATES:
        return Reply.err(400, "Invalid settle state")
    o = tx.doc.get(r.id)
    if o is None:
        return Reply.err(404, "Promise not found")
    if o.promise.state != PENDING:
        # Settlement is terminal: a second settle reports the first one.
        return Reply.ok({"promise": o.promise.to_record(r.id)})
    return Reply.ok({"promise": settle(tx, r.id, r.state, r.value, now, cfg)})


def promise_register_callback(tx: Tx, r: PromiseRegisterCallback, now: int, cfg: KernelCfg) -> Reply:
    if r.awaited == r.awaiter:
        return Reply.err(400, "Awaited and awaiter must be different promises")
    if origin_of(r.awaited) != origin_of(r.awaiter):
        return Reply.err(400, "Awaiter and awaited must belong to the same origin")
    awaited = tx.doc.get(r.awaited)
    if awaited is None:
        return Reply.err(404, "Awaited promise not found")
    awaiter = tx.doc.get(r.awaiter)
    if awaiter is None:
        return Reply.err(422, "Awaiter promise not found")
    if awaiter.promise.target() is None:
        return Reply.err(422, "Awaiter promise has no resonate:target tag")
    if not awaited.promise.is_external():
        return Reply.err(422, "Awaited promise is not awaitable")
    record = awaited.promise.to_record(r.awaited)

    # Registering against a promise that has already settled does nothing: the
    # caller learns the outcome from the record it gets back. It is not a wake,
    # and the reason is an invariant rather than a preference. A task suspends
    # only on promises that are pending at the time (`task.suspend` answers 300
    # otherwise), and a settlement drains every callback it holds, so a
    # suspended task always has a rung on a pending promise. Waking one here
    # would be a transition out of `suspended` that consumed no callback, which
    # `consistent_wake_follows_callback_consumption` forbids — and the state it
    # defends against is one the catalogue says is unreachable.
    #
    # (The Rust kernel does wake, following its SQL backend, where the
    # registration inserts a *ready callback* that a later step drains. The
    # coalesced machine has no later step, and the specification's
    # `promiseRegisterCallback` accordingly does nothing here:
    # `spec/02-abstract/external.lean:78-83`. Found by the Hypothesis machine.)
    if awaited.promise.state == PENDING and awaiter.promise.state == PENDING:
        # Registration order is protocol-visible; the pair is unique.
        if r.awaiter not in awaited.promise.callbacks:
            awaited.promise.callbacks.append(r.awaiter)
    return Reply.ok({"promise": record})


def promise_register_listener(tx: Tx, r: PromiseRegisterListener) -> Reply:
    if not is_valid_address(r.address):
        return Reply.err(400, "Invalid listener address")
    o = tx.doc.get(r.awaited)
    if o is None:
        return Reply.err(404, "Awaited promise not found")
    if not o.promise.is_external():
        # A listener is an obligation, and the server owes an observation only
        # where someone can be blocked.
        return Reply.err(422, "Awaited promise is not awaitable")
    if o.promise.state == PENDING and r.address not in o.promise.listeners:
        o.promise.listeners.append(r.address)
    return Reply.ok({"promise": o.promise.to_record(r.awaited)})


# ---------------------------------------------------------------------------
# Task operations
# ---------------------------------------------------------------------------


def task_get(tx: Tx, r: TaskGet) -> Reply:
    o = tx.doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found")
    return Reply.ok({"task": o.task.to_record(r.id)})


def task_create(tx: Tx, r: TaskCreate, now: int, cfg: KernelCfg) -> Reply:
    """A worker claiming work by describing it: creates the promise if absent
    and hands back a task already acquired by the caller. No dispatch, because
    the caller *is* the worker."""
    a = r.action
    address = a.tags.get(TAG_TARGET)
    if address is None:
        return Reply.err(400, "Action must have a resonate:target tag")
    if not is_valid_address(address):
        return Reply.err(400, "Invalid resonate:target address")
    if a.tags.get(TAG_TIMER) == "true":
        return Reply.err(400, "A timer promise must not have a resonate:target tag")
    if TAG_DELAY in a.tags:
        return Reply.err(400, "Action must not have a resonate:delay tag")
    if r.ttl < 1:
        return Reply.err(400, "TTL must be a positive integer")
    o = tx.doc.get(a.id)
    if o is not None and o.task is not None:
        t = o.task
        if t.state == T_PENDING:
            # The version bump is the fence: every later write by a previous
            # holder fails its version check.
            t.state = T_ACQUIRED
            t.version += 1
            t.pid = r.pid
            t.ttl = r.ttl
            t.resumes.clear()
            t.arm_lease(now + r.ttl)
            return Reply.ok({
                "task": t.to_record(a.id),
                "promise": o.promise.to_record(a.id),
                "preload": preload(tx.doc, a.id, cfg),
            })
        if t.state == T_FULFILLED:
            # The work is already done. No preload on this branch.
            return Reply.ok({
                "task": t.to_record(a.id),
                "promise": o.promise.to_record(a.id),
                "preload": [],
            })
        return Reply.err(409, "Already exists")
    if o is not None:
        # A promise without a task is a promise nobody can be dispatched for.
        return Reply.err(422, "The promise does not have a resonate:target tag")

    # Neither exists: create both. The task is born acquired by the caller,
    # never pending, so no dispatch is emitted.
    o = insert_promise(tx, a.id, a, now)
    t = Task(state=T_FULFILLED, version=0)
    if o.promise.state == PENDING:
        t.state = T_ACQUIRED
        t.version = 1
        t.pid = r.pid
        t.ttl = r.ttl
        t.arm_lease(now + r.ttl)
    o.task = t
    return Reply.ok({
        "task": t.to_record(a.id),
        "promise": o.promise.to_record(a.id),
        "preload": preload(tx.doc, a.id, cfg),
    })


def task_acquire(tx: Tx, r: TaskAcquire, now: int, cfg: KernelCfg) -> Reply:
    if r.ttl < 1:
        return Reply.err(400, "TTL must be a positive integer")
    o = tx.doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found")
    t = o.task
    if t.state != T_PENDING:
        return Reply.err(409, "Task is not pending")
    if t.version != r.version:
        return Reply.err(409, "Version mismatch")
    # Claim: bump the version (the fence), take the lease, drop the resumes
    # the previous run buffered.
    t.state = T_ACQUIRED
    t.version += 1
    t.pid = r.pid
    t.ttl = r.ttl
    t.resumes.clear()
    t.arm_lease(now + r.ttl)
    return Reply.ok({
        "task": t.to_record(r.id),
        "promise": o.promise.to_record(r.id),
        "preload": preload(tx.doc, r.id, cfg),
    })


def task_release(tx: Tx, r: TaskRelease, now: int, cfg: KernelCfg) -> Reply:
    o = tx.doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found")
    t = o.task
    if t.state != T_ACQUIRED or t.version != r.version:
        return Reply.err(409, "Task version mismatch or invalid state")
    # Releasing hands the task back unclaimed at the *same* version; only a
    # claim bumps it, so the next worker acquires with the version it saw.
    t.state = T_PENDING
    t.pid = None
    t.ttl = None
    t.arm_retry(now + cfg.retry_timeout)
    send_execute(tx, r.id, t.version)
    return Reply.ok({})


def task_fulfill(tx: Tx, r: TaskFulfill, now: int, cfg: KernelCfg) -> Reply:
    if r.action.id != r.id:
        return Reply.err(400, "Action ID must match the task ID")
    if r.action.state not in SETTLE_STATES:
        return Reply.err(400, "Invalid settle state")
    o = tx.doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found")
    t = o.task
    if t.state != T_ACQUIRED or t.version != r.version:
        return Reply.err(409, "Task version mismatch or invalid state")
    po = tx.doc.get(r.action.id)
    if po is None:
        return Reply.err(404, "Promise not found")
    if po.promise.state != PENDING:
        # Unreachable while the invariants hold (an acquired task's promise is
        # pending), but the reference fulfils the task regardless.
        t.state = T_FULFILLED
        t.pid = None
        t.ttl = None
        t.resumes.clear()
        t.disarm()
        return Reply.ok({"promise": po.promise.to_record(r.action.id)})
    return Reply.ok({"promise": settle(tx, r.action.id, r.action.state, r.action.value, now, cfg)})


def task_suspend(tx: Tx, r: TaskSuspend, cfg: KernelCfg) -> Reply:
    """Park a task on a set of promises, unless one of them has already
    settled, in which case there is nothing to wait for and the caller is
    told to carry on (300)."""
    if not r.awaited:
        return Reply.err(400, "Actions array cannot be empty")
    if r.id in r.awaited:
        return Reply.err(400, "Action awaited promise must not equal the task ID")
    if len(set(r.awaited)) != len(r.awaited):
        return Reply.err(400, "Awaited promise IDs must be unique")
    if any(origin_of(a) != origin_of(r.id) for a in r.awaited):
        return Reply.err(400, "Awaited promise must belong to the same origin as the task")
    o = tx.doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found")
    t = o.task
    if t.state != T_ACQUIRED or t.version != r.version:
        return Reply.err(409, "Task is not acquired or version mismatch")
    for a in r.awaited:
        if tx.doc.get(a) is None:
            return Reply.err(422, "Awaited promise not found")
    for a in r.awaited:
        if not tx.doc.get(a).promise.is_external():
            return Reply.err(422, "Awaited promise is not awaitable")
    any_settled = any(tx.doc.get(a).promise.state != PENDING for a in r.awaited)
    # Either way the resumes buffered by a previous suspension are stale.
    t.resumes.clear()
    if any_settled:
        return Reply(300, {"preload": preload(tx.doc, r.id, cfg)})
    for a in r.awaited:
        p = tx.doc.get(a).promise
        if r.id not in p.callbacks:
            p.callbacks.append(r.id)
    t.state = T_SUSPENDED
    t.pid = None
    t.ttl = None
    t.disarm()
    return Reply.ok({})


def task_fence(tx: Tx, r: TaskFence, now: int, cfg: KernelCfg) -> Reply:
    """Run one promise operation under the task's version, so a worker that
    lost its lease cannot write. The action's outcome comes back as a nested
    response envelope."""
    if r.action.id == r.id:
        return Reply.err(400, "Action ID must not equal the task ID")
    o = tx.doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found")
    if o.task.state != T_ACQUIRED or o.task.version != r.version:
        return Reply.err(409, "Version mismatch")
    match r.action:
        case PromiseCreate():
            kind, nested = "promise.create", promise_create(tx, r.action, now, cfg)
            if nested.status == 400:
                return nested
        case PromiseSettle():
            kind, nested = "promise.settle", promise_settle(tx, r.action, now, cfg)
        case _:
            return Reply.err(400, "Invalid fence action kind")
    return Reply.ok({
        "action": {
            "kind": kind,
            "head": {"corrId": r.corr_id, "status": nested.status, "version": PROTOCOL_VERSION},
            "data": nested.data,
        },
        "preload": preload(tx.doc, r.id, cfg),
    })


def task_heartbeat(tx: Tx, r: TaskHeartbeat, now: int) -> Reply:
    """Extend the lease of every task in the batch the caller still owns, and
    silently ignore the rest: a liveness signal, not a query."""
    if len({origin_of(id) for id, _ in r.tasks}) > 1:
        return Reply.err(400, "All tasks must belong to the same origin")
    for id, version in r.tasks:
        o = tx.doc.get(id)
        t = o.task if o is not None else None
        if t is not None and t.state == T_ACQUIRED and t.version == version and t.pid == r.pid and t.ttl is not None:
            t.arm_lease(now + t.ttl)
    return Reply.ok({})


def task_halt(tx: Tx, r: TaskHalt) -> Reply:
    o = tx.doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found")
    t = o.task
    if t.state == T_FULFILLED:
        return Reply.err(409, "Task is fulfilled")
    if t.state == T_HALTED:
        return Reply.ok({})
    t.state = T_HALTED
    t.pid = None
    t.ttl = None
    t.disarm()
    return Reply.ok({})


def task_continue(tx: Tx, r: TaskContinue, now: int, cfg: KernelCfg) -> Reply:
    o = tx.doc.get(r.id)
    if o is None or o.task is None:
        return Reply.err(404, "Task not found")
    t = o.task
    if t.state != T_HALTED:
        return Reply.err(409, "Task is not halted")
    t.state = T_PENDING
    t.arm_retry(now + cfg.retry_timeout)
    send_execute(tx, r.id, t.version)
    return Reply.ok({})


# ---------------------------------------------------------------------------
# Shared state transitions
# ---------------------------------------------------------------------------


def settle(tx: Tx, id: str, state: str, value: Value, now: int, cfg: KernelCfg) -> dict[str, Any]:
    """Settle a pending promise and run its settlement chain. Returns the
    record as the caller must report it, captured before the chain runs."""
    o = tx.doc.get(id)
    assert o is not None and o.promise.state == PENDING
    o.promise.state = state
    o.promise.value = copy.deepcopy(value)
    o.promise.settled_at = now
    record = o.promise.to_record(id)
    trigger_settlement(tx, id, now, cfg)
    return record


def preload(doc: Document, id: str, cfg: KernelCfg) -> list[dict[str, Any]]:
    """The promises a worker is handed alongside a task: everything sharing
    the task promise's `resonate:branch`, itself excluded, in id order,
    truncated at `preload_limit`."""
    o = doc.get(id)
    branch = o.promise.tags.get(TAG_BRANCH) if o is not None else None
    if not branch:
        return []
    return [
        x.promise.to_record(x.id)
        for x in doc.objects
        if x.id != id and x.promise.tags.get(TAG_BRANCH) == branch
    ][: cfg.preload_limit]



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


def trigger_settlement(tx: Tx, id: str, now: int, cfg: KernelCfg) -> None:
    """The settlement chain, in one pass and in this order: fulfil the
    promise's own task, wake its awaiters, notify its listeners."""
    o = tx.doc.get(id)
    assert o is not None

    # settlement_enqueued: the settled promise's own task is done. Its
    # registrations against other, still pending promises stay where they
    # are: the specification only ever removes a callback when the awaited
    # promise settles, and the fan-out below skips a finished awaiter. (The
    # Rust kernel deletes them here, mirroring its SQL schema; the wire
    # cannot tell the difference, and the catalogue forbids the deletion.)
    if o.task is not None and o.task.state != T_FULFILLED:
        o.task.state = T_FULFILLED
        o.task.pid = None
        o.task.ttl = None
        o.task.resumes.clear()
        o.task.disarm()

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
    if len(ids) != len(doc.objects):
        return "duplicate object ids"
    # Non-decreasing keys, not a comparison against `sorted(ids)`: distinct
    # ids can share a key (`o:1`, `o:01`), and sorting a set puts those in
    # hash order, so the old form failed some runs of a correct document.
    keys = [dewey(o.id) for o in doc.objects]
    if any(b < a for a, b in zip(keys, keys[1:])):
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
        if t.state in (T_SUSPENDED, T_HALTED, T_FULFILLED) and (t.retry_at is not None or t.lease_at is not None):
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
