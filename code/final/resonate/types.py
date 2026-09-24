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
`codec.py`, serialises documents rather than requests.

## It parses dicts, not bytes

`parse_request` takes an envelope that is already a `dict`. Whoever turned
the body into one -- Flask, a test, a queue -- did that. Each request class
says how it is read: Pydantic validates the data against the class, with
camelCase names on the wire and the protocol's nested actions unwrapped by
the field that holds them.

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
from typing import Annotated, Any

from pydantic import (
    BeforeValidator, ConfigDict, Field, StrictInt, StringConstraints, TypeAdapter,
    ValidationError, with_config,
)
from pydantic.alias_generators import to_camel


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

#: Requests arrive as JSON in the protocol's camelCase (`timeoutAt`, `corrId`).
wire = with_config(ConfigDict(alias_generator=to_camel))

#: A non-empty string: every id, pid, address and state.
Id = Annotated[str, StringConstraints(strict=True, min_length=1)]


def _data(action: dict) -> dict:
    """A nested action is `{kind, head, data}`; only the data is the request."""
    return action["data"]


@wire
@dataclass(frozen=True)
class PromiseGet:
    id: Id


@wire
@dataclass(frozen=True)
class PromiseCreate:
    id: Id
    timeout_at: StrictInt
    param: Value = field(default_factory=Value)
    tags: dict[str, str] = field(default_factory=dict)


@wire
@dataclass(frozen=True)
class PromiseSettle:
    id: Id
    state: Id  # RESOLVED | REJECTED | REJECTED_CANCELED
    value: Value = field(default_factory=Value)


@wire
@dataclass(frozen=True)
class PromiseRegisterCallback:
    awaited: Id
    awaiter: Id  # same origin as `awaited`, and not equal to it


@wire
@dataclass(frozen=True)
class PromiseRegisterListener:
    awaited: Id
    address: Id


@wire
@dataclass(frozen=True)
class TaskGet:
    id: Id


@wire
@dataclass(frozen=True)
class TaskCreate:
    pid: Id
    ttl: StrictInt
    # must carry resonate:target, must not carry resonate:delay
    action: Annotated[PromiseCreate, BeforeValidator(_data)]


@wire
@dataclass(frozen=True)
class TaskAcquire:
    id: Id
    version: StrictInt
    pid: Id
    ttl: StrictInt


@wire
@dataclass(frozen=True)
class TaskRelease:
    id: Id
    version: StrictInt


@wire
@dataclass(frozen=True)
class TaskFulfill:
    id: Id
    version: StrictInt
    action: Annotated[PromiseSettle, BeforeValidator(_data)]  # action.id == id


@wire
@dataclass(frozen=True)
class TaskSuspend:
    id: Id
    version: StrictInt
    # Unique, same origin, none equal to `id`. On the wire, one
    # promise.register_callback action each.
    awaited: Annotated[
        tuple[Id, ...],
        Field(validation_alias="actions", min_length=1),
        BeforeValidator(lambda actions: [_data(a)["awaited"] for a in actions]),
    ]


def _promise_action(action: dict) -> PromiseCreate | PromiseSettle:
    kind = "promise.create" if action.get("kind") == "promise.create" else "promise.settle"
    return REQUESTS[kind].validate_python(_data(action))


@wire
@dataclass(frozen=True)
class TaskFence:
    id: Id
    version: StrictInt
    corr_id: str  # the envelope's, echoed in the nested response head
    action: Annotated[PromiseCreate | PromiseSettle, BeforeValidator(_promise_action)]


@wire
@dataclass(frozen=True)
class TaskHeartbeat:
    pid: Id
    # (id, version), all one origin. On the wire, a list of {id, version}.
    tasks: Annotated[
        tuple[tuple[Id, StrictInt], ...],
        Field(min_length=1),
        BeforeValidator(lambda tasks: [(t["id"], t["version"]) for t in tasks]),
    ]


@wire
@dataclass(frozen=True)
class TaskHalt:
    id: Id


@wire
@dataclass(frozen=True)
class TaskContinue:
    id: Id


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


@dataclass(frozen=True)
class Timeout:
    """The internal message: a deadline for this origin came due. Not a
    protocol request — no client can send one — but a transition on the
    origin's document all the same."""

    origin: str


#: The address of this service's own endpoint, for a message the service
#: sends to itself. A queue resolves it against its base URL.
HERE = "/"


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


def encode_message(msg: Execute | Unblock | Timeout) -> dict:
    match msg:
        case Execute():
            return {"kind": "execute", "task": {"id": msg.task_id, "version": msg.version}}
        case Unblock():
            return {"kind": "unblock", "promise": msg.promise}
        case Timeout():
            return {"kind": "timeout", "origin": msg.origin}


def decode_message(body: dict) -> Execute | Unblock | Timeout:
    try:
        match body.get("kind"):
            case "execute":
                task = body["task"]
                return Execute(task["id"], task["version"])
            case "unblock":
                return Unblock(body["promise"])
            case "timeout":
                return Timeout(body["origin"])
            case other:
                raise Invalid(f"unknown message kind {other!r}")
    except (KeyError, TypeError) as e:
        raise Invalid(f"malformed {body.get('kind')} message: missing {e}") from None


# ---------------------------------------------------------------------------
# Requests
# ---------------------------------------------------------------------------


REQUESTS = {kind: TypeAdapter(cls) for kind, cls in {
    "promise.get": PromiseGet,
    "promise.create": PromiseCreate,
    "promise.settle": PromiseSettle,
    "promise.register_callback": PromiseRegisterCallback,
    "promise.register_listener": PromiseRegisterListener,
    "task.get": TaskGet,
    "task.create": TaskCreate,
    "task.acquire": TaskAcquire,
    "task.release": TaskRelease,
    "task.fulfill": TaskFulfill,
    "task.suspend": TaskSuspend,
    "task.fence": TaskFence,
    "task.heartbeat": TaskHeartbeat,
    "task.halt": TaskHalt,
    "task.continue": TaskContinue,
}.items()}


def parse_request(envelope: dict) -> Req:
    """One envelope to one typed request, or `Invalid`."""
    if not isinstance(envelope, dict) or not isinstance(envelope.get("data"), dict):
        raise Invalid("an envelope is an object with a data object")
    kind, data = envelope.get("kind"), envelope["data"]
    if kind not in REQUESTS:
        raise Invalid(f"unknown request kind {kind!r}")
    if kind == "task.fence":
        data = {**data, "corrId": str(envelope.get("head", {}).get("corrId", ""))}
    try:
        return REQUESTS[kind].validate_python(data)
    except (ValidationError, KeyError, TypeError, AttributeError) as e:
        raise Invalid(f"{kind}: {e}") from None


def encode_reply(reply: Reply) -> dict:
    return {"head": {"status": reply.status}, "data": reply.data}
