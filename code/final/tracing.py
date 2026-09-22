"""What happened, in the order it happened, with the request it belonged to.

Three ways to watch this system were tried before this one. `python -m
trace --trackcalls` gives a call *graph* — a fact about the source, not
about a run. `sys.setprofile` gives a real sequence and needs no wrappers,
but it cannot tell a raise from a `return None`, which in a system whose
whole story is 412-versus-429 is not a detail, and it costs about 4x on
calls. Hand-written recording subclasses work and are exact, but they are
one class per implementation and they see nothing above the ports.

So: a decorator on what we own, a protocol-derived wrapper on what we do
not, both feeding one log, and a context variable carrying the request
that caused it all.

    from tracing import recording, trace, watch

    with recording() as t:
        ...
    print(t.tree())

## What it is for

Not comparing implementations, and not proving determinism. Those are
things it happens to be good for. It is for reading: `test/research.trace`
is one run of the research agent, nested by who called whom, checked in
because somebody read it and agreed that is the path the design says the
system should take. A change to the path is then a diff a person has to
look at and accept, rather than something that slips through because the
final answer was still right.

`fingerprint()` is the cheap way to ask "same path?" — sixteen characters
instead of a hundred lines. The hundred lines are the artifact.

## Why a context variable

A worker runs a durable function under `asyncio.run`, and the branches of
a `gather` are separate tasks. `contextvars` is the one mechanism that
survives both: `asyncio` copies the current context into each task it
creates, so a request id set in `Routes.handle` reaches every store call
made by every branch, without threading an argument through the kernel.
It is the same mechanism the SDK already uses for `_INVOCATION` and
`_FRAME`, for the same reason.

Concurrency is why the log lives in the context rather than in a module
global. One container can serve eighty requests at once; a global would
interleave eight runs into one unreadable list and two tests into one.

## Off by default, and cheap when off

Every decorated call checks one context variable and returns. Nothing is
formatted, nothing is allocated. `recording()` is what turns it on, for
the duration of a block and for that context only.

## Why a record holds no clock, no address, no object id

Because a path you cannot compare is a path you cannot sign off. If two
runs of the same thing printed differently, the diff would be noise and
nobody would read it. So a record holds names, arguments and results and
nothing that varies between processes — and long strings are digested, so
a document body contributes its identity without its bulk and a codec
change moves one character rather than a thousand lines.

The first thing to break this rule was the tracer's own: a `Durable`
passed to the worker's inner half printed as `<sdk.Durable object at
0x7ff99d975cd0>`. Nothing whose repr carries an address is recorded now.
"""

from __future__ import annotations

import functools
import hashlib
import inspect
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field, fields, is_dataclass
from typing import Any, Callable

#: How long a string may be before it is kept as a digest instead.
BRIEF = 48

#: What a heap address looks like. Nothing containing one is recordable:
#: it differs in every process, so it would make `fingerprint` a lie.
ADDRESS = re.compile(r"0x[0-9a-fA-F]{6,}")


@dataclass
class Call:
    """One call, as it will be compared. No timing, no addresses."""

    depth: int
    where: str      # the request that caused it, or "" outside one
    name: str
    args: str
    result: str = "..."

    def entered(self) -> str:
        return f"{'  ' * self.depth}\u2192 {self.name}({self.args})"

    def returned(self) -> str:
        return f"{'  ' * self.depth}\u2190 {self.name} = {self.result}"


@dataclass
class Trace:
    """Two events per call: going in, and coming back out.

    One line per call would be half as long and would lose the thing
    worth having. A single line says what a call did but not *when it
    finished*, so two calls that overlapped and two that ran one after
    the other read identically. Today everything here is sequential and
    the distinction costs nothing; the moment a container serves two
    deliveries at once, or a leaf does real I/O inside a `gather`, it is
    the only thing that says which happened.

    It also puts a raise where it happened. `\u2190 Durable.invoke = !Blocked`
    lands after the calls the attempt made before it gave up, rather than
    on the line that opened it.
    """

    #: ("call" | "ret", the call), in the order those two things happened.
    events: list[tuple[str, Call]] = field(default_factory=list)

    @property
    def calls(self) -> list[Call]:
        return [c for kind, c in self.events if kind == "call"]

    def tree(self, where: str | None = None) -> str:
        """Indented by nesting, arrowed by direction, and headed by cause.

        A run is a sequence of things that were each asked for by
        something: a request, a delivery. Printing that heading when it
        changes is what makes a trace readable from the top rather than
        from the middle — it answers "why is this happening" before the
        reader has to ask.
        """
        out, last = [], None
        for kind, c in self.events:
            if where is not None and c.where != where:
                continue
            # Once per cause, and once per outermost call even when two in
            # a row share a cause: three deliveries to the same worker are
            # three deliveries, and a heading that printed once would say
            # they were one.
            if c.where and (c.where != last or (kind == "call" and c.depth == 0)):
                out.append(f"[{c.where}]")
            last = c.where
            out.append(c.entered() if kind == "call" else c.returned())
        return "\n".join(out)

    def of(self, name: str) -> list[Call]:
        return [c for c in self.calls if c.name == name]

    def sequence(self, where: str | None = None, depth: int | None = None,
                 caller: str = "Runtime", preamble: str | None = None) -> str:
        """The same events as a Mermaid sequence diagram.

        A trace already is one: the name before the dot is the participant,
        going in is an arrow from whoever was already running, coming back
        is the dashed arrow home. Nothing is invented here, which is the
        point — `SEQUENCE.md` was drawn by hand from what the code was
        believed to do, and this is drawn from what it did.

        `depth` cuts the diagram off below a level, because every call in
        this run is hundreds of arrows and no one reads that. Both events
        of a call carry the same depth, so cutting never leaves an arrow
        that does not come back.

        `preamble` is a note drawn across the top. A trace records one
        process, so anything that happens between two processes is a gap
        in the picture with nothing to mark it — and a reader is left to
        infer a connection that was never drawn. Saying what the gap is
        beats drawing an arrow nobody recorded.
        """
        def who(call: Call) -> str:
            return call.name.split(".")[0]

        def label(text: str, room: int = 58) -> str:
            text = text.replace('"', "'").replace("\n", " ")
            return text if len(text) <= room else text[:room - 1] + "\u2026"

        # Participants left to right by who calls whom, so an arrow points
        # rightwards and crosses nothing. First appearance would be simpler
        # and would put the worker to the right of the ports it reaches
        # through the engine, which is the one thing a reader must not
        # believe.
        shown = [(kind, c) for kind, c in self.events
                 if (where is None or c.where == where)
                 and (depth is None or c.depth <= depth)]
        calls_whom: dict[str, set[str]] = {}
        first, stack = [], [caller]
        for kind, c in shown:
            if kind == "call":
                if who(c) not in first:
                    first.append(who(c))
                calls_whom.setdefault(stack[-1], set()).add(who(c))
                stack.append(who(c))
            else:
                stack.pop()
        order, placed = [], {caller}
        while len(placed) < len(first) + 1:
            for name in first:
                if name in placed:
                    continue
                callers = {a for a, bs in calls_whom.items() if name in bs and a != name}
                if callers <= placed:          # everyone who calls it is already left
                    order.append(name)
                    placed.add(name)
                    break
            else:                              # a cycle: fall back to first appearance
                for name in first:
                    if name not in placed:
                        order.append(name)
                        placed.add(name)
                        break

        out = ["sequenceDiagram", "    autonumber",
               f"    participant {caller}"]
        out += [f"    participant {p}" for p in order]
        if preamble is not None and order:
            out.append(f"    Note over {caller},{order[-1]}: {label(preamble, 200)}")
        stack = [caller]
        for kind, c in shown:
            method = c.name.split(".", 1)[1] if "." in c.name else c.name
            if kind == "call":
                if c.where and len(stack) == 1:
                    # A cause worth naming: a trace whose outermost frame
                    # is not the route — one recorded from inside, say —
                    # would otherwise not say what asked for the work. When
                    # the route *is* the outermost frame its own arrow
                    # already says it, and nothing is emitted here.
                    out.append(f"    Note over {caller}: {label(c.where, 40)}")
                out.append(f"    {stack[-1]}->>+{who(c)}: {label(method + '(' + c.args + ')')}")
                stack.append(who(c))
            else:
                stack.pop()
                out.append(f"    {who(c)}-->>-{stack[-1]}: {label(c.result)}")
        return "\n".join(out)

    def fingerprint(self) -> str:
        """The path, as sixteen hex characters: a cheap way to ask whether
        it is still the path that was reviewed. The answer worth reading
        is `tree()`; this is the way to notice you should read it."""
        return hashlib.sha256(self.tree().encode()).hexdigest()[:16]


_LOG: ContextVar[Trace | None] = ContextVar("trace.log", default=None)
_DEPTH: ContextVar[int] = ContextVar("trace.depth", default=0)
_WHERE: ContextVar[str] = ContextVar("trace.where", default="")


@contextmanager
def recording():
    """Turn tracing on for this context and collect what happens."""
    log = Trace()
    token = _LOG.set(log)
    try:
        yield log
    finally:
        _LOG.reset(token)


@contextmanager
def because(what: str):
    """Name the thing that caused what follows: a request, a delivery."""
    token = _WHERE.set(what)
    try:
        yield
    finally:
        _WHERE.reset(token)


def brief(value: Any) -> str:
    """A value, as something short, stable and characteristic.

    A document body is thousands of bytes and belongs in a trace by its
    identity rather than its content, so it becomes a digest and a length:
    two runs that wrote the same bytes say so, and a reader is not asked
    to scroll past them. Everything else shrinks by the same rule — show
    what distinguishes it, then stop.
    """
    if isinstance(value, str):
        if len(value) > BRIEF:
            return f"<{hashlib.sha256(value.encode()).hexdigest()[:8]} {len(value)}b>"
        return repr(value)
    if isinstance(value, (list, tuple)):
        if len(value) > 4:
            return f"[{len(value)} items]"
        inside = ", ".join(brief(x) for x in value)
        return f"({inside})" if isinstance(value, tuple) else f"[{inside}]"
    if isinstance(value, dict):
        if len(value) > 4:
            return f"{{{len(value)} keys}}"
        return "{" + ", ".join(f"{k!r}: {brief(v)}" for k, v in value.items()) + "}"
    shown = repr(value)
    if ADDRESS.search(shown):
        # The default `object.__repr__` carries a heap address, which is
        # different in every process and would make a fingerprint useless.
        # A type that wants to appear in a trace says so with a `__repr__`.
        return f"<{type(value).__name__}>"
    if len(shown) <= BRIEF * 2:
        return shown
    if is_dataclass(value):
        # The first field is the one that says which of them this is: an
        # id, a state, a status.
        first = fields(value)[0]
        return f"{type(value).__name__}({first.name}={brief(getattr(value, first.name))}, ...)"
    return f"<{type(value).__name__}>"


def _args(fn: Callable, a: tuple, kw: dict) -> str:
    """Positional arguments by name, `self` dropped, in signature order."""
    try:
        bound = inspect.signature(fn).bind_partial(*a, **kw)
    except TypeError:  # pragma: no cover - a call that will fail anyway
        return ", ".join(brief(x) for x in a)
    return ", ".join(f"{k}={brief(v)}" for k, v in bound.arguments.items()
                     if k != "self")


def _enter(name: str, args: str, depth: int) -> Call:
    call = Call(depth, _WHERE.get(), name, args)
    log = _LOG.get()
    if log is not None:
        log.events.append(("call", call))
    return call


def _leave(call: Call, result: str) -> None:
    call.result = result
    log = _LOG.get()
    if log is not None:
        log.events.append(("ret", call))


def trace(fn: Callable) -> Callable:
    """Watch this function. Works on `def` and on `async def`.

    A raised exception is recorded as a result, because in this system a
    refusal is an outcome: `Conflict` is the whole reason the engine never
    loops, and a trace that hid it would be a trace of a different system.
    """
    name = f"{fn.__qualname__.split('.')[-2]}.{fn.__name__}" \
        if "." in fn.__qualname__ else fn.__name__

    if inspect.iscoroutinefunction(fn):
        @functools.wraps(fn)
        async def watched_async(*a, **kw):
            if _LOG.get() is None:
                return await fn(*a, **kw)
            call = _enter(name, _args(fn, a, kw), _DEPTH.get())
            token = _DEPTH.set(call.depth + 1)
            try:
                out = await fn(*a, **kw)
            except BaseException as e:
                _leave(call, f"!{type(e).__name__}")
                raise
            finally:
                _DEPTH.reset(token)
            _leave(call, brief(out))
            return out
        return watched_async

    @functools.wraps(fn)
    def watched(*a, **kw):
        if _LOG.get() is None:
            return fn(*a, **kw)
        call = _enter(name, _args(fn, a, kw), _DEPTH.get())
        token = _DEPTH.set(call.depth + 1)
        try:
            out = fn(*a, **kw)
        except BaseException as e:
            _leave(call, f"!{type(e).__name__}")
            raise
        finally:
            _DEPTH.reset(token)
        _leave(call, brief(out))
        return out
    return watched


class watch:
    """Any implementation of one of our protocols, watched.

    The decorator is for what we own. This is for what we do not: the
    protocol says which operations count, so one wrapper serves the
    simulator and the bucket alike, and `store_gcp.py` needs no line of
    instrumentation to appear in a trace.
    """

    def __init__(self, inner: Any, protocol: type, label: str) -> None:
        object.__setattr__(self, "_inner", inner)
        object.__setattr__(self, "_label", label)
        object.__setattr__(self, "_ops", {
            n for n in vars(protocol) if not n.startswith("_")})

    def __getattr__(self, name: str) -> Any:
        attr = getattr(object.__getattribute__(self, "_inner"), name)
        if name not in object.__getattribute__(self, "_ops") or not callable(attr):
            return attr
        return trace(_named(attr, f"{object.__getattribute__(self, '_label')}.{name}"))


def _named(fn: Callable, name: str) -> Callable:
    """`trace` takes the name from `__qualname__`; a bound method of a
    simulator would call itself `Store.get` when what a reader wants is
    `store.get`, the port."""
    @functools.wraps(fn)
    def renamed(*a, **kw):
        return fn(*a, **kw)
    renamed.__qualname__ = name
    renamed.__name__ = name.split(".")[-1]
    return renamed
