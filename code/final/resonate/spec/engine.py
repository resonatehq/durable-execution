"""What an engine is: one method, and three layers.

    EngineP   an engine, once it exists: it processes
    EngineC   how one is made: ports in, engine out
    EngineM   a module that offers one, under the name `Engine`

`EngineM` is the useful one. A conformance suite cannot be handed a class,
because a real implementation may want to choose its class at import time,
and it cannot be handed an instance, because the suite has to supply the
world the engine runs in. It is handed the module, and reaches for `Engine`.

`EngineM.Engine` is a read-only property rather than a plain attribute, and
that is not a style choice: a protocol's mutable attribute is invariant, so
`Engine: EngineC` demands a value that is *exactly* `EngineC` and a type
checker rejects `class Engine:` for it. A property is covariant, and a class
object satisfies it by being callable with the right arguments, with no
adapter. `test_types.py` checks it with mypy.

The types alone say nothing about behaviour. `testing/conformance/engine.py`
is the part that does: it drives an engine through a script and holds every
state it commits to the catalogue in `testing/properties.py`, which is the
specification's rather than ours. An implementation that passes has the same
observable behaviour as the reference, whatever it does inside -- a different
language, a different store, a kernel written from the specification rather
than transcribed from it.
"""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from ..kernel import KernelCfg
from ..types import Reply, Req, Timeout
from .queue import QueueP
from .store import StoreP

__all__ = ["Msg", "EngineP", "EngineC", "EngineM"]

#: What an engine is handed: a request a client sent, or a deadline coming
#: due. The engine is the only place that has to know which, because it is
#: the only place that knows where a message came from.
Msg = Req | Timeout


@runtime_checkable
class EngineP(Protocol):
    """An engine.

    One method, because there is one thing to do: a request and a deadline
    differ in which way the state machine is consulted and in nothing else,
    so a caller never has to know which kind of shell it is talking to.

    `process` returns the protocol's answer and raises only when the world
    refused: `Conflict` when the write lost its race, which the caller
    retries because every operation is idempotent, and whatever the ports
    raise when they cannot answer at all.

    One member, and deliberately no more. An engine has no name, no
    identity and no lifecycle to manage: everything it knows is in the
    bucket, so two of them are interchangeable and a conformance report
    names the module it was handed rather than asking the engine who it is.
    """

    def process(self, msg: Msg, now: int) -> Reply: ...


class EngineC(Protocol):
    """How an engine is made: the two ports, and the dials.

    The ports are arguments rather than imports because the conformance
    suite has to supply them. That is not a testing convenience -- it is the
    same seam that lets one engine run over a bucket in production and over
    a dict in a simulation, and it is why a simulated run is a real run.
    """

    def __call__(self, store: StoreP, queue: QueueP,
                 cfg: KernelCfg = ..., prefix: str = ...) -> EngineP: ...

    # This signature is real: every engine takes the same two ports,
    # because a port is an interface rather than a configuration. `StoreC`
    # and `QueueC` cannot say as much, and say so.


class EngineM(Protocol):
    """A module that offers an engine."""

    @property
    def Engine(self) -> EngineC: ...
