"""What goes over a wire: requests in, messages out.

Two seams, both JSON, and both of them the point at which this system stops
being Python objects and becomes something another implementation could sit
on the other side of.

Requests arrive as the protocol's envelope, `{"kind": ..., "data": {...}}`,
and are parsed into the typed requests the kernel decides. Parsing is where
a malformed request becomes a 400 and never reaches the state machine; the
*semantic* doors, the ones the catalogue shadows, are the kernel's own.

Messages leave as JSON too, because a queue carries bytes. The simulated
queue carries the same JSON the real one does, so the thing the tests drive
and the thing production drives differ in where the bytes go and nowhere
else.
"""

from __future__ import annotations

from typing import Any

from kernel import (
    Execute, PromiseCreate, PromiseGet, PromiseRegisterCallback,
    PromiseRegisterListener, PromiseSettle, Reply, Req, TaskAcquire,
    TaskContinue, TaskCreate, TaskFence, TaskFulfill, TaskGet, TaskHalt,
    TaskHeartbeat, TaskRelease, TaskSuspend, Unblock, Value,
)


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
