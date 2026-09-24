"""The protocol: everything that can be said, and its form on a wire.

Fifteen requests a client may send, the reply that comes back, the two
messages a queue carries, and the parsing that turns an envelope into one
of them. Nothing here decides anything -- `kernel.py` does that, and what
it decides *about* is this.

These used to be in two files. The types were in `kernel.py`, because the
kernel matches on them, and the parsing was in `wire.py`, because parsing
felt like an edge. That split has a good general argument behind it -- keep
a domain model away from its serialisation, so one model can have several
wire formats -- and this system does not collect on it: there is exactly
one wire format for a request, and the only other encoder in the project,
`codec.py`, serialises documents rather than requests. What the split cost
instead was an invariant nobody enforced. A field added to `PromiseCreate`
has to be learned by `_create`, and with the two in different files nothing
says so; together, they are eleven lines apart.

## It parses dicts, not bytes

`parse_request` takes an envelope that is already a `dict`. Whoever turned
the body into one -- Flask, a test, a queue -- did that, and this module
imports nothing to do it. That is what lets `kernel.py` import this file
without importing anything of the outside world: the kernel still reads no
clock, generates no id and calls nothing, and its alphabet now lives beside
the grammar for writing it down.

## Two seams, both JSON

Requests in, messages out, and both are the point at which this stops being
Python objects and becomes something another implementation could sit on
the other side of. Parsing is where a malformed request becomes a 400 and
never reaches the state machine; the *semantic* doors, the ones the
specification's catalogue shadows, are the kernel's own and are not here.

The simulated queue carries the same JSON the real one does, so the thing
the tests drive and the thing production drives differ in where the bytes
go and nowhere else.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


# ---------------------------------------------------------------------------
# What a payload is
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
# What comes back, and what a queue carries
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Execute:
    task_id: str
    version: int


@dataclass(frozen=True)
class Unblock:
    promise: dict[str, Any]


#: The URL a deadline is delivered to. Everything after it is the origin to
#: sweep, exactly as a Cloud Run route would read it.
SWEEP = "sweep/"


@dataclass(frozen=True)
class Timeout:
    """The internal message: a deadline for this origin came due. Not a
    protocol request — no client can send one — but a transition on the
    origin's document all the same."""

    origin: str


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

# ---------------------------------------------------------------------------
# The wire
# ---------------------------------------------------------------------------

class Invalid(Exception):
    """The envelope is not a request. A 400, and nothing is read or written."""


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------


def encode_message(msg: Execute | Unblock) -> dict:
    if isinstance(msg, Execute):
        return {"kind": "execute", "task": {"id": msg.task_id, "version": msg.version}}
    return {"kind": "unblock", "promise": msg.promise}


def decode_message(body: dict) -> Execute | Unblock:
    match body.get("kind"):
        case "execute":
            task = body["task"]
            return Execute(task["id"], task["version"])
        case "unblock":
            return Unblock(body["promise"])
        case other:
            raise Invalid(f"unknown message kind {other!r}")


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


def _value(d: Any) -> Value:
    d = d or {}
    if not isinstance(d, dict):
        raise Invalid("a payload is an object")
    return Value(headers=d.get("headers"), data=d.get("data"))


def _create(d: dict) -> PromiseCreate:
    return PromiseCreate(_str(d, "id"), _int(d, "timeoutAt"),
                         _value(d.get("param")), dict(d.get("tags") or {}))


def _settle(d: dict) -> PromiseSettle:
    return PromiseSettle(_str(d, "id"), _str(d, "state"), _value(d.get("value")))


def _str(d: dict, k: str) -> str:
    v = d.get(k)
    if not isinstance(v, str) or not v:
        raise Invalid(f"{k} must be a non-empty string")
    return v


def _int(d: dict, k: str) -> int:
    v = d.get(k)
    if not isinstance(v, int) or isinstance(v, bool):
        raise Invalid(f"{k} must be an integer")
    return v


def _action(d: dict) -> dict:
    """The protocol nests a request inside `action`, with its own kind and
    head. Only the data is ours to read."""
    a = d.get("action")
    if not isinstance(a, dict) or not isinstance(a.get("data"), dict):
        raise Invalid("action must carry a data object")
    return a


def parse_request(envelope: dict) -> Req:
    """One envelope to one typed request, or `Invalid`."""
    if not isinstance(envelope, dict):
        raise Invalid("an envelope is an object")
    kind, d = envelope.get("kind"), envelope.get("data")
    if not isinstance(d, dict):
        raise Invalid("data must be an object")
    try:
        match kind:
            case "promise.get":
                return PromiseGet(_str(d, "id"))
            case "promise.create":
                return _create(d)
            case "promise.settle":
                return _settle(d)
            case "promise.register_callback":
                return PromiseRegisterCallback(_str(d, "awaited"), _str(d, "awaiter"))
            case "promise.register_listener":
                return PromiseRegisterListener(_str(d, "awaited"), _str(d, "address"))
            case "task.get":
                return TaskGet(_str(d, "id"))
            case "task.create":
                return TaskCreate(_str(d, "pid"), _int(d, "ttl"), _create(_action(d)["data"]))
            case "task.acquire":
                return TaskAcquire(_str(d, "id"), _int(d, "version"), _str(d, "pid"), _int(d, "ttl"))
            case "task.release":
                return TaskRelease(_str(d, "id"), _int(d, "version"))
            case "task.fulfill":
                return TaskFulfill(_str(d, "id"), _int(d, "version"), _settle(_action(d)["data"]))
            case "task.suspend":
                actions = d.get("actions")
                if not isinstance(actions, list) or not actions:
                    raise Invalid("actions must be a non-empty array")
                return TaskSuspend(_str(d, "id"), _int(d, "version"),
                                   tuple(_str(a["data"], "awaited") for a in actions))
            case "task.fence":
                action = _action(d)
                inner = action["data"]
                nested = _create(inner) if action.get("kind") == "promise.create" else _settle(inner)
                return TaskFence(_str(d, "id"), _int(d, "version"),
                                 str(envelope.get("head", {}).get("corrId", "")), nested)
            case "task.heartbeat":
                rows = d.get("tasks")
                if not isinstance(rows, list) or not rows:
                    raise Invalid("tasks must be a non-empty array")
                return TaskHeartbeat(_str(d, "pid"),
                                     tuple((_str(t, "id"), _int(t, "version")) for t in rows))
            case "task.halt":
                return TaskHalt(_str(d, "id"))
            case "task.continue":
                return TaskContinue(_str(d, "id"))
            case other:
                raise Invalid(f"unknown request kind {other!r}")
    except Invalid:
        raise
    except (KeyError, TypeError, AttributeError) as e:
        raise Invalid(f"{kind}: {e}") from None


def encode_reply(reply: Reply) -> dict:
    return {"head": {"status": reply.status}, "data": reply.data}
