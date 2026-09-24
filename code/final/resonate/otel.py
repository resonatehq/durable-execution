"""Spans, from ids that were already durable.

The other two ways of watching this system answer different questions.
`tracing.py` answers *did the path change* — deterministic, in-process, a
fingerprint you diff. The document answers *did the run do the right thing*
— deterministic, and readable from another machine a week later. Neither
answers *why was it slow*, or *what happened on the attempt that died
before it wrote anything*, and those are the two questions a person asks
while something is on fire.

That is what this is for, and it is the only one of the three that is
sampled, lossy and non-deterministic. Losing a span costs nothing here.
Losing a document loses a run.

## No trace context is propagated, because none is needed

The usual way to build a distributed trace is to thread a trace id and a
parent span id through every call, in a header. This system does not have
to, because its ids are already durable and already positional:

    trace_id  = hash(origin)            every promise of one run agrees
    span_id   = hash(promise_id)        the promise *is* the span
    parent_id = hash(dewey_parent)      `o:2.1` sits under `o:2`

A container that picks up a task three days after the run began, knowing
nothing but the task id, computes the same ids as the container that
started it. There is no header to lose, nothing to plumb through the
kernel, and no way for a resumed run to land in a trace of its own.

## Two spans, always

A **logical** span is the promise: it starts when the promise was created
and ends when it settled, both read from the document rather than from any
process's clock. It is what the caller awaited. A root that suspends and
resumes four times is one logical span covering the whole run, which is
the thing worth looking at.

A **physical** span is one attempt at running it, in one process, timed by
that process. Its id mixes the promise id with a random id minted per
attempt, so attempts never collide.

Every logical span gets its physical ones. Not "only when something went
wrong" — that would be a conditional the structure does not need, and it
would mean the shape of the trace depended on the outcome, so you could
not tell a fast run from a run that was never instrumented.

What the pair buys is the difference between them. A logical span of
forty seconds containing two physical spans of eighty milliseconds is a
run that spent thirty-nine seconds waiting, not working. That is the
common production question and neither span answers it alone.

## Who emits, and why nothing is written twice

A logical span is emitted by whoever commits the settlement. A settlement
is committed once — that is what the conditional write buys — so the span
is emitted once, by construction rather than by deduplication.

A process that dies between the commit and the export loses the span, and
a later one will not re-emit it. That is the lossiness admitted above.
Should it ever be re-emitted, it would be identical: the id is derived and
both timestamps come from the document, so two emissions of one settlement
are the same fact written twice rather than two versions of it.

## What the kernel knows about any of this

Nothing, and it must stay nothing. `kernel.py` imports no module of this
system and no library, which is what makes a decision replayable and
explorable. Everything here is the shell: the engine calls `settled`, the
worker and the SDK open `attempt`. Off by default, and when off the cost
is one lookup and a return.
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator

from .kernel import PENDING, RESOLVED

#: What a span's outcome is called. OTel's own vocabulary is `OK`, `ERROR`
#: and `UNSET`; the third is the one that matters here, because a suspension
#: is neither success nor failure and calling it either would be a lie.
OK, ERROR, UNSET = "OK", "ERROR", "UNSET"


# ---------------------------------------------------------------------------
# Ids
# ---------------------------------------------------------------------------


def _digest(size: int, *parts: str) -> bytes:
    """A stable id of `size` bytes from strings, in every process and every
    run. Not `hash()`: that is salted per process, which would put two
    containers of one run into two traces."""
    h = hashlib.blake2b(b"\x00".join(p.encode("utf-8") for p in parts),
                        digest_size=size).digest()
    # All-zero is the one value the specification reserves for "no such id",
    # so a span that happened to hash to it would be dropped. One in 2^64,
    # and two lines to never think about again.
    return h if any(h) else b"\x01" * size


def trace_id(origin: str) -> bytes:
    """Sixteen bytes, one per run. Every promise of an origin agrees on it
    without being told, which is the whole trick."""
    return _digest(16, "trace", origin)


def span_id(*parts: str) -> bytes:
    """Eight bytes. One part is a logical span, two a physical one."""
    return _digest(8, "span", *parts)


def dewey_parent(id: str) -> str | None:
    """Whose call this was, by position. `o:2.1` -> `o:2` -> `o` -> None.

    The root of a run has no parent: everything before the first ':' is the
    origin, so an id without one is where the lineage starts.
    """
    head = id.find(":")
    if head < 0:
        return None
    cut = max(id.rfind(":"), id.rfind("."))
    return id[:cut] if cut >= head else None


# ---------------------------------------------------------------------------
# What a span is, before anybody exports one
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Span:
    """One span, as plain data.

    Deliberately not an OpenTelemetry object. The ids are the interesting
    part and they are derived from this system's own ids, so they are worth
    asserting in a test that does not need a tracer provider, an exporter,
    a batch processor and a shutdown hook to run. `export_gcp` turns these
    into the library's objects at the edge, which is the same place every
    other outside thing in this repository lives.
    """

    name: str
    trace: bytes
    span: bytes
    parent: bytes | None
    start_ms: int
    end_ms: int
    status: str = UNSET
    attributes: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_ms(self) -> int:
        return self.end_ms - self.start_ms

    def __repr__(self) -> str:
        kind = self.attributes.get("de.span", "?")
        return (f"<{kind} {self.name} {self.span.hex()} "
                f"under {self.parent.hex() if self.parent else '-'} "
                f"{self.duration_ms}ms {self.status}>")


# ---------------------------------------------------------------------------
# On and off
# ---------------------------------------------------------------------------

#: The process-wide sink, set by `to()`. A module global rather than only a
#: context variable because a context variable set at start-up does not
#: reach the other threads a WSGI server runs requests on.
_SINK: Callable[[Span], None] | None = None

#: A per-context override, which is how a test collects spans without
#: touching the process. Same mechanism `tracing.py` uses, for the same
#: reason: `asyncio` copies the context into each task, so the branches of
#: a `gather` inherit it.
_LOCAL: ContextVar[Callable[[Span], None] | None] = ContextVar("otel", default=None)


def to(sink: Callable[[Span], None] | None) -> None:
    """Send spans somewhere, process-wide. `None` turns it off again."""
    global _SINK
    _SINK = sink


def sink() -> Callable[[Span], None] | None:
    return _LOCAL.get() or _SINK


def emit(span: Span) -> None:
    out = sink()
    if out is not None:
        out(span)


@contextmanager
def collecting() -> Iterator[list[Span]]:
    """Gather the spans of a block, for this context only."""
    got: list[Span] = []
    token = _LOCAL.set(got.append)
    try:
        yield got
    finally:
        _LOCAL.reset(token)


# ---------------------------------------------------------------------------
# The logical span: a promise, from creation to settlement
# ---------------------------------------------------------------------------


def _name(promise) -> str:
    """What to call this span in a list of them.

    The parameter of a durable call names its function; a sleep names its
    duration. Anything else -- a promise a client created by hand -- is
    named by nothing, and gets its state instead of a guess.
    """
    try:
        param = json.loads(promise.param.data) if promise.param.data else None
    except (ValueError, TypeError):
        return "promise"
    if isinstance(param, dict):
        if isinstance(param.get("f"), str):
            # The version only when there is one, so an unversioned project
            # never reads about versions in its own traces.
            v = param.get("v")
            return f"{param['f']}@{v}" if v else param["f"]
        if "sleep" in param:
            return f"sleep {param['sleep']}ms"
    return "promise"


def _span_of(promise, id: str, origin: str) -> Span:
    parent = dewey_parent(id)
    return Span(
        name=_name(promise),
        trace=trace_id(origin),
        span=span_id(id),
        parent=span_id(parent) if parent else None,
        start_ms=promise.created_at,
        # A settled promise has a settled_at; the caller checks that before
        # asking for a span, so this is a fallback and not a policy.
        end_ms=promise.settled_at if promise.settled_at is not None else promise.created_at,
        status=OK if promise.state == RESOLVED else ERROR,
        attributes={
            "de.span": "logical",
            "de.promise": id,
            "de.state": promise.state,
            "de.origin": origin,
        },
    )


def settled(before, after, origin: str) -> None:
    """Emit a logical span for every promise this transition settled.

    Called by the engine once a commit has succeeded, with the document as
    it was and as it now is. Reading the delta rather than the request is
    what makes this complete: a promise that expired in a sweep, a timer
    that resolved because its deadline passed, and one a worker settled
    outright all look the same here, and none of them is a special case.
    """
    if sink() is None:
        return
    was = {o.id: o.promise.state for o in before.objects}
    for o in after.objects:
        if o.promise.state != PENDING and was.get(o.id, PENDING) == PENDING:
            emit(_span_of(o.promise, o.id, origin))


# ---------------------------------------------------------------------------
# The physical span: one attempt, in one process
# ---------------------------------------------------------------------------

#: Which attempt is running here. Set at the top of an attempt and inherited
#: by every durable call made inside it, so a local call knows which run of
#: its parent's body it belonged to without being handed anything.
_ATTEMPT: ContextVar[str | None] = ContextVar("attempt", default=None)


@contextmanager
def attempt(id: str, origin: str, now: Callable[[], int] | None = None,
            name: str | None = None, benign: tuple = (),
            **attributes: Any) -> Iterator[dict]:
    """Time one attempt at running the promise `id`, in this process.

    Yields a dict the caller may put an outcome in: `status` if the attempt
    ended as something other than a plain success, and any `de.*` attribute
    worth carrying. A
    suspension is `UNSET` rather than `ERROR` -- stopping to wait for a
    value you do not have is how this system works, and a trace that
    painted every fan-out red would be a trace nobody read. Which
    exceptions mean that is the caller's to say, in `benign`, because the
    kernel's vocabulary is not this module's business.

    The random half of the id is minted per *attempt*, not per delivered
    request, and everything nested inside inherits it. A worker's outer
    half may run the inner half more than once for one delivery -- a
    suspension that finds nothing left to wait for goes round again -- so
    a per-request id would collide with itself in exactly the case worth
    looking at. Within one attempt a position runs at most once, so the
    pair is unique.

    Where the platform has an id of its own it belongs in `attributes`, so
    a span can be matched to a log line. It is not used as the id, because
    it is not ours and may not be there.
    """
    if sink() is None:
        yield {}
        return
    clock = now or (lambda: int(time.time() * 1000))
    inv = _ATTEMPT.get() or os.urandom(8).hex()
    token = _ATTEMPT.set(inv)
    start = clock()
    out: dict = {"status": UNSET}
    try:
        yield out
    except BaseException as e:
        if not isinstance(e, benign):
            out.setdefault("de.error", type(e).__name__)
            out["status"] = ERROR
        raise
    finally:
        _ATTEMPT.reset(token)
        emit(Span(
            name=name or id,
            trace=trace_id(origin),
            span=span_id(id, inv),
            parent=span_id(id),
            start_ms=start,
            end_ms=clock(),
            status=out.pop("status", UNSET),
            attributes={
                "de.span": "physical",
                "de.promise": id,
                "de.invocation": inv,
                "de.origin": origin,
                **{k: v for k, v in attributes.items() if v is not None},
                **out,
            },
        ))
